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
    mcp_config = {
        'mcpServers': {
            server_name: {
                'command': cmd,
                'args': [bridge_path],
                'env': {
                    'AGENT_ROLE': agent_name,
                    'NATS_URL': nats_url,
                    'WORKSPACE_DIR': working_dir,
                }
            }
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
    # Configure the control window with panes:
    #   Pane 0: orchestrator process
    #   Pane 1: nats-monitor (horizontal split for side-by-side layout)
    #   Pane 2: manager agent (bottom, if configured -- autonomous monitor)
    local nats_subjects="${NATS_URL##*://}"
    nats_subjects="${nats_subjects%%/*}"

    tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" @label "orchestrator"
    tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" \
        "cd ${ROOT_DIR} && python3 -m orchestrator ${PROJECT}" Enter

    tmux_cmd split-window -h -t "${SESSION_NAME}:${CONTROL_WINDOW}"
    tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" @label "nats-monitor"

    local monitor_script="${SCRIPT_DIR}/nats-monitor.sh"
    if [ -f "$monitor_script" ]; then
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" \
            "bash ${monitor_script}" Enter
    else
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" \
            "nats sub 'agents.>'" Enter
    fi

    # Launch manager agent in the control window if configured
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

    if [ "$has_manager" = "yes" ]; then
        tmux_cmd split-window -v -t "${SESSION_NAME}:${CONTROL_WINDOW}.0"

        local manager_label
        manager_label=$(python3 -c "
import json, sys
agents = json.loads(sys.stdin.read())
print(agents.get('manager', {}).get('label', 'manager'))
" <<< "$AGENTS_JSON")
        tmux_cmd set-option -p -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" @label "$manager_label"

        local mcp_config_path="${MCP_DIR}/manager.json"
        local manager_prompt
        manager_prompt=$(python3 -c "
import json, sys
agents = json.loads(sys.stdin.read())
print(agents.get('manager', {}).get('system_prompt', ''))
" <<< "$AGENTS_JSON")

        local manager_cmd="cd ${ROOT_DIR} && unset CLAUDECODE; claude --dangerously-skip-permissions --strict-mcp-config --mcp-config ${mcp_config_path} --allowedTools mcp__mas-bridge__check_messages,mcp__mas-bridge__send_message,mcp__mas-bridge__send_to_agent"
        if [ -n "$manager_prompt" ]; then
            manager_cmd="${manager_cmd} --append-system-prompt '${manager_prompt}'"
        fi

        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" "$manager_cmd" Enter
    fi
}

setup_control_window

# -----------------------------------------------------------------------
# Agents window: one pane per agent in tiled layout
# -----------------------------------------------------------------------
tmux_cmd new-window -t "$SESSION_NAME" -n "$AGENTS_WINDOW"

# Extract per-agent details in a single Python call to avoid N+1 invocations.
# Output format: one JSON object per line, each containing agent config fields.
AGENT_DETAILS=$(_ROOT_DIR="$ROOT_DIR" python3 -c "
import json, sys, os

root_dir = os.environ.get('_ROOT_DIR', '.')
agents = json.loads(sys.stdin.read())
for name, cfg in agents.items():
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
        local allowed_tools="mcp__mas-bridge__check_messages,mcp__mas-bridge__send_message,mcp__mas-bridge__send_to_agent"

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
    local pane_idx=0

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

        # Create additional panes (first pane already exists with new-window)
        if [ "$pane_idx" -gt 0 ]; then
            tmux_cmd split-window -t "${SESSION_NAME}:${AGENTS_WINDOW}"
            tmux_cmd select-layout -t "${SESSION_NAME}:${AGENTS_WINDOW}" tiled
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
        tmux_cmd set-option -p -t "${SESSION_NAME}:${AGENTS_WINDOW}.${pane_idx}" @label "$pane_title"

        # Pre-check connectivity for remote agents
        if [ -n "$ssh_host" ] && [ "$DRY_RUN" != "true" ]; then
            if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -n "$ssh_host" "echo ok" &>/dev/null; then
                echo "WARNING: Agent '${name}' -- cannot reach ${ssh_host} (connection timed out or refused)" >&2
                tmux_cmd send-keys -t "${SESSION_NAME}:${AGENTS_WINDOW}.${pane_idx}" \
                    "echo 'ERROR: Cannot reach ${ssh_host} -- check IP address and network connectivity'" Enter
                pane_idx=$((pane_idx + 1))
                continue
            fi
        fi

        # For SSH agents, wrap in auto-reconnect loop
        if [ -n "$ssh_host" ]; then
            local launch_script="/tmp/mas-launch-${name}.sh"
            echo "$launch_cmd" > "$launch_script"
            tmux_cmd send-keys -t "${SESSION_NAME}:${AGENTS_WINDOW}.${pane_idx}" \
                "bash ${SCRIPT_DIR}/ssh-reconnect.sh ${name}" Enter
        else
            tmux_cmd send-keys -t "${SESSION_NAME}:${AGENTS_WINDOW}.${pane_idx}" "$launch_cmd" Enter
        fi

        pane_idx=$((pane_idx + 1))
    done <<< "$AGENT_DETAILS"

    # Final tiled layout pass for even distribution
    tmux_cmd select-layout -t "${SESSION_NAME}:${AGENTS_WINDOW}" tiled
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
# Each platform gets its own launcher; all share the same tmux commands:
#   Window 1: tmux new-session -t <session> \; select-window -t control
#   Window 2: tmux new-session -t <session> \; select-window -t agents
# -----------------------------------------------------------------------
TMUX_BIN="$(command -v tmux)"
TMUX_CMD_CONTROL="${TMUX_BIN} new-session -t ${SESSION_NAME} \; select-window -t ${CONTROL_WINDOW}"
TMUX_CMD_AGENTS="${TMUX_BIN} new-session -t ${SESSION_NAME} \; select-window -t ${AGENTS_WINDOW}"

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
