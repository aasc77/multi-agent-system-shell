"""
Tests for scripts/stop.sh -- Stop/Cleanup Script

TDD Contract (RED phase):
These tests define the expected behavior of the stop.sh utility script.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R8: Scripts (stop.sh kills tmux session, cleans up MCP configs)
  - R1: tmux Session Layout (session lifecycle management)

Acceptance criteria from task rgr-12:
  1. stop.sh kills tmux session for the given project
  2. stop.sh cleans up projects/<name>/.mcp-configs/ directory
  3. stop.sh --kill-nats also stops nats-server
  4. stop.sh prints 'already stopped' and exits 0 if session doesn't exist

Test categories:
  1. Script existence and executability
  2. Tmux session kill
  3. MCP configs cleanup
  4. --kill-nats flag behavior
  5. Idempotent behavior (already stopped)
  6. Missing/invalid arguments
  7. Exit codes
"""

import json
import os
import stat
import subprocess
import tempfile

import pytest
import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
STOP_SCRIPT = os.path.join(REPO_ROOT, "scripts", "stop.sh")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmux_for_test() -> list[str]:
    """Return the ``tmux`` argv prefix honoring ``MAS_TMUX_SOCKET``.

    Conftest sets this env var for the whole pytest session (see #46)
    so every direct ``subprocess.run(["tmux", ...])`` in these tests
    rides the isolated socket instead of the default one.
    """
    socket = os.environ.get("MAS_TMUX_SOCKET")
    if socket:
        return ["tmux", "-L", socket]
    return ["tmux"]


def _run_stop_script(args=None, env_overrides=None, cwd=None):
    """Helper to run stop.sh and capture output."""
    cmd = [STOP_SCRIPT]
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_with_mcp_configs(tmp_path):
    """Create a project directory with .mcp-configs/ containing generated files."""
    project_dir = tmp_path / "projects" / "testproj"
    mcp_dir = project_dir / ".mcp-configs"
    mcp_dir.mkdir(parents=True)

    # Create some MCP config files
    for agent in ["writer", "dev", "qa"]:
        config = {
            "mcpServers": {
                "mas-bridge": {
                    "command": "node",
                    "args": ["mcp-bridge/index.js"],
                    "env": {
                        "AGENT_ROLE": agent,
                        "NATS_URL": "nats://localhost:4222",
                    },
                }
            }
        }
        with open(mcp_dir / f"{agent}.json", "w") as f:
            json.dump(config, f)

    # Create project config
    project_config = {
        "project": "testproj",
        "tmux": {"session_name": "testproj"},
        "agents": {
            "writer": {"runtime": "claude_code"},
            "dev": {"runtime": "claude_code"},
            "qa": {"runtime": "claude_code"},
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
        yaml.dump(project_config, f)

    return {
        "root": tmp_path,
        "project_dir": project_dir,
        "mcp_dir": mcp_dir,
    }


# ===========================================================================
# 1. SCRIPT EXISTENCE AND EXECUTABILITY
# ===========================================================================


class TestStopScriptExists:
    """stop.sh must exist at scripts/stop.sh and be executable."""

    def test_stop_script_exists(self):
        """scripts/stop.sh must exist."""
        assert os.path.isfile(STOP_SCRIPT), (
            f"scripts/stop.sh not found at {STOP_SCRIPT}"
        )

    def test_stop_script_is_executable(self):
        """scripts/stop.sh must have execute permissions."""
        assert os.path.isfile(STOP_SCRIPT), "Script must exist first"
        mode = os.stat(STOP_SCRIPT).st_mode
        assert mode & stat.S_IXUSR, "scripts/stop.sh must be executable by owner"

    def test_stop_script_has_shebang(self):
        """scripts/stop.sh must start with a proper shebang line."""
        assert os.path.isfile(STOP_SCRIPT), "Script must exist first"
        with open(STOP_SCRIPT, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!"), (
            f"Expected shebang, got: {first_line}"
        )
        assert "bash" in first_line or "sh" in first_line, (
            f"Shebang must reference bash or sh, got: {first_line}"
        )


# ===========================================================================
# 2. TMUX SESSION KILL
# ===========================================================================


class TestTmuxSessionKill:
    """stop.sh must kill the tmux session for the given project."""

    def test_kills_tmux_session(self):
        """stop.sh must attempt to kill the tmux session matching the project."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "kill-session" in output.lower() or "kill" in output.lower(), (
            "stop.sh must kill the tmux session"
        )

    def test_kills_correct_session_name(self):
        """stop.sh must kill the session matching the project name."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("myproject", env_overrides=env)
        output = result.stdout + result.stderr
        assert "myproject" in output, (
            "Must kill session matching project name 'myproject'"
        )

    def test_exit_0_after_successful_kill(self):
        """stop.sh must exit 0 after successfully killing the session."""
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        assert result.returncode == 0, (
            f"Expected exit code 0 after kill, got {result.returncode}"
        )


# ===========================================================================
# 2b. BACKGROUND SERVICES CLEANUP
# ===========================================================================


class TestBackgroundServicesCleanup:
    """stop.sh must pkill all three background services that start.sh spawns.

    start.sh launches the knowledge indexer, speaker service, and thermostat
    service as backgrounded Python children (start.sh:495-524). If stop.sh
    misses any of them, they reparent to launchd (PPID=1) and linger as
    zombies after a teardown. Each test here asserts the dry-run pkill
    invocation for one service; together they guard against a future
    refactor that drops a service from the kill loop.
    """

    def test_dry_run_kills_knowledge_indexer(self):
        """stop.sh dry-run must include pkill for knowledge-store/indexer.py."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "pkill -f knowledge-store/indexer.py" in output, (
            "stop.sh must pkill the knowledge indexer"
        )

    def test_dry_run_kills_speaker_service(self):
        """stop.sh dry-run must include pkill for services/speaker-service.py."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "pkill -f services/speaker-service.py" in output, (
            "stop.sh must pkill the speaker service"
        )

    def test_dry_run_kills_thermostat_service(self):
        """stop.sh dry-run must include pkill for services/thermostat-service.py."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "pkill -f services/thermostat-service.py" in output, (
            "stop.sh must pkill the thermostat service"
        )


# ===========================================================================
# 3. MCP CONFIGS CLEANUP
# ===========================================================================


class TestMcpConfigsCleanup:
    """stop.sh must clean up the .mcp-configs/ directory."""

    def test_cleans_mcp_configs_directory(self, project_with_mcp_configs):
        """stop.sh must clean up projects/<name>/.mcp-configs/ directory."""
        mcp_dir = project_with_mcp_configs["mcp_dir"]

        # Verify pre-condition: MCP configs exist
        assert mcp_dir.exists(), "Pre-condition: .mcp-configs/ must exist"
        assert len(list(mcp_dir.iterdir())) > 0, "Pre-condition: MCP configs must exist"

        result = _run_stop_script(
            "testproj",
            cwd=str(project_with_mcp_configs["root"]),
        )

        # After stop.sh, the .mcp-configs/ directory should be cleaned up
        # Either the directory is removed or its contents are deleted
        if mcp_dir.exists():
            files = list(mcp_dir.iterdir())
            assert len(files) == 0, (
                f".mcp-configs/ should be empty after stop, found: {files}"
            )
        # If directory doesn't exist, that's also acceptable

    def test_handles_missing_mcp_configs_dir(self, tmp_path):
        """stop.sh must handle case where .mcp-configs/ doesn't exist."""
        project_dir = tmp_path / "projects" / "nomcp"
        project_dir.mkdir(parents=True)

        config = {
            "project": "nomcp",
            "tmux": {"session_name": "nomcp"},
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

        # No .mcp-configs/ directory -- should not crash
        result = _run_stop_script("nomcp", cwd=str(tmp_path))
        assert result.returncode == 0, (
            f"stop.sh must not crash when .mcp-configs/ is missing, exit code: {result.returncode}"
        )

    def test_removes_all_agent_configs(self, project_with_mcp_configs):
        """All generated agent config files must be removed."""
        mcp_dir = project_with_mcp_configs["mcp_dir"]

        _run_stop_script(
            "testproj",
            cwd=str(project_with_mcp_configs["root"]),
        )

        for agent in ["writer", "dev", "qa"]:
            config_path = mcp_dir / f"{agent}.json"
            assert not config_path.exists(), (
                f"MCP config for {agent} must be removed after stop"
            )


# ===========================================================================
# 4. --kill-nats FLAG BEHAVIOR
# ===========================================================================


class TestKillNatsFlag:
    """stop.sh --kill-nats must also stop nats-server."""

    def test_kill_nats_flag_stops_nats_server(self):
        """stop.sh --kill-nats must stop nats-server."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script(["demo", "--kill-nats"], env_overrides=env)
        output = result.stdout + result.stderr
        # Should contain evidence of killing nats-server
        assert any(term in output.lower() for term in [
            "nats-server", "kill", "pkill", "killall", "stop"
        ]), (
            f"--kill-nats must stop nats-server, got output: {output}"
        )

    def test_without_kill_nats_does_not_stop_nats(self):
        """stop.sh without --kill-nats must NOT stop nats-server."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        # Should NOT contain nats kill commands
        # We check that the output doesn't try to stop nats-server
        assert "pkill nats" not in output.lower() and "killall nats" not in output.lower(), (
            f"Without --kill-nats, must NOT stop nats-server, got: {output}"
        )

    def test_kill_nats_flag_accepted_before_project(self):
        """--kill-nats flag should work in any argument position."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script(["demo", "--kill-nats"], env_overrides=env)
        assert result.returncode == 0, (
            f"--kill-nats flag must be accepted, exit code: {result.returncode}"
        )


# ===========================================================================
# 4a. GROUPED-SESSION CLEANUP
# ===========================================================================


class TestGroupedSessionCleanup:
    """stop.sh must kill ALL sessions in the group, not just the primary.

    When start.sh opens iTerm windows, it uses
    `tmux new-session -A -s <name>-control -t <name>` which creates
    secondary sessions in the same group. Without the grouped-cleanup
    fix, stop.sh only killed the primary session and the cruft piled up.
    """

    def test_dry_run_references_control_and_agents_suffixes(self):
        """In dry-run mode with a session 'present', the output should
        mention killing the -control and -agents sibling sessions."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        output = result.stdout + result.stderr
        assert "demo-control" in output, (
            f"stop.sh must attempt to kill 'demo-control' grouped session, got: {output}"
        )
        assert "demo-agents" in output, (
            f"stop.sh must attempt to kill 'demo-agents' grouped session, got: {output}"
        )

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not installed",
    )
    def test_real_tmux_kills_all_grouped_sessions(self, tmp_path):
        """Integration: create a grouped tmux session family, stop it,
        verify ALL grouped sessions are gone."""
        import uuid

        session = f"masstoptest-{uuid.uuid4().hex[:8]}"

        # #46: every tmux op must ride the test socket so we cannot
        # collide with user sessions on the default socket. conftest
        # sets MAS_TMUX_SOCKET in our env; prepend `-L <socket>` here.
        tmux = _tmux_for_test()

        # Clean slate.
        subprocess.run(
            [*tmux, "kill-session", "-t", session],
            capture_output=True,
        )

        # Create primary + 2 secondaries + 1 stray auto-numbered session.
        subprocess.run(
            [*tmux, "new-session", "-d", "-s", session, "-n", "control"],
            check=True,
        )
        subprocess.run(
            [*tmux, "new-session", "-d", "-A", "-s", f"{session}-control",
             "-t", session],
            check=True,
        )
        subprocess.run(
            [*tmux, "new-session", "-d", "-A", "-s", f"{session}-agents",
             "-t", session],
            check=True,
        )
        # Stray auto-numbered grouped session (old start.sh behavior pre-fix)
        subprocess.run(
            [*tmux, "new-session", "-d", "-t", session],
            check=True,
        )

        # Verify pre-condition: multiple sessions in the group exist.
        pre = subprocess.run(
            [*tmux, "list-sessions", "-F",
             "#{session_name}|#{session_group}"],
            capture_output=True, text=True,
        )
        group_members = [
            line.split("|")[0]
            for line in pre.stdout.splitlines()
            if line.endswith("|" + session) or line.startswith(session + "|")
        ]
        assert len(group_members) >= 3, (
            f"Pre-condition: expected 3+ grouped sessions, got {group_members}"
        )

        # Create a minimal project directory so stop.sh doesn't error
        # on the MCP configs cleanup.
        project_dir = tmp_path / "projects" / session
        project_dir.mkdir(parents=True)

        try:
            result = _run_stop_script(session, cwd=str(tmp_path))
            assert result.returncode == 0, (
                f"stop.sh failed: stdout={result.stdout!r} stderr={result.stderr!r}"
            )

            # Verify post-condition: NO sessions in the group remain.
            post = subprocess.run(
                [*tmux, "list-sessions", "-F",
                 "#{session_name}|#{session_group}"],
                capture_output=True, text=True,
            )
            remaining = [
                line for line in post.stdout.splitlines()
                if session in line
            ]
            assert len(remaining) == 0, (
                f"All grouped sessions should be killed, but found: {remaining}"
            )
        finally:
            # Belt-and-suspenders cleanup
            for suffix in ["", "-control", "-agents"]:
                subprocess.run(
                    [*tmux, "kill-session", "-t", f"{session}{suffix}"],
                    capture_output=True,
                )


# ===========================================================================
# 5. IDEMPOTENT BEHAVIOR (ALREADY STOPPED)
# ===========================================================================


class TestIdempotentStop:
    """stop.sh must be idempotent: prints 'already stopped' if no session."""

    def test_prints_already_stopped_when_no_session(self):
        """stop.sh must print 'already stopped' if tmux session doesn't exist."""
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "false"

        result = _run_stop_script("demo", env_overrides=env)
        output = (result.stdout + result.stderr).lower()
        assert "already stopped" in output, (
            f"Must print 'already stopped' when session doesn't exist, got: {output}"
        )

    def test_exits_0_when_already_stopped(self):
        """stop.sh must exit 0 even if session doesn't exist."""
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "false"

        result = _run_stop_script("demo", env_overrides=env)
        assert result.returncode == 0, (
            f"Must exit 0 when already stopped, got exit code: {result.returncode}"
        )

    def test_does_not_error_on_repeated_calls(self):
        """Calling stop.sh multiple times must not produce errors."""
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "false"

        result1 = _run_stop_script("demo", env_overrides=env)
        result2 = _run_stop_script("demo", env_overrides=env)
        assert result1.returncode == 0
        assert result2.returncode == 0


# ===========================================================================
# 6. MISSING/INVALID ARGUMENTS
# ===========================================================================


class TestStopArguments:
    """stop.sh must handle missing/invalid arguments properly."""

    def test_no_arguments_shows_usage(self):
        """Running stop.sh without arguments must show usage info."""
        result = subprocess.run(
            [STOP_SCRIPT],
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

    def test_project_argument_is_used(self):
        """The project argument must identify which session to stop."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("myproject", env_overrides=env)
        output = result.stdout + result.stderr
        assert "myproject" in output, (
            "Project name must appear in script output"
        )


# ===========================================================================
# 7. EXIT CODES
# ===========================================================================


class TestStopExitCodes:
    """stop.sh must exit 0 on success (or already stopped), 1 on bad args."""

    def test_exit_0_on_successful_stop(self):
        """Must exit 0 when session is successfully stopped."""
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "true"

        result = _run_stop_script("demo", env_overrides=env)
        assert result.returncode == 0

    def test_exit_0_when_already_stopped(self):
        """Must exit 0 when session doesn't exist (idempotent)."""
        env = os.environ.copy()
        env["_TEST_SESSION_EXISTS"] = "false"

        result = _run_stop_script("demo", env_overrides=env)
        assert result.returncode == 0

    def test_exit_1_on_no_arguments(self):
        """Must exit 1 when called without arguments."""
        result = subprocess.run(
            [STOP_SCRIPT],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
        assert result.returncode == 1
