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
# Kill tmux session
# -----------------------------------------------------------------------
if session_exists; then
    echo "Stopping tmux session '$SESSION_NAME'..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] tmux kill-session -t $SESSION_NAME"
    else
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
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
