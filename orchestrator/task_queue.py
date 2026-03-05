"""Task Queue Manager for the Multi-Agent System Shell.

Loads tasks from a JSON file, manages status transitions, and persists changes.

Requirements traced to PRD:
  - R5: Task Queue
  - Status model: pending -> in_progress -> completed | stuck

Usage:
    tq = TaskQueue("projects/demo/tasks.json")
    task = tq.get_next_pending()
    tq.mark_in_progress(task["id"])
    tq.mark_completed(task["id"])
    tq.save()
"""

import json
from pathlib import Path
from typing import Optional, Union

# Valid status transitions: {current_status: {allowed_next_statuses}}
_VALID_TRANSITIONS = {
    "pending": {"in_progress"},
    "in_progress": {"completed", "stuck"},
    "completed": set(),
    "stuck": set(),
}


class TaskQueueError(Exception):
    """Error raised for invalid task queue operations."""
    pass


class TaskQueue:
    """Manages a queue of tasks loaded from a JSON file.

    Args:
        file_path: Path to the tasks.json file (str or Path).

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file contains invalid JSON.
        TaskQueueError: If the JSON is missing the 'tasks' key.
    """

    def __init__(self, file_path: Union[str, Path]) -> None:
        self._file_path = Path(file_path)
        self._load()

    def _load(self) -> None:
        """Load tasks from the JSON file."""
        data = json.loads(self._file_path.read_text())
        if "tasks" not in data:
            raise TaskQueueError("JSON file missing required 'tasks' key")
        self._tasks: list[dict] = data["tasks"]

    @property
    def tasks(self) -> list[dict]:
        """Return the list of task dicts."""
        return self._tasks

    def _get_task(self, task_id: str) -> dict:
        """Find a task by ID or raise TaskQueueError."""
        for task in self._tasks:
            if task["id"] == task_id:
                return task
        raise TaskQueueError(f"Unknown task_id: {task_id}")

    def _transition(self, task_id: str, target_status: str) -> None:
        """Transition a task to target_status, enforcing valid transitions."""
        task = self._get_task(task_id)
        current = task["status"]
        allowed = _VALID_TRANSITIONS.get(current, set())
        if target_status not in allowed:
            raise TaskQueueError(
                f"Invalid transition: {current} -> {target_status} "
                f"for task {task_id}"
            )
        task["status"] = target_status

    def mark_in_progress(self, task_id: str) -> None:
        """Set task status to 'in_progress'. Only valid from 'pending'."""
        self._transition(task_id, "in_progress")

    def mark_completed(self, task_id: str) -> None:
        """Set task status to 'completed'. Only valid from 'in_progress'."""
        self._transition(task_id, "completed")

    def mark_stuck(self, task_id: str) -> None:
        """Set task status to 'stuck'. Only valid from 'in_progress'."""
        self._transition(task_id, "stuck")

    def increment_attempts(self, task_id: str) -> None:
        """Increment the attempts counter for a task."""
        task = self._get_task(task_id)
        task["attempts"] += 1

    def is_stuck(self, task_id: str, max_attempts: int) -> bool:
        """Return True if task's attempts >= max_attempts."""
        task = self._get_task(task_id)
        return task["attempts"] >= max_attempts

    def get_next_pending(self) -> Optional[dict]:
        """Return the first task with status 'pending', or None."""
        for task in self._tasks:
            if task["status"] == "pending":
                return task
        return None

    def all_done(self) -> bool:
        """Return True when no tasks are 'pending' or 'in_progress'."""
        return all(
            task["status"] not in ("pending", "in_progress")
            for task in self._tasks
        )

    def save(self) -> None:
        """Persist current task state back to the JSON file."""
        data = {"tasks": self._tasks}
        self._file_path.write_text(json.dumps(data, indent=2))
