#!/usr/bin/env bash
# dgx-dashboard.sh -- Real-time DGX resource dashboard
#
# Usage:
#   ./scripts/dgx-dashboard.sh [--interval N]
#
# Refreshes every N seconds (default: 5). Press Ctrl-C to exit.

set -uo pipefail

DGX_HOST="${DGX_HOST:-dgx@192.168.1.51}"
INTERVAL="${1:-5}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=3 -o IdentitiesOnly=yes"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $(basename "$0") [interval_seconds]"
    echo "  Default interval: 5 seconds"
    echo "  DGX_HOST env var: $DGX_HOST"
    exit 0
fi

# Parse --interval flag
if [[ "${1:-}" == "--interval" ]]; then
    INTERVAL="${2:-5}"
fi

_ssh() {
    ssh $SSH_OPTS "$DGX_HOST" "$@" 2>/dev/null
}

while true; do
    NOW=$(date '+%Y-%m-%d %H:%M:%S')

    # Gather all data in one SSH call to minimize latency
    DATA=$(_ssh 'bash -s' << 'REMOTE_EOF'
echo "===GPU==="
nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,power.limit --format=csv,noheader,nounits 2>/dev/null || echo "UNAVAILABLE"

echo "===CPU==="
top -bn1 | head -5 | tail -3 2>/dev/null

echo "===MEM==="
free -h 2>/dev/null | grep -E 'Mem|Swap'

echo "===DISK==="
df -h / /home 2>/dev/null | tail -2

echo "===DOCKER==="
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo "UNAVAILABLE"

echo "===VLLM==="
vllm_code=$(curl -sf -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/v1/models 2>/dev/null) || vllm_code="DOWN"
echo "$vllm_code"

echo "===MAGENTIC==="
mui_code=$(curl -sf -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/ 2>/dev/null) || mui_code="DOWN"
echo "$mui_code"

echo "===TOKENS==="
curl -sf -m 3 http://127.0.0.1:5000/metrics 2>/dev/null | awk -F'[{ }]' '
/^vllm:prompt_tokens_total\{/       { printf "prompt_tokens %s\n", $NF }
/^vllm:generation_tokens_total\{/   { printf "gen_tokens %s\n", $NF }
/^vllm:request_success_total.*finished_reason="stop"/  { printf "req_stop %s\n", $NF }
/^vllm:request_success_total.*finished_reason="length"/ { printf "req_length %s\n", $NF }
/^vllm:request_success_total.*finished_reason="error"/  { printf "req_error %s\n", $NF }
/^vllm:num_requests_running\{/      { printf "req_running %s\n", $NF }
/^vllm:num_requests_waiting\{/      { printf "req_waiting %s\n", $NF }
/^vllm:kv_cache_usage_perc\{/       { printf "kv_cache %s\n", $NF }
/^vllm:prefix_cache_hits_total\{/   { printf "cache_hits %s\n", $NF }
/^vllm:prefix_cache_queries_total\{/ { printf "cache_queries %s\n", $NF }
' || echo "UNAVAILABLE"

echo "===NETWORK==="
ss -tuln 2>/dev/null | grep -E ':(5000|8888|4222|6080) ' | awk '{printf "  %-6s %s\n", $1, $5}'

echo "===UPTIME==="
uptime 2>/dev/null
REMOTE_EOF
    ) || { echo "  SSH connection failed to $DGX_HOST"; sleep "$INTERVAL"; continue; }

    # Parse sections (awk for macOS/BSD compatibility)
    _section() {
        echo "$DATA" | awk "/^===$1===$/{found=1;next} /^===/{found=0} found"
    }

    GPU=$(_section GPU)
    CPU=$(_section CPU)
    MEM=$(_section MEM)
    DISK=$(_section DISK)
    DOCKER=$(_section DOCKER)
    VLLM=$(_section VLLM)
    MAGENTIC=$(_section MAGENTIC)
    NETWORK=$(_section NETWORK)
    UPTIME=$(_section UPTIME)

    # ─── Render to buffer, then output all at once (no flicker) ───
    FRAME=$(
    printf "\033[1;36m╔══════════════════════════════════════════════════════════════╗\033[0m\n"
    printf "\033[1;36m║           DGX SPARK REAL-TIME DASHBOARD                     ║\033[0m\n"
    printf "\033[1;36m║           %s   [refresh: %ss]             ║\033[0m\n" "$NOW" "$INTERVAL"
    printf "\033[1;36m╚══════════════════════════════════════════════════════════════╝\033[0m\n"

    # GPU
    printf "\n\033[1;33m── GPU ──────────────────────────────────────────────────────\033[0m\n"
    if [[ "$GPU" == "UNAVAILABLE" ]]; then
        printf "  \033[31mGPU data unavailable\033[0m\n"
    else
        IFS=',' read -r gpu_name gpu_temp gpu_util mem_util mem_used mem_total power_draw power_limit <<< "$GPU"
        gpu_name=$(echo "$gpu_name" | xargs)
        gpu_temp=$(echo "$gpu_temp" | xargs)
        gpu_util=$(echo "$gpu_util" | xargs)
        mem_util=$(echo "$mem_util" | xargs)
        mem_used=$(echo "$mem_used" | xargs)
        mem_total=$(echo "$mem_total" | xargs)
        power_draw=$(echo "$power_draw" | xargs)
        power_limit=$(echo "$power_limit" | xargs)

        printf "  %-14s \033[1;37m%s\033[0m\n" "Model:" "$gpu_name"
        printf "  %-14s \033[1;37m%s°C\033[0m\n" "Temperature:" "$gpu_temp"

        # GPU utilization bar
        bar_len=30
        gpu_util_int=${gpu_util%%.*}
        filled=$(( gpu_util_int * bar_len / 100 ))
        empty=$(( bar_len - filled ))
        if (( gpu_util_int > 80 )); then color="31"; elif (( gpu_util_int > 50 )); then color="33"; else color="32"; fi
        printf "  %-14s \033[${color}m" "GPU Util:"
        printf '█%.0s' $(seq 1 $filled 2>/dev/null) || true
        printf '░%.0s' $(seq 1 $empty 2>/dev/null) || true
        printf " %s%%\033[0m\n" "$gpu_util"

        # Memory utilization bar
        mem_util_int=${mem_util%%.*}
        filled=$(( mem_util_int * bar_len / 100 ))
        empty=$(( bar_len - filled ))
        if (( mem_util_int > 80 )); then color="31"; elif (( mem_util_int > 50 )); then color="33"; else color="32"; fi
        printf "  %-14s \033[${color}m" "VRAM:"
        printf '█%.0s' $(seq 1 $filled 2>/dev/null) || true
        printf '░%.0s' $(seq 1 $empty 2>/dev/null) || true
        printf " %s%% (%s / %s MiB)\033[0m\n" "$mem_util" "$mem_used" "$mem_total"

        printf "  %-14s \033[1;37m%sW / %sW\033[0m\n" "Power:" "$power_draw" "$power_limit"
    fi

    # CPU & Memory
    printf "\n\033[1;33m── CPU & MEMORY ─────────────────────────────────────────────\033[0m\n"
    echo "$CPU" | while IFS= read -r line; do
        printf "  %s\n" "$line"
    done
    echo "$MEM" | while IFS= read -r line; do
        printf "  %s\n" "$line"
    done

    # Disk
    printf "\n\033[1;33m── DISK ─────────────────────────────────────────────────────\033[0m\n"
    echo "$DISK" | while IFS= read -r line; do
        printf "  %s\n" "$line"
    done

    # Services
    printf "\n\033[1;33m── SERVICES ─────────────────────────────────────────────────\033[0m\n"
    VLLM=$(echo "$VLLM" | tr -d '[:space:]')
    MAGENTIC=$(echo "$MAGENTIC" | tr -d '[:space:]')
    if [[ "$VLLM" == "200" ]]; then
        printf "  vLLM (Fara-7B)     \033[1;32m● UP\033[0m  :5000\n"
    else
        printf "  vLLM (Fara-7B)     \033[1;31m● DOWN\033[0m  :5000  (status: %s)\n" "$VLLM"
    fi
    if [[ "$MAGENTIC" == "200" ]]; then
        printf "  Magentic-UI        \033[1;32m● UP\033[0m  :8888\n"
    else
        printf "  Magentic-UI        \033[1;31m● DOWN\033[0m  :8888  (status: %s)\n" "$MAGENTIC"
    fi

    # Token Usage
    TOKENS=$(_section TOKENS)
    printf "\n\033[1;33m── vLLM TOKEN USAGE ─────────────────────────────────────────\033[0m\n"
    if [[ "$TOKENS" == "UNAVAILABLE" || -z "$TOKENS" ]]; then
        printf "  \033[31mMetrics unavailable\033[0m\n"
    else
        _tok_val() { echo "$TOKENS" | awk -v k="$1" '$1==k {printf "%.0f", $2}'; }
        prompt_tok=$(_tok_val prompt_tokens)
        gen_tok=$(_tok_val gen_tokens)
        total_tok=$(( ${prompt_tok:-0} + ${gen_tok:-0} ))
        req_stop=$(_tok_val req_stop)
        req_length=$(_tok_val req_length)
        req_error=$(_tok_val req_error)
        total_req=$(( ${req_stop:-0} + ${req_length:-0} + ${req_error:-0} ))
        req_running=$(_tok_val req_running)
        req_waiting=$(_tok_val req_waiting)
        kv_cache=$(_tok_val kv_cache)
        cache_hits=$(_tok_val cache_hits)
        cache_queries=$(_tok_val cache_queries)

        # Format large numbers with commas
        _fmt() { printf "%'d" "$1" 2>/dev/null || echo "$1"; }

        printf "  %-20s \033[1;37m%s\033[0m\n" "Prompt tokens:" "$(_fmt "${prompt_tok:-0}")"
        printf "  %-20s \033[1;37m%s\033[0m\n" "Generated tokens:" "$(_fmt "${gen_tok:-0}")"
        printf "  %-20s \033[1;37m%s\033[0m\n" "Total tokens:" "$(_fmt "$total_tok")"
        printf "  %-20s \033[1;37m%s\033[0m  (stop: %s, length: %s, error: %s)\n" \
            "Requests:" "$total_req" "${req_stop:-0}" "${req_length:-0}" "${req_error:-0}"
        printf "  %-20s \033[1;33m%s running, %s waiting\033[0m\n" "Active:" "${req_running:-0}" "${req_waiting:-0}"

        # KV cache bar
        kv_pct=$(echo "${kv_cache:-0}" | awk '{printf "%.0f", $1 * 100}')
        bar_len=30
        filled=$(( kv_pct * bar_len / 100 ))
        empty=$(( bar_len - filled ))
        if (( kv_pct > 80 )); then color="31"; elif (( kv_pct > 50 )); then color="33"; else color="32"; fi
        printf "  %-20s \033[${color}m" "KV Cache:"
        printf '█%.0s' $(seq 1 $filled 2>/dev/null) || true
        printf '░%.0s' $(seq 1 $empty 2>/dev/null) || true
        printf " %s%%\033[0m\n" "$kv_pct"

        # Prefix cache hit rate
        if [[ "${cache_queries:-0}" -gt 0 ]]; then
            hit_rate=$(echo "$cache_hits $cache_queries" | awk '{printf "%.1f", ($1/$2)*100}')
            printf "  %-20s \033[1;37m%s%%\033[0m  (%s / %s)\n" "Prefix cache hit:" "$hit_rate" "$(_fmt "${cache_hits:-0}")" "$(_fmt "${cache_queries:-0}")"
        fi
    fi

    # Docker
    printf "\n\033[1;33m── DOCKER CONTAINERS ────────────────────────────────────────\033[0m\n"
    if [[ "$DOCKER" == "UNAVAILABLE" ]]; then
        printf "  \033[31mDocker unavailable\033[0m\n"
    else
        echo "$DOCKER" | while IFS=$'\t' read -r name status ports; do
            # Truncate long names
            short_name="${name:0:35}"
            short_status="${status:0:20}"
            printf "  %-37s \033[32m%s\033[0m\n" "$short_name" "$short_status"
        done
    fi

    # Listening ports
    printf "\n\033[1;33m── NETWORK (key ports) ──────────────────────────────────────\033[0m\n"
    if [[ -n "$NETWORK" ]]; then
        echo "$NETWORK" | while IFS= read -r line; do
            printf "  %s\n" "$line"
        done
    else
        printf "  (no key ports detected)\n"
    fi

    # Uptime
    printf "\n\033[1;33m── UPTIME ───────────────────────────────────────────────────\033[0m\n"
    printf "  %s\n" "$UPTIME"

    printf "\n\033[2mPress Ctrl-C to exit\033[0m\n"
    )

    # Single atomic write: move to top, print frame, clear leftover
    printf '\033[H%s\033[J' "$FRAME"

    sleep "$INTERVAL"
done
