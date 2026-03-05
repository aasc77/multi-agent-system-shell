#!/usr/bin/env bash
# reset-tasks.sh -- Reset all tasks to pending with 0 attempts
#
# Requirements traced to PRD:
#   R5: Task Queue (task statuses: pending, in_progress, completed, stuck)
#   R8: Scripts (reset-tasks.sh resets all tasks to pending, attempts to 0)
#
# Usage:
#   ./scripts/reset-tasks.sh <project>

set -euo pipefail

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
PROJECT_DIR="${ROOT_DIR}/projects/${PROJECT}"
TASKS_FILE="${PROJECT_DIR}/tasks.json"

# -----------------------------------------------------------------------
# Validate project exists
# -----------------------------------------------------------------------
if [ ! -d "$PROJECT_DIR" ]; then
    echo "Error: Project '$PROJECT' does not exist at $PROJECT_DIR" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Validate tasks.json exists
# -----------------------------------------------------------------------
if [ ! -f "$TASKS_FILE" ]; then
    echo "Error: tasks.json not found at $TASKS_FILE" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Reset tasks using Python
# -----------------------------------------------------------------------
python3 -c "
import json, sys

tasks_path = sys.argv[1]

with open(tasks_path) as f:
    data = json.load(f)

for task in data.get('tasks', []):
    task['status'] = 'pending'
    task['attempts'] = 0

with open(tasks_path, 'w') as f:
    json.dump(data, f, indent=2)
" "$TASKS_FILE"

echo "All tasks in '$PROJECT' reset to pending with 0 attempts."
exit 0
