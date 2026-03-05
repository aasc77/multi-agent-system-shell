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
# Environment variables for testing:
#   _TEST_DRY_RUN        -- "true" to print commands instead of executing
#   _TEST_SESSION_EXISTS -- "true"/"false" to override tmux session check

set -euo pipefail

ROOT_DIR="$(pwd)"
DRY_RUN="${_TEST_DRY_RUN:-false}"

# -----------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------
if [ $# -lt 1 ]; then
    echo "Usage: $0 <project> [--kill-nats]" >&2
    echo "  project: Name of the project directory under projects/" >&2
    exit 1
fi

# Parse arguments
PROJECT=""
KILL_NATS=false

for arg in "$@"; do
    case "$arg" in
        --kill-nats)
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
    echo "Usage: $0 <project> [--kill-nats]" >&2
    exit 1
fi

SESSION_NAME="$PROJECT"

# -----------------------------------------------------------------------
# Check if session exists
# -----------------------------------------------------------------------
session_exists() {
    if [ -n "${_TEST_SESSION_EXISTS:-}" ]; then
        [ "$_TEST_SESSION_EXISTS" = "true" ]
        return
    fi
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
}

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
MCP_DIR="${ROOT_DIR}/projects/${PROJECT}/.mcp-configs"
if [ -d "$MCP_DIR" ]; then
    echo "Cleaning up MCP configs at $MCP_DIR..."
    rm -rf "$MCP_DIR"
    echo "MCP configs cleaned."
fi

# -----------------------------------------------------------------------
# Kill NATS if --kill-nats flag
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
