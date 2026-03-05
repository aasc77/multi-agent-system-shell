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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(pwd)"

# -----------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------
if [ $# -lt 1 ]; then
    echo "Usage: $0 <project>" >&2
    echo "  project: Name of the project directory under projects/" >&2
    exit 1
fi

PROJECT="$1"

# -----------------------------------------------------------------------
# Preflight checks
# -----------------------------------------------------------------------
REQUIRED_TOOLS=("tmux" "python3" "nats-server")
MISSING_TOOLS=()

for tool in "${REQUIRED_TOOLS[@]}"; do
    is_missing=false

    # Test hook: _TEST_MISSING_TOOLS overrides real checks
    if [ -n "${_TEST_MISSING_TOOLS:-}" ]; then
        IFS=',' read -ra TEST_MISSING <<< "$_TEST_MISSING_TOOLS"
        for tm in "${TEST_MISSING[@]}"; do
            if [ "$tm" = "$tool" ]; then
                is_missing=true
                break
            fi
        done
    else
        if ! command -v "$tool" &>/dev/null; then
            is_missing=true
        fi
    fi

    if [ "$is_missing" = true ]; then
        MISSING_TOOLS+=("$tool")
    fi
done

if [ ${#MISSING_TOOLS[@]} -gt 0 ]; then
    echo "Error: Required tools are missing. Please install them:" >&2
    for tool in "${MISSING_TOOLS[@]}"; do
        echo "  - $tool (required, not found)" >&2
    done
    exit 1
fi

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
# Parse configs using Python (YAML parsing)
# -----------------------------------------------------------------------
CONFIG_JSON=$(python3 -c "
import json, sys
try:
    import yaml
except ImportError:
    sys.exit(0)

import os

project_config_path = '$PROJECT_CONFIG'
global_config_path = '$GLOBAL_CONFIG'

# Load global config
global_cfg = {}
if os.path.isfile(global_config_path):
    with open(global_config_path) as f:
        global_cfg = yaml.safe_load(f) or {}

# Load project config
with open(project_config_path) as f:
    project_cfg = yaml.safe_load(f) or {}

# Deep merge (project overrides global, 2 levels)
merged = dict(global_cfg)
for key, val in project_cfg.items():
    if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
        merged[key] = {**merged[key], **val}
    else:
        merged[key] = val

print(json.dumps(merged))
")

if [ -z "$CONFIG_JSON" ]; then
    echo "Error: Failed to parse config files" >&2
    exit 1
fi

# Extract values from config
SESSION_NAME=$(echo "$CONFIG_JSON" | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('tmux',{}).get('session_name', c.get('project','mas')))")
NATS_URL=$(echo "$CONFIG_JSON" | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('nats',{}).get('url','nats://localhost:4222'))")
AGENTS_JSON=$(echo "$CONFIG_JSON" | python3 -c "import json,sys; c=json.load(sys.stdin); print(json.dumps(c.get('agents',{})))")

# -----------------------------------------------------------------------
# MCP config generation (for claude_code agents only)
# -----------------------------------------------------------------------
MCP_DIR="${PROJECT_DIR}/.mcp-configs"
mkdir -p "$MCP_DIR"

# Generate MCP configs using Python
python3 -c "
import json, os, sys

agents = json.loads('$AGENTS_JSON')
mcp_dir = '$MCP_DIR'
project_dir = '$PROJECT_DIR'
nats_url = '$NATS_URL'
script_dir = os.path.dirname('$SCRIPT_DIR')
root_dir = '$ROOT_DIR'

# Determine bridge path
bridge_path = os.path.join(root_dir, 'mcp-bridge', 'index.js')

for agent_name, agent_cfg in agents.items():
    runtime = agent_cfg.get('runtime', '')
    if runtime != 'claude_code':
        continue

    working_dir = agent_cfg.get('working_dir', project_dir)

    mcp_config = {
        'mcpServers': {
            'mas-bridge': {
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

# -----------------------------------------------------------------------
# NATS auto-start
# -----------------------------------------------------------------------
nats_is_running() {
    if [ -n "${_TEST_NATS_RUNNING:-}" ]; then
        [ "$_TEST_NATS_RUNNING" = "true" ]
        return $?
    fi
    pgrep -x nats-server &>/dev/null
    return $?
}

SETUP_NATS="${SCRIPT_DIR}/setup-nats.sh"

if ! nats_is_running; then
    echo "NATS is not running. Starting NATS via setup-nats.sh..."
    if [ -f "$SETUP_NATS" ]; then
        bash "$SETUP_NATS" 2>&1 || echo "Warning: setup-nats.sh returned non-zero" >&2
    else
        echo "Warning: setup-nats.sh not found at $SETUP_NATS" >&2
    fi
else
    echo "NATS is already running. Skipping NATS start."
fi

# -----------------------------------------------------------------------
# Dry-run mode: print commands instead of executing
# -----------------------------------------------------------------------
DRY_RUN="${_TEST_DRY_RUN:-false}"

tmux_cmd() {
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
    if [ -n "${_TEST_SESSION_EXISTS:-}" ]; then
        [ "$_TEST_SESSION_EXISTS" = "true" ]
        return $?
    fi
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
    return $?
}

if session_exists; then
    echo "Existing tmux session '$SESSION_NAME' found. Killing it..."
    tmux_cmd kill-session -t "$SESSION_NAME"
fi

# -----------------------------------------------------------------------
# Create tmux session with control window
# -----------------------------------------------------------------------
echo "Creating new tmux session '$SESSION_NAME'..."

# Create new session with control window (first pane runs orchestrator)
tmux_cmd new-session -d -s "$SESSION_NAME" -n control -x 200 -y 50

# Set pane-border-status to top for the session
tmux_cmd set-option -t "$SESSION_NAME" pane-border-status top

# First pane: orchestrator
tmux_cmd send-keys -t "${SESSION_NAME}:control.0" "echo 'orchestrator: python3 -m orchestrator'" Enter

# Split horizontally (-h) for nats-monitor pane (side-by-side)
tmux_cmd split-window -h -t "${SESSION_NAME}:control"
tmux_cmd send-keys -t "${SESSION_NAME}:control.1" "echo 'nats-monitor: nats sub agents.>'" Enter

# -----------------------------------------------------------------------
# Create agents window with one pane per agent
# -----------------------------------------------------------------------
tmux_cmd new-window -t "$SESSION_NAME" -n agents

# Parse agent names into array
AGENT_NAMES=$(echo "$AGENTS_JSON" | python3 -c "import json,sys; agents=json.load(sys.stdin); print(' '.join(agents.keys()))")
AGENT_COUNT=$(echo "$AGENTS_JSON" | python3 -c "import json,sys; agents=json.load(sys.stdin); print(len(agents))")

PANE_IDX=0
for AGENT_NAME in $AGENT_NAMES; do
    AGENT_CFG=$(echo "$AGENTS_JSON" | python3 -c "
import json, sys
agents = json.load(sys.stdin)
print(json.dumps(agents['$AGENT_NAME']))
")
    RUNTIME=$(echo "$AGENT_CFG" | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('runtime',''))")
    COMMAND=$(echo "$AGENT_CFG" | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('command',''))")
    WORKING_DIR=$(echo "$AGENT_CFG" | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('working_dir',''))")
    SSH_HOST=$(echo "$AGENT_CFG" | python3 -c "import json,sys; c=json.load(sys.stdin); print(c.get('ssh_host',''))")

    # Create additional panes (first pane already exists)
    if [ "$PANE_IDX" -gt 0 ]; then
        tmux_cmd split-window -t "${SESSION_NAME}:agents"
        tmux_cmd select-layout -t "${SESSION_NAME}:agents" tiled
    fi

    # Build the launch command
    LAUNCH_CMD=""
    if [ "$RUNTIME" = "claude_code" ]; then
        MCP_CONFIG_PATH="${MCP_DIR}/${AGENT_NAME}.json"
        LAUNCH_CMD="claude --mcp-config ${MCP_CONFIG_PATH}"
    elif [ "$RUNTIME" = "script" ]; then
        LAUNCH_CMD="$COMMAND"
    fi

    # Prepend SSH if needed
    if [ -n "$SSH_HOST" ]; then
        LAUNCH_CMD="ssh ${SSH_HOST} '${LAUNCH_CMD}'"
    fi

    # Set pane title
    tmux_cmd select-pane -t "${SESSION_NAME}:agents.${PANE_IDX}" -T "$AGENT_NAME"

    # Send the launch command
    tmux_cmd send-keys -t "${SESSION_NAME}:agents.${PANE_IDX}" "$LAUNCH_CMD" Enter

    PANE_IDX=$((PANE_IDX + 1))
done

# Apply tiled layout to agents window
tmux_cmd select-layout -t "${SESSION_NAME}:agents" tiled

# Select the control window
tmux_cmd select-window -t "${SESSION_NAME}:control"

echo "Session '$SESSION_NAME' created successfully with control and agents windows."
echo "Attach with: tmux attach -t $SESSION_NAME"

exit 0
