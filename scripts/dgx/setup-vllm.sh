#!/usr/bin/env bash
# setup-vllm.sh -- Pull NVIDIA ARM64 vLLM image and start Qwen2.5-32B container
#
# Pulls the NVIDIA ARM64-optimized vLLM Docker image, starts a container
# serving Qwen2.5-32B-Instruct-AWQ on port 5000 with GPU passthrough.
#
# Usage:
#   ./scripts/dgx/setup-vllm.sh [--dry-run] [--help]
#
# Environment variables:
#   VLLM_IMAGE       -- Docker image (default: nvcr.io/nvidia/vllm:26.01-py3)
#   VLLM_MODEL       -- Model name (default: Qwen/Qwen2.5-32B-Instruct-AWQ)
#   VLLM_PORT        -- Port to expose (default: 5000)
#   VLLM_CONTAINER   -- Container name (default: qwen-vllm)
#   HF_TOKEN_FILE    -- Path to HF token (default: ~/.config/huggingface/token)

set -euo pipefail

# -----------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------
readonly VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.01-py3}"
readonly VLLM_MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-32B-Instruct-AWQ}"
readonly VLLM_PORT="${VLLM_PORT:-5000}"
readonly VLLM_CONTAINER="${VLLM_CONTAINER:-qwen-vllm}"
readonly HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.config/huggingface/token}"

DRY_RUN=false

# -----------------------------------------------------------------------
# Parse args
# -----------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --help|-h)
            echo "Usage: $(basename "$0") [--dry-run] [--help]"
            echo ""
            echo "Pull NVIDIA ARM64 vLLM Docker image and start Qwen2.5-32B on port $VLLM_PORT."
            echo ""
            echo "Options:"
            echo "  --dry-run  Print Docker commands without executing"
            echo "  --help     Show this message"
            echo ""
            echo "Environment variables:"
            echo "  VLLM_IMAGE       Docker image (default: $VLLM_IMAGE)"
            echo "  VLLM_MODEL       Model to serve (default: $VLLM_MODEL)"
            echo "  VLLM_PORT        Port to expose (default: $VLLM_PORT)"
            echo "  VLLM_CONTAINER   Container name (default: $VLLM_CONTAINER)"
            echo "  HF_TOKEN_FILE    HF token path (default: $HF_TOKEN_FILE)"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

# -----------------------------------------------------------------------
# Validate HF token
# -----------------------------------------------------------------------
if [[ ! -f "$HF_TOKEN_FILE" ]]; then
    echo "Error: HF token not found at $HF_TOKEN_FILE" >&2
    echo "Run setup-hf-token.sh first." >&2
    exit 1
fi

HF_TOKEN=$(cat "$HF_TOKEN_FILE")

# -----------------------------------------------------------------------
# Check if container already running
# -----------------------------------------------------------------------
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$VLLM_CONTAINER"; then
    echo "Container '$VLLM_CONTAINER' is already running."
    exit 0
fi

# -----------------------------------------------------------------------
# Pull image
# -----------------------------------------------------------------------
PULL_CMD="docker pull $VLLM_IMAGE"
if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] $PULL_CMD"
else
    echo "Pulling vLLM image: $VLLM_IMAGE"
    $PULL_CMD
fi

# -----------------------------------------------------------------------
# Remove stopped container if exists
# -----------------------------------------------------------------------
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$VLLM_CONTAINER"; then
    RM_CMD="docker rm -f $VLLM_CONTAINER"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "[DRY-RUN] $RM_CMD"
    else
        $RM_CMD
    fi
fi

# -----------------------------------------------------------------------
# Start container
# -----------------------------------------------------------------------
RUN_CMD="docker run -d \
    --name $VLLM_CONTAINER \
    --gpus all \
    --ipc=host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    -p ${VLLM_PORT}:8000 \
    -e HUGGING_FACE_HUB_TOKEN=$HF_TOKEN \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    $VLLM_IMAGE \
    python3 -m vllm.entrypoints.openai.api_server \
    --model $VLLM_MODEL \
    --host 0.0.0.0 \
    --port 8000 \
    --trust-remote-code \
    --quantization awq \
    --gpu-memory-utilization 0.35 \
    --max-model-len 8192"

if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] $RUN_CMD"
else
    echo "Starting vLLM container '$VLLM_CONTAINER' on port $VLLM_PORT..."
    eval "$RUN_CMD"
    echo "Container started. Waiting for model to load..."
    echo "Check status with: ./scripts/dgx/manage-vllm.sh status"
fi
