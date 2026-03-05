"""
Tests for scripts/setup-nats.sh -- NATS Installation and Startup Script

TDD Contract (RED phase):
These tests define the expected behavior of the setup-nats.sh utility script.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R8: Scripts (setup-nats.sh installs nats-server + nats CLI, starts with JetStream)
  - R3: Communication Flow (NATS JetStream is the data layer)

Acceptance criteria from task rgr-12:
  1. setup-nats.sh installs nats-server and nats CLI via brew
  2. setup-nats.sh starts nats-server with JetStream enabled

Test categories:
  1. Script existence and executability
  2. Brew installation of nats-server
  3. Brew installation of nats CLI
  4. JetStream-enabled startup
  5. Idempotent behavior (already installed/running)
  6. Exit codes
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
SETUP_NATS_SCRIPT = os.path.join(REPO_ROOT, "scripts", "setup-nats.sh")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_setup_nats(env_overrides=None, cwd=None):
    """Helper to run setup-nats.sh and capture output."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [SETUP_NATS_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or REPO_ROOT,
        timeout=60,
    )
    return result


# ===========================================================================
# 1. SCRIPT EXISTENCE AND EXECUTABILITY
# ===========================================================================


class TestSetupNatsScriptExists:
    """setup-nats.sh must exist at scripts/setup-nats.sh and be executable."""

    def test_setup_nats_script_exists(self):
        """scripts/setup-nats.sh must exist."""
        assert os.path.isfile(SETUP_NATS_SCRIPT), (
            f"scripts/setup-nats.sh not found at {SETUP_NATS_SCRIPT}"
        )

    def test_setup_nats_script_is_executable(self):
        """scripts/setup-nats.sh must have execute permissions."""
        assert os.path.isfile(SETUP_NATS_SCRIPT), "Script must exist first"
        mode = os.stat(SETUP_NATS_SCRIPT).st_mode
        assert mode & stat.S_IXUSR, "scripts/setup-nats.sh must be executable by owner"

    def test_setup_nats_script_has_shebang(self):
        """scripts/setup-nats.sh must start with a proper shebang line."""
        assert os.path.isfile(SETUP_NATS_SCRIPT), "Script must exist first"
        with open(SETUP_NATS_SCRIPT, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!"), (
            f"Expected shebang, got: {first_line}"
        )
        assert "bash" in first_line or "sh" in first_line, (
            f"Shebang must reference bash or sh, got: {first_line}"
        )


# ===========================================================================
# 2. BREW INSTALLATION OF NATS-SERVER
# ===========================================================================


class TestNatsServerInstall:
    """setup-nats.sh must install nats-server via brew."""

    def test_installs_nats_server_via_brew(self):
        """Must use brew to install nats-server."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "false"
        env["_TEST_NATS_CLI_INSTALLED"] = "false"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        assert "brew" in output.lower(), (
            f"Must use brew for installation, got: {output}"
        )
        assert "nats-server" in output.lower(), (
            f"Must install nats-server, got: {output}"
        )

    def test_skips_install_if_nats_server_already_installed(self):
        """If nats-server is already installed, should skip installation."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "true"
        env["_TEST_NATS_CLI_INSTALLED"] = "true"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        # Should either not try to install or indicate it's already present
        assert (
            "already" in output.lower()
            or "skip" in output.lower()
            or "install" not in output.lower()
            or "brew install nats-server" not in output.lower()
        ), (
            f"Should skip nats-server install if already installed, got: {output}"
        )

    def test_references_brew_install(self):
        """The script must contain or output a brew install command for nats-server."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "false"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        # Must reference "brew install nats-server" or similar
        assert "brew" in output.lower() and "nats-server" in output.lower(), (
            f"Must reference brew install for nats-server, got: {output}"
        )


# ===========================================================================
# 3. BREW INSTALLATION OF NATS CLI
# ===========================================================================


class TestNatsCliInstall:
    """setup-nats.sh must install the nats CLI via brew."""

    def test_installs_nats_cli_via_brew(self):
        """Must use brew to install the nats CLI tool."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "false"
        env["_TEST_NATS_CLI_INSTALLED"] = "false"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        assert "brew" in output.lower(), (
            f"Must use brew for installation, got: {output}"
        )
        # nats CLI is typically installed via "brew install nats-io/nats-tools/nats"
        # or "brew tap nats-io/nats-tools && brew install nats"
        assert "nats" in output.lower(), (
            f"Must install nats CLI, got: {output}"
        )

    def test_skips_install_if_nats_cli_already_installed(self):
        """If nats CLI is already installed, should skip installation."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "true"
        env["_TEST_NATS_CLI_INSTALLED"] = "true"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        assert (
            "already" in output.lower()
            or "skip" in output.lower()
            or result.returncode == 0
        ), (
            f"Should skip nats CLI install if already installed, got: {output}"
        )


# ===========================================================================
# 4. JETSTREAM-ENABLED STARTUP
# ===========================================================================


class TestJetStreamStartup:
    """setup-nats.sh must start nats-server with JetStream enabled."""

    def test_starts_nats_with_jetstream(self):
        """nats-server must be started with JetStream flag (-js)."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_RUNNING"] = "false"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        # JetStream is enabled via -js flag or --jetstream flag
        assert any(flag in output for flag in ["-js", "--jetstream", "jetstream"]), (
            f"Must start nats-server with JetStream enabled, got: {output}"
        )

    def test_starts_nats_server_process(self):
        """Must actually start the nats-server process."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_RUNNING"] = "false"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        assert "nats-server" in output.lower(), (
            f"Must start nats-server, got: {output}"
        )

    def test_skips_start_if_already_running(self):
        """If nats-server is already running, should skip startup."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_RUNNING"] = "true"

        result = _run_setup_nats(env_overrides=env)
        output = result.stdout + result.stderr
        assert (
            "already running" in output.lower()
            or "skip" in output.lower()
            or result.returncode == 0
        ), (
            f"Should indicate NATS is already running, got: {output}"
        )


# ===========================================================================
# 5. EXIT CODES
# ===========================================================================


class TestSetupNatsExitCodes:
    """setup-nats.sh must exit 0 on success."""

    def test_exit_0_on_success(self):
        """Must exit 0 when setup completes successfully."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "true"
        env["_TEST_NATS_CLI_INSTALLED"] = "true"
        env["_TEST_NATS_RUNNING"] = "true"

        result = _run_setup_nats(env_overrides=env)
        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}. stderr: {result.stderr}"
        )

    def test_exit_0_after_fresh_install(self):
        """Must exit 0 after installing and starting everything."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_NATS_SERVER_INSTALLED"] = "false"
        env["_TEST_NATS_CLI_INSTALLED"] = "false"
        env["_TEST_NATS_RUNNING"] = "false"

        result = _run_setup_nats(env_overrides=env)
        assert result.returncode == 0, (
            f"Expected exit code 0 after fresh install, got {result.returncode}"
        )
