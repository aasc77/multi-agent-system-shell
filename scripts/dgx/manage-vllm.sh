#!/usr/bin/env bash
# manage-vllm.sh -- Lifecycle management for vLLM container
#
# Subcommands: start, stop, status, logs, flush
#
# Usage:
#   ./scripts/dgx/manage-vllm.sh <start|stop|status|logs|flush> [--help]
#
# Environment variables:
#   VLLM_CONTAINER -- Container name (default: qwen-vllm)
#   VLLM_PORT      -- Port (default: 5000)

set -euo pipefail

readonly VLLM_CONTAINER="${VLLM_CONTAINER:-qwen-vllm}"
readonly VLLM_PORT="${VLLM_PORT:-5000}"
readonly HEALTHCHECK_URL="http://localhost:${VLLM_PORT}/v1/models"

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || -z "${1:-}" ]]; then
    echo "Usage: $(basename "$0") <start|stop|status|logs|flush>"
    echo ""
    echo "Manage the vLLM container lifecycle."
    echo ""
    echo "Subcommands:"
    echo "  start   Start the stopped container"
    echo "  stop    Stop the running container"
    echo "  status  Check healthcheck endpoint (/v1/models)"
    echo "  logs    Tail container logs"
    echo "  flush   Flush system memory caches (requires sudo)"
    echo ""
    echo "Environment variables:"
    echo "  VLLM_CONTAINER  Container name (default: $VLLM_CONTAINER)"
    echo "  VLLM_PORT       Port (default: $VLLM_PORT)"
    exit 0
fi

SUBCOMMAND="$1"

case "$SUBCOMMAND" in
    start)
        if docker ps --format '{{.Names}}' | grep -qx "$VLLM_CONTAINER"; then
            echo "Container '$VLLM_CONTAINER' is already running."
            exit 0
        fi
        if docker ps -a --format '{{.Names}}' | grep -qx "$VLLM_CONTAINER"; then
            echo "Starting container '$VLLM_CONTAINER'..."
            docker start "$VLLM_CONTAINER"
        else
            echo "Error: Container '$VLLM_CONTAINER' does not exist. Run setup-vllm.sh first." >&2
            exit 1
        fi
        ;;

    stop)
        if docker ps --format '{{.Names}}' | grep -qx "$VLLM_CONTAINER"; then
            echo "Stopping container '$VLLM_CONTAINER'..."
            docker stop "$VLLM_CONTAINER"
        else
            echo "Container '$VLLM_CONTAINER' is not running."
        fi
        ;;

    status)
        if ! docker ps --format '{{.Names}}' | grep -qx "$VLLM_CONTAINER"; then
            echo "FAIL: Container '$VLLM_CONTAINER' is not running."
            exit 1
        fi
        echo "Container is running."
        if curl -sf "$HEALTHCHECK_URL" > /dev/null 2>&1; then
            echo "PASS: vLLM healthcheck OK ($HEALTHCHECK_URL)"
            exit 0
        else
            echo "FAIL: vLLM healthcheck failed ($HEALTHCHECK_URL)"
            exit 1
        fi
        ;;

    logs)
        docker logs -f "$VLLM_CONTAINER"
        ;;

    flush)
        echo "Flushing system memory caches..."
        sudo sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches'
        echo "Memory caches flushed."
        ;;

    *)
        echo "Error: Unknown subcommand '$SUBCOMMAND'" >&2
        echo "Run '$(basename "$0") --help' for usage." >&2
        exit 1
        ;;
esac
