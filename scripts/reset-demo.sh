#!/usr/bin/env bash
# reset-demo.sh -- Reset a project for clean testing
#
# Usage:
#   ./scripts/reset-demo.sh [project]   (default: demo)

set -euo pipefail

PROJECT="${1:-demo}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="${ROOT_DIR}/projects/${PROJECT}"
TASKS_FILE="${PROJECT_DIR}/tasks.json"

echo "Resetting project: ${PROJECT}"

# 1. Kill tmux session
if tmux has-session -t "$PROJECT" 2>/dev/null; then
    tmux kill-session -t "$PROJECT"
    echo "  Killed tmux session: ${PROJECT}"
fi

# 2. Clear CLAUDECODE from tmux server
tmux set-environment -g -u CLAUDECODE 2>/dev/null || true
echo "  Cleared CLAUDECODE from tmux env"

# 3. Clean NATS stream
if command -v nats &>/dev/null; then
    nats stream rm AGENTS --force 2>/dev/null && echo "  Deleted NATS stream: AGENTS" || echo "  No NATS stream to delete"
fi

# 4. Reset tasks.json -- set all statuses to pending, attempts to 0
if [ -f "$TASKS_FILE" ]; then
    python3 -c "
import json
with open('${TASKS_FILE}') as f:
    data = json.load(f)
for task in data.get('tasks', []):
    task['status'] = 'pending'
    task['attempts'] = 0
with open('${TASKS_FILE}', 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
"
    echo "  Reset tasks to pending: ${TASKS_FILE}"
fi

# 5. Remove orchestrator log
if [ -f "${PROJECT_DIR}/orchestrator.log" ]; then
    rm "${PROJECT_DIR}/orchestrator.log"
    echo "  Removed orchestrator.log"
fi

# 6. Remove session report
if [ -f "${PROJECT_DIR}/session-report.md" ]; then
    rm "${PROJECT_DIR}/session-report.md"
    echo "  Removed session-report.md"
fi

echo ""
echo "Ready. Run: ./scripts/start.sh ${PROJECT}"
