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
# Examples:
#   ./scripts/nats-monitor.sh                  # Subscribe to all agent messages
#   ./scripts/nats-monitor.sh agents.writer.>   # Subscribe to writer only
#   ./scripts/nats-monitor.sh system.health     # Subscribe to health checks
#   ./scripts/nats-monitor.sh ">"               # Subscribe to ALL subjects
#
# Environment variables for testing:
#   _TEST_DRY_RUN          -- "true" to print command instead of executing
#   _TEST_NATS_CLI_MISSING -- "true" to simulate missing nats CLI

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
readonly DEFAULT_SUBJECT="agents.>"
readonly NATS_CLI_INSTALL_CMD="brew install nats-io/nats-tools/nats"
readonly NATS_CLI_MISSING_MSG="Error: nats CLI is not installed. Install with: ${NATS_CLI_INSTALL_CMD}"

DRY_RUN="${_TEST_DRY_RUN:-false}"
SUBJECT="${1:-${DEFAULT_SUBJECT}}"

# -----------------------------------------------------------------------
# require_nats_cli -- Verify the nats CLI is available; exit 1 if not.
# Respects _TEST_NATS_CLI_MISSING env var for testability.
# -----------------------------------------------------------------------
require_nats_cli() {
    if [ "${_TEST_NATS_CLI_MISSING:-}" = "true" ]; then
        echo "${NATS_CLI_MISSING_MSG}" >&2
        exit 1
    fi

    if ! command -v nats &>/dev/null; then
        echo "${NATS_CLI_MISSING_MSG}" >&2
        exit 1
    fi
}

require_nats_cli

# -----------------------------------------------------------------------
# Subscribe to NATS subjects
# -----------------------------------------------------------------------
if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY-RUN] nats sub \"$SUBJECT\""
    echo "Subscribing to subject: $SUBJECT"
    exit 0
fi

exec nats sub "$SUBJECT"
