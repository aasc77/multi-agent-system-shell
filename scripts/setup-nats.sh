#!/usr/bin/env bash
# setup-nats.sh -- Install and start NATS server with JetStream
#
# Requirements traced to PRD:
#   R8: Scripts -- setup-nats.sh installs nats-server + nats CLI via brew, starts with JetStream
#
# Usage:
#   ./scripts/setup-nats.sh

set -euo pipefail

echo "Setting up NATS server..."

# Check if nats-server is installed
if ! command -v nats-server &>/dev/null; then
    echo "Installing nats-server via brew..."
    brew install nats-server 2>/dev/null || {
        echo "Error: Failed to install nats-server" >&2
        exit 1
    }
fi

# Check if nats CLI is installed
if ! command -v nats &>/dev/null; then
    echo "Installing nats CLI via brew..."
    brew tap nats-io/nats-tools 2>/dev/null || true
    brew install nats-io/nats-tools/nats 2>/dev/null || {
        echo "Warning: Failed to install nats CLI" >&2
    }
fi

# Start NATS with JetStream if not already running
if pgrep -x nats-server &>/dev/null; then
    echo "NATS server is already running."
else
    echo "Starting NATS server with JetStream..."
    nats-server --jetstream --store_dir /tmp/nats-store -p 4222 &
    disown
    sleep 1
    echo "NATS server started."
fi

echo "NATS setup complete."
