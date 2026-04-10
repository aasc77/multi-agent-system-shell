#!/usr/bin/env bash
# bounce-orchestrator.sh -- Cleanly restart the orchestrator for a project
# without touching agent panes or the rest of the tmux session.
#
# Usage:
#   ./scripts/bounce-orchestrator.sh [project]
#
# Behavior:
#   1. Reads the orchestrator PID from /tmp/mas-orch-<project>.lock
#   2. Sends SIGTERM, waits up to 5s, then SIGKILL if still alive
#   3. Removes the lock file
#   4. Relaunches orchestrator in the control window's orchestrator pane
#
# The flock added in orchestrator/__main__.py auto-releases on process exit
# because the kernel tracks the underlying fd. We still rm the lock file
# afterwards so bounce is fully idempotent even on a dirty previous exit.
#
# Environment variables for testing:
#   _TEST_DRY_RUN    -- "true" to print commands instead of executing
#   _TEST_SKIP_TMUX  -- "true" to skip the tmux send-keys step (unit tests)

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
ORCH_PANE="${SESSION_NAME}:${CONTROL_WINDOW}.0"

DRY_RUN="${_TEST_DRY_RUN:-false}"
SKIP_TMUX="${_TEST_SKIP_TMUX:-false}"

# -----------------------------------------------------------------------
# Logging helper
# -----------------------------------------------------------------------
log() {
    echo "[bounce-orchestrator] $*"
}

# -----------------------------------------------------------------------
# Stop existing orchestrator process
# -----------------------------------------------------------------------
stop_orchestrator() {
    # If no lock file, there's nothing we can identify to stop.
    if [ ! -f "$LOCK_PATH" ]; then
        log "No lock file at ${LOCK_PATH} -- nothing to stop."
        return 0
    fi

    local pid
    pid="$(cat "$LOCK_PATH" 2>/dev/null || true)"

    if [ -z "$pid" ] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        log "Lock file ${LOCK_PATH} has no valid pid (content: '${pid}') -- removing stale lock."
        if [ "$DRY_RUN" = "true" ]; then
            echo "[DRY-RUN] rm -f ${LOCK_PATH}"
        else
            rm -f "$LOCK_PATH"
        fi
        return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        log "Orchestrator pid ${pid} not running -- removing stale lock."
        if [ "$DRY_RUN" = "true" ]; then
            echo "[DRY-RUN] rm -f ${LOCK_PATH}"
        else
            rm -f "$LOCK_PATH"
        fi
        return 0
    fi

    log "Stopping orchestrator (pid ${pid})..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] kill -TERM ${pid}"
        echo "[DRY-RUN] rm -f ${LOCK_PATH}"
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true

    # Wait up to 5 seconds (10 × 0.5s) for graceful shutdown.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done

    if kill -0 "$pid" 2>/dev/null; then
        log "Orchestrator pid ${pid} did not exit in 5s -- sending SIGKILL."
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.5
    fi

    # The flock is released automatically when the fd closes, but remove
    # the lock file so a dirty previous shutdown doesn't leave cruft.
    rm -f "$LOCK_PATH"
    log "Orchestrator stopped."
}

# -----------------------------------------------------------------------
# Relaunch orchestrator in the control pane
# -----------------------------------------------------------------------
relaunch_orchestrator() {
    if [ "$SKIP_TMUX" = "true" ]; then
        log "Skipping tmux relaunch (_TEST_SKIP_TMUX=true)."
        return 0
    fi

    local launch_cmd="cd ${REPO_ROOT} && python3 -m orchestrator ${PROJECT}"

    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] tmux send-keys -t ${ORCH_PANE} '${launch_cmd}' Enter"
        return 0
    fi

    if ! command -v tmux &>/dev/null; then
        log "ERROR: tmux not found in PATH -- cannot relaunch orchestrator." >&2
        return 1
    fi

    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        log "WARNING: tmux session '${SESSION_NAME}' not found -- orchestrator not relaunched."
        log "Run scripts/start.sh ${PROJECT} to create the session."
        return 0
    fi

    tmux send-keys -t "$ORCH_PANE" "$launch_cmd" Enter
    log "Orchestrator relaunched in pane ${ORCH_PANE}."
}

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
stop_orchestrator
relaunch_orchestrator
