#!/usr/bin/env bash
# ops-status.sh -- Architect operations status check
#
# Checks health of all MAS nodes and services from the orchestrator machine.
# Uses NATS for messaging health, direct HTTP for services, SSH for remote checks.
#
# Usage:
#   ./scripts/ops-status.sh [--help]
#   ./scripts/ops-status.sh [node]       # check specific node: dgx, hub, macmini, nats, all
#
# Examples:
#   ./scripts/ops-status.sh              # check everything
#   ./scripts/ops-status.sh dgx          # just DGX services
#   ./scripts/ops-status.sh nats         # just NATS health

set -euo pipefail

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------
DGX_HOST="dgx@192.168.1.51"
MACMINI_HOST="angelserrano@192.168.1.31"
NATS_URL="nats://192.168.1.25:4222"
VLLM_URL="http://192.168.1.51:5000"
SSH_TIMEOUT=5

PASS=0
FAIL=0
WARN=0

# -----------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $(basename "$0") [node]"
    echo ""
    echo "Check health of MAS nodes and services."
    echo ""
    echo "Nodes:"
    echo "  all       Check everything (default)"
    echo "  nats      NATS server + JetStream"
    echo "  dgx       DGX services (vLLM, Magentic-UI, Docker, disk)"
    echo "  hub       Local hub agent"
    echo "  macmini   Mac Mini agent"
    echo "  tmux      tmux session status"
    exit 0
fi

TARGET="${1:-all}"

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
_check() {
    local name="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "  PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name"
        FAIL=$((FAIL + 1))
    fi
}

_warn() {
    local name="$1" msg="$2"
    echo "  WARN  $name -- $msg"
    WARN=$((WARN + 1))
}

_info() {
    local name="$1" val="$2"
    echo "  INFO  $name: $val"
}

_ssh_check() {
    local host="$1" name="$2"
    shift 2
    local cmd="$*"
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=$SSH_TIMEOUT -o IdentitiesOnly=yes "$host" "$cmd" > /dev/null 2>&1; then
        echo "  PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name"
        FAIL=$((FAIL + 1))
    fi
}

_ssh_info() {
    local host="$1" name="$2"
    shift 2
    local cmd="$*"
    local val
    val=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=$SSH_TIMEOUT -o IdentitiesOnly=yes "$host" "$cmd" 2>/dev/null) || val="(unreachable)"
    echo "  INFO  $name: $val"
}

# -----------------------------------------------------------------------
# NATS
# -----------------------------------------------------------------------
check_nats() {
    echo ""
    echo "=== NATS ==="
    _check "nats-server running" pgrep -x nats-server
    _check "nats CLI available" command -v nats

    if command -v nats &>/dev/null; then
        local streams
        streams=$(nats stream ls --json 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
        _info "JetStream streams" "$streams"

        local subjects
        subjects=$(nats sub "agents.>" --count 1 --timeout 1s 2>/dev/null && echo "active" || echo "no recent messages")
        _info "agent subjects" "$subjects"
    fi
}

# -----------------------------------------------------------------------
# DGX
# -----------------------------------------------------------------------
check_dgx() {
    echo ""
    echo "=== DGX ($DGX_HOST) ==="

    # SSH connectivity
    _check "SSH reachable" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=$SSH_TIMEOUT -o IdentitiesOnly=yes "$DGX_HOST" "echo ok"

    # GPU
    _ssh_info "$DGX_HOST" "GPU" "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo 'nvidia-smi failed'"

    # Disk
    _ssh_info "$DGX_HOST" "Disk free" "df -h / | tail -1 | awk '{print \$4}'"

    # Docker
    _ssh_check "$DGX_HOST" "Docker running" "docker info > /dev/null 2>&1"

    # vLLM container
    local vllm_status
    vllm_status=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=$SSH_TIMEOUT -o IdentitiesOnly=yes "$DGX_HOST" \
        "docker ps --filter name=fara-vllm --format '{{.Status}}' 2>/dev/null" 2>/dev/null || echo "")
    if [[ -n "$vllm_status" ]]; then
        echo "  PASS  vLLM container running ($vllm_status)"
        PASS=$((PASS + 1))
    else
        local stopped
        stopped=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=$SSH_TIMEOUT -o IdentitiesOnly=yes "$DGX_HOST" \
            "docker ps -a --filter name=fara-vllm --format '{{.Status}}' 2>/dev/null" 2>/dev/null || echo "")
        if [[ -n "$stopped" ]]; then
            echo "  FAIL  vLLM container stopped ($stopped)"
        else
            echo "  FAIL  vLLM container not found"
        fi
        FAIL=$((FAIL + 1))
    fi

    # vLLM healthcheck (direct HTTP over LAN)
    if curl -sf "${VLLM_URL}/v1/models" > /dev/null 2>&1; then
        echo "  PASS  vLLM healthcheck (${VLLM_URL}/v1/models)"
        PASS=$((PASS + 1))

        # Model info
        local model
        model=$(curl -sf "${VLLM_URL}/v1/models" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data'][0]['id'])" 2>/dev/null || echo "?")
        _info "vLLM model" "$model"
    else
        echo "  FAIL  vLLM healthcheck (${VLLM_URL}/v1/models)"
        FAIL=$((FAIL + 1))
    fi

    # Magentic-UI
    _ssh_check "$DGX_HOST" "Magentic-UI installed" "test -d ~/magentic-ui"
    _ssh_check "$DGX_HOST" "Magentic-UI running" 'kill -0 $(cat /tmp/magentic-ui.pid 2>/dev/null) 2>/dev/null || lsof -ti :8888 >/dev/null 2>&1'

    # HF token
    _ssh_check "$DGX_HOST" "HF token present" "test -f ~/.config/huggingface/token"

    # MAS workspace
    _ssh_check "$DGX_HOST" "mas-workspace exists" "test -d ~/mas-workspace"
    _ssh_check "$DGX_HOST" "mas-bridge installed" "test -f ~/mas-bridge/index.js"
}

# -----------------------------------------------------------------------
# Hub (local)
# -----------------------------------------------------------------------
check_hub() {
    echo ""
    echo "=== Hub (local) ==="
    _check "Claude Code available" command -v claude
    _info "Node.js" "$(node --version 2>/dev/null || echo 'not found')"
    _info "Python" "$(python3 --version 2>/dev/null || echo 'not found')"
}

# -----------------------------------------------------------------------
# Mac Mini
# -----------------------------------------------------------------------
check_macmini() {
    echo ""
    echo "=== Mac Mini ($MACMINI_HOST) ==="
    _check "SSH reachable" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=$SSH_TIMEOUT -o IdentitiesOnly=yes "$MACMINI_HOST" "echo ok"
    _ssh_check "$MACMINI_HOST" "mas-workspace exists" "test -d ~/mas-workspace"
    _ssh_check "$MACMINI_HOST" "mas-bridge installed" "test -f ~/mas-bridge/index.js"
    _ssh_info "$MACMINI_HOST" "Node.js" "node --version 2>/dev/null || echo 'not found'"
}

# -----------------------------------------------------------------------
# tmux
# -----------------------------------------------------------------------
check_tmux() {
    echo ""
    echo "=== tmux Sessions ==="
    local sessions
    sessions=$(tmux list-sessions 2>/dev/null || echo "(none)")
    echo "$sessions" | while IFS= read -r line; do
        _info "session" "$line"
    done
}

# -----------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------
echo "=== MAS Ops Status ==="
echo "  $(date '+%Y-%m-%d %H:%M:%S')"

case "$TARGET" in
    all)
        check_nats
        check_dgx
        check_hub
        check_macmini
        check_tmux
        ;;
    nats)    check_nats ;;
    dgx)     check_dgx ;;
    hub)     check_hub ;;
    macmini) check_macmini ;;
    tmux)    check_tmux ;;
    *)
        echo "Unknown target: $TARGET" >&2
        echo "Options: all, nats, dgx, hub, macmini, tmux" >&2
        exit 1
        ;;
esac

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
TOTAL=$((PASS + FAIL))
echo "=== Summary: ${PASS}/${TOTAL} passed, ${WARN} warnings ==="

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
