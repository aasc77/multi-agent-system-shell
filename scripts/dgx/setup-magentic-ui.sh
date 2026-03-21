#!/usr/bin/env bash
# setup-magentic-ui.sh -- Clone and install Magentic-UI with Fara extensions
#
# Clones the Magentic-UI repo, installs with Fara extensions, and configures
# to use the local vLLM endpoint. Idempotent: skips if already installed.
#
# Usage:
#   ./scripts/dgx/setup-magentic-ui.sh [--help]
#
# Environment variables:
#   MAGENTIC_UI_DIR   -- Install directory (default: ~/magentic-ui)
#   VLLM_ENDPOINT     -- vLLM endpoint (default: http://localhost:5000/v1)

set -euo pipefail

readonly MAGENTIC_UI_REPO="https://github.com/microsoft/magentic-ui.git"
readonly MAGENTIC_UI_DIR="${MAGENTIC_UI_DIR:-$HOME/magentic-ui}"
readonly VLLM_ENDPOINT="${VLLM_ENDPOINT:-http://localhost:5000/v1}"
readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly CONFIG_FILE="${SCRIPT_DIR}/magentic-ui-config.yaml"

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $(basename "$0") [--help]"
    echo ""
    echo "Clone and install Magentic-UI with Fara extensions."
    echo "Configures to use local vLLM at $VLLM_ENDPOINT."
    echo "Idempotent: skips clone/install if already present."
    echo ""
    echo "Environment variables:"
    echo "  MAGENTIC_UI_DIR   Install directory (default: $MAGENTIC_UI_DIR)"
    echo "  VLLM_ENDPOINT     vLLM endpoint (default: $VLLM_ENDPOINT)"
    exit 0
fi

# -----------------------------------------------------------------------
# Clone repo
# -----------------------------------------------------------------------
if [[ -d "$MAGENTIC_UI_DIR/.git" ]]; then
    echo "Magentic-UI already cloned at $MAGENTIC_UI_DIR. Pulling latest..."
    git -C "$MAGENTIC_UI_DIR" pull --ff-only 2>/dev/null || true
else
    echo "Cloning Magentic-UI..."
    git clone "$MAGENTIC_UI_REPO" "$MAGENTIC_UI_DIR"
fi

# -----------------------------------------------------------------------
# Install dependencies
# -----------------------------------------------------------------------
cd "$MAGENTIC_UI_DIR"

# Use a venv to avoid PEP 668 externally-managed-environment errors
VENV_DIR="${MAGENTIC_UI_DIR}/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if [[ -f "pyproject.toml" ]]; then
    echo "Installing Magentic-UI (pip install -e .)..."
    pip install -e ".[fara]" 2>/dev/null || pip install -e . 2>/dev/null || {
        echo "Warning: pip install with extras failed. Trying without..." >&2
        pip install -e .
    }
elif [[ -f "requirements.txt" ]]; then
    echo "Installing dependencies from requirements.txt..."
    pip install -r requirements.txt
elif [[ -f "package.json" ]]; then
    echo "Installing Node.js dependencies..."
    npm install
fi

# -----------------------------------------------------------------------
# Copy config
# -----------------------------------------------------------------------
if [[ -f "$CONFIG_FILE" ]]; then
    cp "$CONFIG_FILE" "$MAGENTIC_UI_DIR/config.yaml"
    echo "Config copied to $MAGENTIC_UI_DIR/config.yaml"
else
    echo "Warning: Config file not found at $CONFIG_FILE" >&2
    echo "Creating minimal config..."
    cat > "$MAGENTIC_UI_DIR/config.yaml" <<EOF
provider:
  endpoint: "$VLLM_ENDPOINT"
  model: "microsoft/Fara-7B"
EOF
fi

echo "Magentic-UI installed at $MAGENTIC_UI_DIR"
