#!/usr/bin/env bash
# setup-manager-worktree.sh -- Idempotent git worktree setup for manager isolation.
#
# Creates a sibling worktree at ../multi-agent-system-shell-manager/
# on the `manager-worktree` branch so manager's fun-mode and
# operational-script writes don't cross-contaminate whichever branch
# hub has checked out in the primary working directory. See #45.
#
# Safe to re-run. If the worktree already exists, this script exits
# 0 without modification. If the branch exists locally or on the
# `github` remote, it is reused. Otherwise a new branch is forked
# from `feat/manager-agent` (preferring the local ref; falling back
# to `github/feat/manager-agent` when only the remote is present).
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

# If the path already appears in `git worktree list`, there is nothing
# left to do. Parse the porcelain output to be resilient against
# colors, trailing (prunable) annotations, etc.
if git worktree list --porcelain 2>/dev/null \
    | awk -v p="$WORKTREE_PATH" '$1=="worktree" && $2==p {found=1} END{exit !found}'; then
    log "Worktree already present at ${WORKTREE_PATH} — no-op."
    exit 0
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
    # No branch anywhere — fork a new one from BASE_BRANCH.
    local_base="refs/heads/${BASE_BRANCH}"
    remote_base="refs/remotes/${REMOTE_NAME}/${BASE_BRANCH}"

    if git show-ref --verify --quiet "$local_base"; then
        base_ref="$BASE_BRANCH"
    elif git show-ref --verify --quiet "$remote_base"; then
        base_ref="${REMOTE_NAME}/${BASE_BRANCH}"
    else
        log "ERROR: base branch '${BASE_BRANCH}' not found locally or on '${REMOTE_NAME}'." >&2
        log "Fetch it (\`git fetch ${REMOTE_NAME} ${BASE_BRANCH}\`) and retry." >&2
        exit 1
    fi

    log "Forking '${WORKTREE_BRANCH}' from '${base_ref}'."
    run git worktree add -b "$WORKTREE_BRANCH" "$WORKTREE_PATH" "$base_ref"
fi

log "Worktree ready at ${WORKTREE_PATH}"
