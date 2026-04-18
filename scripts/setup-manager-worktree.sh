#!/usr/bin/env bash
# setup-manager-worktree.sh -- Idempotent git worktree setup for manager isolation.
#
# Creates a sibling worktree at ../multi-agent-system-shell-manager/
# on the `manager-worktree` branch so manager's fun-mode and
# operational-script writes don't cross-contaminate whichever branch
# hub has checked out in the primary working directory. See #45.
#
# Safe to re-run. If the worktree already exists, this script exits
# 0 without modification. If the `manager-worktree` branch already
# exists (local or remote), it is reused. Otherwise a new branch is
# forked from `feat/manager-agent`, preferring the REMOTE ref over
# the local ref (see #60) so the manager worktree tracks upstream
# state rather than whatever local work dev has in progress. The
# local ref is used only as a fallback when the remote is not
# configured or unreachable.
#
# Usage:
#   ./scripts/setup-manager-worktree.sh
#
# Environment variables for testing:
#   _TEST_DRY_RUN          -- "true" to print what would happen
#   MAS_MANAGER_WORKTREE   -- override the sibling path (default:
#                             ../multi-agent-system-shell-manager)
#   MAS_REMOTE_NAME        -- override the git remote name used for
#                             the remote-branch fallback (default:
#                             "origin" — matches git's own default).
#                             The upstream checkout on aasc77's dev
#                             machine uses the "github" remote; set
#                             MAS_REMOTE_NAME=github there. See #45
#                             QA feedback.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PARENT_DIR="$(cd "${REPO_ROOT}/.." && pwd)"
WORKTREE_PATH="${MAS_MANAGER_WORKTREE:-${PARENT_DIR}/multi-agent-system-shell-manager}"
WORKTREE_BRANCH="manager-worktree"
BASE_BRANCH="feat/manager-agent"
REMOTE_NAME="${MAS_REMOTE_NAME:-origin}"

DRY_RUN="${_TEST_DRY_RUN:-false}"

log() {
    echo "[setup-manager-worktree] $*"
}

run() {
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

cd "$REPO_ROOT"

# If the path already appears in `git worktree list`, it is either
# the right worktree (no-op) or a pre-existing one on the WRONG
# branch (error). A naive path-only idempotency check would silently
# leave the wrong worktree in place and manager would point at the
# wrong branch. See #61 Observation 2.
#
# `git worktree list --porcelain` emits a record per worktree like:
#     worktree /abs/path
#     HEAD <sha>
#     branch refs/heads/<branch-name>    (or `detached`)
# Extract the `branch` line for our target path and compare to the
# expected WORKTREE_BRANCH.
if git worktree list --porcelain 2>/dev/null \
    | awk -v p="$WORKTREE_PATH" '$1=="worktree" && $2==p {found=1} END{exit !found}'; then
    existing_branch="$(
        git worktree list --porcelain 2>/dev/null \
            | awk -v p="$WORKTREE_PATH" '
                $1=="worktree" && $2==p {found=1; next}
                found && $1=="branch" {print $2; exit}
                found && NF==0 {exit}
            '
    )"
    # `existing_branch` is either `refs/heads/<name>` or empty
    # (detached-HEAD worktree). Strip the refs/heads/ prefix for
    # comparison. An empty value means detached — treat as wrong.
    existing_short="${existing_branch#refs/heads/}"
    if [ "$existing_short" = "$WORKTREE_BRANCH" ]; then
        log "Worktree already present at ${WORKTREE_PATH} — no-op."
        exit 0
    fi
    actual="${existing_short:-detached}"
    log "ERROR: worktree at ${WORKTREE_PATH} is on branch '${actual}', expected '${WORKTREE_BRANCH}'." >&2
    log "Remove/reconfigure it (git worktree remove ${WORKTREE_PATH}) then re-run." >&2
    exit 1
fi

# The target path may exist without being a worktree (e.g., a stale
# manual checkout) — bail early with a clear message instead of
# letting `git worktree add` fail with a noisier error.
if [ -e "$WORKTREE_PATH" ]; then
    log "ERROR: ${WORKTREE_PATH} exists but is not a registered git worktree." >&2
    log "Remove or move it, then re-run this script." >&2
    exit 1
fi

# Decide which ref to hand `git worktree add`. Priority: existing
# local branch > existing remote tracking branch > new branch forked
# from BASE_BRANCH (local > remote).
if git show-ref --verify --quiet "refs/heads/${WORKTREE_BRANCH}"; then
    log "Reusing existing local branch '${WORKTREE_BRANCH}'."
    run git worktree add "$WORKTREE_PATH" "$WORKTREE_BRANCH"
elif git show-ref --verify --quiet "refs/remotes/${REMOTE_NAME}/${WORKTREE_BRANCH}"; then
    log "Creating local branch '${WORKTREE_BRANCH}' tracking '${REMOTE_NAME}/${WORKTREE_BRANCH}'."
    run git worktree add -b "$WORKTREE_BRANCH" "$WORKTREE_PATH" \
        "${REMOTE_NAME}/${WORKTREE_BRANCH}"
else
    # No worktree branch yet — fork a new one from BASE_BRANCH.
    #
    # Priority order (#60): PREFER remote over local.
    #   1. refs/remotes/${REMOTE_NAME}/${BASE_BRANCH} (remote)
    #   2. refs/heads/${BASE_BRANCH} (local fallback when offline
    #      or remote unreachable)
    #   3. error
    #
    # Rationale: the manager worktree is a fresh checkout used for
    # manager's fun-mode / operational writes. It should track what
    # the rest of the fleet sees on the upstream branch, not whatever
    # state dev happens to have on their local `feat/manager-agent`
    # (which may be stale or mid-experiment). If dev has unpushed
    # local work, that's dev's branch to manage — the manager
    # worktree intentionally matches origin.
    local_base="refs/heads/${BASE_BRANCH}"
    remote_base="refs/remotes/${REMOTE_NAME}/${BASE_BRANCH}"
    has_local_base="false"
    has_remote_base="false"
    git show-ref --verify --quiet "$local_base" && has_local_base="true"
    git show-ref --verify --quiet "$remote_base" && has_remote_base="true"

    if [ "$has_remote_base" = "true" ]; then
        base_ref="${REMOTE_NAME}/${BASE_BRANCH}"
        if [ "$has_local_base" = "true" ]; then
            # Both exist — describe how local compares to remote so
            # the operator isn't surprised that their local work
            # wasn't used. `rev-list --left-right --count` emits
            # "<behind>\t<ahead>" where behind = commits on remote
            # but not local, ahead = commits on local but not remote.
            divergence="$(
                git rev-list --left-right --count \
                    "${remote_base#refs/remotes/}...${BASE_BRANCH}" \
                    2>/dev/null || echo ""
            )"
            behind="$(echo "$divergence" | awk '{print $1}')"
            ahead="$(echo "$divergence" | awk '{print $2}')"
            if [ -n "$behind" ] && [ -n "$ahead" ]; then
                if [ "$behind" -gt 0 ] && [ "$ahead" -gt 0 ]; then
                    state="diverged (local ${ahead} ahead / ${behind} behind)"
                elif [ "$behind" -gt 0 ]; then
                    state="local ${behind} commits behind"
                elif [ "$ahead" -gt 0 ]; then
                    state="local ${ahead} commits ahead"
                else
                    state="in sync"
                fi
                log "Using remote base '${base_ref}' (local '${BASE_BRANCH}' is ${state})."
            else
                log "Using remote base '${base_ref}' (local '${BASE_BRANCH}' also present)."
            fi
        else
            log "Using remote base '${base_ref}'."
        fi
    elif [ "$has_local_base" = "true" ]; then
        base_ref="$BASE_BRANCH"
        log "Using local base '${BASE_BRANCH}' (remote '${REMOTE_NAME}/${BASE_BRANCH}' not configured or unreachable)."
    else
        log "ERROR: base branch '${BASE_BRANCH}' not found locally or on '${REMOTE_NAME}'." >&2
        log "Fetch it (\`git fetch ${REMOTE_NAME} ${BASE_BRANCH}\`) and retry." >&2
        exit 1
    fi

    log "Forking '${WORKTREE_BRANCH}' from '${base_ref}'."
    run git worktree add -b "$WORKTREE_BRANCH" "$WORKTREE_PATH" "$base_ref"
fi

log "Worktree ready at ${WORKTREE_PATH}"
