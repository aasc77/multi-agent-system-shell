"""
Tests for scripts/nats-monitor.sh -- NATS Message Monitor Script

TDD Contract (RED phase):
These tests define the expected behavior of the nats-monitor.sh utility script.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R1: tmux Session Layout (NATS monitor pane runs nats sub "agents.>")
  - R8: Scripts (nats-monitor.sh subscribes to NATS subjects, optional filter)

Acceptance criteria from task rgr-12:
  1. nats-monitor.sh subscribes to agents.> by default
  2. nats-monitor.sh accepts optional subject filter argument

Test categories:
  1. Script existence and executability
  2. Default subject subscription (agents.>)
  3. Custom subject filter argument
  4. Uses nats CLI (nats sub command)
  5. Exit codes and error handling
"""

import os
import stat
import subprocess

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
NATS_MONITOR_SCRIPT = os.path.join(REPO_ROOT, "scripts", "nats-monitor.sh")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_nats_monitor(args=None, env_overrides=None, cwd=None, timeout=10):
    """Helper to run nats-monitor.sh and capture output.

    Note: nats-monitor.sh normally runs indefinitely (subscribing to NATS),
    so we use a short timeout and _TEST_DRY_RUN mode for testing.
    """
    cmd = [NATS_MONITOR_SCRIPT]
    if args:
        if isinstance(args, str):
            cmd.append(args)
        else:
            cmd.extend(args)

    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd or REPO_ROOT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # For a long-running subscriber, timeout is expected behavior
        # in non-dry-run mode. We construct a result-like object.
        return None

    return result


# ===========================================================================
# 1. SCRIPT EXISTENCE AND EXECUTABILITY
# ===========================================================================


class TestNatsMonitorScriptExists:
    """nats-monitor.sh must exist at scripts/nats-monitor.sh and be executable."""

    def test_nats_monitor_script_exists(self):
        """scripts/nats-monitor.sh must exist."""
        assert os.path.isfile(NATS_MONITOR_SCRIPT), (
            f"scripts/nats-monitor.sh not found at {NATS_MONITOR_SCRIPT}"
        )

    def test_nats_monitor_script_is_executable(self):
        """scripts/nats-monitor.sh must have execute permissions."""
        assert os.path.isfile(NATS_MONITOR_SCRIPT), "Script must exist first"
        mode = os.stat(NATS_MONITOR_SCRIPT).st_mode
        assert mode & stat.S_IXUSR, "scripts/nats-monitor.sh must be executable by owner"

    def test_nats_monitor_script_has_shebang(self):
        """scripts/nats-monitor.sh must start with a proper shebang line."""
        assert os.path.isfile(NATS_MONITOR_SCRIPT), "Script must exist first"
        with open(NATS_MONITOR_SCRIPT, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!"), (
            f"Expected shebang, got: {first_line}"
        )
        assert "bash" in first_line or "sh" in first_line, (
            f"Shebang must reference bash or sh, got: {first_line}"
        )


# ===========================================================================
# 2. DEFAULT SUBJECT SUBSCRIPTION (agents.>)
# ===========================================================================


class TestDefaultSubject:
    """nats-monitor.sh must subscribe to agents.> by default."""

    def test_subscribes_to_agents_wildcard_by_default(self):
        """When called without arguments, must subscribe to 'agents.>'."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor(env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        assert "agents.>" in output, (
            f"Must subscribe to 'agents.>' by default, got: {output}"
        )

    def test_uses_agents_wildcard_subject(self):
        """Default subject must be exactly 'agents.>'."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor(env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        # Verify the default subject appears in the nats sub command
        assert "agents.>" in output, (
            f"Default subject must be 'agents.>', got: {output}"
        )


# ===========================================================================
# 3. CUSTOM SUBJECT FILTER ARGUMENT
# ===========================================================================


class TestCustomSubjectFilter:
    """nats-monitor.sh must accept an optional subject filter argument."""

    def test_accepts_custom_subject(self):
        """Must accept a custom subject filter as first argument."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor("agents.writer.>", env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        assert "agents.writer.>" in output, (
            f"Must use custom subject 'agents.writer.>', got: {output}"
        )

    def test_custom_subject_overrides_default(self):
        """Custom subject must override the default 'agents.>'."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor("system.health", env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        assert "system.health" in output, (
            f"Must use custom subject 'system.health', got: {output}"
        )

    def test_accepts_specific_agent_inbox(self):
        """Must accept agent-specific inbox subjects."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor("agents.qa.inbox", env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        assert "agents.qa.inbox" in output, (
            f"Must accept 'agents.qa.inbox' as subject, got: {output}"
        )

    def test_accepts_broad_wildcard(self):
        """Must accept broad wildcard subjects like '>'."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor(">", env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        # The '>' wildcard should be passed to nats sub
        assert result.returncode == 0 or ">" in output, (
            f"Must accept '>' as a valid subject, got: {output}"
        )


# ===========================================================================
# 4. USES NATS CLI (nats sub command)
# ===========================================================================


class TestUsesNatsCli:
    """nats-monitor.sh must use the nats CLI 'nats sub' command."""

    def test_uses_nats_sub_command(self):
        """Must invoke 'nats sub' to subscribe to subjects."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor(env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        assert "nats sub" in output.lower() or "nats" in output.lower(), (
            f"Must use 'nats sub' command, got: {output}"
        )

    def test_invokes_nats_executable(self):
        """Must reference the 'nats' executable."""
        # Read the script content to verify it references 'nats sub'
        assert os.path.isfile(NATS_MONITOR_SCRIPT), "Script must exist first"
        with open(NATS_MONITOR_SCRIPT, "r") as f:
            content = f.read()
        assert "nats sub" in content or "nats " in content, (
            f"Script must contain 'nats sub' command"
        )

    def test_dry_run_shows_command(self):
        """In dry-run mode, must show the nats sub command that would be run."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor(env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        output = result.stdout + result.stderr
        # Should show the command with subject
        assert "nats" in output.lower() and "sub" in output.lower(), (
            f"Dry run must show nats sub command, got: {output}"
        )


# ===========================================================================
# 5. EXIT CODES AND ERROR HANDLING
# ===========================================================================


class TestNatsMonitorExitCodes:
    """nats-monitor.sh exit codes and error handling."""

    def test_exit_0_in_dry_run(self):
        """Must exit 0 in dry-run mode."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = _run_nats_monitor(env_overrides=env)
        assert result is not None, "Script should complete in dry-run mode"
        assert result.returncode == 0, (
            f"Expected exit code 0 in dry-run, got {result.returncode}"
        )

    def test_handles_missing_nats_cli(self):
        """Must handle case where nats CLI is not installed."""
        env = os.environ.copy()
        # Remove nats from PATH
        env["PATH"] = "/usr/bin:/bin"
        env["_TEST_NATS_CLI_MISSING"] = "true"

        result = _run_nats_monitor(env_overrides=env, timeout=5)
        if result is not None:
            output = (result.stdout + result.stderr).lower()
            # Should either error about missing nats or exit non-zero
            assert result.returncode != 0 or "nats" in output, (
                f"Must indicate nats CLI is missing or error, got: {output}"
            )
