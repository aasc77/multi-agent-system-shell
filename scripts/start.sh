#!/usr/bin/env bash
# start.sh -- Main launch script for Multi-Agent System Shell
#
# Requirements traced to PRD:
#   R1: tmux Session Layout (control + agents windows, pane-border-status)
#   R2: Config-Driven Agents (MCP config generation, runtimes)
#   R8: Scripts (start.sh preflight, idempotent, NATS auto-start, exit codes)
#
# Usage:
#   ./scripts/start.sh <project>
#
# Environment variables for testing:
#   _TEST_MISSING_TOOLS  -- comma-separated list of tools to pretend are missing
#   _TEST_NATS_RUNNING   -- "true" or "false" to override NATS running check
#   _TEST_SESSION_EXISTS -- "true" to pretend tmux session already exists
#   _TEST_DRY_RUN        -- "true" to print commands instead of executing them

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(pwd)"

readonly RUNTIME_CLAUDE_CODE="claude_code"
readonly RUNTIME_SCRIPT="script"
readonly CONTROL_WINDOW="control"
readonly AGENTS_WINDOW="agents"
readonly MCP_CONFIGS_DIR_NAME=".mcp-configs"
readonly MCP_SERVER_NAME="mas-bridge"
readonly MCP_BRIDGE_REL_PATH="mcp-bridge/index.js"
readonly DEFAULT_NATS_URL="nats://localhost:4222"
readonly DEFAULT_SESSION_NAME="mas"

# -----------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------
validate_arguments() {
    # Validate that a project name was provided.
    if [ $# -lt 1 ]; then
        echo "Usage: $0 <project>" >&2
        echo "  project: Name of the project directory under projects/" >&2
        exit 1
    fi
}

validate_arguments "$@"
PROJECT="$1"

# -----------------------------------------------------------------------
# Preflight checks -- verify required tools are installed
# -----------------------------------------------------------------------
REQUIRED_TOOLS=("tmux" "python3" "nats-server")

is_tool_missing() {
    # Check if a single tool is considered missing.
    # Respects _TEST_MISSING_TOOLS env var for testability.
    local tool="$1"

    if [ -n "${_TEST_MISSING_TOOLS:-}" ]; then
        IFS=',' read -ra test_missing <<< "$_TEST_MISSING_TOOLS"
        for tm in "${test_missing[@]}"; do
            if [ "$tm" = "$tool" ]; then
                return 0  # missing
            fi
        done
        return 1  # not missing
    fi

    ! command -v "$tool" &>/dev/null
}

run_preflight_checks() {
    # Verify all required tools are installed; exit 1 with a clear
    # listing of every missing tool if any are absent.
    local missing=()

    for tool in "${REQUIRED_TOOLS[@]}"; do
        if is_tool_missing "$tool"; then
            missing+=("$tool")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        echo "Error: Required tools are missing. Please install them:" >&2
        for tool in "${missing[@]}"; do
            echo "  - $tool (required, not found)" >&2
        done
        exit 1
    fi
}

run_preflight_checks

# -----------------------------------------------------------------------
# Project config validation
# -----------------------------------------------------------------------
PROJECT_DIR="${ROOT_DIR}/projects/${PROJECT}"
PROJECT_CONFIG="${PROJECT_DIR}/config.yaml"
GLOBAL_CONFIG="${ROOT_DIR}/config.yaml"

if [ ! -f "$PROJECT_CONFIG" ]; then
    echo "Error: Project '${PROJECT}' not found. No such file: ${PROJECT_CONFIG}" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Parse configs using Python (YAML parsing with deep merge)
# -----------------------------------------------------------------------
parse_configs() {
    # Parse and merge global + project YAML configs into JSON.
    # Project values override global; merges two levels deep for dicts.
    python3 -c "
import json, sys, os
try:
    import yaml
except ImportError:
    sys.exit(0)

project_config_path = os.environ['_PROJECT_CONFIG']
global_config_path = os.environ['_GLOBAL_CONFIG']

global_cfg = {}
if os.path.isfile(global_config_path):
    with open(global_config_path) as f:
        global_cfg = yaml.safe_load(f) or {}

with open(project_config_path) as f:
    project_cfg = yaml.safe_load(f) or {}

# Deep merge: project overrides global, two levels
merged = dict(global_cfg)
for key, val in project_cfg.items():
    if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
        merged[key] = {**merged[key], **val}
    else:
        merged[key] = val

print(json.dumps(merged))
"
}

CONFIG_JSON=$(
    _PROJECT_CONFIG="$PROJECT_CONFIG" \
    _GLOBAL_CONFIG="$GLOBAL_CONFIG" \
    parse_configs
)

if [ -z "$CONFIG_JSON" ]; then
    echo "Error: Failed to parse config files" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Extract config values via a single consolidated Python call
# -----------------------------------------------------------------------
read_config_values() {
    # Extract session_name, nats_url, and agents JSON from merged config.
    # Outputs three lines: SESSION_NAME, NATS_URL, AGENTS_JSON.
    python3 -c "
import json, sys

config = json.loads(sys.stdin.read())

session_name = config.get('tmux', {}).get('session_name', config.get('project', '$DEFAULT_SESSION_NAME'))
nats_url = config.get('nats', {}).get('url', '$DEFAULT_NATS_URL')
agents = json.dumps(config.get('agents', {}))

print(session_name)
print(nats_url)
print(agents)
" <<< "$CONFIG_JSON"
}

CONFIG_VALUES=$(read_config_values)
SESSION_NAME=$(echo "$CONFIG_VALUES" | sed -n '1p')
NATS_URL=$(echo "$CONFIG_VALUES" | sed -n '2p')
AGENTS_JSON=$(echo "$CONFIG_VALUES" | sed -n '3p')

# -----------------------------------------------------------------------
# MCP config generation (for claude_code agents only)
# -----------------------------------------------------------------------
MCP_DIR="${PROJECT_DIR}/${MCP_CONFIGS_DIR_NAME}"
mkdir -p "$MCP_DIR"

generate_mcp_configs() {
    # Generate per-agent MCP config files (.json) for each claude_code agent.
    # Each config wires the MCP bridge with the agent's role, NATS URL,
    # and workspace directory.
    # For remote agents (ssh_host set), uses remote_bridge_path and
    # remote_working_dir from config; copies the MCP config to the remote.
    python3 -c "
import json, os, sys

agents = json.loads(os.environ['_AGENTS_JSON'])
mcp_dir = os.environ['_MCP_DIR']
project_dir = os.environ['_PROJECT_DIR']
nats_url = os.environ['_NATS_URL']
root_dir = os.environ['_ROOT_DIR']
bridge_rel_path = os.environ['_MCP_BRIDGE_REL_PATH']
server_name = os.environ['_MCP_SERVER_NAME']
runtime_claude = os.environ['_RUNTIME_CLAUDE_CODE']

local_bridge_path = os.path.join(root_dir, bridge_rel_path)
_home_cache = {}

# For remote agents, replace localhost/127.0.0.1 in NATS URL with this machine's hostname
# so it resolves correctly regardless of wired/Wi-Fi IP changes (mDNS)
import socket
_hostname = socket.gethostname()
if not _hostname.endswith('.local'):
    _hostname += '.local'
import re
remote_nats_url = re.sub(r'(nats://)(?:localhost|127\.0\.0\.1)', rf'\1{_hostname}', nats_url)

for agent_name, agent_cfg in agents.items():
    if agent_cfg.get('runtime', '') != runtime_claude:
        continue

    ssh_host = agent_cfg.get('ssh_host', '')
    is_remote = bool(ssh_host)

    if is_remote:
        # Resolve ~ to absolute path (node doesn't expand ~ in args)
        # Query remote home dir via SSH (cached per host to avoid repeated calls)
        ssh_host = agent_cfg.get('ssh_host', '')
        if ssh_host not in _home_cache:
            import subprocess
            try:
                result = subprocess.run(
                    ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'IdentitiesOnly=yes',
                     '-o', 'ConnectTimeout=5', ssh_host, 'echo ~'],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0 or not result.stdout.strip():
                    print(f'WARNING: Could not reach {ssh_host} for agent \"{agent_name}\" (connection timed out or refused). Using fallback home path.', file=sys.stderr)
                    user_part = ssh_host.split('@')[0] if '@' in ssh_host else ssh_host
                    _home_cache[ssh_host] = '/home/' + user_part
                else:
                    _home_cache[ssh_host] = result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError) as e:
                print(f'WARNING: Could not reach {ssh_host} for agent \"{agent_name}\" ({e}). Using fallback home path.', file=sys.stderr)
                user_part = ssh_host.split('@')[0] if '@' in ssh_host else ssh_host
                _home_cache[ssh_host] = '/home/' + user_part
        home_dir = _home_cache[ssh_host]
        bridge_path = agent_cfg.get('remote_bridge_path', '~/mas-bridge/index.js').replace('~', home_dir)
        working_dir = agent_cfg.get('remote_working_dir', '~/mas-workspace').replace('~', home_dir)
        node_cmd = agent_cfg.get('remote_node_path', 'node').replace('~', home_dir)
    else:
        bridge_path = local_bridge_path
        working_dir = agent_cfg.get('working_dir', project_dir)

    cmd = node_cmd if is_remote else 'node'
    agent_nats_url = remote_nats_url if is_remote else nats_url
    mcp_config = {
        'mcpServers': {
            server_name: {
                'command': cmd,
                'args': [bridge_path],
                'env': {
                    'AGENT_ROLE': agent_name,
                    'NATS_URL': agent_nats_url,
                    'WORKSPACE_DIR': working_dir,
                }
            }
        }
    }

    # Add knowledge-store MCP server for local agents only
    if not is_remote:
        ks_path = os.path.join(root_dir, 'knowledge-store', 'server.py')
        if os.path.isfile(ks_path):
            mcp_config['mcpServers']['knowledge-store'] = {
                'command': 'python3',
                'args': [ks_path],
                'env': {
                    'AGENT_ROLE': agent_name,
                    'NATS_URL': nats_url,
                    'OLLAMA_URL': 'http://localhost:11434',
                    'CHROMADB_PATH': os.path.join(root_dir, 'data', 'chromadb'),
                }
            }

    config_path = os.path.join(mcp_dir, f'{agent_name}.json')
    with open(config_path, 'w') as f:
        json.dump(mcp_config, f, indent=2)

    # Print remote agents for shell to scp configs
    if is_remote:
        print(f'{agent_name}|{ssh_host}|{config_path}')
"
}

REMOTE_AGENTS=$( \
    _AGENTS_JSON="$AGENTS_JSON" \
    _MCP_DIR="$MCP_DIR" \
    _PROJECT_DIR="$PROJECT_DIR" \
    _NATS_URL="$NATS_URL" \
    _ROOT_DIR="$ROOT_DIR" \
    _MCP_BRIDGE_REL_PATH="$MCP_BRIDGE_REL_PATH" \
    _MCP_SERVER_NAME="$MCP_SERVER_NAME" \
    _RUNTIME_CLAUDE_CODE="$RUNTIME_CLAUDE_CODE" \
    generate_mcp_configs \
)

# Copy MCP configs to remote hosts
if [ -n "$REMOTE_AGENTS" ]; then
    while IFS='|' read -r agent_name ssh_host config_path; do
        [ -z "$agent_name" ] && continue
        remote_config_dir="~/.mas-mcp-configs"
        if [ "${_TEST_DRY_RUN:-false}" = "true" ]; then
            echo "[DRY-RUN] scp ${config_path} ${ssh_host}:${remote_config_dir}/${agent_name}.json"
        else
            if ! ssh -n -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ConnectTimeout=5 "$ssh_host" "mkdir -p ${remote_config_dir}" 2>/dev/null; then
                echo "WARNING: Could not reach ${ssh_host} for agent '${agent_name}' (connection timed out or refused). Skipping remote config copy." >&2
                continue
            fi
            if scp -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ConnectTimeout=5 "$config_path" "${ssh_host}:${remote_config_dir}/${agent_name}.json" 2>/dev/null \
               || scp -O -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ConnectTimeout=5 "$config_path" "${ssh_host}:${remote_config_dir}/${agent_name}.json" 2>/dev/null; then
                echo "Copied MCP config for ${agent_name} to ${ssh_host}:${remote_config_dir}/${agent_name}.json"
            else
                echo "WARNING: Failed to copy MCP config to ${ssh_host} for agent '${agent_name}'." >&2
            fi
        fi
    done <<< "$REMOTE_AGENTS"
fi

# -----------------------------------------------------------------------
# NATS auto-start
# -----------------------------------------------------------------------
nats_is_running() {
    # Check whether nats-server is currently running.
    # Respects _TEST_NATS_RUNNING env var for testability.
    if [ -n "${_TEST_NATS_RUNNING:-}" ]; then
        [ "$_TEST_NATS_RUNNING" = "true" ]
        return
    fi
    pgrep -x nats-server &>/dev/null
}

auto_start_nats() {
    # Start NATS via setup-nats.sh if not already running.
    local setup_nats="${SCRIPT_DIR}/setup-nats.sh"

    if nats_is_running; then
        echo "NATS is already running. Skipping NATS start."
        return
    fi

    echo "NATS is not running. Starting NATS via setup-nats.sh..."
    if [ -f "$setup_nats" ]; then
        bash "$setup_nats" 2>&1 || echo "Warning: setup-nats.sh returned non-zero" >&2
    else
        echo "Warning: setup-nats.sh not found at $setup_nats" >&2
    fi
}

auto_start_nats

# -----------------------------------------------------------------------
# Dry-run mode: print commands instead of executing
# -----------------------------------------------------------------------
DRY_RUN="${_TEST_DRY_RUN:-false}"

tmux_cmd() {
    # Execute a tmux command, or print it in dry-run mode.
    if [ "$DRY_RUN" = "true" ]; then
        echo "[DRY-RUN] tmux $*"
    else
        tmux "$@" 2>/dev/null || true
    fi
}

# -----------------------------------------------------------------------
# Idempotent tmux session: kill existing session if present
# -----------------------------------------------------------------------
session_exists() {
    # Check whether the tmux session already exists.
    # Respects _TEST_SESSION_EXISTS env var for testability.
    if [ -n "${_TEST_SESSION_EXISTS:-}" ]; then
        [ "$_TEST_SESSION_EXISTS" = "true" ]
        return
    fi
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
}

ensure_clean_session() {
    # Kill any pre-existing tmux session for idempotency,
    # then create a fresh session with the control window.
    if session_exists; then
        echo "Existing tmux session '$SESSION_NAME' found. Killing it..."
        tmux_cmd kill-session -t "$SESSION_NAME"
    fi

    # If tmux-continuum is configured to auto-restore, its "last" symlink
    # would re-inflate the previous snapshot (ghost panes, stale grouped
    # sessions) on top of the fresh session we're building. Move the
    # symlink aside so continuum's restore is a no-op for this run. The
    # timestamped archive files are untouched so prior snapshots are
    # still recoverable if needed.
    for resurrect_last in "$HOME/.local/share/tmux/resurrect/last" "$HOME/.tmux/resurrect/last"; do
        if [ -L "$resurrect_last" ]; then
            mv "$resurrect_last" "${resurrect_last}.pre-start-$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
        fi
    done

    echo "Creating new tmux session '$SESSION_NAME'..."
    tmux_cmd new-session -d -s "$SESSION_NAME" -n "$CONTROL_WINDOW" -x 200 -y 50
    tmux_cmd set-option -g pane-border-status top
    tmux_cmd set-option -g pane-border-format ' #{@label} '
    tmux_cmd set-option -g pane-border-lines heavy
    tmux_cmd set-option -g allow-set-title off
    tmux_cmd set-option -t "$SESSION_NAME" mouse on
    # Remove CLAUDECODE env var so claude_code agents can launch in tmux panes
    tmux_cmd set-environment -g -u CLAUDECODE 2>/dev/null || true
    tmux_cmd set-environment -t "$SESSION_NAME" -u CLAUDECODE 2>/dev/null || true
}

ensure_clean_session

# -----------------------------------------------------------------------
# Control window: orchestrator + nats-monitor (side-by-side)
# -----------------------------------------------------------------------
setup_control_window() {
    # Configure the control window based on whether a manager agent is
    # configured.
    #
    # Layout WITH manager (3 panes):
    #   Pane 0: manager agent   (left, full height)
    #   Pane 1: orchestrator    (top-right)
    #   Pane 2: nats-monitor    (bottom-right)
    #
    # Layout WITHOUT manager (2 panes, legacy fallback):
    #   Pane 0: orchestrator    (left)
    #   Pane 1: nats-monitor    (right)
    #
    # Pane-index math: tmux assigns indices in spatial reading order
    # (top-to-bottom, left-to-right). Starting from a single pane and
    # doing `split -h -t .0` then `split -v -t .1` yields exactly the
    # three-pane layout above, with pane 1 staying top-right because
    # the vertical split of pane 1 places the new pane BELOW it.
    local nats_subjects="${NATS_URL##*://}"
    nats_subjects="${nats_subjects%%/*}"

    # Detect manager upfront so we can branch on the layout.
    local has_manager
    has_manager=$(python3 -c "
import json, sys
agents = json.loads(sys.stdin.read())
mgr = agents.get('manager', {})
if mgr.get('role') == 'monitor' and mgr.get('runtime') == 'claude_code':
    print('yes')
else:
    print('no')
" <<< "$AGENTS_JSON")

    # -- Background services (pane-independent) --

    # Start knowledge-store indexer as a background daemon (if available)
    local indexer_script="${ROOT_DIR}/knowledge-store/indexer.py"
    if [ -f "$indexer_script" ]; then
        mkdir -p "${ROOT_DIR}/data"
        NATS_URL="${NATS_URL}" \
        CHROMADB_PATH="${ROOT_DIR}/data/chromadb" \
        OLLAMA_URL="http://localhost:11434" \
            python3 "$indexer_script" >> "${ROOT_DIR}/data/indexer.log" 2>&1 &
        echo "Knowledge indexer started (PID: $!)"
    fi

    # Start speaker service as a background daemon (routes speak requests to hassio)
    local speaker_script="${ROOT_DIR}/services/speaker-service.py"
    if [ -f "$speaker_script" ]; then
        mkdir -p "${ROOT_DIR}/data"
        NATS_URL="${NATS_URL}" \
        NATS_STREAM="${STREAM_NAME:-AGENTS}" \
            python3 "$speaker_script" >> "${ROOT_DIR}/data/speaker-service.log" 2>&1 &
        echo "Speaker service started (PID: $!)"
    fi

    # Start thermostat service as a background daemon (HA climate control via NATS)
    local thermostat_script="${ROOT_DIR}/services/thermostat-service.py"
    if [ -f "$thermostat_script" ]; then
        mkdir -p "${ROOT_DIR}/data"
        NATS_URL="${NATS_URL}" \
        NATS_STREAM="${STREAM_NAME:-AGENTS}" \
            python3 "$thermostat_script" >> "${ROOT_DIR}/data/thermostat-service.log" 2>&1 &
        echo "Thermostat service started (PID: $!)"
    fi

    local monitor_script="${SCRIPT_DIR}/nats-monitor.sh"
    local orch_launch="cd ${ROOT_DIR} && python3 -m orchestrator ${PROJECT}"
    local monitor_launch
    if [ -f "$monitor_script" ]; then
        monitor_launch="bash ${monitor_script}"
    else
        monitor_launch="nats sub 'agents.>'"
    fi

    if [ "$has_manager" = "yes" ]; then
        # --- 3-pane layout: [manager | (orch / nats-monitor)] ---

        # Pane 0 = manager (left, full height)
        local manager_label
        manager_label=$(python3 -c "
import json, sys
agents = json.loads(sys.stdin.read())
print(agents.get('manager', {}).get('label', 'manager'))
" <<< "$AGENTS_JSON")
        tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" @label "$manager_label"

        local mcp_config_path="${MCP_DIR}/manager.json"
        local manager_prompt
        manager_prompt=$(python3 -c "
import json, sys
agents = json.loads(sys.stdin.read())
print(agents.get('manager', {}).get('system_prompt', ''))
" <<< "$AGENTS_JSON")

        local manager_cmd="cd ${ROOT_DIR} && unset CLAUDECODE; claude --dangerously-skip-permissions --strict-mcp-config --mcp-config ${mcp_config_path} --allowedTools mcp__mas-bridge__check_messages,mcp__mas-bridge__send_message,mcp__mas-bridge__send_to_agent,mcp__knowledge-store__search_knowledge,mcp__knowledge-store__index_knowledge,mcp__knowledge-store__gsheets_create,mcp__knowledge-store__gsheets_read,mcp__knowledge-store__gsheets_list"
        if [ -n "$manager_prompt" ]; then
            manager_cmd="${manager_cmd} --append-system-prompt '${manager_prompt}'"
        fi
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" "$manager_cmd" Enter

        # Pane 1 = orchestrator (top-right). Default 50/50 horizontal
        # split of pane 0 gives the manager ~100 cols (half of a 200
        # col window) — wide enough to read messages comfortably.
        tmux_cmd split-window -h -t "${SESSION_NAME}:${CONTROL_WINDOW}.0"
        tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" @label "orchestrator"
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" "$orch_launch" Enter

        # Pane 2 = nats-monitor (bottom-right). Vertical split of pane 1
        # inserts the new pane below, so pane 1 remains the top-right.
        tmux_cmd split-window -v -t "${SESSION_NAME}:${CONTROL_WINDOW}.1"
        tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.2" @label "nats-monitor"
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.2" "$monitor_launch" Enter
    else
        # --- 2-pane fallback: [orch | nats-monitor] ---

        # Pane 0 = orchestrator
        tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" @label "orchestrator"
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" "$orch_launch" Enter

        # Pane 1 = nats-monitor (right)
        tmux_cmd split-window -h -t "${SESSION_NAME}:${CONTROL_WINDOW}"
        tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" @label "nats-monitor"
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" "$monitor_launch" Enter
    fi
}

setup_control_window

# -----------------------------------------------------------------------
# Agents window: one pane per agent in tiled layout
# -----------------------------------------------------------------------
tmux_cmd new-window -t "$SESSION_NAME" -n "$AGENTS_WINDOW"

# Extract per-agent details in a single Python call to avoid N+1 invocations.
# Output format: one JSON object per line, each containing agent config fields.
# Agents are emitted in TMUX_PANE_ORDER so the 2x3 agents window grid shows:
#   row 1: hub (dev)    macmini (qa)
#   row 2: hassio       RTX5090
#   row 3: dgx (dgx1)   dgx2
# Agents not in the priority list are appended at the end in config order.
AGENT_DETAILS=$(_ROOT_DIR="$ROOT_DIR" python3 -c "
import json, sys, os

TMUX_PANE_ORDER = ['hub', 'macmini', 'hassio', 'RTX5090', 'dgx', 'dgx2']

root_dir = os.environ.get('_ROOT_DIR', '.')
agents = json.loads(sys.stdin.read())

# Reorder: priority list first (preserving its order), then any remaining
# agents in their original config order.
ordered_names = [n for n in TMUX_PANE_ORDER if n in agents]
ordered_names += [n for n in agents if n not in TMUX_PANE_ORDER]

for name in ordered_names:
    cfg = agents[name]
    wd = cfg.get('working_dir', '')
    # Resolve relative paths against root_dir
    if wd and not os.path.isabs(wd):
        wd = os.path.normpath(os.path.join(root_dir, wd))
    print(json.dumps({
        'name': name,
        'runtime': cfg.get('runtime', ''),
        'command': cfg.get('command', ''),
        'working_dir': wd,
        'ssh_host': cfg.get('ssh_host', ''),
        'system_prompt': cfg.get('system_prompt', ''),
        'remote_working_dir': cfg.get('remote_working_dir', ''),
        'remote_node_path': cfg.get('remote_node_path', ''),
        'label': cfg.get('label', ''),
    }))
" <<< "$AGENTS_JSON")

build_launch_command() {
    # Build the tmux send-keys command for a given agent based on its
    # runtime type (claude_code or script) and optional SSH host.
    # Remote claude_code agents use ~/.mas-mcp-configs/<name>.json and
    # remote_working_dir from config.
    local agent_name="$1"
    local runtime="$2"
    local command="$3"
    local ssh_host="$4"
    local working_dir="$5"
    local system_prompt="$6"
    local remote_working_dir="$7"
    local remote_node_path="$8"
    local launch_cmd=""

    if [ "$runtime" = "$RUNTIME_CLAUDE_CODE" ]; then
        local allowed_tools="mcp__mas-bridge__check_messages,mcp__mas-bridge__send_message,mcp__mas-bridge__send_to_agent,mcp__knowledge-store__search_knowledge,mcp__knowledge-store__index_knowledge,mcp__knowledge-store__gsheets_create,mcp__knowledge-store__gsheets_read,mcp__knowledge-store__gsheets_list"

        if [ -n "$ssh_host" ]; then
            # Remote agent: use remote paths, pass settings inline to skip onboarding prompts
            local mcp_config_path="~/.mas-mcp-configs/${agent_name}.json"
            local wd="${remote_working_dir:-~/mas-workspace}"
            # Prepend custom node path's directory to PATH if specified
            local path_prefix=""
            if [ -n "$remote_node_path" ]; then
                local node_dir
                node_dir=$(dirname "$remote_node_path")
                path_prefix="export PATH=${node_dir}:\$PATH && "
            fi
            launch_cmd="${path_prefix}cd ${wd} && unset CLAUDECODE; claude --dangerously-skip-permissions --strict-mcp-config --settings '{\"skipDangerousModePermissionPrompt\":true}' --mcp-config ${mcp_config_path} --allowedTools ${allowed_tools}"
        else
            # Local agent: use local paths
            local mcp_config_path="${MCP_DIR}/${agent_name}.json"
            launch_cmd="unset CLAUDECODE; claude --dangerously-skip-permissions --strict-mcp-config --mcp-config ${mcp_config_path} --allowedTools ${allowed_tools}"
            # Prepend cd to working directory if specified
            if [ -n "$working_dir" ] && [ -d "$working_dir" ]; then
                launch_cmd="cd ${working_dir} && ${launch_cmd}"
            fi
        fi

        # Append system prompt if configured
        # NOTE: system_prompt must NOT contain single quotes -- they break
        # shell quoting, especially through SSH double-quote wrapping.
        if [ -n "$system_prompt" ]; then
            launch_cmd="${launch_cmd} --append-system-prompt '${system_prompt}'"
        fi
    elif [ "$runtime" = "$RUNTIME_SCRIPT" ]; then
        launch_cmd="$command"
    fi

    # Prepend SSH wrapper if remote host is specified
    if [ -n "$ssh_host" ]; then
        # Use double quotes for SSH to avoid nested single-quote conflicts
        # (e.g., --append-system-prompt uses single quotes internally)
        # Use -t to force TTY allocation (required for Claude Code's interactive TUI)
        local escaped_cmd="${launch_cmd//\"/\\\"}"
        launch_cmd="ssh -t -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 ${ssh_host} \"${escaped_cmd}\""
    fi

    echo "$launch_cmd"
}

setup_agent_panes() {
    # Create one tmux pane per agent in the agents window, set pane
    # titles, and send the appropriate launch command to each pane.
    #
    # Layout: 2-column row-major grid (6 agents → 3 rows × 2 cols).
    # We track pane IDs (e.g. %5) instead of indices because tmux renumbers
    # indices when you split a non-last pane, and stale indices would land
    # labels/send-keys on the wrong pane.
    local pane_idx=0
    local pane_ids=()  # parallel array: pane_ids[N] = tmux pane_id for grid cell N

    # Seed pane_ids[0] with the already-existing first pane of AGENTS_WINDOW.
    pane_ids[0]=$(tmux display-message -p -t "${SESSION_NAME}:${AGENTS_WINDOW}.0" '#{pane_id}')

    while IFS= read -r agent_line; do
        [ -z "$agent_line" ] && continue

        local name runtime command working_dir ssh_host remote_working_dir
        name=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")

        # Skip manager agent -- it's launched in the control window, not agents window
        if [ "$name" = "manager" ]; then
            continue
        fi

        runtime=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['runtime'])")
        command=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['command'])")
        ssh_host=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh_host'])")
        working_dir=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['working_dir'])")
        system_prompt=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['system_prompt'])")
        remote_working_dir=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['remote_working_dir'])")
        remote_node_path=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['remote_node_path'])")
        label=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['label'])")

        # Create additional panes in a 2-column row-major grid:
        #   cell 0: already exists (from new-window)                  → top-left
        #   cell 1: split cell 0 horizontally                         → top-right
        #   cell N ≥ 2: split cell (N-2) vertically                  → next row, same column
        # Result for 6 agents:
        #   row 1: 0 | 1
        #   row 2: 2 | 3
        #   row 3: 4 | 5
        # Each split captures the new pane's id via `-P -F '#{pane_id}'`
        # and stores it in pane_ids[N], so subsequent label/send-keys
        # target the right pane regardless of tmux index renumbering.
        local target_id=""
        if [ "$pane_idx" -eq 1 ]; then
            target_id=$(tmux_cmd split-window -h -P -F '#{pane_id}' -t "${pane_ids[0]}")
            pane_ids[1]="$target_id"
        elif [ "$pane_idx" -ge 2 ]; then
            local parent_idx=$((pane_idx - 2))
            target_id=$(tmux_cmd split-window -v -P -F '#{pane_id}' -t "${pane_ids[$parent_idx]}")
            pane_ids[$pane_idx]="$target_id"
        else
            target_id="${pane_ids[0]}"
        fi

        local launch_cmd
        launch_cmd=$(build_launch_command "$name" "$runtime" "$command" "$ssh_host" "$working_dir" "$system_prompt" "$remote_working_dir" "$remote_node_path")

        # Build pane title: use config label if set, else "name (host)" / "name (local)"
        local pane_title="$name"
        if [ -n "$label" ]; then
            pane_title="$label"
        elif [ -n "$ssh_host" ]; then
            local host_label="${ssh_host#*@}"  # strip user@ prefix, keep IP/hostname
            pane_title="$name ($host_label)"
        else
            pane_title="$name (local)"
        fi
        tmux_cmd set-option -p -t "${target_id}" @label "$pane_title"

        # Pre-check connectivity for remote agents
        if [ -n "$ssh_host" ] && [ "$DRY_RUN" != "true" ]; then
            if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -n "$ssh_host" "echo ok" &>/dev/null; then
                echo "WARNING: Agent '${name}' -- cannot reach ${ssh_host} (connection timed out or refused)" >&2
                tmux_cmd send-keys -t "${target_id}" \
                    "echo 'ERROR: Cannot reach ${ssh_host} -- check IP address and network connectivity'" Enter
                pane_idx=$((pane_idx + 1))
                continue
            fi
        fi

        # For SSH agents, wrap in auto-reconnect loop
        if [ -n "$ssh_host" ]; then
            local launch_script="/tmp/mas-launch-${name}.sh"
            echo "$launch_cmd" > "$launch_script"
            tmux_cmd send-keys -t "${target_id}" \
                "bash ${SCRIPT_DIR}/ssh-reconnect.sh ${name}" Enter
        else
            tmux_cmd send-keys -t "${target_id}" "$launch_cmd" Enter
        fi

        pane_idx=$((pane_idx + 1))
    done <<< "$AGENT_DETAILS"

    # Equalize row heights and column widths in the 2x3 grid without
    # collapsing it into tiled (which would flip us back to 3x2 for 6 panes).
    tmux_cmd select-layout -t "${SESSION_NAME}:${AGENTS_WINDOW}" -E
}

setup_agent_panes

# -----------------------------------------------------------------------
# Finalize: open two iTerm windows (control + agents)
# -----------------------------------------------------------------------
echo "Session '$SESSION_NAME' created successfully with ${CONTROL_WINDOW} and ${AGENTS_WINDOW} windows."

if [ "$DRY_RUN" = "true" ]; then
    echo "Attach with: tmux attach -t $SESSION_NAME"
    exit 0
fi

# -----------------------------------------------------------------------
# Open two terminal windows (control + agents) using grouped sessions.
#
# We use `new-session -A -s <deterministic-name> -t <group>` so the
# secondary sessions have predictable names (${SESSION_NAME}-control,
# ${SESSION_NAME}-agents). Without -A -s, tmux auto-appends a numeric
# suffix on every attach (remote-test-40, remote-test-42, …) and the
# cruft piles up because stop.sh only killed the primary session.
#
# -A: attach if a session with that name already exists (idempotent)
# -s <name>: deterministic session name
# -t <group>: join the group so windows are shared with the primary
# -----------------------------------------------------------------------
TMUX_BIN="$(command -v tmux)"

# Kill stale grouped sessions that tmux-resurrect/continuum may have restored.
# Without this, `new-session -A` below would attach to the restored (empty)
# session instead of creating a fresh one grouped with ${SESSION_NAME},
# producing ghost windows with bare zsh panes instead of the real workload.
"${TMUX_BIN}" kill-session -t "${SESSION_NAME}-control" 2>/dev/null || true
"${TMUX_BIN}" kill-session -t "${SESSION_NAME}-agents"  2>/dev/null || true

TMUX_CMD_CONTROL="${TMUX_BIN} new-session -A -s ${SESSION_NAME}-control -t ${SESSION_NAME} \; select-window -t ${CONTROL_WINDOW}"
TMUX_CMD_AGENTS="${TMUX_BIN} new-session -A -s ${SESSION_NAME}-agents -t ${SESSION_NAME} \; select-window -t ${AGENTS_WINDOW}"

open_two_windows() {
    if command -v osascript &>/dev/null; then
        # macOS + iTerm2
        osascript -e 'tell application "iTerm2"
            activate
            create window with default profile command "'"${TMUX_CMD_CONTROL}"'"
        end tell' 2>/dev/null
        sleep 1
        osascript -e 'tell application "iTerm2"
            create window with default profile command "'"${TMUX_CMD_AGENTS}"'"
        end tell' 2>/dev/null
        echo "Opened iTerm windows: ${CONTROL_WINDOW} + ${AGENTS_WINDOW}"

    elif command -v wt.exe &>/dev/null; then
        # Windows Terminal (WSL)
        wt.exe new-tab --title "${CONTROL_WINDOW}" -- bash -c "${TMUX_CMD_CONTROL}" 2>/dev/null
        sleep 1
        wt.exe new-tab --title "${AGENTS_WINDOW}" -- bash -c "${TMUX_CMD_AGENTS}" 2>/dev/null
        echo "Opened Windows Terminal tabs: ${CONTROL_WINDOW} + ${AGENTS_WINDOW}"

    elif command -v gnome-terminal &>/dev/null; then
        # Linux (GNOME)
        gnome-terminal --title="${CONTROL_WINDOW}" -- bash -c "${TMUX_CMD_CONTROL}" 2>/dev/null &
        sleep 1
        gnome-terminal --title="${AGENTS_WINDOW}" -- bash -c "${TMUX_CMD_AGENTS}" 2>/dev/null &
        echo "Opened GNOME terminals: ${CONTROL_WINDOW} + ${AGENTS_WINDOW}"

    elif command -v xterm &>/dev/null; then
        # Linux (xterm fallback)
        xterm -title "${CONTROL_WINDOW}" -e "${TMUX_CMD_CONTROL}" 2>/dev/null &
        sleep 1
        xterm -title "${AGENTS_WINDOW}" -e "${TMUX_CMD_AGENTS}" 2>/dev/null &
        echo "Opened xterm windows: ${CONTROL_WINDOW} + ${AGENTS_WINDOW}"

    else
        # Last resort: attach in current terminal, print instructions for second
        echo "Open a second terminal and run:"
        echo "  ${TMUX_CMD_AGENTS}"
        echo ""
        exec ${TMUX_CMD_CONTROL}
    fi
}

open_two_windows

# Start pipe-pane logging on all agent panes (after agents have launched)
if [ -f "${ROOT_DIR}/scripts/start-agent-logging.sh" ]; then
    sleep 5  # give panes time to settle
    bash "${ROOT_DIR}/scripts/start-agent-logging.sh" "$PROJECT" || true
fi
