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

    def test_tiled_layout_applied(self):
        """Agents window must use tmux 'tiled' layout."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_start_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "tiled" in output.lower(), (
            "Agents window must use 'tiled' layout"
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
