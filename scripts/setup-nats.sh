#!/usr/bin/env bash
# setup-nats.sh -- Install and start NATS server with JetStream
#
# Requirements traced to PRD:
#   R8: Scripts -- setup-nats.sh installs nats-server + nats CLI via brew, starts with JetStream
#
# Usage:
#   ./scripts/setup-nats.sh
#
# Environment variables for testing:
#   _TEST_DRY_RUN               -- "true" to print commands instead of executing
#   _TEST_NATS_SERVER_INSTALLED -- "true"/"false" to override nats-server installed check
#   _TEST_NATS_CLI_INSTALLED    -- "true"/"false" to override nats CLI installed check
#   _TEST_NATS_RUNNING          -- "true"/"false" to override nats running check

set -euo pipefail

DRY_RUN="${_TEST_DRY_RUN:-false}"

echo "Setting up NATS server..."

# -----------------------------------------------------------------------
# Check/install nats-server
# -----------------------------------------------------------------------
nats_server_installed() {
    if [ -n "${_TEST_NATS_SERVER_INSTALLED:-}" ]; then
        [ "$_TEST_NATS_SERVER_INSTALLED" = "true" ]
        return
    fi
    command -v nats-server &>/dev/null
}

if ! nats_server_installed; then
    echo "Installing nats-server via brew..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] brew install nats-server"
    else
        brew install nats-server 2>/dev/null || {
            echo "Error: Failed to install nats-server" >&2
            exit 1
        }
    fi
else
    echo "nats-server already installed. Skipping."
fi

# -----------------------------------------------------------------------
# Check/install nats CLI
# -----------------------------------------------------------------------
nats_cli_installed() {
    if [ -n "${_TEST_NATS_CLI_INSTALLED:-}" ]; then
        [ "$_TEST_NATS_CLI_INSTALLED" = "true" ]
        return
    fi
    command -v nats &>/dev/null
}

if ! nats_cli_installed; then
    echo "Installing nats CLI via brew..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] brew tap nats-io/nats-tools"
        echo "[DRY-RUN] brew install nats-io/nats-tools/nats"
    else
        brew tap nats-io/nats-tools 2>/dev/null || true
        brew install nats-io/nats-tools/nats 2>/dev/null || {
            echo "Warning: Failed to install nats CLI" >&2
        }
    fi
else
    echo "nats CLI already installed. Skipping."
fi

# -----------------------------------------------------------------------
# Start NATS with JetStream
# -----------------------------------------------------------------------
nats_is_running() {
    if [ -n "${_TEST_NATS_RUNNING:-}" ]; then
        [ "$_TEST_NATS_RUNNING" = "true" ]
        return
    fi
    pgrep -x nats-server &>/dev/null
}

if nats_is_running; then
    echo "NATS server is already running. Skipping start."
else
    echo "Starting nats-server with JetStream enabled..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] nats-server -js --store_dir /tmp/nats-store -p 4222 &"
    else
        nats-server -js --store_dir /tmp/nats-store -p 4222 &
        disown
        sleep 1
    fi
    echo "nats-server started with -js flag."
fi

echo "NATS setup complete."
