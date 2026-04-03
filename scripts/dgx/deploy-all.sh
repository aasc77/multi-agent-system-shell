#!/usr/bin/env bash
# deploy-all.sh -- One-shot deployment: vLLM + Magentic-UI on DGX
#
# Runs setup steps in order and verifies with smoke test.
#
# Usage:
#   ./scripts/dgx/deploy-all.sh [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $(basename "$0") [--help]"
    echo ""
    echo "One-shot deployment of Qwen2.5-32B (vLLM) + Magentic-UI."
    echo ""
    echo "Steps:"
    echo "  1. Setup HF token (skip if exists)"
    echo "  2. Setup vLLM container"
    echo "  3. Setup Magentic-UI"
    echo "  4. Wait for vLLM healthcheck"
    echo "  5. Start Magentic-UI"
    echo "  6. Run smoke test"
    exit 0
fi

echo "=== Qwen2.5-32B + Magentic-UI Deployment ==="
echo ""

# -----------------------------------------------------------------------
# Step 1: HF Token
# -----------------------------------------------------------------------
echo "--- Step 1/6: HF Token ---"
bash "$SCRIPT_DIR/setup-hf-token.sh"
echo ""

# -----------------------------------------------------------------------
# Step 2: vLLM Container
# -----------------------------------------------------------------------
echo "--- Step 2/6: vLLM Container ---"
bash "$SCRIPT_DIR/setup-vllm.sh"
echo ""

# -----------------------------------------------------------------------
# Step 3: Magentic-UI
# -----------------------------------------------------------------------
echo "--- Step 3/6: Magentic-UI ---"
bash "$SCRIPT_DIR/setup-magentic-ui.sh"
echo ""

# -----------------------------------------------------------------------
# Step 4: Wait for vLLM healthcheck
# -----------------------------------------------------------------------
echo "--- Step 4/6: Waiting for vLLM healthcheck ---"
VLLM_PORT="${VLLM_PORT:-5000}"
HEALTHCHECK_URL="http://localhost:${VLLM_PORT}/v1/models"
MAX_WAIT=300
WAITED=0

while ! curl -sf "$HEALTHCHECK_URL" > /dev/null 2>&1; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "Error: vLLM did not become healthy after ${MAX_WAIT}s" >&2
        echo "Check logs: docker logs qwen-vllm" >&2
        exit 1
    fi
    echo "  Waiting for vLLM... (${WAITED}s / ${MAX_WAIT}s)"
    sleep 10
    WAITED=$((WAITED + 10))
done
echo "vLLM is healthy."
echo ""

# -----------------------------------------------------------------------
# Step 5: Start Magentic-UI
# -----------------------------------------------------------------------
echo "--- Step 5/6: Start Magentic-UI ---"
bash "$SCRIPT_DIR/manage-magentic-ui.sh" start
echo ""

# -----------------------------------------------------------------------
# Step 6: Smoke test
# -----------------------------------------------------------------------
echo "--- Step 6/6: Smoke Test ---"
bash "$SCRIPT_DIR/smoke-test.sh"
