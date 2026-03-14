#!/usr/bin/env bash
# smoke-test.sh -- End-to-end verification of Qwen2.5-32B + Magentic-UI stack
#
# Checks:
#   1. vLLM /v1/models returns 200
#   2. vLLM responds to a simple completion request
#   3. Magentic-UI process is running
#
# Usage:
#   ./scripts/dgx/smoke-test.sh [--help]
#
# Exit code 0 on all pass, non-zero on any failure.

set -euo pipefail

VLLM_PORT="${VLLM_PORT:-5000}"
VLLM_URL="http://localhost:${VLLM_PORT}"
PASS=0
FAIL=0

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $(basename "$0") [--help]"
    echo ""
    echo "End-to-end verification of the Qwen2.5-32B + Magentic-UI stack."
    echo ""
    echo "Checks:"
    echo "  1. vLLM healthcheck (/v1/models)"
    echo "  2. vLLM inference test (simple prompt)"
    echo "  3. Magentic-UI process running"
    exit 0
fi

check() {
    local name="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Smoke Test ==="
echo ""

# -----------------------------------------------------------------------
# Check 1: vLLM healthcheck
# -----------------------------------------------------------------------
check "vLLM healthcheck (/v1/models)" curl -sf "${VLLM_URL}/v1/models"

# -----------------------------------------------------------------------
# Check 2: vLLM inference
# -----------------------------------------------------------------------
_test_inference() {
    local response
    response=$(curl -sf "${VLLM_URL}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{
            "model": "Qwen/Qwen2.5-32B-Instruct-AWQ",
            "messages": [{"role": "user", "content": "Say hello"}],
            "max_tokens": 16
        }')
    echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['choices'][0]['message']['content']"
}
check "vLLM inference (simple prompt)" _test_inference

# -----------------------------------------------------------------------
# Check 3: Magentic-UI running
# -----------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
check "Magentic-UI running" bash "$SCRIPT_DIR/manage-magentic-ui.sh" status

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
TOTAL=$((PASS + FAIL))
echo "Results: ${PASS}/${TOTAL} passed"

if [[ $FAIL -gt 0 ]]; then
    echo "SMOKE TEST FAILED"
    exit 1
fi

echo "SMOKE TEST PASSED"
exit 0
