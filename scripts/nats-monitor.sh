#!/usr/bin/env bash
# nats-monitor.sh -- NATS Message Monitor
#
# Requirements traced to PRD:
#   R1: tmux Session Layout (NATS monitor pane runs nats sub "agents.>")
#   R8: Scripts (nats-monitor.sh subscribes to NATS subjects, optional filter)
#
# Usage:
#   ./scripts/nats-monitor.sh [subject]
#   Default subject: agents.>
#
# Environment variables for testing:
#   _TEST_DRY_RUN          -- "true" to print command instead of executing
#   _TEST_NATS_CLI_MISSING -- "true" to simulate missing nats CLI

set -euo pipefail

DRY_RUN="${_TEST_DRY_RUN:-false}"
SUBJECT="${1:-agents.>}"

# -----------------------------------------------------------------------
# Check nats CLI availability
# -----------------------------------------------------------------------
if [ "${_TEST_NATS_CLI_MISSING:-}" = "true" ]; then
    echo "Error: nats CLI is not installed. Install with: brew install nats-io/nats-tools/nats" >&2
    exit 1
fi

if ! command -v nats &>/dev/null; then
    echo "Error: nats CLI is not installed. Install with: brew install nats-io/nats-tools/nats" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Subscribe to NATS subjects
# -----------------------------------------------------------------------
if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY-RUN] nats sub \"$SUBJECT\""
    echo "Subscribing to subject: $SUBJECT"
    exit 0
fi

exec nats sub "$SUBJECT"
