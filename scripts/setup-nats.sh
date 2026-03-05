#!/usr/bin/env bash
# setup-nats.sh -- Install and start NATS server with JetStream
#
# Requirements traced to PRD:
#   R8: Scripts -- setup-nats.sh installs nats-server + nats CLI via brew, starts with JetStream
#
# Usage:
#   ./scripts/setup-nats.sh

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
readonly NATS_PORT=4222
readonly NATS_STORE_DIR="/tmp/nats-store"
readonly NATS_TAP="nats-io/nats-tools"
readonly NATS_FORMULA="nats-io/nats-tools/nats"

# -----------------------------------------------------------------------
# Installation
# -----------------------------------------------------------------------
install_nats_server() {
    # Install nats-server via Homebrew if not already present.
    if command -v nats-server &>/dev/null; then
        return
    fi

    echo "Installing nats-server via brew..."
    brew install nats-server 2>/dev/null || {
        echo "Error: Failed to install nats-server" >&2
        exit 1
    }
}

install_nats_cli() {
    # Install the nats CLI tool via Homebrew if not already present.
    if command -v nats &>/dev/null; then
        return
    fi

    echo "Installing nats CLI via brew..."
    brew tap "$NATS_TAP" 2>/dev/null || true
    brew install "$NATS_FORMULA" 2>/dev/null || {
        echo "Warning: Failed to install nats CLI" >&2
    }
}

# -----------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------
start_nats_server() {
    # Start NATS with JetStream if not already running.
    if pgrep -x nats-server &>/dev/null; then
        echo "NATS server is already running."
        return
    fi

    echo "Starting NATS server with JetStream..."
    nats-server --jetstream --store_dir "$NATS_STORE_DIR" -p "$NATS_PORT" &
    disown
    sleep 1
    echo "NATS server started."
}

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
echo "Setting up NATS server..."
install_nats_server
install_nats_cli
start_nats_server
echo "NATS setup complete."
