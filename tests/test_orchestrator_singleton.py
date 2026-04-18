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


# ===========================================================================
# 4. BOUNCE FOLLOW-UPS (manager feedback on 9e5a085)
# ===========================================================================


class TestBounceNoLockPsFallback:
    """Bounce must find a running orch via `ps` when no lock file exists.

    Migration case: the first bounce after adopting the flock code has
    an old pre-flock orch running but no lock file. The script must
    find and kill it via pattern matching on `ps` output.
    """

    def test_ps_fallback_finds_running_orch(self):
        """With no lock file, _TEST_PS_OVERRIDE feeds fake ps output and
        the script must identify the orch pid from it."""
        project = "ps_fallback_test"
        lock = f"/tmp/mas-orch-{project}.lock"
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass

        # Start a real sleep process we can legitimately kill.
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            # Hand-crafted ps output that looks like a running orch
            # but points at our holder's real pid.
            fake_ps = (
                f"{holder.pid} python3 -m orchestrator {project}\n"
                "9999 /bin/bash -c something-unrelated\n"
                "1234 /usr/bin/node index.js\n"
            )

            env = os.environ.copy()
            env["_TEST_SKIP_TMUX"] = "true"
            env["_TEST_PS_OVERRIDE"] = fake_ps

            result = subprocess.run(
                [BOUNCE_SCRIPT, project],
                capture_output=True, text=True, env=env, timeout=15,
            )
            assert result.returncode == 0, (
                f"bounce should succeed, got: {result.stderr!r}"
            )
            combined = result.stdout + result.stderr
            assert "found running orch via ps" in combined.lower(), (
                f"Expected ps-fallback log line, got: {combined}"
            )
            assert str(holder.pid) in combined

            # Verify the holder was actually killed.
            for _ in range(20):
                if holder.poll() is not None:
                    break
                time.sleep(0.25)
            assert holder.poll() is not None, (
                "ps-fallback did not kill the running process"
            )
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            if os.path.exists(lock):
                os.unlink(lock)

    def test_ps_fallback_ignores_non_matching_processes(self):
        """ps output without the target project name must yield 'nothing to stop'."""
        project = "ps_nomatch_test"
        lock = f"/tmp/mas-orch-{project}.lock"
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass

        # ps output mentions orchestrator but for a DIFFERENT project.
        fake_ps = (
            "12345 python3 -m orchestrator someOtherProject\n"
            "67890 /bin/bash some-script\n"
        )

        env = os.environ.copy()
        env["_TEST_SKIP_TMUX"] = "true"
        env["_TEST_PS_OVERRIDE"] = fake_ps

        result = subprocess.run(
            [BOUNCE_SCRIPT, project],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "nothing to stop" in combined.lower(), (
            f"Expected 'nothing to stop' when no match, got: {combined}"
        )
        assert "found running orch via ps" not in combined.lower()

    def test_ps_fallback_skipped_when_lock_file_present(self):
        """When a valid lock file exists, the ps fallback must NOT run —
        the lock file is the source of truth."""
        project = "ps_skip_test"
        lock = f"/tmp/mas-orch-{project}.lock"

        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            with open(lock, "w") as f:
                f.write(str(holder.pid))

            # ps output that would point at a DIFFERENT pid — we want
            # to prove the script ignored it and used the lock file.
            fake_ps = "55555 python3 -m orchestrator ps_skip_test\n"

            env = os.environ.copy()
            env["_TEST_SKIP_TMUX"] = "true"
            env["_TEST_PS_OVERRIDE"] = fake_ps

            result = subprocess.run(
                [BOUNCE_SCRIPT, project],
                capture_output=True, text=True, env=env, timeout=15,
            )
            assert result.returncode == 0
            combined = result.stdout + result.stderr
            # Must NOT mention the ps-fallback path.
            assert "found running orch via ps" not in combined.lower()
            # Must mention the real holder pid from the lock file.
            assert str(holder.pid) in combined
            # The 55555 pid from fake ps must NOT appear in logs.
            assert "55555" not in combined
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            if os.path.exists(lock):
                os.unlink(lock)


class TestBouncePaneLookup:
    """Bounce must find the orchestrator pane by @label, not hardcoded index.

    After Fix 5, the orchestrator pane lives at control.1 when a
    manager is configured (control.0 is the manager). Sending the orch
    launch command into control.0 would clobber the manager pane.
    """

    def test_dry_run_uses_test_pane_lookup_override(self):
        """The _TEST_PANE_LOOKUP env var must force the resolved pane,
        proving the script is parameterizable."""
        project = "pane_lookup_test"
        try:
            os.unlink(f"/tmp/mas-orch-{project}.lock")
        except FileNotFoundError:
            pass

        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["_TEST_PANE_LOOKUP"] = f"{project}:control.1"

        result = subprocess.run(
            [BOUNCE_SCRIPT, project],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert f"-t {project}:control.1" in combined, (
            f"Expected pane override to be used, got: {combined}"
        )
        # The default control.0 must NOT be referenced.
        assert f"-t {project}:control.0" not in combined

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not installed",
    )
    def test_real_tmux_finds_orchestrator_at_control_1(self):
        """Integration: set up a real tmux control window where pane 0 is
        labelled 'manager' and pane 1 is labelled 'orchestrator' (the
        Fix 5 layout). Verify bounce resolves to control.1, not .0."""
        import uuid

        project = f"bouncepane-{uuid.uuid4().hex[:8]}"
        session = project
        tmux = _tmux_for_test()  # #46: dedicated socket

        # Clean slate.
        subprocess.run(
            [*tmux, "kill-session", "-t", session], capture_output=True
        )

        try:
            # Create the Fix 5 layout: manager (0, left) | orch (1, top-right).
            subprocess.run(
                [*tmux, "new-session", "-d", "-s", session,
                 "-n", "control", "-x", "200", "-y", "50"],
                check=True,
            )
            subprocess.run(
                [*tmux, "set-option", "-p",
                 "-t", f"{session}:control.0", "@label", "manager"],
                check=True,
            )
            subprocess.run(
                [*tmux, "split-window", "-h", "-t", f"{session}:control.0"],
                check=True,
            )
            subprocess.run(
                [*tmux, "set-option", "-p",
                 "-t", f"{session}:control.1", "@label", "orchestrator"],
                check=True,
            )

            env = os.environ.copy()
            env["_TEST_DRY_RUN"] = "true"

            result = subprocess.run(
                [BOUNCE_SCRIPT, project],
                capture_output=True, text=True, env=env, timeout=10,
            )
            assert result.returncode == 0
            combined = result.stdout + result.stderr

            # The dry-run must target control.1 (where orchestrator is)
            # NOT control.0 (where manager is).
            assert f"-t {session}:control.1" in combined, (
                f"Expected label-based lookup to find control.1, got: {combined}"
            )
            assert f"-t {session}:control.0" not in combined, (
                "bounce must NOT target control.0 (manager pane)"
            )
        finally:
            subprocess.run(
                [*tmux, "kill-session", "-t", session], capture_output=True
            )
            try:
                os.unlink(f"/tmp/mas-orch-{project}.lock")
            except FileNotFoundError:
                pass

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not installed",
    )
    def test_real_tmux_falls_back_when_no_orch_label(self):
        """If no pane has @label=orchestrator, fall back to control.0."""
        import uuid

        project = f"bouncefb-{uuid.uuid4().hex[:8]}"
        session = project
        tmux = _tmux_for_test()  # #46: dedicated socket

        subprocess.run(
            [*tmux, "kill-session", "-t", session], capture_output=True
        )

        try:
            subprocess.run(
                [*tmux, "new-session", "-d", "-s", session,
                 "-n", "control", "-x", "200", "-y", "50"],
                check=True,
            )
            # Set a label that is NOT "orchestrator".
            subprocess.run(
                [*tmux, "set-option", "-p",
                 "-t", f"{session}:control.0", "@label", "something-else"],
                check=True,
            )

            env = os.environ.copy()
            env["_TEST_DRY_RUN"] = "true"

            result = subprocess.run(
                [BOUNCE_SCRIPT, project],
                capture_output=True, text=True, env=env, timeout=10,
            )
            assert result.returncode == 0
            combined = result.stdout + result.stderr

            # Fallback must use control.0.
            assert f"-t {session}:control.0" in combined, (
                f"Expected fallback to control.0, got: {combined}"
            )
        finally:
            subprocess.run(
                [*tmux, "kill-session", "-t", session], capture_output=True
            )
            try:
                os.unlink(f"/tmp/mas-orch-{project}.lock")
            except FileNotFoundError:
                pass
