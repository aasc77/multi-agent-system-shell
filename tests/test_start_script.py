"""
Tests for scripts/start.sh -- Main Launch Script

TDD Contract (RED phase):
These tests define the expected behavior of the start.sh launch script.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R1: tmux Session Layout (control + agents windows, pane-border-status)
  - R2: Config-Driven Agents (MCP config generation, runtimes)
  - R8: Scripts (start.sh preflight, idempotent, NATS auto-start, exit codes)

Acceptance criteria from task rgr-11:
  1. Preflight checks for tmux, python3, nats-server; fails with clear error listing all missing
  2. Auto-starts NATS via setup-nats.sh if not running
  3. Idempotent: kills existing tmux session before creating new one
  4. Creates 'control' window with orchestrator + nats-monitor panes side-by-side
  5. Creates 'agents' window with one pane per agent in tiled layout
  6. Pane titles enabled via pane-border-status top
  7. Generates .mcp-configs/<agent>.json for each claude_code agent with correct env vars
  8. Launches Claude Code agents with claude --mcp-config <path>
  9. Launches script agents with their configured command
  10. SSH support: panes for agents with ssh_host SSH into the host before launching
  11. Exit code 0 on success, 1 on failure

Test categories:
  1. Script existence and executability
  2. Preflight checks -- detect missing tools
  3. NATS auto-start
  4. Idempotent tmux session creation
  5. Control window layout
  6. Agents window layout
  7. Pane border status
  8. MCP config generation
  9. Claude Code agent launching
  10. Script agent launching
  11. SSH remote agent support
  12. Exit codes and error handling
  13. Missing/invalid arguments
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap

import pytest
import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# We'll resolve the script path relative to the repo root.
# In the worktree, the repo root is the worktree root.
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
START_SCRIPT = os.path.join(REPO_ROOT, "scripts", "start.sh")
SETUP_NATS_SCRIPT = os.path.join(REPO_ROOT, "scripts", "setup-nats.sh")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary project structure for testing start.sh."""
    # Create project directory
    project_dir = tmp_path / "projects" / "testproj"
    project_dir.mkdir(parents=True)

    # Create project config
    project_config = {
        "project": "testproj",
        "tmux": {"session_name": "testproj"},
        "agents": {
            "writer": {
                "runtime": "claude_code",
                "working_dir": str(tmp_path / "workspace"),
                "system_prompt": "You are a writer.",
            },
            "executor": {
                "runtime": "script",
                "command": "python3 agents/echo_agent.py --role executor",
            },
        },
        "state_machine": {
            "initial": "idle",
            "states": {
                "idle": {"description": "No active task"},
                "waiting_writer": {"agent": "writer"},
                "waiting_executor": {"agent": "executor"},
            },
            "transitions": [
                {
                    "from": "idle",
                    "to": "waiting_writer",
                    "trigger": "task_assigned",
                    "action": "assign_to_agent",
                    "action_args": {"target_agent": "writer"},
                },
            ],
        },
    }
    config_path = project_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(project_config, f)

    # Create global config
    global_config = {
        "nats": {
            "url": "nats://localhost:4222",
            "stream": "AGENTS",
            "subjects_prefix": "agents",
        },
        "tmux": {
            "nudge_prompt": "You have new messages. Use check_messages with your role.",
            "nudge_cooldown_seconds": 30,
            "max_nudge_retries": 20,
        },
        "tasks": {"max_attempts_per_task": 5},
    }
    global_config_path = tmp_path / "config.yaml"
    with open(global_config_path, "w") as f:
        yaml.dump(global_config, f)

    # Create tasks.json
    tasks = {
        "tasks": [
            {
                "id": "task-1",
                "title": "Test task",
                "description": "A test task",
                "status": "pending",
                "attempts": 0,
            }
        ]
    }
    tasks_path = project_dir / "tasks.json"
    with open(tasks_path, "w") as f:
        json.dump(tasks, f)

    # Create workspace dir
    (tmp_path / "workspace").mkdir(exist_ok=True)

    return {
        "root": tmp_path,
        "project_dir": project_dir,
        "config_path": config_path,
        "global_config_path": global_config_path,
        "project_config": project_config,
        "global_config": global_config,
    }


@pytest.fixture
def three_agent_project(tmp_path):
    """Create a project with 3 agents (qa, dev, refactor)."""
    project_dir = tmp_path / "projects" / "rgr"
    project_dir.mkdir(parents=True)

    config = {
        "project": "rgr",
        "tmux": {"session_name": "rgr"},
        "agents": {
            "qa": {"runtime": "claude_code", "working_dir": str(tmp_path / "qa")},
            "dev": {"runtime": "claude_code", "working_dir": str(tmp_path / "dev")},
            "refactor": {"runtime": "claude_code", "working_dir": str(tmp_path / "ref")},
        },
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

    global_config = {
        "nats": {"url": "nats://localhost:4222"},
        "tmux": {"nudge_cooldown_seconds": 30, "max_nudge_retries": 20},
    }
    with open(tmp_path / "config.yaml", "w") as f:
        yaml.dump(global_config, f)

    for d in ["qa", "dev", "ref"]:
        (tmp_path / d).mkdir(exist_ok=True)

    return {"root": tmp_path, "project_dir": project_dir, "config": config}


@pytest.fixture
def ssh_agent_project(tmp_path):
    """Create a project with an SSH remote agent."""
    project_dir = tmp_path / "projects" / "remote"
    project_dir.mkdir(parents=True)

    config = {
        "project": "remote",
        "tmux": {"session_name": "remote"},
        "agents": {
            "local_writer": {
                "runtime": "claude_code",
                "working_dir": str(tmp_path / "writer"),
            },
            "remote_reviewer": {
                "runtime": "claude_code",
                "working_dir": "/home/user/project",
                "ssh_host": "dgx1.local",
            },
            "remote_script": {
                "runtime": "script",
                "command": "python3 agents/echo_agent.py",
                "ssh_host": "dgx2.local",
            },
        },
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

    global_config = {
        "nats": {"url": "nats://localhost:4222"},
        "tmux": {"nudge_cooldown_seconds": 30, "max_nudge_retries": 20},
    }
    with open(tmp_path / "config.yaml", "w") as f:
        yaml.dump(global_config, f)

    (tmp_path / "writer").mkdir(exist_ok=True)

    return {"root": tmp_path, "project_dir": project_dir, "config": config}


def _run_start_script(project_name, env_overrides=None, cwd=None):
    """Helper to run start.sh and capture output."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [START_SCRIPT, project_name],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or REPO_ROOT,
        timeout=30,
    )
    return result


# ===========================================================================
# 1. SCRIPT EXISTENCE AND EXECUTABILITY
# ===========================================================================


class TestScriptExists:
    """start.sh must exist at scripts/start.sh and be executable."""

    def test_start_script_exists(self):
        """scripts/start.sh must exist."""
        assert os.path.isfile(START_SCRIPT), (
            f"scripts/start.sh not found at {START_SCRIPT}"
        )

    def test_start_script_is_executable(self):
        """scripts/start.sh must have execute permissions."""
        assert os.path.isfile(START_SCRIPT), "Script must exist first"
        mode = os.stat(START_SCRIPT).st_mode
        assert mode & stat.S_IXUSR, "scripts/start.sh must be executable by owner"

    def test_start_script_has_shebang(self):
        """scripts/start.sh must start with a proper shebang line."""
        assert os.path.isfile(START_SCRIPT), "Script must exist first"
        with open(START_SCRIPT, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!"), (
            f"Expected shebang, got: {first_line}"
        )
        assert "bash" in first_line or "sh" in first_line, (
            f"Shebang must reference bash or sh, got: {first_line}"
        )


# ===========================================================================
# 2. PREFLIGHT CHECKS -- DETECT MISSING TOOLS
# ===========================================================================


class TestPreflightChecks:
    """start.sh must check for tmux, python3, nats-server before proceeding."""

    def test_preflight_checks_for_tmux(self):
        """start.sh must verify tmux is installed."""
        # Create a PATH that excludes tmux
        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"  # Minimal PATH unlikely to have tmux
        # We override the command check by using a wrapper that removes tmux
        env["_TEST_MISSING_TOOLS"] = "tmux"

        result = _run_start_script("demo", env_overrides=env)
        # If tmux is truly missing, should fail
        # The test validates the script checks for it
        assert "tmux" in result.stderr.lower() or "tmux" in result.stdout.lower() or result.returncode == 1

    def test_preflight_checks_for_python3(self):
        """start.sh must verify python3 is installed."""
        env = os.environ.copy()
        env["_TEST_MISSING_TOOLS"] = "python3"

        result = _run_start_script("demo", env_overrides=env)
        assert "python3" in result.stderr.lower() or "python3" in result.stdout.lower() or result.returncode == 1

    def test_preflight_checks_for_nats_server(self):
        """start.sh must verify nats-server is installed."""
        env = os.environ.copy()
        env["_TEST_MISSING_TOOLS"] = "nats-server"

        result = _run_start_script("demo", env_overrides=env)
        assert "nats-server" in result.stderr.lower() or "nats-server" in result.stdout.lower() or result.returncode == 1

    def test_preflight_lists_all_missing_tools(self):
        """When multiple tools are missing, ALL must be listed in the error."""
        env = os.environ.copy()
        env["_TEST_MISSING_TOOLS"] = "tmux,python3,nats-server"

        result = _run_start_script("demo", env_overrides=env)
        output = (result.stdout + result.stderr).lower()
        assert result.returncode == 1, "Should exit 1 when tools are missing"
        assert "tmux" in output, "Must list tmux as missing"
        assert "python3" in output, "Must list python3 as missing"
        assert "nats-server" in output, "Must list nats-server as missing"

    def test_preflight_exits_1_on_missing_tools(self):
        """Must exit with code 1 when any tool is missing."""
        env = os.environ.copy()
        env["_TEST_MISSING_TOOLS"] = "nats-server"

        result = _run_start_script("demo", env_overrides=env)
        assert result.returncode == 1

    def test_preflight_error_is_clear(self):
        """Error message must clearly indicate which tools are missing."""
        env = os.environ.copy()
        env["_TEST_MISSING_TOOLS"] = "tmux"

        result = _run_start_script("demo", env_overrides=env)
        output = (result.stdout + result.stderr).lower()
        # Should contain words like "missing", "required", "not found", "install"
        assert any(word in output for word in ["missing", "required", "not found", "install"]), (
            f"Error message should clearly indicate missing tools, got: {output}"
        )


# ===========================================================================
# 3. NATS AUTO-START
# ===========================================================================


class TestNatsAutoStart:
    """start.sh must auto-start NATS if not running."""

    def test_calls_setup_nats_when_not_running(self):
        """If NATS is not running, start.sh must invoke setup-nats.sh."""
        env = os.environ.copy()
        env["_TEST_NATS_RUNNING"] = "false"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        # Should reference setup-nats.sh or starting NATS
        assert "setup-nats" in output.lower() or "nats" in output.lower()

    def test_skips_nats_start_when_already_running(self):
        """If NATS is already running, setup-nats.sh should NOT be invoked."""
        env = os.environ.copy()
        env["_TEST_NATS_RUNNING"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        # Should NOT say "starting NATS" or invoke setup-nats.sh
        assert "starting nats" not in output.lower() or result.returncode == 0

    def test_setup_nats_script_exists(self):
        """setup-nats.sh must exist at scripts/setup-nats.sh."""
        assert os.path.isfile(SETUP_NATS_SCRIPT), (
            f"scripts/setup-nats.sh not found at {SETUP_NATS_SCRIPT}"
        )


# ===========================================================================
# 3b. BACKGROUND SERVICE IDEMPOTENCY
# ===========================================================================


class TestServiceIdempotency:
    """start.sh must skip spawning background services that are already
    running. A ``com.local.knowledge-indexer`` launchd plist (or a prior
    start.sh run that didn't get reaped) can leave the indexer / speaker
    / thermostat daemons alive; start.sh's ``service_is_running`` helper
    detects each via ``pgrep -f`` and bypasses the spawn to avoid
    duplicate writers fighting over the same ChromaDB + NATS subjects.

    Tests force the "already running" branch with per-service env vars
    (``_TEST_INDEXER_RUNNING``, ``_TEST_SPEAKER_RUNNING``,
    ``_TEST_THERMOSTAT_RUNNING``). The ``test_per_service_isolation``
    test is what makes the three-env-var design load-bearing: it proves
    that forcing one service's "running" flag does NOT also suppress
    the other two. Without it, a broken helper that returned true for
    everything would still pass the three single-service tests.
    """

    # All four tests below pin every per-service env var explicitly (even
    # the ones not under test) so the helper's pgrep fallback never runs
    # against the real host — otherwise a stray launchd-managed indexer or
    # a leftover speaker/thermostat from a prior dev session would flip the
    # result non-deterministically.

    def test_skips_indexer_when_already_running(self):
        """_TEST_INDEXER_RUNNING=true must bypass the indexer spawn."""
        env = os.environ.copy()
        env["_TEST_INDEXER_RUNNING"] = "true"
        env["_TEST_SPEAKER_RUNNING"] = "false"
        env["_TEST_THERMOSTAT_RUNNING"] = "false"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "Knowledge indexer already running. Skipping." in output, (
            "Must log that the indexer spawn was skipped"
        )
        assert "Knowledge indexer started (PID" not in output, (
            "Must NOT start a new indexer when one is already running"
        )

    def test_skips_speaker_when_already_running(self):
        """_TEST_SPEAKER_RUNNING=true must bypass the speaker-service spawn."""
        env = os.environ.copy()
        env["_TEST_INDEXER_RUNNING"] = "false"
        env["_TEST_SPEAKER_RUNNING"] = "true"
        env["_TEST_THERMOSTAT_RUNNING"] = "false"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "Speaker service already running. Skipping." in output, (
            "Must log that the speaker-service spawn was skipped"
        )
        assert "Speaker service started (PID" not in output, (
            "Must NOT start a new speaker-service when one is already running"
        )

    def test_skips_thermostat_when_already_running(self):
        """_TEST_THERMOSTAT_RUNNING=true must bypass the thermostat-service spawn."""
        env = os.environ.copy()
        env["_TEST_INDEXER_RUNNING"] = "false"
        env["_TEST_SPEAKER_RUNNING"] = "false"
        env["_TEST_THERMOSTAT_RUNNING"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "Thermostat service already running. Skipping." in output, (
            "Must log that the thermostat-service spawn was skipped"
        )
        assert "Thermostat service started (PID" not in output, (
            "Must NOT start a new thermostat-service when one is already running"
        )

    def test_per_service_isolation(self):
        """Forcing one service's 'running' flag must NOT suppress the others.

        Sets ``_TEST_SPEAKER_RUNNING=true`` with the other two pinned to
        ``false`` and asserts speaker is skipped while the indexer and
        thermostat spawns still fire. This proves the helper's per-service
        env-var dispatch actually reads each flag independently instead of
        short-circuiting on any one being set.
        """
        env = os.environ.copy()
        env["_TEST_INDEXER_RUNNING"] = "false"
        env["_TEST_SPEAKER_RUNNING"] = "true"
        env["_TEST_THERMOSTAT_RUNNING"] = "false"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr

        # Speaker is skipped
        assert "Speaker service already running. Skipping." in output, (
            "Speaker must be skipped when _TEST_SPEAKER_RUNNING=true"
        )
        assert "Speaker service started (PID" not in output, (
            "Speaker must NOT spawn when _TEST_SPEAKER_RUNNING=true"
        )

        # Indexer and thermostat still spawn
        assert "Knowledge indexer started (PID" in output, (
            "Indexer must still spawn when only speaker is flagged running"
        )
        assert "Thermostat service started (PID" in output, (
            "Thermostat must still spawn when only speaker is flagged running"
        )


# ===========================================================================
# 4. IDEMPOTENT TMUX SESSION CREATION
# ===========================================================================


class TestIdempotentSession:
    """start.sh must be idempotent: kill existing session, create fresh."""

    def test_kills_existing_session(self):
        """If tmux session already exists, start.sh must kill it first."""
        # The script should call 'tmux kill-session' for the project session
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "true"
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "kill-session" in output.lower() or "kill" in output.lower(), (
            "start.sh must kill existing tmux session before creating new one"
        )

    def test_creates_new_session(self):
        """start.sh must create a new tmux session."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "new-session" in output.lower() or "new" in output.lower(), (
            "start.sh must create a new tmux session"
        )

    def test_session_name_matches_config(self, temp_project):
        """Tmux session name must match the session_name from project config."""
        # The session name "testproj" should appear in tmux commands
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr
        assert "testproj" in output, (
            "Session name should match project config's session_name"
        )


# ===========================================================================
# 5. CONTROL WINDOW LAYOUT
# ===========================================================================


class TestControlWindow:
    """Control window must have orchestrator + nats-monitor panes side-by-side."""

    def test_control_window_exists(self):
        """Tmux session must have a 'control' window."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "control" in output.lower(), (
            "Must create a 'control' window"
        )

    def test_control_has_two_panes(self):
        """Control window must have exactly 2 panes (orchestrator + nats-monitor)."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        # Should contain split-window for the second pane
        assert "split-window" in output.lower() or "split" in output.lower(), (
            "Control window must split into 2 panes"
        )

    def test_orchestrator_pane_runs_orchestrator(self):
        """First pane in control window must run the orchestrator."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "orchestrator" in output.lower(), (
            "Control window must run the orchestrator"
        )

    def test_nats_monitor_pane_runs_monitor(self):
        """Second pane in control window must run nats-monitor."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "nats-monitor" in output.lower() or "nats sub" in output.lower(), (
            "Control window must run nats-monitor"
        )

    def test_panes_are_side_by_side(self):
        """Control window panes must be arranged side-by-side (horizontal split)."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        # Horizontal split uses -h flag in tmux split-window
        assert "-h" in output or "horizontal" in output.lower(), (
            "Control panes must be side-by-side (horizontal split with -h)"
        )


# ===========================================================================
# 5a. CONTROL WINDOW 3-PANE LAYOUT (with manager agent)
# ===========================================================================


@pytest.fixture
def manager_project(tmp_path):
    """Create a project with a manager agent in monitor role (+ one worker)."""
    project_dir = tmp_path / "projects" / "mgrtest"
    project_dir.mkdir(parents=True)

    config = {
        "project": "mgrtest",
        "tmux": {"session_name": "mgrtest"},
        "agents": {
            "manager": {
                "role": "monitor",
                "runtime": "claude_code",
                "label": "manager",
                "system_prompt": "You are the manager.",
            },
            "dev": {
                "runtime": "claude_code",
                "working_dir": str(tmp_path / "dev"),
            },
        },
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

    global_config = {
        "nats": {"url": "nats://localhost:4222"},
        "tmux": {"nudge_cooldown_seconds": 30, "max_nudge_retries": 20},
    }
    with open(tmp_path / "config.yaml", "w") as f:
        yaml.dump(global_config, f)

    (tmp_path / "dev").mkdir(exist_ok=True)

    return {"root": tmp_path, "project_dir": project_dir, "config": config}


class TestControlWindowLayoutWithManager:
    """With manager configured, control window = [manager | orch / nats-monitor].

    Target pane indices (spatial order):
      0 = manager (left, full height)
      1 = orchestrator (top-right)
      2 = nats-monitor (bottom-right)

    Verified in dry-run by checking the order of split-window / set-option
    / send-keys commands emitted.
    """

    def _dry_run(self, manager_project):
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        result = _run_start_script(
            "mgrtest",
            env_overrides=env,
            cwd=str(manager_project["root"]),
        )
        return result.stdout + result.stderr

    def test_pane_0_labelled_manager(self, manager_project):
        """Pane 0 in control window must be labelled 'manager'."""
        output = self._dry_run(manager_project)
        assert "set-option -p -t mgrtest:control.0 @label manager" in output, (
            f"Expected pane 0 to be labelled 'manager', got:\n{output}"
        )

    def test_pane_1_labelled_orchestrator(self, manager_project):
        """Pane 1 in control window must be labelled 'orchestrator'."""
        output = self._dry_run(manager_project)
        assert "set-option -p -t mgrtest:control.1 @label orchestrator" in output, (
            f"Expected pane 1 to be labelled 'orchestrator', got:\n{output}"
        )

    def test_pane_2_labelled_nats_monitor(self, manager_project):
        """Pane 2 in control window must be labelled 'nats-monitor'."""
        output = self._dry_run(manager_project)
        assert "set-option -p -t mgrtest:control.2 @label nats-monitor" in output, (
            f"Expected pane 2 to be labelled 'nats-monitor', got:\n{output}"
        )

    def test_split_order_creates_target_layout(self, manager_project):
        """The split sequence must be: split -h on pane 0, then split -v on pane 1.

        This order produces the target layout:
          [manager | (orch / nats-mon)]
        with manager on the left full-height, orch top-right, nats-mon bot-right.
        """
        output = self._dry_run(manager_project)
        lines = [l for l in output.splitlines() if "split-window" in l]
        assert len(lines) >= 2, (
            f"Expected at least 2 control-window splits, got: {lines}"
        )
        # First split must be -h on pane 0 (manager -> orch on right)
        split_h = [l for l in lines if "split-window -h" in l and "mgrtest:control.0" in l]
        assert split_h, (
            f"Expected split-window -h -t mgrtest:control.0, got lines: {lines}"
        )
        # Second split must be -v on pane 1 (orch -> nats-mon below)
        split_v = [l for l in lines if "split-window -v" in l and "mgrtest:control.1" in l]
        assert split_v, (
            f"Expected split-window -v -t mgrtest:control.1, got lines: {lines}"
        )

    def test_orchestrator_runs_in_pane_1(self, manager_project):
        """The orchestrator launch command must be sent to pane 1 (not pane 0)."""
        output = self._dry_run(manager_project)
        assert (
            "send-keys -t mgrtest:control.1 cd" in output
            and "python3 -m orchestrator mgrtest" in output
        ), (
            f"Orchestrator must be launched in pane 1, got:\n{output}"
        )

    def test_manager_runs_in_pane_0(self, manager_project):
        """The manager claude command must be sent to pane 0."""
        output = self._dry_run(manager_project)
        # Manager's claude command lands in pane 0
        assert "send-keys -t mgrtest:control.0" in output, (
            f"Manager must be launched in pane 0, got:\n{output}"
        )
        assert "claude" in output and "manager.json" in output

    def test_no_manager_project_keeps_two_pane_fallback(self):
        """When manager is NOT configured, control window stays 2-pane."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr

        # Demo has no manager; pane 0 should be orchestrator, pane 1 nats-monitor.
        assert "set-option -p -t demo:control.0 @label orchestrator" in output, (
            f"Demo (no manager) should keep orch in pane 0, got:\n{output}"
        )
        assert "set-option -p -t demo:control.1 @label nats-monitor" in output, (
            f"Demo (no manager) should keep nats-monitor in pane 1, got:\n{output}"
        )
        # No split-window -v on control pane 1 (that's only in 3-pane layout)
        assert "split-window -v -t demo:control.1" not in output, (
            f"Demo (no manager) should not vertically split pane 1, got:\n{output}"
        )


# ===========================================================================
# 5b. DETERMINISTIC GROUPED-SESSION NAMES
# ===========================================================================


class TestGroupedSessionNames:
    """start.sh must use deterministic names for the control/agents grouped sessions.

    We changed from `tmux new-session -t <name>` (which auto-numbers
    cruft like remote-test-40, -42, …) to
    `tmux new-session -A -s <name>-control -t <name>` so secondary
    sessions have predictable names: <name>-control and <name>-agents.
    """

    def test_start_script_uses_A_flag_for_grouped_sessions(self):
        """The start.sh source must invoke new-session with -A -s for the
        iTerm launcher commands (visible in the script source)."""
        with open(START_SCRIPT) as f:
            src = f.read()
        # The two grouped-session launcher commands
        assert "new-session -A -s ${SESSION_NAME}-control -t ${SESSION_NAME}" in src, (
            "start.sh must create a deterministic <session>-control session"
        )
        assert "new-session -A -s ${SESSION_NAME}-agents -t ${SESSION_NAME}" in src, (
            "start.sh must create a deterministic <session>-agents session"
        )


# ===========================================================================
# 6. AGENTS WINDOW LAYOUT
# ===========================================================================


class TestAgentsWindow:
    """Agents window must have one pane per agent in tiled layout."""

    def test_agents_window_exists(self):
        """Tmux session must have an 'agents' window."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "agents" in output.lower(), (
            "Must create an 'agents' window"
        )

    def test_one_pane_per_agent(self, temp_project):
        """Agents window must have exactly one pane per agent defined in config."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr
        # 2 agents defined -> 1 initial pane + 1 split = 2 panes
        # Check for split-window calls or pane creation
        split_count = output.lower().count("split-window")
        assert split_count >= 1, (
            f"Expected at least 1 split-window for 2 agents, got {split_count}"
        )

    def test_three_agents_create_three_panes(self, three_agent_project):
        """3 agents must create 3 panes (initial + 2 splits)."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("rgr", env_overrides=env, cwd=str(three_agent_project["root"]))
        output = result.stdout + result.stderr
        # 3 agents -> first pane is created with new-window, 2 more via split
        split_count = output.lower().count("split-window")
        assert split_count >= 2, (
            f"Expected at least 2 split-window for 3 agents, got {split_count}"
        )

    def test_layout_equalized_not_tiled(self):
        """Agents window must use an explicit 2x3 grid (select-layout -E),
        NOT tmux's `tiled` preset.

        `tiled` puts 6 panes as 3 columns × 2 rows (landscape). We want
        2 columns × 3 rows (portrait) so the terminal window is taller
        than wide and each pane gets enough vertical space for a Claude
        TUI. `select-layout -E` equalizes cell sizes without switching
        shape; the shape itself is built by explicit `split-window -h`
        / `split-window -v` calls in setup_agent_panes().

        Previous revision of this test asserted `tiled` was in the
        dry-run output — that assertion was made stale by PR #47, which
        intentionally moved to the 2x3 layout. We switched to a
        structural assertion against start.sh directly because the
        dry-run code path short-circuits before reaching the final
        `select-layout -E` call (the split loop needs a real tmux pane
        id seed which dry-run doesn't provide).
        """
        with open(START_SCRIPT, "r") as f:
            content = f.read()

        # The good pattern: select-layout -E applied to the agents window.
        assert re.search(
            r"select-layout\s+[^\n]*-E[^\n]*\$\{?SESSION_NAME\}?:\$\{?AGENTS_WINDOW\}?",
            content,
        ) or re.search(
            r"select-layout\s+-t\s+[\"']?\$\{?SESSION_NAME\}?:\$\{?AGENTS_WINDOW\}?[\"']?\s+-E",
            content,
        ), (
            "start.sh must finalize the agents window with "
            "`select-layout -E` to equalize the explicit 2x3 grid. "
            "If you restored `select-layout tiled`, you also restored "
            "the landscape 3x2 layout bug PR #47 fixed."
        )

        # The bad pattern: no `select-layout tiled` directive.
        assert not re.search(
            r"select-layout\s+[^\n]*\btiled\b",
            content,
        ), (
            "start.sh must NOT apply `select-layout tiled` to the agents "
            "window. tiled reshapes 6 panes into 3x2 landscape; we need "
            "the explicit 2x3 portrait grid from PR #47."
        )


# ===========================================================================
# 7. PANE BORDER STATUS
# ===========================================================================


class TestPaneBorderStatus:
    """Pane titles must be enabled via pane-border-status top."""

    def test_pane_border_status_top(self):
        """pane-border-status must be set to 'top'."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "pane-border-status" in output.lower(), (
            "Must set pane-border-status"
        )
        assert "top" in output.lower(), (
            "pane-border-status must be set to 'top'"
        )


# ===========================================================================
# 8. MCP CONFIG GENERATION
# ===========================================================================


class TestMcpConfigGeneration:
    """start.sh must generate per-agent MCP config files for claude_code agents."""

    def test_generates_config_for_claude_code_agent(self, temp_project):
        """Must generate .mcp-configs/<agent>.json for claude_code agents."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        # After running start.sh, the config file should exist
        # Since start.sh doesn't exist yet, this test verifies the contract
        assert not writer_config.exists(), (
            "Pre-condition: MCP config should not exist before running start.sh"
        )

        # Run start.sh (will fail in RED phase)
        result = _run_start_script(
            "testproj",
            cwd=str(temp_project["root"]),
        )

        # After a successful run, the config should be created
        assert writer_config.exists(), (
            f"Expected MCP config at {writer_config}"
        )

    def test_does_not_generate_config_for_script_agent(self, temp_project):
        """Must NOT generate .mcp-configs/<agent>.json for script runtime agents."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        executor_config = mcp_dir / "executor.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert not executor_config.exists(), (
            "Must NOT generate MCP config for script agents"
        )

    def test_mcp_config_has_correct_structure(self, temp_project):
        """MCP config must have mcpServers.mas-bridge structure."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            config = json.load(f)

        assert "mcpServers" in config, "Must have mcpServers key"
        assert "mas-bridge" in config["mcpServers"], "Must have mas-bridge server"
        bridge = config["mcpServers"]["mas-bridge"]
        assert "command" in bridge, "Must have command field"
        assert "args" in bridge, "Must have args field"
        assert "env" in bridge, "Must have env field"

    def test_mcp_config_command_is_node(self, temp_project):
        """MCP config command must be 'node'."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            config = json.load(f)

        assert config["mcpServers"]["mas-bridge"]["command"] == "node"

    def test_mcp_config_args_contains_bridge_path(self, temp_project):
        """MCP config args must contain the path to mcp-bridge/index.js."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            config = json.load(f)

        args = config["mcpServers"]["mas-bridge"]["args"]
        assert any("mcp-bridge/index.js" in arg for arg in args), (
            f"Args must contain path to mcp-bridge/index.js, got: {args}"
        )

    def test_mcp_config_has_agent_role(self, temp_project):
        """MCP config env must contain AGENT_ROLE matching the agent name."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            config = json.load(f)

        env = config["mcpServers"]["mas-bridge"]["env"]
        assert env.get("AGENT_ROLE") == "writer", (
            f"AGENT_ROLE must be 'writer', got: {env.get('AGENT_ROLE')}"
        )

    def test_mcp_config_has_nats_url(self, temp_project):
        """MCP config env must contain NATS_URL from global config."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            config = json.load(f)

        env = config["mcpServers"]["mas-bridge"]["env"]
        assert env.get("NATS_URL") == "nats://localhost:4222", (
            f"NATS_URL must be 'nats://localhost:4222', got: {env.get('NATS_URL')}"
        )

    def test_mcp_config_has_workspace_dir(self, temp_project):
        """MCP config env must contain WORKSPACE_DIR matching agent's working_dir."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            config = json.load(f)

        env = config["mcpServers"]["mas-bridge"]["env"]
        expected_dir = str(temp_project["root"] / "workspace")
        assert env.get("WORKSPACE_DIR") == expected_dir, (
            f"WORKSPACE_DIR must be '{expected_dir}', got: {env.get('WORKSPACE_DIR')}"
        )

    def test_mcp_config_uses_project_dir_as_fallback(self, tmp_path):
        """When agent has no working_dir, WORKSPACE_DIR must default to project dir."""
        project_dir = tmp_path / "projects" / "nowd"
        project_dir.mkdir(parents=True)

        config = {
            "project": "nowd",
            "tmux": {"session_name": "nowd"},
            "agents": {
                "writer": {
                    "runtime": "claude_code",
                    # No working_dir specified
                },
            },
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

        global_config = {"nats": {"url": "nats://localhost:4222"}}
        with open(tmp_path / "config.yaml", "w") as f:
            yaml.dump(global_config, f)

        _run_start_script("nowd", cwd=str(tmp_path))

        mcp_config_path = project_dir / ".mcp-configs" / "writer.json"
        assert mcp_config_path.exists(), "MCP config must be created"
        with open(mcp_config_path) as f:
            mcp_config = json.load(f)

        workspace = mcp_config["mcpServers"]["mas-bridge"]["env"]["WORKSPACE_DIR"]
        assert workspace == str(project_dir), (
            f"WORKSPACE_DIR should default to project dir '{project_dir}', got: {workspace}"
        )

    def test_generates_config_for_all_claude_code_agents(self, three_agent_project):
        """Must generate separate MCP configs for each claude_code agent."""
        project_dir = three_agent_project["project_dir"]
        mcp_dir = project_dir / ".mcp-configs"

        _run_start_script("rgr", cwd=str(three_agent_project["root"]))

        for agent_name in ["qa", "dev", "refactor"]:
            config_path = mcp_dir / f"{agent_name}.json"
            assert config_path.exists(), (
                f"Must generate MCP config for {agent_name} at {config_path}"
            )

    def test_each_agent_config_has_unique_role(self, three_agent_project):
        """Each agent's MCP config must have a unique AGENT_ROLE."""
        project_dir = three_agent_project["project_dir"]
        mcp_dir = project_dir / ".mcp-configs"

        _run_start_script("rgr", cwd=str(three_agent_project["root"]))

        roles = set()
        for agent_name in ["qa", "dev", "refactor"]:
            config_path = mcp_dir / f"{agent_name}.json"
            assert config_path.exists(), f"Config for {agent_name} must exist"
            with open(config_path) as f:
                config = json.load(f)
            role = config["mcpServers"]["mas-bridge"]["env"]["AGENT_ROLE"]
            assert role == agent_name, f"AGENT_ROLE must be {agent_name}, got {role}"
            roles.add(role)

        assert len(roles) == 3, "Each agent must have a unique AGENT_ROLE"

    def test_mcp_config_is_valid_json(self, temp_project):
        """Generated MCP config must be valid JSON."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        writer_config = mcp_dir / "writer.json"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert writer_config.exists(), "MCP config must be created"
        with open(writer_config) as f:
            # json.load will raise if invalid
            config = json.load(f)
        assert isinstance(config, dict), "MCP config must be a JSON object"


# ===========================================================================
# 9. CLAUDE CODE AGENT LAUNCHING
# ===========================================================================


class TestClaudeCodeLaunching:
    """Claude Code agents must be launched with claude --mcp-config <path>."""

    def test_launches_claude_with_mcp_config(self, temp_project):
        """Claude Code agent must be launched with --mcp-config flag."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr

        assert "claude" in output.lower() and "--mcp-config" in output.lower(), (
            "Must launch claude with --mcp-config flag"
        )

    def test_mcp_config_path_points_to_generated_file(self, temp_project):
        """The --mcp-config path must point to the generated agent config file."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr

        # Should reference the .mcp-configs/writer.json path
        assert ".mcp-configs/writer.json" in output or "mcp-configs" in output.lower(), (
            "Must reference the generated MCP config path"
        )


# ===========================================================================
# 10. SCRIPT AGENT LAUNCHING
# ===========================================================================


class TestScriptAgentLaunching:
    """Script agents must be launched with their configured command."""

    def test_launches_script_agent_with_command(self, temp_project):
        """Script agent must be launched with the command from config."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr

        assert "echo_agent.py" in output, (
            "Must launch script agent with configured command"
        )

    def test_script_agent_not_launched_with_mcp_config(self, temp_project):
        """Script agents must NOT be launched with --mcp-config."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr

        # executor is a script agent -- should not have --mcp-config for executor
        # This is a structural check -- we verify the executor pane does NOT get --mcp-config
        lines = output.split("\n")
        executor_lines = [l for l in lines if "executor" in l.lower() and "echo_agent" in l]
        for line in executor_lines:
            assert "--mcp-config" not in line, (
                f"Script agent must not use --mcp-config: {line}"
            )


# ===========================================================================
# 11. SSH REMOTE AGENT SUPPORT
# ===========================================================================


class TestSshSupport:
    """Agents with ssh_host must SSH into the remote host before launching."""

    def test_ssh_into_remote_host_for_claude_agent(self, ssh_agent_project):
        """Agent with ssh_host must SSH into that host before launching claude."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("remote", env_overrides=env, cwd=str(ssh_agent_project["root"]))
        output = result.stdout + result.stderr

        assert "ssh" in output.lower() and "dgx1.local" in output, (
            "Must SSH into dgx1.local for remote_reviewer agent"
        )

    def test_ssh_into_remote_host_for_script_agent(self, ssh_agent_project):
        """Script agent with ssh_host must also SSH into the host."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("remote", env_overrides=env, cwd=str(ssh_agent_project["root"]))
        output = result.stdout + result.stderr

        assert "dgx2.local" in output, (
            "Must SSH into dgx2.local for remote_script agent"
        )

    def test_local_agent_no_ssh(self, ssh_agent_project):
        """Agent without ssh_host must NOT use SSH."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("remote", env_overrides=env, cwd=str(ssh_agent_project["root"]))
        output = result.stdout + result.stderr

        # local_writer should not have ssh associated with it
        lines = output.split("\n")
        local_writer_lines = [l for l in lines if "local_writer" in l]
        for line in local_writer_lines:
            assert "ssh" not in line.lower() or "ssh_host" not in line.lower(), (
                f"Local agent must not use SSH: {line}"
            )


# ===========================================================================
# 12. EXIT CODES AND ERROR HANDLING
# ===========================================================================


class TestExitCodes:
    """start.sh must exit 0 on success, 1 on failure."""

    def test_exit_0_on_success(self):
        """Must exit with code 0 when everything succeeds."""
        # This requires a full valid setup; will fail in RED phase
        # because start.sh doesn't exist
        result = _run_start_script("demo")
        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )

    def test_exit_1_on_missing_project(self):
        """Must exit with code 1 when project config doesn't exist."""
        result = _run_start_script("nonexistent_project_xyz")
        assert result.returncode == 1, (
            f"Expected exit code 1 for missing project, got {result.returncode}"
        )

    def test_exit_1_on_preflight_failure(self):
        """Must exit with code 1 when preflight checks fail."""
        env = os.environ.copy()
        env["_TEST_MISSING_TOOLS"] = "tmux,python3,nats-server"

        result = _run_start_script("demo", env_overrides=env)
        assert result.returncode == 1


# ===========================================================================
# 13. MISSING/INVALID ARGUMENTS
# ===========================================================================


class TestArguments:
    """start.sh must handle missing/invalid arguments properly."""

    def test_no_arguments_shows_usage(self):
        """Running start.sh without arguments must show usage info."""
        result = subprocess.run(
            [START_SCRIPT],
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

    def test_project_argument_is_used(self, temp_project):
        """The project argument must be used to locate the project config."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("testproj", env_overrides=env, cwd=str(temp_project["root"]))
        output = result.stdout + result.stderr
        assert "testproj" in output, (
            "Project name must appear in script output"
        )

    def test_invalid_project_fails_gracefully(self):
        """Invalid project name must produce a clear error and exit 1."""
        result = _run_start_script("this-project-does-not-exist-12345")
        assert result.returncode == 1
        output = (result.stdout + result.stderr).lower()
        assert any(word in output for word in ["not found", "does not exist", "error", "no such"]), (
            f"Must show clear error for invalid project, got: {output}"
        )


# ===========================================================================
# 14. INTEGRATION SCENARIOS
# ===========================================================================


class TestIntegrationScenarios:
    """End-to-end integration checks for start.sh."""

    def test_mcp_configs_dir_created(self, temp_project):
        """The .mcp-configs directory must be created inside the project dir."""
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"

        _run_start_script("testproj", cwd=str(temp_project["root"]))

        assert mcp_dir.is_dir(), (
            f".mcp-configs directory must be created at {mcp_dir}"
        )

    def test_full_two_agent_setup(self, temp_project):
        """Full setup with 2 agents: writer (claude_code) + executor (script)."""
        result = _run_start_script("testproj", cwd=str(temp_project["root"]))

        # writer MCP config should exist
        mcp_dir = temp_project["project_dir"] / ".mcp-configs"
        assert (mcp_dir / "writer.json").exists(), "writer MCP config must exist"
        assert not (mcp_dir / "executor.json").exists(), "executor MCP config must NOT exist"

        # Verify writer config content
        with open(mcp_dir / "writer.json") as f:
            config = json.load(f)

        assert config["mcpServers"]["mas-bridge"]["env"]["AGENT_ROLE"] == "writer"
        assert config["mcpServers"]["mas-bridge"]["env"]["NATS_URL"] == "nats://localhost:4222"

    def test_full_three_agent_setup(self, three_agent_project):
        """Full setup with 3 claude_code agents."""
        result = _run_start_script("rgr", cwd=str(three_agent_project["root"]))

        mcp_dir = three_agent_project["project_dir"] / ".mcp-configs"
        for name in ["qa", "dev", "refactor"]:
            config_path = mcp_dir / f"{name}.json"
            assert config_path.exists(), f"MCP config for {name} must exist"

            with open(config_path) as f:
                config = json.load(f)
            assert config["mcpServers"]["mas-bridge"]["env"]["AGENT_ROLE"] == name


# ===========================================================================
# 15. PROVIDER TEMPLATE SUBSTITUTION
# ===========================================================================


def _build_provider_project(tmp_path, agent_prompt, providers_global=None,
                            project_name="providerproj"):
    """Spin up a minimal temp project with one local claude_code agent whose
    ``system_prompt`` is the caller-supplied string. In dry-run mode,
    start.sh prints each ``tmux send-keys`` invocation (including the
    agent's resolved ``--append-system-prompt``) to stdout, which the
    tests then grep.
    """
    if providers_global is None:
        providers_global = {
            "stt": {"backend": "whisper", "url": "http://192.168.1.51:5112"},
            "tts": {"backend": "piper", "url": "http://192.168.1.51:5111"},
        }

    project_dir = tmp_path / "projects" / project_name
    project_dir.mkdir(parents=True)

    project_config = {
        "project": project_name,
        "tmux": {"session_name": project_name},
        "agents": {
            "voicebot": {
                "runtime": "claude_code",
                "working_dir": str(tmp_path / "ws"),
                "system_prompt": agent_prompt,
            },
        },
        "state_machine": {
            "initial": "idle",
            "states": {"idle": {"description": "noop"}},
            "transitions": [
                {"from": "idle", "to": "idle", "trigger": "task_assigned"},
            ],
        },
    }
    with open(project_dir / "config.yaml", "w") as f:
        yaml.dump(project_config, f)

    global_config = {
        "nats": {"url": "nats://localhost:4222"},
        "tmux": {"nudge_cooldown_seconds": 30, "max_nudge_retries": 20},
        "providers": providers_global,
    }
    with open(tmp_path / "config.yaml", "w") as f:
        yaml.dump(global_config, f)

    (tmp_path / "ws").mkdir(exist_ok=True)
    return project_dir


class TestProviderSubstitution:
    """``{{providers.<section>.<field>}}`` placeholders in ``system_prompt``
    are resolved from the merged config's ``providers`` subtree before the
    agent launch command is built.
    """

    def test_placeholder_resolved_to_config_value(self, tmp_path):
        """``{{providers.stt.url}}`` must be replaced with the config value."""
        prompt = "STT at {{providers.stt.url}} with backend {{providers.stt.backend}}."
        _build_provider_project(tmp_path, prompt)

        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"
        result = _run_start_script(
            "providerproj", env_overrides=env, cwd=str(tmp_path),
        )
        output = result.stdout + result.stderr

        # Resolved URL + backend show up in the launch command.
        assert "http://192.168.1.51:5112" in output, (
            "Expected resolved providers.stt.url in dry-run launch output"
        )
        assert "backend whisper" in output, (
            "Expected resolved providers.stt.backend in dry-run launch output"
        )
        # No raw placeholder survives into the launch command.
        assert "{{providers.stt.url}}" not in output
        assert "{{providers.stt.backend}}" not in output
        # Debug line must be emitted to stderr for operators.
        assert "providers.stt = whisper @ http://192.168.1.51:5112" in output

    def test_unknown_placeholder_errors_naming_agent_and_token(self, tmp_path):
        """Unknown ``{{providers.bogus.url}}`` must fail loudly."""
        prompt = "broken {{providers.bogus.url}} prompt"
        _build_provider_project(tmp_path, prompt)

        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"
        result = _run_start_script(
            "providerproj", env_overrides=env, cwd=str(tmp_path),
        )
        combined = result.stdout + result.stderr

        assert result.returncode != 0, (
            "start.sh must exit non-zero when a provider placeholder cannot be resolved"
        )
        assert "voicebot" in combined, (
            "Error must name the offending agent (voicebot)"
        )
        assert "{{providers.bogus.url}}" in combined, (
            "Error must name the unresolved token"
        )

    def test_literal_braces_preserved_and_double_occurrence_resolves(self, tmp_path):
        """Non-matching ``{{...}}`` sequences are preserved verbatim, and a
        placeholder that appears twice in the same prompt is resolved both times.
        """
        prompt = (
            "Literal {{not a provider}} stays. "
            "First {{providers.stt.url}} and second {{providers.stt.url}}."
        )
        _build_provider_project(tmp_path, prompt)

        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"
        result = _run_start_script(
            "providerproj", env_overrides=env, cwd=str(tmp_path),
        )
        output = result.stdout + result.stderr

        # The non-matching literal is preserved byte-for-byte.
        assert "{{not a provider}}" in output, (
            "Literal non-matching {{...}} must be preserved"
        )
        # Both occurrences of the same placeholder resolve.
        assert output.count("http://192.168.1.51:5112") >= 2, (
            "A placeholder repeated twice must resolve both times"
        )
        # No raw provider placeholder survives.
        assert "{{providers.stt.url}}" not in output


# ===========================================================================
# Grouped-session pre-create regression guard
#
# Context: PR #48 added `tmux new-session -d -A -s ${SESSION_NAME}-{control,
# agents} -t ${SESSION_NAME}` calls in start.sh so the grouped sessions exist
# BEFORE the terminal-launcher (iTerm2 / Terminal.app / wt.exe) tries to
# attach them. Without that pre-create, the grouped sessions only come into
# existence if and when the terminal launcher succeeds — and on Terminal.app-
# only macOS hosts, the iTerm2 osascript silently no-ops, leaving the user
# with `tmux attach -t <session>-control` returning "can't find session".
#
# This fix was subsequently regressed when another feature branch was merged
# forward from a pre-PR-#48 base, silently re-overwriting start.sh with the
# older pattern. This structural test fires on every PR (no INTEGRATION gate)
# so a future regression fails CI instead of only being caught when the user
# tries to attach and it does not work.
#
# What we assert: the exact two `new-session -d -A -s ...-{control,agents}`
# lines are present. We do NOT pin the surrounding select-window lines or the
# exact whitespace — that would be too brittle — but the pre-create intent
# must be clear from the file contents.
# ===========================================================================


class TestGroupedSessionPreCreateRegression:
    """Pre-create regression guard for the grouped tmux sessions."""

    def test_pre_create_control_grouped_session(self):
        """``start.sh`` must pre-create ``${SESSION_NAME}-control`` detached.

        Regression: without ``new-session -d -A -s ${SESSION_NAME}-control``,
        ``tmux attach -t remote-test-control`` returns "can't find session"
        for Terminal.app users (iTerm2 osascript silent-fail path).
        """
        with open(START_SCRIPT, "r") as f:
            content = f.read()

        # Match: `new-session -d -A -s "${SESSION_NAME}-control"` with
        # optional whitespace and optional quoting around the name. We also
        # accept ``\$\{SESSION_NAME\}`` without braces just in case someone
        # removes the braces in a refactor.
        assert re.search(
            r"new-session\s+-d\s+-A\s+-s\s+[\"']?\$\{?SESSION_NAME\}?-control[\"']?",
            content,
        ), (
            "start.sh must pre-create the ${SESSION_NAME}-control grouped "
            "session detached via `tmux new-session -d -A -s "
            "${SESSION_NAME}-control -t ${SESSION_NAME}`. Without this, "
            "Terminal.app users hit \"can't find session: <session>-control\" "
            "every time they run start.sh."
        )

    def test_pre_create_agents_grouped_session(self):
        """``start.sh`` must pre-create ``${SESSION_NAME}-agents`` detached.

        Same regression path as the control test — both grouped sessions
        need to exist before the terminal launcher runs.
        """
        with open(START_SCRIPT, "r") as f:
            content = f.read()

        assert re.search(
            r"new-session\s+-d\s+-A\s+-s\s+[\"']?\$\{?SESSION_NAME\}?-agents[\"']?",
            content,
        ), (
            "start.sh must pre-create the ${SESSION_NAME}-agents grouped "
            "session detached via `tmux new-session -d -A -s "
            "${SESSION_NAME}-agents -t ${SESSION_NAME}`. Without this, "
            "Terminal.app users hit \"can't find session: <session>-agents\" "
            "every time they run start.sh."
        )

    def test_pre_create_happens_before_terminal_launcher(self):
        """The pre-create calls must run BEFORE ``open_two_windows``.

        If the pre-create lines live inside ``open_two_windows`` (the iTerm2/
        Terminal.app launcher), they only fire on the branch that runs, which
        re-introduces the original regression for any host that does not
        match that branch. This test pins the ordering: both
        ``new-session -d -A`` lines must appear before the ``open_two_windows()``
        function definition.
        """
        with open(START_SCRIPT, "r") as f:
            content = f.read()

        launcher_start = content.find("open_two_windows()")
        assert launcher_start != -1, (
            "open_two_windows() function not found in start.sh"
        )

        pre_create_section = content[:launcher_start]

        assert re.search(
            r"new-session\s+-d\s+-A\s+-s\s+[\"']?\$\{?SESSION_NAME\}?-control[\"']?",
            pre_create_section,
        ), (
            "The -control grouped-session pre-create must happen BEFORE "
            "open_two_windows(). Putting it inside the launcher re-introduces "
            "the Terminal.app-silent-fail regression."
        )
        assert re.search(
            r"new-session\s+-d\s+-A\s+-s\s+[\"']?\$\{?SESSION_NAME\}?-agents[\"']?",
            pre_create_section,
        ), (
            "The -agents grouped-session pre-create must happen BEFORE "
            "open_two_windows(). Putting it inside the launcher re-introduces "
            "the Terminal.app-silent-fail regression."
        )
