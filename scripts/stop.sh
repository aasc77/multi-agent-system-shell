#!/usr/bin/env bash
# stop.sh -- Stop/Cleanup Script
#
# Requirements traced to PRD:
#   R8: Scripts (stop.sh kills tmux session, cleans up MCP configs)
#   R1: tmux Session Layout (session lifecycle management)
#
# Usage:
#   ./scripts/stop.sh <project> [--kill-nats]
#
# Options:
#   --kill-nats  Also stop nats-server process (default: leave running)
#
# Environment variables for testing:
#   _TEST_DRY_RUN        -- "true" to print commands instead of executing
#   _TEST_SESSION_EXISTS -- "true"/"false" to override tmux session check

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
readonly ROOT_DIR="$(pwd)"
readonly MCP_CONFIGS_DIR_NAME=".mcp-configs"
readonly FLAG_KILL_NATS="--kill-nats"

DRY_RUN="${_TEST_DRY_RUN:-false}"

# -----------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------
if [ $# -lt 1 ]; then
    echo "Usage: $0 <project> [${FLAG_KILL_NATS}]" >&2
    echo "  project: Name of the project directory under projects/" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Parse arguments
# -----------------------------------------------------------------------
PROJECT=""
KILL_NATS=false

for arg in "$@"; do
    case "$arg" in
        "${FLAG_KILL_NATS}")
            KILL_NATS=true
            ;;
        *)
            if [ -z "$PROJECT" ]; then
                PROJECT="$arg"
            fi
            ;;
    esac
done

if [ -z "$PROJECT" ]; then
    echo "Usage: $0 <project> [${FLAG_KILL_NATS}]" >&2
    exit 1
fi

SESSION_NAME="$PROJECT"

# -----------------------------------------------------------------------
# session_exists -- Check whether the tmux session is running.
# Respects _TEST_SESSION_EXISTS env var for testability.
# -----------------------------------------------------------------------
session_exists() {
    if [ -n "${_TEST_SESSION_EXISTS:-}" ]; then
        [ "$_TEST_SESSION_EXISTS" = "true" ]
        return
    fi
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
}

# -----------------------------------------------------------------------
# Signal SSH reconnect loops to stop before killing the session
# -----------------------------------------------------------------------
echo "Signaling SSH agents to stop reconnecting..."
for sentinel in /tmp/mas-launch-*; do
    [ -f "$sentinel" ] || continue
    agent_name=$(basename "$sentinel" | sed 's/^mas-launch-//; s/\.sh$//')
    touch "/tmp/mas-stop-${agent_name}"
done

# -----------------------------------------------------------------------
# Kill knowledge-store indexer
# -----------------------------------------------------------------------
echo "Stopping knowledge indexer..."
if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY-RUN] pkill -f knowledge-store/indexer.py"
else
    pkill -f "knowledge-store/indexer.py" 2>/dev/null || true
fi

# -----------------------------------------------------------------------
# Kill tmux session(s)
#
# start.sh creates a grouped-session family:
#   <name>            -- primary (-d -s <name>)
#   <name>-control    -- secondary for the control window launcher
#   <name>-agents     -- secondary for the agents window launcher
#
# We must kill ALL of them, otherwise leftover grouped sessions pile up
# and show stale layouts pointing at whichever window they last attached.
# Belt-and-suspenders: also query tmux for any remaining sessions in the
# group (catches historical sessions auto-numbered by tmux pre-fix).
# -----------------------------------------------------------------------
kill_tmux_session() {
    local target="$1"
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] tmux kill-session -t $target"
    else
        tmux kill-session -t "$target" 2>/dev/null || true
    fi
}

if session_exists; then
    echo "Stopping tmux session '$SESSION_NAME' and grouped sessions..."

    # Kill the deterministic names first.
    for suffix in "" "-control" "-agents"; do
        target="${SESSION_NAME}${suffix}"
        if [ "$DRY_RUN" = "true" ]; then
            kill_tmux_session "$target"
        else
            if tmux has-session -t "$target" 2>/dev/null; then
                kill_tmux_session "$target"
            fi
        fi
    done

    # Belt-and-suspenders: any other sessions in the same group (e.g.
    # auto-numbered cruft created before the -A -s fix landed).
    if [ "$DRY_RUN" != "true" ]; then
        tmux list-sessions -F '#{session_name}|#{session_group}' 2>/dev/null \
            | awk -F'|' -v g="${SESSION_NAME}" '$2==g {print $1}' \
            | while read -r stray; do
                [ -z "$stray" ] && continue
                echo "Killing stray grouped session: $stray"
                tmux kill-session -t "$stray" 2>/dev/null || true
            done
    fi

    echo "Session '$SESSION_NAME' killed."
else
    echo "Session '$SESSION_NAME' already stopped."
fi

# -----------------------------------------------------------------------
# Clean up MCP configs
# -----------------------------------------------------------------------
MCP_DIR="${ROOT_DIR}/projects/${PROJECT}/${MCP_CONFIGS_DIR_NAME}"
if [ -d "$MCP_DIR" ]; then
    echo "Cleaning up MCP configs at $MCP_DIR..."
    rm -rf "$MCP_DIR"
    echo "MCP configs cleaned."
fi

# -----------------------------------------------------------------------
# Kill NATS if --kill-nats flag provided
# -----------------------------------------------------------------------
if [ "$KILL_NATS" = true ]; then
    echo "Stopping nats-server..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] pkill nats-server"
    else
        pkill nats-server 2>/dev/null || true
    fi
    echo "nats-server stopped."
fi

exit 0
