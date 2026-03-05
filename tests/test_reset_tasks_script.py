"""
Tests for scripts/reset-tasks.sh -- Task Reset Script

TDD Contract (RED phase):
These tests define the expected behavior of the reset-tasks.sh utility script.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R5: Task Queue (task statuses: pending, in_progress, completed, stuck)
  - R8: Scripts (reset-tasks.sh resets all tasks to pending, attempts to 0)

Acceptance criteria from task rgr-12:
  1. reset-tasks.sh resets all task statuses to pending and attempts to 0

Test categories:
  1. Script existence and executability
  2. Reset task statuses to pending
  3. Reset task attempts to 0
  4. Preserves other task fields (id, title, description)
  5. Handles multiple tasks
  6. Missing/invalid arguments
  7. Error handling (missing tasks.json)
  8. Exit codes
"""

import json
import os
import stat
import subprocess

import pytest
import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
RESET_TASKS_SCRIPT = os.path.join(REPO_ROOT, "scripts", "reset-tasks.sh")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_reset_tasks(args=None, env_overrides=None, cwd=None):
    """Helper to run reset-tasks.sh and capture output."""
    cmd = [RESET_TASKS_SCRIPT]
    if args:
        if isinstance(args, str):
            cmd.append(args)
        else:
            cmd.extend(args)

    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or REPO_ROOT,
        timeout=30,
    )
    return result


def _read_tasks(tasks_path):
    """Read tasks from a tasks.json file."""
    with open(tasks_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_with_tasks(tmp_path):
    """Create a project with tasks in various states."""
    project_dir = tmp_path / "projects" / "testproj"
    project_dir.mkdir(parents=True)

    tasks = {
        "tasks": [
            {
                "id": "task-1",
                "title": "First task",
                "description": "Do the first thing",
                "status": "completed",
                "attempts": 3,
            },
            {
                "id": "task-2",
                "title": "Second task",
                "description": "Do the second thing",
                "status": "stuck",
                "attempts": 5,
            },
            {
                "id": "task-3",
                "title": "Third task",
                "description": "Do the third thing",
                "status": "in_progress",
                "attempts": 1,
            },
            {
                "id": "task-4",
                "title": "Fourth task",
                "description": "Already pending but with attempts",
                "status": "pending",
                "attempts": 2,
            },
        ]
    }

    tasks_path = project_dir / "tasks.json"
    with open(tasks_path, "w") as f:
        json.dump(tasks, f, indent=2)

    # Create project config
    config = {
        "project": "testproj",
        "tmux": {"session_name": "testproj"},
        "agents": {},
        "state_machine": {
            "initial": "idle",
            "states": {"idle": {}},
            "transitions": [
                {"from": "idle", "to": "idle", "trigger": "task_assigned"},
            ],
        },
    }
    with open(project_dir / "config.yaml", "w") as f:
        yaml.dump(config, f)

    return {
        "root": tmp_path,
        "project_dir": project_dir,
        "tasks_path": tasks_path,
        "original_tasks": tasks,
    }


@pytest.fixture
def project_single_task(tmp_path):
    """Create a project with a single completed task."""
    project_dir = tmp_path / "projects" / "single"
    project_dir.mkdir(parents=True)

    tasks = {
        "tasks": [
            {
                "id": "task-1",
                "title": "Only task",
                "description": "The only task",
                "status": "completed",
                "attempts": 2,
            },
        ]
    }

    tasks_path = project_dir / "tasks.json"
    with open(tasks_path, "w") as f:
        json.dump(tasks, f, indent=2)

    config = {
        "project": "single",
        "tmux": {"session_name": "single"},
        "agents": {},
        "state_machine": {
            "initial": "idle",
            "states": {"idle": {}},
            "transitions": [
                {"from": "idle", "to": "idle", "trigger": "task_assigned"},
            ],
        },
    }
    with open(project_dir / "config.yaml", "w") as f:
        yaml.dump(config, f)

    return {
        "root": tmp_path,
        "project_dir": project_dir,
        "tasks_path": tasks_path,
    }


# ===========================================================================
# 1. SCRIPT EXISTENCE AND EXECUTABILITY
# ===========================================================================


class TestResetTasksScriptExists:
    """reset-tasks.sh must exist at scripts/reset-tasks.sh and be executable."""

    def test_reset_tasks_script_exists(self):
        """scripts/reset-tasks.sh must exist."""
        assert os.path.isfile(RESET_TASKS_SCRIPT), (
            f"scripts/reset-tasks.sh not found at {RESET_TASKS_SCRIPT}"
        )

    def test_reset_tasks_script_is_executable(self):
        """scripts/reset-tasks.sh must have execute permissions."""
        assert os.path.isfile(RESET_TASKS_SCRIPT), "Script must exist first"
        mode = os.stat(RESET_TASKS_SCRIPT).st_mode
        assert mode & stat.S_IXUSR, "scripts/reset-tasks.sh must be executable by owner"

    def test_reset_tasks_script_has_shebang(self):
        """scripts/reset-tasks.sh must start with a proper shebang line."""
        assert os.path.isfile(RESET_TASKS_SCRIPT), "Script must exist first"
        with open(RESET_TASKS_SCRIPT, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!"), (
            f"Expected shebang, got: {first_line}"
        )
        assert "bash" in first_line or "sh" in first_line, (
            f"Shebang must reference bash or sh, got: {first_line}"
        )


# ===========================================================================
# 2. RESET TASK STATUSES TO PENDING
# ===========================================================================


class TestResetStatuses:
    """reset-tasks.sh must reset all task statuses to 'pending'."""

    def test_resets_completed_to_pending(self, project_with_tasks):
        """Completed tasks must be reset to pending."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task1 = next(t for t in tasks["tasks"] if t["id"] == "task-1")
        assert task1["status"] == "pending", (
            f"task-1 (was completed) must be reset to pending, got: {task1['status']}"
        )

    def test_resets_stuck_to_pending(self, project_with_tasks):
        """Stuck tasks must be reset to pending."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task2 = next(t for t in tasks["tasks"] if t["id"] == "task-2")
        assert task2["status"] == "pending", (
            f"task-2 (was stuck) must be reset to pending, got: {task2['status']}"
        )

    def test_resets_in_progress_to_pending(self, project_with_tasks):
        """In-progress tasks must be reset to pending."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task3 = next(t for t in tasks["tasks"] if t["id"] == "task-3")
        assert task3["status"] == "pending", (
            f"task-3 (was in_progress) must be reset to pending, got: {task3['status']}"
        )

    def test_already_pending_stays_pending(self, project_with_tasks):
        """Tasks already pending must remain pending."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task4 = next(t for t in tasks["tasks"] if t["id"] == "task-4")
        assert task4["status"] == "pending", (
            f"task-4 (was pending) must remain pending, got: {task4['status']}"
        )

    def test_all_tasks_are_pending(self, project_with_tasks):
        """ALL tasks must have status 'pending' after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        for task in tasks["tasks"]:
            assert task["status"] == "pending", (
                f"Task {task['id']} must be pending, got: {task['status']}"
            )


# ===========================================================================
# 3. RESET TASK ATTEMPTS TO 0
# ===========================================================================


class TestResetAttempts:
    """reset-tasks.sh must reset all task attempts to 0."""

    def test_resets_attempts_to_zero(self, project_with_tasks):
        """All tasks must have attempts set to 0 after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        for task in tasks["tasks"]:
            assert task["attempts"] == 0, (
                f"Task {task['id']} must have attempts=0, got: {task['attempts']}"
            )

    def test_resets_high_attempt_count(self, project_with_tasks):
        """Task with attempts=5 must be reset to 0."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task2 = next(t for t in tasks["tasks"] if t["id"] == "task-2")
        assert task2["attempts"] == 0, (
            f"task-2 (had attempts=5) must be reset to 0, got: {task2['attempts']}"
        )


# ===========================================================================
# 4. PRESERVES OTHER TASK FIELDS
# ===========================================================================


class TestPreservesFields:
    """reset-tasks.sh must preserve id, title, description fields."""

    def test_preserves_task_id(self, project_with_tasks):
        """Task IDs must be preserved after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        ids = [t["id"] for t in tasks["tasks"]]
        assert "task-1" in ids
        assert "task-2" in ids
        assert "task-3" in ids
        assert "task-4" in ids

    def test_preserves_task_title(self, project_with_tasks):
        """Task titles must be preserved after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task1 = next(t for t in tasks["tasks"] if t["id"] == "task-1")
        assert task1["title"] == "First task", (
            f"Task title must be preserved, got: {task1['title']}"
        )

    def test_preserves_task_description(self, project_with_tasks):
        """Task descriptions must be preserved after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        task1 = next(t for t in tasks["tasks"] if t["id"] == "task-1")
        assert task1["description"] == "Do the first thing", (
            f"Task description must be preserved, got: {task1['description']}"
        )

    def test_preserves_task_count(self, project_with_tasks):
        """Number of tasks must be preserved after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        assert len(tasks["tasks"]) == 4, (
            f"Must preserve all 4 tasks, got: {len(tasks['tasks'])}"
        )


# ===========================================================================
# 5. HANDLES MULTIPLE TASKS
# ===========================================================================


class TestMultipleTasks:
    """reset-tasks.sh must handle projects with varying numbers of tasks."""

    def test_resets_single_task(self, project_single_task):
        """Must work correctly with a single task."""
        _run_reset_tasks(
            "single",
            cwd=str(project_single_task["root"]),
        )

        tasks = _read_tasks(project_single_task["tasks_path"])
        assert len(tasks["tasks"]) == 1
        assert tasks["tasks"][0]["status"] == "pending"
        assert tasks["tasks"][0]["attempts"] == 0

    def test_resets_all_four_tasks(self, project_with_tasks):
        """Must reset all 4 tasks correctly."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        tasks = _read_tasks(project_with_tasks["tasks_path"])
        assert len(tasks["tasks"]) == 4
        for task in tasks["tasks"]:
            assert task["status"] == "pending"
            assert task["attempts"] == 0


# ===========================================================================
# 6. MISSING/INVALID ARGUMENTS
# ===========================================================================


class TestResetTasksArguments:
    """reset-tasks.sh must handle missing/invalid arguments."""

    def test_no_arguments_shows_usage(self):
        """Running reset-tasks.sh without arguments must show usage info."""
        result = subprocess.run(
            [RESET_TASKS_SCRIPT],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
        output = (result.stdout + result.stderr).lower()
        assert result.returncode == 1, "Must exit 1 when no project argument given"
        assert "usage" in output or "project" in output, (
            f"Must show usage message, got: {output}"
        )

    def test_invalid_project_fails_gracefully(self):
        """Invalid project name must produce a clear error."""
        result = _run_reset_tasks("nonexistent_project_xyz_99")
        assert result.returncode == 1, (
            f"Expected exit code 1 for invalid project, got {result.returncode}"
        )
        output = (result.stdout + result.stderr).lower()
        assert any(word in output for word in ["not found", "does not exist", "error", "no such"]), (
            f"Must show clear error for invalid project, got: {output}"
        )


# ===========================================================================
# 7. ERROR HANDLING (MISSING tasks.json)
# ===========================================================================


class TestErrorHandling:
    """reset-tasks.sh must handle error cases gracefully."""

    def test_missing_tasks_json(self, tmp_path):
        """Must fail gracefully when tasks.json doesn't exist."""
        project_dir = tmp_path / "projects" / "notasks"
        project_dir.mkdir(parents=True)

        config = {
            "project": "notasks",
            "tmux": {"session_name": "notasks"},
        }
        with open(project_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)

        result = _run_reset_tasks("notasks", cwd=str(tmp_path))
        assert result.returncode != 0, (
            "Must fail when tasks.json is missing"
        )
        output = (result.stdout + result.stderr).lower()
        assert any(word in output for word in ["tasks.json", "not found", "error", "no such"]), (
            f"Must indicate tasks.json is missing, got: {output}"
        )

    def test_valid_json_output(self, project_with_tasks):
        """Output tasks.json must be valid JSON after reset."""
        _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )

        # json.load will raise if invalid
        tasks = _read_tasks(project_with_tasks["tasks_path"])
        assert isinstance(tasks, dict), "tasks.json must contain a JSON object"
        assert "tasks" in tasks, "tasks.json must have a 'tasks' key"
        assert isinstance(tasks["tasks"], list), "tasks must be a list"


# ===========================================================================
# 8. EXIT CODES
# ===========================================================================


class TestResetTasksExitCodes:
    """reset-tasks.sh exit code behavior."""

    def test_exit_0_on_success(self, project_with_tasks):
        """Must exit 0 when tasks are successfully reset."""
        result = _run_reset_tasks(
            "testproj",
            cwd=str(project_with_tasks["root"]),
        )
        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}. stderr: {result.stderr}"
        )

    def test_exit_1_on_no_arguments(self):
        """Must exit 1 when called without arguments."""
        result = subprocess.run(
            [RESET_TASKS_SCRIPT],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
        assert result.returncode == 1

    def test_exit_1_on_missing_project(self):
        """Must exit 1 when project doesn't exist."""
        result = _run_reset_tasks("does_not_exist_12345")
        assert result.returncode == 1
