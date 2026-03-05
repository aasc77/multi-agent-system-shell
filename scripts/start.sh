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

bridge_path = os.path.join(root_dir, bridge_rel_path)

for agent_name, agent_cfg in agents.items():
    if agent_cfg.get('runtime', '') != runtime_claude:
        continue

    working_dir = agent_cfg.get('working_dir', project_dir)

    mcp_config = {
        'mcpServers': {
            server_name: {
                'command': 'node',
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
"
}

_AGENTS_JSON="$AGENTS_JSON" \
_MCP_DIR="$MCP_DIR" \
_PROJECT_DIR="$PROJECT_DIR" \
_NATS_URL="$NATS_URL" \
_ROOT_DIR="$ROOT_DIR" \
_MCP_BRIDGE_REL_PATH="$MCP_BRIDGE_REL_PATH" \
_MCP_SERVER_NAME="$MCP_SERVER_NAME" \
_RUNTIME_CLAUDE_CODE="$RUNTIME_CLAUDE_CODE" \
generate_mcp_configs

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
    tmux_cmd set-option -t "$SESSION_NAME" pane-border-status top
    # Remove CLAUDECODE env var so claude_code agents can launch in tmux panes
    tmux_cmd set-environment -g -u CLAUDECODE 2>/dev/null || true
    tmux_cmd set-environment -t "$SESSION_NAME" -u CLAUDECODE 2>/dev/null || true
}

ensure_clean_session

# -----------------------------------------------------------------------
# Control window: orchestrator + nats-monitor (side-by-side)
# -----------------------------------------------------------------------
setup_control_window() {
    # Configure the control window with two panes:
    #   Pane 0: orchestrator process
    #   Pane 1: nats-monitor (horizontal split for side-by-side layout)
    local nats_subjects="${NATS_URL##*://}"
    nats_subjects="${nats_subjects%%/*}"

    tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.0" \
        "cd ${ROOT_DIR} && python3 -m orchestrator ${PROJECT}" Enter

    tmux_cmd split-window -h -t "${SESSION_NAME}:${CONTROL_WINDOW}"

    local monitor_script="${SCRIPT_DIR}/nats-monitor.sh"
    if [ -f "$monitor_script" ]; then
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" \
            "bash ${monitor_script}" Enter
    else
        tmux_cmd send-keys -t "${SESSION_NAME}:${CONTROL_WINDOW}.1" \
            "nats sub 'agents.>'" Enter
    fi
}

setup_control_window

# -----------------------------------------------------------------------
# Agents window: one pane per agent in tiled layout
# -----------------------------------------------------------------------
tmux_cmd new-window -t "$SESSION_NAME" -n "$AGENTS_WINDOW"

# Extract per-agent details in a single Python call to avoid N+1 invocations.
# Output format: one JSON object per line, each containing agent config fields.
AGENT_DETAILS=$(python3 -c "
import json, sys

agents = json.loads(sys.stdin.read())
for name, cfg in agents.items():
    print(json.dumps({
        'name': name,
        'runtime': cfg.get('runtime', ''),
        'command': cfg.get('command', ''),
        'working_dir': cfg.get('working_dir', ''),
        'ssh_host': cfg.get('ssh_host', ''),
    }))
" <<< "$AGENTS_JSON")

build_launch_command() {
    # Build the tmux send-keys command for a given agent based on its
    # runtime type (claude_code or script) and optional SSH host.
    local agent_name="$1"
    local runtime="$2"
    local command="$3"
    local ssh_host="$4"
    local launch_cmd=""

    if [ "$runtime" = "$RUNTIME_CLAUDE_CODE" ]; then
        local mcp_config_path="${MCP_DIR}/${agent_name}.json"
        launch_cmd="unset CLAUDECODE; claude --mcp-config ${mcp_config_path}"
    elif [ "$runtime" = "$RUNTIME_SCRIPT" ]; then
        launch_cmd="$command"
    fi

    # Prepend SSH wrapper if remote host is specified
    if [ -n "$ssh_host" ]; then
        launch_cmd="ssh ${ssh_host} '${launch_cmd}'"
    fi

    echo "$launch_cmd"
}

setup_agent_panes() {
    # Create one tmux pane per agent in the agents window, set pane
    # titles, and send the appropriate launch command to each pane.
    local pane_idx=0

    while IFS= read -r agent_line; do
        [ -z "$agent_line" ] && continue

        local name runtime command working_dir ssh_host
        name=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
        runtime=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['runtime'])")
        command=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['command'])")
        ssh_host=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh_host'])")

        # Create additional panes (first pane already exists with new-window)
        if [ "$pane_idx" -gt 0 ]; then
            tmux_cmd split-window -t "${SESSION_NAME}:${AGENTS_WINDOW}"
            tmux_cmd select-layout -t "${SESSION_NAME}:${AGENTS_WINDOW}" tiled
        fi

        local launch_cmd
        launch_cmd=$(build_launch_command "$name" "$runtime" "$command" "$ssh_host")

        tmux_cmd select-pane -t "${SESSION_NAME}:${AGENTS_WINDOW}.${pane_idx}" -T "$name"
        tmux_cmd send-keys -t "${SESSION_NAME}:${AGENTS_WINDOW}.${pane_idx}" "$launch_cmd" Enter

        pane_idx=$((pane_idx + 1))
    done <<< "$AGENT_DETAILS"

    # Final tiled layout pass for even distribution
    tmux_cmd select-layout -t "${SESSION_NAME}:${AGENTS_WINDOW}" tiled
}

setup_agent_panes

# -----------------------------------------------------------------------
# Finalize: select control window and print success message
# -----------------------------------------------------------------------
tmux_cmd select-window -t "${SESSION_NAME}:${AGENTS_WINDOW}"

echo "Session '$SESSION_NAME' created successfully with ${CONTROL_WINDOW} and ${AGENTS_WINDOW} windows."

# Auto-attach if running in an interactive terminal
if [ -t 0 ] && [ "$DRY_RUN" != "true" ]; then
    exec tmux attach -t "$SESSION_NAME"
else
    echo "Attach with: tmux attach -t $SESSION_NAME"
fi
