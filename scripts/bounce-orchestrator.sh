#!/usr/bin/env bash
# bounce-orchestrator.sh -- Cleanly restart the orchestrator for a project
# without touching agent panes or the rest of the tmux session.
#
# Usage:
#   ./scripts/bounce-orchestrator.sh [project]
#
# Behavior:
#   1. Reads the orchestrator PID from /tmp/mas-orch-<project>.lock,
#      OR falls back to `ps`-based lookup for the migration case
#      (first bounce after adopting the flock code — no lock file yet).
#   2. Sends SIGTERM, waits up to 5s, then SIGKILL if still alive
#   3. Removes the lock file
#   4. Finds the orchestrator pane by @label lookup (same pattern as
#      orchestrator/tmux_comm.py::_scan_control_pane_labels) and sends
#      the launch command there. Falls back to control.0 ONLY when no
#      pane labelled "orchestrator" is found — important because the
#      Fix 5 layout puts orch at control.1, not control.0.
#
# The flock added in orchestrator/__main__.py auto-releases on process exit
# because the kernel tracks the underlying fd. We still rm the lock file
# afterwards so bounce is fully idempotent even on a dirty previous exit.
#
# Environment variables for testing:
#   _TEST_DRY_RUN         -- "true" to print commands instead of executing
#   _TEST_SKIP_TMUX       -- "true" to skip the tmux send-keys step
#   _TEST_PS_OVERRIDE     -- override `ps` output (for unit tests)
#   _TEST_PANE_LOOKUP     -- override the resolved pane (for unit tests)

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT="${1:-remote-test}"
LOCK_PATH="/tmp/mas-orch-${PROJECT}.lock"
SESSION_NAME="${PROJECT}"
CONTROL_WINDOW="control"
DEFAULT_ORCH_PANE="${SESSION_NAME}:${CONTROL_WINDOW}.0"

DRY_RUN="${_TEST_DRY_RUN:-false}"
SKIP_TMUX="${_TEST_SKIP_TMUX:-false}"

# If MAS_TMUX_SOCKET is set, route every tmux invocation through
# `tmux -L <socket>` so pytest can isolate tmux ops from the user's
# default-socket sessions. See #46.
_tmux() {
    if [ -n "${MAS_TMUX_SOCKET:-}" ]; then
        command tmux -L "$MAS_TMUX_SOCKET" "$@"
    else
        command tmux "$@"
    fi
}

# -----------------------------------------------------------------------
# Logging helper
# -----------------------------------------------------------------------
log() {
    echo "[bounce-orchestrator] $*"
}

# -----------------------------------------------------------------------
# find_orch_pid_from_ps -- migration fallback
#
# When there's no lock file (the first bounce after adopting flock, or
# after a stale-file cleanup), find the running orch by pattern-matching
# on `ps`. Returns the pid on stdout or empty string if none found.
# Respects _TEST_PS_OVERRIDE for unit tests.
# -----------------------------------------------------------------------
find_orch_pid_from_ps() {
    local ps_output
    if [ -n "${_TEST_PS_OVERRIDE:-}" ]; then
        ps_output="$_TEST_PS_OVERRIDE"
    else
        ps_output="$(ps -Ao pid=,args= 2>/dev/null || true)"
    fi

    # Match "python3 -m orchestrator <PROJECT>" (or python/-m variants).
    # Use awk for reliability across BSD/GNU ps output variations.
    echo "$ps_output" | awk -v proj="$PROJECT" '
        /python[0-9.]* +-m +orchestrator/ {
            # The project name must appear as a standalone argument.
            for (i = 1; i <= NF; i++) {
                if ($i == proj) {
                    print $1
                    exit
                }
            }
        }
    '
}

# -----------------------------------------------------------------------
# find_orchestrator_pane -- label-based pane lookup
#
# Walk the control window's panes, read their @label, and return the
# pane target (e.g. "remote-test:control.1") whose label is
# "orchestrator". Falls back to ${DEFAULT_ORCH_PANE} if no label match
# or tmux is unavailable.
# -----------------------------------------------------------------------
find_orchestrator_pane() {
    if [ -n "${_TEST_PANE_LOOKUP:-}" ]; then
        echo "$_TEST_PANE_LOOKUP"
        return 0
    fi

    if ! command -v tmux &>/dev/null; then
        echo "$DEFAULT_ORCH_PANE"
        return 0
    fi

    local panes_output
    panes_output="$(
        _tmux list-panes -t "${SESSION_NAME}:${CONTROL_WINDOW}" \
            -F '#{pane_index}|#{@label}' 2>/dev/null || true
    )"

    if [ -z "$panes_output" ]; then
        echo "$DEFAULT_ORCH_PANE"
        return 0
    fi

    local idx
    idx="$(
        echo "$panes_output" \
            | awk -F'|' '$2 == "orchestrator" { print $1; exit }'
    )"

    if [ -n "$idx" ]; then
        echo "${SESSION_NAME}:${CONTROL_WINDOW}.${idx}"
    else
        echo "$DEFAULT_ORCH_PANE"
    fi
}

# -----------------------------------------------------------------------
# kill_pid_gracefully -- SIGTERM with 5s grace, then SIGKILL
# -----------------------------------------------------------------------
kill_pid_gracefully() {
    local pid="$1"
    kill -TERM "$pid" 2>/dev/null || true

    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done

    if kill -0 "$pid" 2>/dev/null; then
        log "pid ${pid} did not exit in 5s -- sending SIGKILL."
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.5
    fi
}

# -----------------------------------------------------------------------
# Stop existing orchestrator process
# -----------------------------------------------------------------------
stop_orchestrator() {
    local pid=""

    # Primary path: read pid from lock file.
    if [ -f "$LOCK_PATH" ]; then
        pid="$(cat "$LOCK_PATH" 2>/dev/null || true)"

        if [ -z "$pid" ] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            log "Lock file ${LOCK_PATH} has no valid pid (content: '${pid}') -- removing stale lock."
            if [ "$DRY_RUN" = "true" ]; then
                echo "[DRY-RUN] rm -f ${LOCK_PATH}"
            else
                rm -f "$LOCK_PATH"
            fi
            pid=""
        elif ! kill -0 "$pid" 2>/dev/null; then
            log "Orchestrator pid ${pid} from lock file not running -- removing stale lock."
            if [ "$DRY_RUN" = "true" ]; then
                echo "[DRY-RUN] rm -f ${LOCK_PATH}"
            else
                rm -f "$LOCK_PATH"
            fi
            pid=""
        fi
    fi

    # Fallback: search for a running orch process by ps pattern. This
    # handles the migration case where the old pre-flock orchestrator
    # is still running but never wrote a lock file.
    if [ -z "$pid" ]; then
        local ps_pid
        ps_pid="$(find_orch_pid_from_ps)"
        if [ -n "$ps_pid" ]; then
            log "No lock file but found running orch via ps: pid ${ps_pid}"
            pid="$ps_pid"
        fi
    fi

    if [ -z "$pid" ]; then
        log "No running orchestrator found for project '${PROJECT}' -- nothing to stop."
        return 0
    fi

    log "Stopping orchestrator (pid ${pid})..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] kill -TERM ${pid}"
        echo "[DRY-RUN] rm -f ${LOCK_PATH}"
        return 0
    fi

    kill_pid_gracefully "$pid"

    # The flock is released automatically when the fd closes, but remove
    # the lock file so a dirty previous shutdown doesn't leave cruft.
    rm -f "$LOCK_PATH"
    log "Orchestrator stopped."
}

# -----------------------------------------------------------------------
# Relaunch orchestrator in the correct pane (label-based lookup)
# -----------------------------------------------------------------------
relaunch_orchestrator() {
    if [ "$SKIP_TMUX" = "true" ]; then
        log "Skipping tmux relaunch (_TEST_SKIP_TMUX=true)."
        return 0
    fi

    local orch_pane
    orch_pane="$(find_orchestrator_pane)"

    local launch_cmd="cd ${REPO_ROOT} && python3 -m orchestrator ${PROJECT}"

    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] tmux send-keys -t ${orch_pane} '${launch_cmd}' Enter"
        return 0
    fi

    if ! command -v tmux &>/dev/null; then
        log "ERROR: tmux not found in PATH -- cannot relaunch orchestrator." >&2
        return 1
    fi

    if ! _tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        log "WARNING: tmux session '${SESSION_NAME}' not found -- orchestrator not relaunched."
        log "Run scripts/start.sh ${PROJECT} to create the session."
        return 0
    fi

    _tmux send-keys -t "$orch_pane" "$launch_cmd" Enter
    log "Orchestrator relaunched in pane ${orch_pane}."
}

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
stop_orchestrator
relaunch_orchestrator
