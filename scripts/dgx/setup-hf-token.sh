#!/usr/bin/env bash
# setup-hf-token.sh -- Securely store Hugging Face API token
#
# Writes token to ~/.config/huggingface/token with 600 permissions.
# Never echoes the token to stdout. Idempotent: skips if token exists.
#
# Usage:
#   ./scripts/dgx/setup-hf-token.sh [--help]
#
# Environment variables for testing:
#   _TEST_DRY_RUN -- "true" to print commands instead of executing

set -euo pipefail

readonly TOKEN_DIR="$HOME/.config/huggingface"
readonly TOKEN_FILE="$TOKEN_DIR/token"

DRY_RUN="${_TEST_DRY_RUN:-false}"

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $(basename "$0") [--help]"
    echo ""
    echo "Securely prompt for and store a Hugging Face API token."
    echo "Token is written to $TOKEN_FILE with 600 permissions."
    echo "Skips if token already exists."
    exit 0
fi

# -----------------------------------------------------------------------
# Check existing token
# -----------------------------------------------------------------------
if [[ -f "$TOKEN_FILE" ]]; then
    echo "HF token already exists at $TOKEN_FILE. Skipping."
    exit 0
fi

# -----------------------------------------------------------------------
# Prompt for token (no echo)
# -----------------------------------------------------------------------
echo "Enter your Hugging Face API token (input will be hidden):"
read -rs HF_TOKEN

if [[ -z "$HF_TOKEN" ]]; then
    echo "Error: Token cannot be empty." >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Write token securely
# -----------------------------------------------------------------------
if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] mkdir -p $TOKEN_DIR"
    echo "[DRY-RUN] write token to $TOKEN_FILE"
    echo "[DRY-RUN] chmod 600 $TOKEN_FILE"
else
    mkdir -p "$TOKEN_DIR"
    printf '%s' "$HF_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi

echo "HF token stored at $TOKEN_FILE (perms: 600)."
