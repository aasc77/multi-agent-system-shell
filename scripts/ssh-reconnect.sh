#!/usr/bin/env bash
# ssh-reconnect.sh — auto-reconnect wrapper for remote SSH agents
#
# Usage: ssh-reconnect.sh <agent-name>
#
# Reads the launch command from /tmp/mas-launch-<agent-name>.sh and runs it
# in a loop. Reconnects automatically when SSH drops (exit code != 0).
# Stops when:
#   - The command exits cleanly (exit 0 = user quit intentionally)
#   - A stop sentinel file exists (/tmp/mas-stop-<agent-name>)
#   - Max retries exceeded (50)
#
# Backoff: 5s → 10s → 20s → 40s → 60s (capped). Resets after a connection
# survives longer than 60 seconds.

set -uo pipefail

AGENT_NAME="${1:?Usage: ssh-reconnect.sh <agent-name>}"
LAUNCH_SCRIPT="/tmp/mas-launch-${AGENT_NAME}.sh"
SENTINEL="/tmp/mas-stop-${AGENT_NAME}"

MAX_RETRIES=50
MAX_DELAY=60
STABLE_THRESHOLD=60  # seconds — connection is "stable" if it lasts this long

delay=5
retry=0

# Clean up sentinel from previous runs
rm -f "$SENTINEL"

if [ ! -f "$LAUNCH_SCRIPT" ]; then
    echo "ERROR: Launch script not found: $LAUNCH_SCRIPT"
    exit 1
fi

while true; do
    start_time=$(date +%s)

    # Run the agent launch command
    bash "$LAUNCH_SCRIPT"
    rc=$?

    # Clean exit — user quit intentionally (e.g. /exit in Claude Code)
    if [ $rc -eq 0 ]; then
        echo ""
        echo "[$AGENT_NAME] Session ended normally."
        break
    fi

    # Check if stop was requested (by stop.sh or manual touch)
    if [ -f "$SENTINEL" ]; then
        echo "[$AGENT_NAME] Stop requested."
        rm -f "$SENTINEL"
        break
    fi

    # Track retries
    retry=$((retry + 1))
    if [ $retry -gt $MAX_RETRIES ]; then
        echo "[$AGENT_NAME] Max retries ($MAX_RETRIES) reached. Giving up."
        break
    fi

    # If the connection lasted long enough, reset backoff
    elapsed=$(( $(date +%s) - start_time ))
    if [ $elapsed -ge $STABLE_THRESHOLD ]; then
        delay=5
        retry=0
    fi

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  $AGENT_NAME — connection lost (exit $rc)"
    echo "  Reconnecting in ${delay}s...  (attempt $retry/$MAX_RETRIES)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    sleep "$delay"

    # Exponential backoff capped at MAX_DELAY
    delay=$((delay * 2))
    if [ $delay -gt $MAX_DELAY ]; then
        delay=$MAX_DELAY
    fi
done

# Clean up launch script
rm -f "$LAUNCH_SCRIPT"
