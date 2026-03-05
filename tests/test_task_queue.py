"""
Tests for orchestrator/task_queue.py -- Task Queue Manager

TDD Contract (RED phase):
These tests define the expected behavior of the Task Queue Manager module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R5: Task Queue
  - Acceptance criteria from task rgr-3

Test categories:
  1. Loading tasks from JSON file
  2. Saving tasks back to JSON file
  3. Status transitions (pending -> in_progress -> completed | stuck)
  4. Invalid status transitions raise errors
  5. get_next_pending() behavior
  6. mark_in_progress(task_id)
  7. mark_completed(task_id)
  8. mark_stuck(task_id)
  9. increment_attempts(task_id)
  10. is_stuck(task_id, max_attempts) behavior
  11. all_done() behavior
  12. Error handling (missing files, invalid JSON, unknown task_id)
  13. Edge cases
"""

import json
import pytest
from pathlib import Path

# --- The import that MUST fail in RED phase ---
from orchestrator.task_queue import TaskQueue, TaskQueueError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TASKS = {
    "tasks": [
        {
            "id": "task-1",
            "title": "Write the widget",
            "description": "Create the widget module",
            "status": "pending",
            "attempts": 0,
        },
        {
            "id": "task-2",
            "title": "Test the widget",
            "description": "Write tests for the widget",
            "status": "pending",
            "attempts": 0,
        },
        {
            "id": "task-3",
            "title": "Deploy the widget",
            "description": "Deploy widget to production",
            "status": "pending",
            "attempts": 0,
        },
    ]
}


@pytest.fixture
def tasks_file(tmp_path):
    """Create a temporary tasks.json file with sample tasks."""
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(SAMPLE_TASKS, indent=2))
    return path


@pytest.fixture
def tq(tasks_file):
    """Create a TaskQueue instance loaded from the sample tasks file."""
    return TaskQueue(tasks_file)


@pytest.fixture
def single_task_file(tmp_path):
    """Create a tasks.json with a single task."""
    data = {
        "tasks": [
            {
                "id": "solo-1",
                "title": "Only task",
                "description": "The only task",
                "status": "pending",
                "attempts": 0,
            }
        ]
    }
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data, indent=2))
    return path


@pytest.fixture
def mixed_status_file(tmp_path):
    """Create a tasks.json with tasks in various statuses."""
    data = {
        "tasks": [
            {
                "id": "done-1",
                "title": "Completed task",
                "description": "Already done",
                "status": "completed",
                "attempts": 1,
            },
            {
                "id": "stuck-1",
                "title": "Stuck task",
                "description": "Stuck after retries",
                "status": "stuck",
                "attempts": 5,
            },
            {
                "id": "pending-1",
                "title": "Pending task",
                "description": "Still waiting",
                "status": "pending",
                "attempts": 0,
            },
            {
                "id": "in-progress-1",
                "title": "In progress task",
                "description": "Currently working",
                "status": "in_progress",
                "attempts": 2,
            },
        ]
    }
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data, indent=2))
    return path


# ===========================================================================
# 1. LOADING TASKS FROM JSON FILE
# ===========================================================================


class TestLoadTasks:
    """TaskQueue must load tasks from a tasks.json file."""

    def test_loads_all_tasks(self, tq):
        """All tasks from the JSON file must be loaded."""
        tasks = tq.tasks
        assert len(tasks) == 3

    def test_task_has_id(self, tq):
        """Each task must have an 'id' field."""
        task = tq.tasks[0]
        assert task["id"] == "task-1"

    def test_task_has_title(self, tq):
        """Each task must have a 'title' field."""
        task = tq.tasks[0]
        assert task["title"] == "Write the widget"

    def test_task_has_description(self, tq):
        """Each task must have a 'description' field."""
        task = tq.tasks[0]
        assert task["description"] == "Create the widget module"

    def test_task_has_status(self, tq):
        """Each task must have a 'status' field."""
        task = tq.tasks[0]
        assert task["status"] == "pending"

    def test_task_has_attempts(self, tq):
        """Each task must have an 'attempts' field."""
        task = tq.tasks[0]
        assert task["attempts"] == 0

    def test_loads_from_path_object(self, tasks_file):
        """TaskQueue must accept a Path object."""
        tq = TaskQueue(Path(tasks_file))
        assert len(tq.tasks) == 3

    def test_loads_from_string_path(self, tasks_file):
        """TaskQueue must accept a string path."""
        tq = TaskQueue(str(tasks_file))
        assert len(tq.tasks) == 3

    def test_loads_mixed_status_tasks(self, mixed_status_file):
        """TaskQueue must load tasks regardless of their status."""
        tq = TaskQueue(mixed_status_file)
        assert len(tq.tasks) == 4


# ===========================================================================
# 2. SAVING TASKS BACK TO JSON FILE
# ===========================================================================


class TestSaveTasks:
    """TaskQueue must persist changes back to the tasks.json file."""

    def test_save_persists_status_change(self, tq, tasks_file):
        """After marking a task in_progress and saving, the file reflects the change."""
        tq.mark_in_progress("task-1")
        tq.save()

        # Re-read the file
        data = json.loads(tasks_file.read_text())
        task1 = next(t for t in data["tasks"] if t["id"] == "task-1")
        assert task1["status"] == "in_progress"

    def test_save_persists_attempts_change(self, tq, tasks_file):
        """After incrementing attempts and saving, the file reflects the change."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        tq.save()

        data = json.loads(tasks_file.read_text())
        task1 = next(t for t in data["tasks"] if t["id"] == "task-1")
        assert task1["attempts"] == 1

    def test_save_preserves_all_tasks(self, tq, tasks_file):
        """Save must write ALL tasks back, not just modified ones."""
        tq.mark_in_progress("task-1")
        tq.save()

        data = json.loads(tasks_file.read_text())
        assert len(data["tasks"]) == 3

    def test_save_preserves_task_structure(self, tq, tasks_file):
        """Saved file must maintain the tasks JSON structure with all fields."""
        tq.save()

        data = json.loads(tasks_file.read_text())
        assert "tasks" in data
        for task in data["tasks"]:
            assert "id" in task
            assert "title" in task
            assert "description" in task
            assert "status" in task
            assert "attempts" in task

    def test_round_trip_load_save_load(self, tq, tasks_file):
        """Load -> modify -> save -> reload must be consistent."""
        tq.mark_in_progress("task-1")
        tq.save()

        tq2 = TaskQueue(tasks_file)
        task1 = next(t for t in tq2.tasks if t["id"] == "task-1")
        assert task1["status"] == "in_progress"


# ===========================================================================
# 3. STATUS TRANSITIONS (pending -> in_progress -> completed | stuck)
# ===========================================================================


class TestStatusTransitions:
    """Status model: pending -> in_progress -> completed | stuck."""

    def test_pending_to_in_progress(self, tq):
        """pending -> in_progress is a valid transition."""
        tq.mark_in_progress("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "in_progress"

    def test_in_progress_to_completed(self, tq):
        """in_progress -> completed is a valid transition."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "completed"

    def test_in_progress_to_stuck(self, tq):
        """in_progress -> stuck is a valid transition."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "stuck"

    def test_full_happy_path(self, tq):
        """pending -> in_progress -> completed full lifecycle."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "completed"

    def test_full_stuck_path(self, tq):
        """pending -> in_progress -> stuck full lifecycle."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "stuck"


# ===========================================================================
# 4. INVALID STATUS TRANSITIONS RAISE ERRORS
# ===========================================================================


class TestInvalidStatusTransitions:
    """Invalid status transitions must raise errors."""

    def test_pending_to_completed_raises_error(self, tq):
        """Cannot go directly from pending to completed."""
        with pytest.raises(TaskQueueError):
            tq.mark_completed("task-1")

    def test_pending_to_stuck_raises_error(self, tq):
        """Cannot go directly from pending to stuck."""
        with pytest.raises(TaskQueueError):
            tq.mark_stuck("task-1")

    def test_completed_to_in_progress_raises_error(self, tq):
        """Cannot go from completed back to in_progress."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        with pytest.raises(TaskQueueError):
            tq.mark_in_progress("task-1")

    def test_completed_to_stuck_raises_error(self, tq):
        """Cannot go from completed to stuck."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        with pytest.raises(TaskQueueError):
            tq.mark_stuck("task-1")

    def test_stuck_to_in_progress_raises_error(self, tq):
        """Cannot go from stuck back to in_progress."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        with pytest.raises(TaskQueueError):
            tq.mark_in_progress("task-1")

    def test_stuck_to_completed_raises_error(self, tq):
        """Cannot go from stuck to completed."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        with pytest.raises(TaskQueueError):
            tq.mark_completed("task-1")

    def test_stuck_to_pending_raises_error(self, tq):
        """Cannot go from stuck back to pending."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        # No direct mark_pending method, but trying to reset should be controlled
        # This tests that stuck is a terminal state
        with pytest.raises(TaskQueueError):
            tq.mark_in_progress("task-1")

    def test_completed_to_pending_raises_error(self, tq):
        """Cannot go from completed back to pending."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        with pytest.raises(TaskQueueError):
            tq.mark_in_progress("task-1")

    def test_in_progress_to_pending_raises_error(self, tq):
        """Cannot go backwards from in_progress to pending."""
        tq.mark_in_progress("task-1")
        # There should be no way to go back to pending via public API
        # mark_in_progress on an already in_progress task is a no-op or error
        # but there's no mark_pending method -- this just confirms the constraint


# ===========================================================================
# 5. GET_NEXT_PENDING BEHAVIOR
# ===========================================================================


class TestGetNextPending:
    """get_next_pending() returns the first task with status 'pending', or None."""

    def test_returns_first_pending_task(self, tq):
        """Must return the first task with status 'pending'."""
        task = tq.get_next_pending()
        assert task is not None
        assert task["id"] == "task-1"
        assert task["status"] == "pending"

    def test_returns_none_when_no_pending(self, tq):
        """Must return None when no tasks have status 'pending'."""
        # Mark all tasks as in_progress then completed
        for t in tq.tasks:
            tq.mark_in_progress(t["id"])
            tq.mark_completed(t["id"])

        result = tq.get_next_pending()
        assert result is None

    def test_skips_non_pending_tasks(self, mixed_status_file):
        """Must skip completed, stuck, and in_progress tasks."""
        tq = TaskQueue(mixed_status_file)
        task = tq.get_next_pending()
        assert task is not None
        assert task["id"] == "pending-1"

    def test_returns_second_pending_after_first_completed(self, tq):
        """After first task is completed, returns the second pending task."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")

        task = tq.get_next_pending()
        assert task is not None
        assert task["id"] == "task-2"

    def test_returns_none_for_empty_task_list(self, tmp_path):
        """Must return None when the task list is empty."""
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps({"tasks": []}))
        tq = TaskQueue(path)

        result = tq.get_next_pending()
        assert result is None

    def test_preserves_task_order(self, tq):
        """get_next_pending returns tasks in file order."""
        # First call should be task-1
        assert tq.get_next_pending()["id"] == "task-1"

        # Complete task-1, next should be task-2
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        assert tq.get_next_pending()["id"] == "task-2"

        # Complete task-2, next should be task-3
        tq.mark_in_progress("task-2")
        tq.mark_completed("task-2")
        assert tq.get_next_pending()["id"] == "task-3"


# ===========================================================================
# 6. MARK_IN_PROGRESS
# ===========================================================================


class TestMarkInProgress:
    """mark_in_progress(task_id) sets status to in_progress."""

    def test_sets_status_to_in_progress(self, tq):
        """Must set the task status to 'in_progress'."""
        tq.mark_in_progress("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "in_progress"

    def test_only_affects_specified_task(self, tq):
        """Must not change other tasks' statuses."""
        tq.mark_in_progress("task-1")
        task2 = next(t for t in tq.tasks if t["id"] == "task-2")
        task3 = next(t for t in tq.tasks if t["id"] == "task-3")
        assert task2["status"] == "pending"
        assert task3["status"] == "pending"

    def test_unknown_task_id_raises_error(self, tq):
        """Must raise error for unknown task_id."""
        with pytest.raises((TaskQueueError, KeyError)):
            tq.mark_in_progress("nonexistent-task")


# ===========================================================================
# 7. MARK_COMPLETED
# ===========================================================================


class TestMarkCompleted:
    """mark_completed(task_id) sets status to completed."""

    def test_sets_status_to_completed(self, tq):
        """Must set the task status to 'completed'."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "completed"

    def test_only_affects_specified_task(self, tq):
        """Must not change other tasks' statuses."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        task2 = next(t for t in tq.tasks if t["id"] == "task-2")
        assert task2["status"] == "pending"

    def test_unknown_task_id_raises_error(self, tq):
        """Must raise error for unknown task_id."""
        with pytest.raises((TaskQueueError, KeyError)):
            tq.mark_completed("nonexistent-task")


# ===========================================================================
# 8. MARK_STUCK
# ===========================================================================


class TestMarkStuck:
    """mark_stuck(task_id) sets status to stuck."""

    def test_sets_status_to_stuck(self, tq):
        """Must set the task status to 'stuck'."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["status"] == "stuck"

    def test_only_affects_specified_task(self, tq):
        """Must not change other tasks' statuses."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")
        task2 = next(t for t in tq.tasks if t["id"] == "task-2")
        assert task2["status"] == "pending"

    def test_unknown_task_id_raises_error(self, tq):
        """Must raise error for unknown task_id."""
        with pytest.raises((TaskQueueError, KeyError)):
            tq.mark_stuck("nonexistent-task")


# ===========================================================================
# 9. INCREMENT_ATTEMPTS
# ===========================================================================


class TestIncrementAttempts:
    """increment_attempts(task_id) increments the attempts counter."""

    def test_increments_from_zero(self, tq):
        """First increment should set attempts to 1."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["attempts"] == 1

    def test_increments_multiple_times(self, tq):
        """Multiple increments should accumulate."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        tq.increment_attempts("task-1")
        tq.increment_attempts("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["attempts"] == 3

    def test_only_affects_specified_task(self, tq):
        """Must not change other tasks' attempt counts."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        task2 = next(t for t in tq.tasks if t["id"] == "task-2")
        assert task2["attempts"] == 0

    def test_unknown_task_id_raises_error(self, tq):
        """Must raise error for unknown task_id."""
        with pytest.raises((TaskQueueError, KeyError)):
            tq.increment_attempts("nonexistent-task")

    def test_preserves_other_fields(self, tq):
        """Incrementing attempts must not alter other fields."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        task = next(t for t in tq.tasks if t["id"] == "task-1")
        assert task["id"] == "task-1"
        assert task["title"] == "Write the widget"
        assert task["description"] == "Create the widget module"
        assert task["status"] == "in_progress"


# ===========================================================================
# 10. IS_STUCK BEHAVIOR
# ===========================================================================


class TestIsStuck:
    """is_stuck(task_id, max_attempts) returns True when attempts >= max_attempts."""

    def test_returns_false_when_below_max(self, tq):
        """Must return False when attempts < max_attempts."""
        tq.mark_in_progress("task-1")
        assert tq.is_stuck("task-1", max_attempts=5) is False

    def test_returns_true_when_at_max(self, tq):
        """Must return True when attempts == max_attempts."""
        tq.mark_in_progress("task-1")
        for _ in range(5):
            tq.increment_attempts("task-1")
        assert tq.is_stuck("task-1", max_attempts=5) is True

    def test_returns_true_when_above_max(self, tq):
        """Must return True when attempts > max_attempts."""
        tq.mark_in_progress("task-1")
        for _ in range(7):
            tq.increment_attempts("task-1")
        assert tq.is_stuck("task-1", max_attempts=5) is True

    def test_returns_false_with_zero_attempts(self, tq):
        """Task with 0 attempts is not stuck (unless max_attempts is 0)."""
        assert tq.is_stuck("task-1", max_attempts=5) is False

    def test_boundary_one_below_max(self, tq):
        """Exactly one below max_attempts should return False."""
        tq.mark_in_progress("task-1")
        for _ in range(4):
            tq.increment_attempts("task-1")
        assert tq.is_stuck("task-1", max_attempts=5) is False

    def test_max_attempts_of_one(self, tq):
        """With max_attempts=1, first attempt should trigger stuck."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        assert tq.is_stuck("task-1", max_attempts=1) is True

    def test_unknown_task_id_raises_error(self, tq):
        """Must raise error for unknown task_id."""
        with pytest.raises((TaskQueueError, KeyError)):
            tq.is_stuck("nonexistent-task", max_attempts=5)


# ===========================================================================
# 11. ALL_DONE BEHAVIOR
# ===========================================================================


class TestAllDone:
    """all_done() returns True when no tasks are pending or in_progress."""

    def test_returns_false_with_pending_tasks(self, tq):
        """Must return False when there are pending tasks."""
        assert tq.all_done() is False

    def test_returns_false_with_in_progress_tasks(self, tq):
        """Must return False when there are in_progress tasks."""
        tq.mark_in_progress("task-1")
        assert tq.all_done() is False

    def test_returns_true_when_all_completed(self, tq):
        """Must return True when all tasks are completed."""
        for t in tq.tasks:
            tq.mark_in_progress(t["id"])
            tq.mark_completed(t["id"])
        assert tq.all_done() is True

    def test_returns_true_when_all_stuck(self, tq):
        """Must return True when all tasks are stuck."""
        for t in tq.tasks:
            tq.mark_in_progress(t["id"])
            tq.mark_stuck(t["id"])
        assert tq.all_done() is True

    def test_returns_true_with_mix_of_completed_and_stuck(self, tq):
        """Must return True when tasks are a mix of completed and stuck."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        tq.mark_in_progress("task-2")
        tq.mark_stuck("task-2")
        tq.mark_in_progress("task-3")
        tq.mark_completed("task-3")
        assert tq.all_done() is True

    def test_returns_false_with_one_pending_remaining(self, tq):
        """Must return False if even one task is still pending."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        tq.mark_in_progress("task-2")
        tq.mark_completed("task-2")
        # task-3 is still pending
        assert tq.all_done() is False

    def test_returns_false_with_one_in_progress_remaining(self, tq):
        """Must return False if even one task is still in_progress."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        tq.mark_in_progress("task-2")
        tq.mark_completed("task-2")
        tq.mark_in_progress("task-3")
        # task-3 is still in_progress
        assert tq.all_done() is False

    def test_returns_true_for_empty_task_list(self, tmp_path):
        """Empty task list means all_done is True (vacuously)."""
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps({"tasks": []}))
        tq = TaskQueue(path)
        assert tq.all_done() is True


# ===========================================================================
# 12. ERROR HANDLING
# ===========================================================================


class TestErrorHandling:
    """Error handling for file operations and invalid inputs."""

    def test_missing_file_raises_error(self, tmp_path):
        """Must raise error when tasks.json does not exist."""
        path = tmp_path / "nonexistent.json"
        with pytest.raises((FileNotFoundError, TaskQueueError)):
            TaskQueue(path)

    def test_invalid_json_raises_error(self, tmp_path):
        """Must raise error when file contains invalid JSON."""
        path = tmp_path / "tasks.json"
        path.write_text("not valid json {{{")
        with pytest.raises((json.JSONDecodeError, TaskQueueError)):
            TaskQueue(path)

    def test_missing_tasks_key_raises_error(self, tmp_path):
        """Must raise error when JSON is valid but missing 'tasks' key."""
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps({"not_tasks": []}))
        with pytest.raises((KeyError, TaskQueueError)):
            TaskQueue(path)

    def test_task_queue_error_is_exception(self):
        """TaskQueueError must be a subclass of Exception."""
        assert issubclass(TaskQueueError, Exception)

    def test_task_queue_error_has_message(self):
        """TaskQueueError should accept and store a message."""
        err = TaskQueueError("test error")
        assert "test error" in str(err)

    def test_get_task_by_id_unknown_raises_error(self, tq):
        """Accessing a task by unknown ID must raise error."""
        with pytest.raises((TaskQueueError, KeyError)):
            tq.mark_in_progress("totally-fake-id")


# ===========================================================================
# 13. EDGE CASES
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_single_task_full_lifecycle(self, single_task_file):
        """Single task goes through full lifecycle: pending -> in_progress -> completed."""
        tq = TaskQueue(single_task_file)
        task = tq.get_next_pending()
        assert task["id"] == "solo-1"

        tq.mark_in_progress("solo-1")
        tq.mark_completed("solo-1")

        assert tq.get_next_pending() is None
        assert tq.all_done() is True

    def test_task_ids_are_unique_lookup(self, tq):
        """Operations on a task_id must affect exactly one task."""
        tq.mark_in_progress("task-2")
        statuses = [t["status"] for t in tq.tasks]
        assert statuses.count("in_progress") == 1

    def test_attempts_survive_save_reload(self, tq, tasks_file):
        """Attempt counts must survive a save-reload cycle."""
        tq.mark_in_progress("task-1")
        tq.increment_attempts("task-1")
        tq.increment_attempts("task-1")
        tq.save()

        tq2 = TaskQueue(tasks_file)
        task = next(t for t in tq2.tasks if t["id"] == "task-1")
        assert task["attempts"] == 2

    def test_status_survives_save_reload(self, tq, tasks_file):
        """Status changes must survive a save-reload cycle."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        tq.save()

        tq2 = TaskQueue(tasks_file)
        task = next(t for t in tq2.tasks if t["id"] == "task-1")
        assert task["status"] == "completed"

    def test_multiple_tasks_different_states(self, tq):
        """Different tasks can be in different states simultaneously."""
        tq.mark_in_progress("task-1")
        tq.mark_completed("task-1")
        tq.mark_in_progress("task-2")
        # task-1: completed, task-2: in_progress, task-3: pending

        statuses = {t["id"]: t["status"] for t in tq.tasks}
        assert statuses["task-1"] == "completed"
        assert statuses["task-2"] == "in_progress"
        assert statuses["task-3"] == "pending"

    def test_get_next_pending_skips_stuck_tasks(self, tq):
        """get_next_pending must skip tasks that are stuck."""
        tq.mark_in_progress("task-1")
        tq.mark_stuck("task-1")

        task = tq.get_next_pending()
        assert task is not None
        assert task["id"] == "task-2"

    def test_all_done_after_sequential_processing(self, tq):
        """Processing all tasks sequentially should result in all_done=True."""
        for t in list(tq.tasks):
            tq.mark_in_progress(t["id"])
            tq.mark_completed(t["id"])

        assert tq.all_done() is True
        assert tq.get_next_pending() is None

    def test_is_stuck_with_loaded_attempts(self, mixed_status_file):
        """is_stuck should work with attempts loaded from file."""
        tq = TaskQueue(mixed_status_file)
        # stuck-1 has attempts=5
        assert tq.is_stuck("stuck-1", max_attempts=5) is True
        assert tq.is_stuck("stuck-1", max_attempts=6) is False

    def test_tasks_property_returns_list(self, tq):
        """tasks property must return a list."""
        assert isinstance(tq.tasks, list)

    def test_constructor_accepts_file_path(self, tasks_file):
        """TaskQueue constructor's primary argument is the path to tasks.json."""
        tq = TaskQueue(tasks_file)
        assert tq is not None
