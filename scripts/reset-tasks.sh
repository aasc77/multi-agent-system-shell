#!/usr/bin/env bash
# reset-tasks.sh -- Reset all tasks to pending with 0 attempts
#
# Requirements traced to PRD:
#   R5: Task Queue (task statuses: pending, in_progress, completed, stuck)
#   R8: Scripts (reset-tasks.sh resets all tasks to pending, attempts to 0)
#
# Usage:
#   ./scripts/reset-tasks.sh <project>
#
# What this script does:
#   1. Validates that the project directory and tasks.json exist
#   2. Resets every task's status to "pending" and attempts to 0
#   3. Preserves all other task fields (id, title, description, etc.)

set -euo pipefail

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
readonly ROOT_DIR="$(pwd)"
readonly TASKS_FILENAME="tasks.json"
readonly RESET_STATUS="pending"
readonly RESET_ATTEMPTS=0

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
TASKS_FILE="${PROJECT_DIR}/${TASKS_FILENAME}"

# -----------------------------------------------------------------------
# Validate project directory exists
# -----------------------------------------------------------------------
if [ ! -d "$PROJECT_DIR" ]; then
    echo "Error: Project '$PROJECT' does not exist at $PROJECT_DIR" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Validate tasks.json exists
# -----------------------------------------------------------------------
if [ ! -f "$TASKS_FILE" ]; then
    echo "Error: ${TASKS_FILENAME} not found at $TASKS_FILE" >&2
    exit 1
fi

# -----------------------------------------------------------------------
# Reset tasks using Python
#   - Sets status -> RESET_STATUS and attempts -> RESET_ATTEMPTS
#   - Preserves all other fields in each task object
# -----------------------------------------------------------------------
python3 -c "
import json, sys

tasks_path = sys.argv[1]
reset_status = sys.argv[2]
reset_attempts = int(sys.argv[3])

with open(tasks_path) as f:
    data = json.load(f)

for task in data.get('tasks', []):
    task['status'] = reset_status
    task['attempts'] = reset_attempts

with open(tasks_path, 'w') as f:
    json.dump(data, f, indent=2)
" "$TASKS_FILE" "$RESET_STATUS" "$RESET_ATTEMPTS"

echo "All tasks in '$PROJECT' reset to ${RESET_STATUS} with ${RESET_ATTEMPTS} attempts."
exit 0
