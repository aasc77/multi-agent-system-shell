#!/usr/bin/env bash
# manage-magentic-ui.sh -- Lifecycle management for Magentic-UI
#
# Subcommands: start, stop, status
#
# Usage:
#   ./scripts/dgx/manage-magentic-ui.sh <start|stop|status> [--help]
#
# Environment variables:
#   MAGENTIC_UI_DIR  -- Install directory (default: ~/magentic-ui)
#   MAGENTIC_UI_PORT -- Port (default: 8888)
#   MAGENTIC_UI_HOST -- Bind address (default: 0.0.0.0)

set -euo pipefail

readonly MAGENTIC_UI_DIR="${MAGENTIC_UI_DIR:-$HOME/magentic-ui}"
readonly MAGENTIC_UI_PORT="${MAGENTIC_UI_PORT:-8888}"
readonly MAGENTIC_UI_HOST="${MAGENTIC_UI_HOST:-0.0.0.0}"
readonly PID_FILE="/tmp/magentic-ui.pid"

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || -z "${1:-}" ]]; then
    echo "Usage: $(basename "$0") <start|stop|status>"
    echo ""
    echo "Manage Magentic-UI lifecycle."
    echo ""
    echo "Subcommands:"
    echo "  start   Start Magentic-UI in the background"
    echo "  stop    Stop the running instance"
    echo "  status  Check if Magentic-UI is running"
    echo ""
    echo "Environment variables:"
    echo "  MAGENTIC_UI_DIR   Install directory (default: $MAGENTIC_UI_DIR)"
    echo "  MAGENTIC_UI_PORT  Port (default: $MAGENTIC_UI_PORT)"
    echo "  MAGENTIC_UI_HOST  Bind address (default: $MAGENTIC_UI_HOST)"
    exit 0
fi

SUBCOMMAND="$1"

_is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

case "$SUBCOMMAND" in
    start)
        if _is_running; then
            echo "Magentic-UI is already running (PID: $(cat "$PID_FILE"))."
            exit 0
        fi

        if [[ ! -d "$MAGENTIC_UI_DIR" ]]; then
            echo "Error: Magentic-UI not found at $MAGENTIC_UI_DIR. Run setup-magentic-ui.sh first." >&2
            exit 1
        fi

        echo "Starting Magentic-UI on ${MAGENTIC_UI_HOST}:${MAGENTIC_UI_PORT}..."
        cd "$MAGENTIC_UI_DIR"

        # Activate venv (PEP 668 -- Ubuntu 24.04 blocks system-wide pip)
        if [[ -f ".venv/bin/activate" ]]; then
            # shellcheck disable=SC1091
            source .venv/bin/activate
        fi

        if ! command -v magentic-ui &>/dev/null; then
            echo "Error: magentic-ui CLI not found. Run setup-magentic-ui.sh first." >&2
            exit 1
        fi

        nohup magentic-ui --fara --host "$MAGENTIC_UI_HOST" --port "$MAGENTIC_UI_PORT" > /tmp/magentic-ui.log 2>&1 &
        echo $! > "$PID_FILE"
        disown
        echo "Magentic-UI started (PID: $(cat "$PID_FILE"), log: /tmp/magentic-ui.log)."
        ;;

    stop)
        if _is_running; then
            local pid
            pid=$(cat "$PID_FILE")
            echo "Stopping Magentic-UI (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            rm -f "$PID_FILE"
            echo "Stopped."
        else
            echo "Magentic-UI is not running."
        fi
        ;;

    status)
        if _is_running; then
            echo "PASS: Magentic-UI is running (PID: $(cat "$PID_FILE"))."
            exit 0
        else
            echo "FAIL: Magentic-UI is not running."
            exit 1
        fi
        ;;

    *)
        echo "Error: Unknown subcommand '$SUBCOMMAND'" >&2
        echo "Run '$(basename "$0") --help' for usage." >&2
        exit 1
        ;;
esac
