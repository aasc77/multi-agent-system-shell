#!/usr/bin/env bash
# setup-nats.sh -- Install and start NATS server with JetStream
#
# Requirements traced to PRD:
#   R8: Scripts -- setup-nats.sh installs nats-server + nats CLI via brew, starts with JetStream
#   R3: Communication Flow -- NATS JetStream is the data layer
#
# Usage:
#   ./scripts/setup-nats.sh
#
# What this script does:
#   1. Installs nats-server via Homebrew (if not already installed)
#   2. Installs the nats CLI via Homebrew tap (if not already installed)
#   3. Starts nats-server with JetStream enabled (if not already running)
#
# Environment variables for testing:
#   _TEST_DRY_RUN               -- "true" to print commands instead of executing
#   _TEST_NATS_SERVER_INSTALLED -- "true"/"false" to override nats-server installed check
#   _TEST_NATS_CLI_INSTALLED    -- "true"/"false" to override nats CLI installed check
#   _TEST_NATS_RUNNING          -- "true"/"false" to override nats running check

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
readonly NATS_PORT=4222
readonly NATS_STORE_DIR="/tmp/nats-store"
readonly BREW_NATS_SERVER="nats-server"
readonly BREW_NATS_CLI_TAP="nats-io/nats-tools"
readonly BREW_NATS_CLI_PKG="nats-io/nats-tools/nats"

DRY_RUN="${_TEST_DRY_RUN:-false}"

echo "Setting up NATS server..."

# -----------------------------------------------------------------------
# nats_server_installed -- Check if nats-server binary is available.
# Respects _TEST_NATS_SERVER_INSTALLED env var for testability.
# -----------------------------------------------------------------------
nats_server_installed() {
    if [ -n "${_TEST_NATS_SERVER_INSTALLED:-}" ]; then
        [ "$_TEST_NATS_SERVER_INSTALLED" = "true" ]
        return
    fi
    command -v nats-server &>/dev/null
}

# -----------------------------------------------------------------------
# Install nats-server
# -----------------------------------------------------------------------
if ! nats_server_installed; then
    echo "Installing nats-server via brew..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] brew install ${BREW_NATS_SERVER}"
    else
        brew install "${BREW_NATS_SERVER}" 2>/dev/null || {
            echo "Error: Failed to install nats-server" >&2
            exit 1
        }
    fi
else
    echo "nats-server already installed. Skipping."
fi

# -----------------------------------------------------------------------
# nats_cli_installed -- Check if nats CLI binary is available.
# Respects _TEST_NATS_CLI_INSTALLED env var for testability.
# -----------------------------------------------------------------------
nats_cli_installed() {
    if [ -n "${_TEST_NATS_CLI_INSTALLED:-}" ]; then
        [ "$_TEST_NATS_CLI_INSTALLED" = "true" ]
        return
    fi
    command -v nats &>/dev/null
}

# -----------------------------------------------------------------------
# Install nats CLI
# -----------------------------------------------------------------------
if ! nats_cli_installed; then
    echo "Installing nats CLI via brew..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] brew tap ${BREW_NATS_CLI_TAP}"
        echo "[DRY-RUN] brew install ${BREW_NATS_CLI_PKG}"
    else
        brew tap "${BREW_NATS_CLI_TAP}" 2>/dev/null || true
        brew install "${BREW_NATS_CLI_PKG}" 2>/dev/null || {
            echo "Warning: Failed to install nats CLI" >&2
        }
    fi
else
    echo "nats CLI already installed. Skipping."
fi

# -----------------------------------------------------------------------
# nats_is_running -- Check if nats-server process is active.
# Respects _TEST_NATS_RUNNING env var for testability.
# -----------------------------------------------------------------------
nats_is_running() {
    if [ -n "${_TEST_NATS_RUNNING:-}" ]; then
        [ "$_TEST_NATS_RUNNING" = "true" ]
        return
    fi
    pgrep -x nats-server &>/dev/null
}

# -----------------------------------------------------------------------
# Start NATS with JetStream
# -----------------------------------------------------------------------
if nats_is_running; then
    echo "NATS server is already running. Skipping start."
else
    echo "Starting nats-server with JetStream enabled..."
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] nats-server -js --store_dir ${NATS_STORE_DIR} -p ${NATS_PORT} &"
    else
        nats-server -js --store_dir "${NATS_STORE_DIR}" -p "${NATS_PORT}" &
        disown
        sleep 1
    fi
    echo "nats-server started with -js flag."
fi

echo "NATS setup complete."
