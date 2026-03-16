#!/usr/bin/env bash
# share-file.sh -- Rsync a file to all agent workspaces
#
# Usage:
#   ./scripts/share-file.sh <project> <file-path>
#
# Example:
#   ./scripts/share-file.sh remote-test ~/Screenshots/bug.png
#   Then tell an agent: "look at shared/bug.png"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SHARED_DIR="shared"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <project> <file-path>" >&2
    exit 1
fi

PROJECT="$1"
FILE_PATH="$2"
PROJECT_CONFIG="${ROOT_DIR}/projects/${PROJECT}/config.yaml"

if [ ! -f "$FILE_PATH" ]; then
    echo "Error: File not found: ${FILE_PATH}" >&2
    exit 1
fi

if [ ! -f "$PROJECT_CONFIG" ]; then
    echo "Error: Project '${PROJECT}' not found." >&2
    exit 1
fi

FILENAME=$(basename "$FILE_PATH")

# Parse agents from config
AGENTS=$(python3 -c "
import yaml, json
with open('$PROJECT_CONFIG') as f:
    cfg = yaml.safe_load(f)
for name, agent in cfg.get('agents', {}).items():
    print(json.dumps({
        'name': name,
        'ssh_host': agent.get('ssh_host', ''),
        'working_dir': agent.get('working_dir', ''),
        'remote_working_dir': agent.get('remote_working_dir', ''),
    }))
")

echo "Sharing ${FILENAME} to all agents..."

while IFS= read -r agent_line; do
    [ -z "$agent_line" ] && continue
    name=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
    ssh_host=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh_host'])")
    working_dir=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['working_dir'])")
    remote_working_dir=$(echo "$agent_line" | python3 -c "import json,sys; print(json.load(sys.stdin)['remote_working_dir'])")

    if [ -n "$ssh_host" ]; then
        # Remote agent
        dest="${remote_working_dir:-~/mas-workspace}/${SHARED_DIR}/"
        ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$ssh_host" "mkdir -p ${dest}" 2>/dev/null
        if rsync -az -e "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5" "$FILE_PATH" "${ssh_host}:${dest}" 2>/dev/null \
           || scp -O -o StrictHostKeyChecking=no "$FILE_PATH" "${ssh_host}:${dest}" 2>/dev/null; then
            echo "  ${name} (${ssh_host}): OK"
        else
            echo "  ${name} (${ssh_host}): FAILED" >&2
        fi
    else
        # Local agent
        if [ -n "$working_dir" ]; then
            wd="$working_dir"
            [ ! "${wd:0:1}" = "/" ] && wd="${ROOT_DIR}/${wd}"
        else
            wd="${ROOT_DIR}"
        fi
        mkdir -p "${wd}/${SHARED_DIR}"
        cp "$FILE_PATH" "${wd}/${SHARED_DIR}/"
        echo "  ${name} (local): OK"
    fi
done <<< "$AGENTS"

echo ""
echo "Done. Tell any agent: \"look at ${SHARED_DIR}/${FILENAME}\""
