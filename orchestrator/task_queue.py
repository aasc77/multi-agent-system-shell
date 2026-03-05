"""Task Queue Manager for the Multi-Agent System Shell.

Loads tasks from a JSON file, manages status transitions, and persists changes.

Requirements traced to PRD:
  - R5: Task Queue
  - Status model: pending -> in_progress -> completed | stuck

Usage::

    tq = TaskQueue("projects/demo/tasks.json")
    task = tq.get_next_pending()
    if task:
        tq.mark_in_progress(task["id"])
        # ... agent does work ...
        tq.mark_completed(task["id"])
        tq.save()
"""

import json
from pathlib import Path
from typing import Optional, Union

# ---------------------------------------------------------------------------
# Status constants -- single source of truth for task status values
# ---------------------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_STUCK = "stuck"

# Statuses that indicate a task is still active (not terminal)
_ACTIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_IN_PROGRESS})

# Valid status transitions: {current_status: {allowed_next_statuses}}
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING: frozenset({STATUS_IN_PROGRESS}),
    STATUS_IN_PROGRESS: frozenset({STATUS_COMPLETED, STATUS_STUCK}),
    STATUS_COMPLETED: frozenset(),
    STATUS_STUCK: frozenset(),
}

# JSON file structure key
_TASKS_KEY = "tasks"


class TaskQueueError(Exception):
    """Error raised for invalid task queue operations."""


class TaskQueue:
    """Manages a queue of tasks loaded from a JSON file.

    Tasks are loaded on construction and held in memory. Mutations (status
    transitions, attempt increments) modify the in-memory list; call
    :meth:`save` to persist changes back to disk.

    Args:
        file_path: Path to the tasks.json file (str or Path).

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file contains invalid JSON.
        TaskQueueError: If the JSON is missing the 'tasks' key.
    """

    def __init__(self, file_path: Union[str, Path]) -> None:
        self._file_path = Path(file_path)
        self._tasks: list[dict] = []
        self._task_index: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load tasks from the JSON file and build the lookup index."""
        data = json.loads(self._file_path.read_text())
        if _TASKS_KEY not in data:
            raise TaskQueueError(
                f"JSON file missing required '{_TASKS_KEY}' key"
            )
        self._tasks = data[_TASKS_KEY]
        self._task_index = {task["id"]: task for task in self._tasks}

    def _get_task(self, task_id: str) -> dict:
        """Return the task dict for *task_id*, or raise :exc:`TaskQueueError`."""
        try:
            return self._task_index[task_id]
        except KeyError:
            raise TaskQueueError(f"Unknown task_id: {task_id}") from None

    def _transition(self, task_id: str, target_status: str) -> None:
        """Transition a task to *target_status*, enforcing the status model.

        Raises:
            TaskQueueError: If the transition is not allowed.
        """
        task = self._get_task(task_id)
        current = task["status"]
        allowed = _VALID_TRANSITIONS.get(current, frozenset())
        if target_status not in allowed:
            raise TaskQueueError(
                f"Invalid transition: {current} -> {target_status} "
                f"for task {task_id}"
            )
        task["status"] = target_status

    # ------------------------------------------------------------------
    # Public API -- properties
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list[dict]:
        """Return the list of task dicts (mutable reference)."""
        return self._tasks

    # ------------------------------------------------------------------
    # Public API -- status transitions
    # ------------------------------------------------------------------

    def mark_in_progress(self, task_id: str) -> None:
        """Set task status to *in_progress*. Only valid from *pending*."""
        self._transition(task_id, STATUS_IN_PROGRESS)

    def mark_completed(self, task_id: str) -> None:
        """Set task status to *completed*. Only valid from *in_progress*."""
        self._transition(task_id, STATUS_COMPLETED)

    def mark_stuck(self, task_id: str) -> None:
        """Set task status to *stuck*. Only valid from *in_progress*."""
        self._transition(task_id, STATUS_STUCK)

    # ------------------------------------------------------------------
    # Public API -- attempt tracking
    # ------------------------------------------------------------------

    def increment_attempts(self, task_id: str) -> None:
        """Increment the attempts counter for a task."""
        task = self._get_task(task_id)
        task["attempts"] += 1

    def is_stuck(self, task_id: str, max_attempts: int) -> bool:
        """Return ``True`` if task's attempts >= *max_attempts*."""
        task = self._get_task(task_id)
        return task["attempts"] >= max_attempts

    # ------------------------------------------------------------------
    # Public API -- queue queries
    # ------------------------------------------------------------------

    def get_next_pending(self) -> Optional[dict]:
        """Return the first task with status *pending*, or ``None``."""
        for task in self._tasks:
            if task["status"] == STATUS_PENDING:
                return task
        return None

    def all_done(self) -> bool:
        """Return ``True`` when no tasks are *pending* or *in_progress*."""
        return all(
            task["status"] not in _ACTIVE_STATUSES
            for task in self._tasks
        )

    # ------------------------------------------------------------------
    # Public API -- persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist current task state back to the JSON file."""
        data = {_TASKS_KEY: self._tasks}
        self._file_path.write_text(json.dumps(data, indent=2))
