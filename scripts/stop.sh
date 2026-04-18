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

# If MAS_TMUX_SOCKET is set, route every tmux invocation through
# `tmux -L <socket>` — dedicated socket = dedicated server = full
# namespace isolation from default-socket user sessions. Used by
# pytest (see tests/conftest.py). See #46.
_tmux() {
    if [ -n "${MAS_TMUX_SOCKET:-}" ]; then
        command tmux -L "$MAS_TMUX_SOCKET" "$@"
    else
        command tmux "$@"
    fi
}

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
    _tmux has-session -t "$SESSION_NAME" 2>/dev/null
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
# Kill background services spawned by start.sh
#
# start.sh launches three daemons as backgrounded children:
#   - knowledge-store/indexer.py
#   - services/speaker-service.py
#   - services/thermostat-service.py
#
# When start.sh's parent bash exits they reparent to launchd (PPID=1)
# and survive a naive tmux-session kill, so we explicitly pkill each.
# We issue SIGTERM first (services/speaker-service.py and
# services/thermostat-service.py install their own asyncio-backed
# SIGTERM handlers for graceful shutdown), wait a short grace period,
# then escalate to SIGKILL for any stragglers whose event loop was
# stuck on a blocking await at the time the signal arrived.
#
# Caveat: pkill -f is a substring match on the full argv, so a process
# whose args happen to contain the same string (e.g. `tail -f services/
# speaker-service.log` or a text editor open on the file) would also be
# killed. In current repo usage nothing else runs those strings, but
# future callers of these pkills should be aware.
# -----------------------------------------------------------------------
BG_SERVICES=(
    "knowledge-store/indexer.py"
    "services/speaker-service.py"
    "services/thermostat-service.py"
)

for svc in "${BG_SERVICES[@]}"; do
    echo "Stopping $svc..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] pkill -f $svc"
    else
        pkill -f "$svc" 2>/dev/null || true
    fi
done

if [ "$DRY_RUN" != "true" ]; then
    sleep 1
    for svc in "${BG_SERVICES[@]}"; do
        if pgrep -f "$svc" &>/dev/null; then
            echo "SIGKILL stragglers: $svc"
            pkill -9 -f "$svc" 2>/dev/null || true
        fi
    done
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
        _tmux kill-session -t "$target" 2>/dev/null || true
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
            if _tmux has-session -t "$target" 2>/dev/null; then
                kill_tmux_session "$target"
            fi
        fi
    done

    # Belt-and-suspenders: any other sessions in the same group (e.g.
    # auto-numbered cruft created before the -A -s fix landed).
    if [ "$DRY_RUN" != "true" ]; then
        # `|| true` on the list: when no tmux server is running on
        # the active socket (MAS_TMUX_SOCKET case during pytest, or
        # default socket with no sessions), `list-sessions` exits
        # non-zero and pipefail would kill stop.sh. Swallow it.
        { _tmux list-sessions -F '#{session_name}|#{session_group}' 2>/dev/null || true; } \
            | awk -F'|' -v g="${SESSION_NAME}" '$2==g {print $1}' \
            | while read -r stray; do
                [ -z "$stray" ] && continue
                echo "Killing stray grouped session: $stray"
                _tmux kill-session -t "$stray" 2>/dev/null || true
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
