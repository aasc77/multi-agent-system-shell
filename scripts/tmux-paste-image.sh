#!/usr/bin/env bash
# tmux-paste-image.sh -- Paste clipboard image into any agent's workspace
#
# Bound to a tmux key (e.g., prefix + V). Detects which agent pane you're in,
# grabs the clipboard image via pngpaste, SCPs it to the agent's shared/ dir,
# and types the path into the pane so Claude Code can read it.
#
# Usage:
#   ./scripts/tmux-paste-image.sh [session_name]
#
# Requires: pngpaste (brew install pngpaste)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
TMP_IMG="/tmp/clipboard-${TIMESTAMP}.png"

# Get session name (passed by tmux or auto-detect)
SESSION="${1:-$(tmux display-message -p '#{session_name}')}"

# --- 1. Grab image from clipboard ---
if ! command -v pngpaste &>/dev/null; then
    tmux display-message "pngpaste not found. Run: brew install pngpaste"
    exit 1
fi

if ! pngpaste "$TMP_IMG" 2>/dev/null; then
    tmux display-message "No image in clipboard"
    exit 1
fi

# --- 2. Detect which pane we're in and map to agent ---
CURRENT_PANE=$(tmux display-message -p '#{pane_index}')
CURRENT_WINDOW=$(tmux display-message -p '#{window_name}')

if [ "$CURRENT_WINDOW" != "agents" ]; then
    tmux display-message "Not in agents window — switch to an agent pane first"
    rm -f "$TMP_IMG"
    exit 1
fi

# Get agent label from pane
PANE_LABEL=$(tmux display-message -p -t "${SESSION}:agents.${CURRENT_PANE}" '#{@label}')
# Extract agent name (strip the "(local)" or "(hostname)" suffix)
AGENT_NAME=$(echo "$PANE_LABEL" | sed 's/ (.*//')

if [ -z "$AGENT_NAME" ]; then
    tmux display-message "Could not detect agent for pane ${CURRENT_PANE}"
    rm -f "$TMP_IMG"
    exit 1
fi

# --- 3. Find agent config and deliver the image ---
PROJECT_CONFIG="${ROOT_DIR}/projects/${SESSION}/config.yaml"
FILENAME="clipboard-${TIMESTAMP}.png"

if [ ! -f "$PROJECT_CONFIG" ]; then
    tmux display-message "No config found for project: ${SESSION}"
    rm -f "$TMP_IMG"
    exit 1
fi

# Parse this agent's config
AGENT_INFO=$(python3 -c "
import yaml, json
with open('$PROJECT_CONFIG') as f:
    cfg = yaml.safe_load(f)
agent = cfg.get('agents', {}).get('$AGENT_NAME', {})
print(json.dumps({
    'ssh_host': agent.get('ssh_host', ''),
    'working_dir': agent.get('working_dir', ''),
    'remote_working_dir': agent.get('remote_working_dir', ''),
}))
" 2>/dev/null)

SSH_HOST=$(echo "$AGENT_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh_host'])")
WORKING_DIR=$(echo "$AGENT_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin)['working_dir'])")
REMOTE_WORKING_DIR=$(echo "$AGENT_INFO" | python3 -c "import json,sys; print(json.load(sys.stdin)['remote_working_dir'])")

if [ -n "$SSH_HOST" ]; then
    # Remote agent — SCP the image
    DEST="${REMOTE_WORKING_DIR:-~/mas-workspace}/shared/"
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$SSH_HOST" "mkdir -p ${DEST}" 2>/dev/null
    if scp -O -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$TMP_IMG" "${SSH_HOST}:${DEST}${FILENAME}" 2>/dev/null; then
        # Type the path into the pane for Claude Code
        REMOTE_PATH="${REMOTE_WORKING_DIR:-~/mas-workspace}/shared/${FILENAME}"
        tmux send-keys -t "${SESSION}:agents.${CURRENT_PANE}" "Look at the image I just pasted: shared/${FILENAME}" Enter
        tmux display-message "Image sent to ${AGENT_NAME} → shared/${FILENAME}"
    else
        tmux display-message "SCP failed to ${AGENT_NAME} (${SSH_HOST})"
    fi
else
    # Local agent — copy directly
    if [ -n "$WORKING_DIR" ]; then
        WD="$WORKING_DIR"
        [ ! "${WD:0:1}" = "/" ] && WD="${ROOT_DIR}/${WD}"
    else
        WD="${ROOT_DIR}"
    fi
    mkdir -p "${WD}/shared"
    cp "$TMP_IMG" "${WD}/shared/${FILENAME}"
    tmux send-keys -t "${SESSION}:agents.${CURRENT_PANE}" "Look at the image I just pasted: shared/${FILENAME}" Enter
    tmux display-message "Image pasted to ${AGENT_NAME} → shared/${FILENAME}"
fi

rm -f "$TMP_IMG"
