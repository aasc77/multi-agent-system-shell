"""
Tests for orchestrator singleton lock and scripts/bounce-orchestrator.sh.

These tests cover the fixes from the "orchestrator duplicate + stale
grouped sessions" incident:

  1. Two orchestrator processes for the same project cannot run
     concurrently — the second exits non-zero with a clear error.
  2. scripts/bounce-orchestrator.sh stops the existing orchestrator
     (via the lock file PID) and relaunches via tmux send-keys.
  3. scripts/bounce-orchestrator.sh is idempotent when no lock exists.

The flock is acquired in orchestrator/__main__.py at import time, BEFORE
any NATS / tmux / delivery init, so the second process exits immediately
without needing a full test environment. We short-circuit the rest of
the imports by pointing at an invalid project and checking that the
"already running" error appears BEFORE the ConfigError that would
otherwise surface.
"""

import os
import signal
import stat
import subprocess
import sys
import textwrap
import time

import pytest


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
BOUNCE_SCRIPT = os.path.join(REPO_ROOT, "scripts", "bounce-orchestrator.sh")


# ---------------------------------------------------------------------------
# Helper: run a lightweight Python process that acquires the orchestrator
# lock for a fake project name, then sleeps. We can't run the full
# orchestrator because it would try to connect to NATS.
# ---------------------------------------------------------------------------

def _spawn_lock_holder(project: str, tmp_lock_dir: str):
    """Spawn a subprocess that acquires the orchestrator flock and sleeps.

    Uses the same flock(/tmp/mas-orch-<project>.lock) protocol as
    orchestrator/__main__.py, so a second real orchestrator should be
    rejected.
    """
    script = textwrap.dedent(f"""
        import fcntl, os, sys, time
        lock_path = "{tmp_lock_dir}/mas-orch-{project}.lock"
        fd = open(lock_path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0); fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        time.sleep(30)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for the child to confirm it has the lock
    line = proc.stdout.readline()
    assert line.strip() == "LOCKED", f"Lock holder failed: {line}"
    return proc


# ===========================================================================
# 1. FLOCK PROTOCOL (standalone — does not import orchestrator)
# ===========================================================================


class TestFlockProtocol:
    """The flock protocol itself rejects a second attempt on the same file."""

    def test_second_flock_attempt_fails(self, tmp_path):
        """Two processes cannot hold LOCK_EX | LOCK_NB on the same file."""
        lock_path = tmp_path / "orch.lock"
        holder = _spawn_lock_holder("testproj", str(tmp_path))
        try:
            # Rename the child's lock file to the one we're about to test.
            # The child writes to /tmp/... by default; for this test we
            # want it under tmp_path, so we spawn a fresh one targeting
            # the exact file.
            holder.terminate()
            holder.wait(timeout=5)

            import fcntl
            fd1 = open(lock_path, "w")
            fcntl.flock(fd1.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            fd2 = open(lock_path, "w")
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Release first lock; second can then acquire.
            fcntl.flock(fd1.fileno(), fcntl.LOCK_UN)
            fd1.close()
            fcntl.flock(fd2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd2.close()
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)


# ===========================================================================
# 2. ORCHESTRATOR MODULE REJECTS DUPLICATE LAUNCH
# ===========================================================================


class TestOrchestratorSingleton:
    """Running `python3 -m orchestrator <project>` twice must fail the 2nd."""

    def _lock_path(self, project: str) -> str:
        return f"/tmp/mas-orch-{project}.lock"

    def _cleanup_lock(self, project: str) -> None:
        try:
            os.unlink(self._lock_path(project))
        except FileNotFoundError:
            pass

    def test_second_launch_rejected(self):
        """Second orchestrator launch must exit non-zero with 'already running'."""
        project = "singleton_test_project"
        self._cleanup_lock(project)
        # Spawn a holder that imports the first few lines of __main__.py's
        # flock logic. We can't run the real orchestrator because it needs
        # NATS; instead, we simulate the lock from __main__.py exactly.
        holder_script = textwrap.dedent(f"""
            import fcntl, os, sys, time
            lock_path = "/tmp/mas-orch-{project}.lock"
            fd = open(lock_path, "w")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.seek(0); fd.truncate()
            fd.write(str(os.getpid()))
            fd.flush()
            sys.stdout.write("LOCKED\\n")
            sys.stdout.flush()
            time.sleep(30)
        """)
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = holder.stdout.readline()
            assert line.strip() == "LOCKED", "Holder failed to acquire lock"

            # Now run the real orchestrator module. It should hit the
            # flock error and exit 1 BEFORE reaching the config load.
            result = subprocess.run(
                [sys.executable, "-m", "orchestrator", project],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
                timeout=15,
            )
            assert result.returncode == 1, (
                f"Expected exit 1, got {result.returncode}. "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
            assert "already running" in result.stderr, (
                f"Expected 'already running' error, got: {result.stderr!r}"
            )
            assert project in result.stderr
            # Should mention the lock file path so users can recover.
            assert self._lock_path(project) in result.stderr
        finally:
            if holder.poll() is None:
                holder.terminate()
                try:
                    holder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=5)
            self._cleanup_lock(project)

    def test_lock_released_on_process_exit(self):
        """After the first orchestrator dies, a second can acquire the lock."""
        project = "singleton_release_test"
        self._cleanup_lock(project)

        # First holder.
        holder_script = textwrap.dedent(f"""
            import fcntl, os, sys, time
            fd = open("/tmp/mas-orch-{project}.lock", "w")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(str(os.getpid())); fd.flush()
            sys.stdout.write("LOCKED\\n"); sys.stdout.flush()
            time.sleep(30)
        """)
        h1 = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert h1.stdout.readline().strip() == "LOCKED"
            # Kill it — kernel must release the flock.
            h1.terminate()
            h1.wait(timeout=5)

            # Second holder should now succeed.
            h2 = subprocess.Popen(
                [sys.executable, "-c", holder_script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                line = h2.stdout.readline()
                assert line.strip() == "LOCKED", (
                    f"Second holder failed to acquire lock: {line!r}"
                )
            finally:
                h2.terminate()
                h2.wait(timeout=5)
        finally:
            if h1.poll() is None:
                h1.kill()
                h1.wait(timeout=5)
            self._cleanup_lock(project)


# ===========================================================================
# 3. BOUNCE-ORCHESTRATOR.SH
# ===========================================================================


class TestBounceOrchestratorScript:
    """scripts/bounce-orchestrator.sh exists, is executable, handles edge cases."""

    def test_script_exists(self):
        assert os.path.isfile(BOUNCE_SCRIPT), (
            f"bounce-orchestrator.sh not found at {BOUNCE_SCRIPT}"
        )

    def test_script_is_executable(self):
        mode = os.stat(BOUNCE_SCRIPT).st_mode
        assert mode & stat.S_IXUSR, "bounce-orchestrator.sh must be executable"

    def test_script_has_shebang(self):
        with open(BOUNCE_SCRIPT) as f:
            first = f.readline().strip()
        assert first.startswith("#!"), f"Missing shebang: {first}"
        assert "bash" in first or "sh" in first

    def test_no_lock_file_is_idempotent(self, tmp_path):
        """Running bounce when no lock file exists must succeed without error."""
        project = "bounce_noop_test"
        lock = f"/tmp/mas-orch-{project}.lock"
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass

        env = os.environ.copy()
        env["_TEST_SKIP_TMUX"] = "true"

        result = subprocess.run(
            [BOUNCE_SCRIPT, project],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0, (
            f"Expected exit 0 when lock missing, got {result.returncode}. "
            f"stderr={result.stderr!r}"
        )
        assert "nothing to stop" in (result.stdout + result.stderr).lower()

    def test_stale_lock_file_is_cleaned_up(self, tmp_path):
        """If lock file points to a dead pid, bounce must remove it."""
        project = "bounce_stale_test"
        lock = f"/tmp/mas-orch-{project}.lock"
        with open(lock, "w") as f:
            # PID 1 is init — we can't kill it, but kill -0 on it would
            # succeed for root and may fail for regular users; safer to
            # write a deliberately absurd pid that can't exist.
            f.write("999999999")

        env = os.environ.copy()
        env["_TEST_SKIP_TMUX"] = "true"

        try:
            result = subprocess.run(
                [BOUNCE_SCRIPT, project],
                capture_output=True, text=True, env=env, timeout=10,
            )
            assert result.returncode == 0, (
                f"Expected exit 0, got {result.returncode}. "
                f"stderr={result.stderr!r}"
            )
            assert not os.path.exists(lock), (
                "Stale lock file should be removed"
            )
        finally:
            if os.path.exists(lock):
                os.unlink(lock)

    def test_kills_live_lock_holder(self):
        """Bounce must SIGTERM a live process whose pid is in the lock file."""
        project = "bounce_live_test"
        lock = f"/tmp/mas-orch-{project}.lock"
        # Start a sleep process we'll pretend is the orchestrator.
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            with open(lock, "w") as f:
                f.write(str(holder.pid))

            env = os.environ.copy()
            env["_TEST_SKIP_TMUX"] = "true"

            result = subprocess.run(
                [BOUNCE_SCRIPT, project],
                capture_output=True, text=True, env=env, timeout=15,
            )
            assert result.returncode == 0, (
                f"bounce-orchestrator failed: {result.stderr!r}"
            )

            # Process should be dead within ~5s of SIGTERM.
            for _ in range(20):
                if holder.poll() is not None:
                    break
                time.sleep(0.25)
            assert holder.poll() is not None, (
                "Bounce did not stop the live lock holder"
            )
            assert not os.path.exists(lock), (
                "Lock file should be removed after bounce"
            )
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            if os.path.exists(lock):
                os.unlink(lock)

    def test_dry_run_shows_launch_command(self):
        """Dry-run mode must print the python3 -m orchestrator launch line."""
        project = "bounce_dryrun_test"
        lock = f"/tmp/mas-orch-{project}.lock"
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass

        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"

        result = subprocess.run(
            [BOUNCE_SCRIPT, project],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "python3 -m orchestrator" in combined, (
            f"Dry-run must mention the orchestrator launch command, got: {combined}"
        )
        assert project in combined
