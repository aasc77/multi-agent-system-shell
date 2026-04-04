#!/usr/bin/env bash
# daily-status.sh -- Daily system status report
#
# Runs ops-status.sh, publishes a summary to NATS (manager inbox),
# and indexes it into the operational_knowledge collection.
#
# Usage:
#   ./scripts/daily-status.sh
#
# Designed to run via launchd at 8am daily.
# See plists/com.mas.daily-status.plist for the launchd config.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
LOG_DIR="${ROOT_DIR}/data/logs"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
DATE_TAG="$(date '+%Y-%m-%d')"

mkdir -p "$LOG_DIR"

# -----------------------------------------------------------------------
# 1. Run ops-status.sh and capture output
# -----------------------------------------------------------------------
OPS_STATUS_SCRIPT="${SCRIPT_DIR}/ops-status.sh"

if [ ! -f "$OPS_STATUS_SCRIPT" ]; then
    echo "ERROR: ops-status.sh not found at $OPS_STATUS_SCRIPT" >&2
    exit 1
fi

echo "Running ops-status.sh..."
STATUS_OUTPUT=$(bash "$OPS_STATUS_SCRIPT" 2>&1) || true
STATUS_EXIT=$?

# Save raw output to log
LOG_FILE="${LOG_DIR}/daily-status-${DATE_TAG}.log"
echo "$STATUS_OUTPUT" > "$LOG_FILE"
echo "Status log saved to $LOG_FILE"

# -----------------------------------------------------------------------
# 2. Build summary
# -----------------------------------------------------------------------
PASS_COUNT=$(echo "$STATUS_OUTPUT" | grep -c "PASS" || echo "0")
FAIL_COUNT=$(echo "$STATUS_OUTPUT" | grep -c "FAIL" || echo "0")
WARN_COUNT=$(echo "$STATUS_OUTPUT" | grep -c "WARN" || echo "0")

# Extract key failures
FAILURES=$(echo "$STATUS_OUTPUT" | grep "FAIL" || echo "(none)")
WARNINGS=$(echo "$STATUS_OUTPUT" | grep "WARN" || echo "(none)")

if [ "$FAIL_COUNT" -gt 0 ]; then
    HEALTH="DEGRADED"
elif [ "$WARN_COUNT" -gt 0 ]; then
    HEALTH="OK (with warnings)"
else
    HEALTH="HEALTHY"
fi

SUMMARY="Daily MAS Status Report — ${DATE_TAG}
Overall: ${HEALTH} (${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${WARN_COUNT} warnings)

Failures:
${FAILURES}

Warnings:
${WARNINGS}

Full output saved to: ${LOG_FILE}"

echo ""
echo "--- Summary ---"
echo "$SUMMARY"

# -----------------------------------------------------------------------
# 3. Publish summary to NATS (manager inbox)
# -----------------------------------------------------------------------
if command -v nats &>/dev/null; then
    MESSAGE_ID="daily-status-${DATE_TAG}"
    NATS_PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'type': 'status_report',
    'message_id': '${MESSAGE_ID}',
    'timestamp': '${TIMESTAMP}',
    'from': 'system',
    'priority': 'normal',
    'message': sys.stdin.read()
}))
" <<< "$SUMMARY")

    if nats pub agents.manager.inbox "$NATS_PAYLOAD" 2>/dev/null; then
        echo "Published summary to NATS (agents.manager.inbox)"
    else
        echo "WARNING: Failed to publish to NATS" >&2
    fi
else
    echo "WARNING: nats CLI not found, skipping NATS publish" >&2
fi

# -----------------------------------------------------------------------
# 4. Index into operational_knowledge collection
# -----------------------------------------------------------------------
echo "Indexing summary into knowledge store..."
echo "$SUMMARY" | ROOT_DIR="$ROOT_DIR" DATE_TAG="$DATE_TAG" python3 -c "
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.environ['ROOT_DIR'], 'knowledge-store'))
import store

async def index():
    text = sys.stdin.read()
    date_tag = os.environ['DATE_TAG']
    try:
        await store.index_document(
            text=text,
            title=f'Daily Status Report {date_tag}',
            category='status',
            doc_id=f'daily-status-{date_tag}',
        )
        print('  OK  Indexed daily status report')
    except Exception as e:
        print(f'  FAIL  Could not index: {e}', file=sys.stderr)

asyncio.run(index())
" 2>&1 || echo "WARNING: Knowledge store indexing failed" >&2

echo "Daily status report complete."
