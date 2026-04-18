"""Unit tests for manager worktree isolation (#45).

Covers:

1. ``scripts/setup-manager-worktree.sh`` exists, is executable, and
   its dry-run plan is what we expect (no-op when the worktree is
   already registered; forks ``manager-worktree`` from
   ``feat/manager-agent`` on a fresh repo).
2. ``scripts/setup-manager-worktree.sh`` is idempotent against a
   real temporary git repo — running it twice is safe and the second
   call exits 0 with no mutation.
3. ``scripts/start.sh`` invokes the setup script (greppable source
   check + dry-run verification).
4. ``projects/remote-test/config.yaml`` points manager at the
   worktree path and no longer points at the shared dir (``.``).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-manager-worktree.sh"
START_SCRIPT = REPO_ROOT / "scripts" / "start.sh"
CONFIG_PATH = REPO_ROOT / "projects" / "remote-test" / "config.yaml"


class TestSetupScriptMetadata:
    def test_script_exists(self):
        assert SETUP_SCRIPT.is_file(), (
            f"{SETUP_SCRIPT} must exist — #45 setup script."
        )

    def test_script_is_executable(self):
        mode = SETUP_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, (
            f"{SETUP_SCRIPT} must be user-executable; got mode {oct(mode)}."
        )

    def test_script_has_bash_shebang(self):
        first_line = SETUP_SCRIPT.read_text().splitlines()[0]
        assert first_line.startswith("#!") and "bash" in first_line

    def test_script_is_syntactically_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"syntax error in {SETUP_SCRIPT}: {result.stderr}"
        )


class TestSetupScriptDryRun:
    """Dry-run plan should not touch the filesystem or git state."""

    def test_dry_run_prints_git_worktree_command(self, tmp_path):
        """With a fresh path (no existing worktree registered there),
        dry-run should plan to fork the branch from feat/manager-agent.

        We intentionally point at ``tmp_path`` via MAS_MANAGER_WORKTREE
        so the test is independent of whether the real sibling path
        at ``/Users/angelserrano/Repositories/multi-agent-system-shell-manager``
        happens to exist on this machine (it will, after macmini QA
        or any prior test-install run).
        """
        fresh_path = tmp_path / "fresh-manager-worktree"
        result = subprocess.run(
            ["bash", str(SETUP_SCRIPT)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "_TEST_DRY_RUN": "true",
                "MAS_MANAGER_WORKTREE": str(fresh_path),
            },
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "[DRY-RUN]" in combined
        assert "git worktree add" in combined
        assert "manager-worktree" in combined
        assert str(fresh_path) in combined

    def test_dry_run_honors_mas_manager_worktree_override(self, tmp_path):
        """_TEST_DRY_RUN + MAS_MANAGER_WORKTREE should redirect the
        plan at an arbitrary path (used by the idempotency test)."""
        override = tmp_path / "override-worktree"
        result = subprocess.run(
            ["bash", str(SETUP_SCRIPT)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "_TEST_DRY_RUN": "true",
                "MAS_MANAGER_WORKTREE": str(override),
            },
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert str(override) in combined

    def test_mas_remote_name_override_routes_remote_branch_lookup(self, tmp_path):
        """MAS_REMOTE_NAME must change which remote the script checks
        for the remote-branch fallback.

        Per macmini QA on PR #59: default hardcode of REMOTE_NAME=github
        silently fell back to a stale local branch on checkouts whose
        remote is named 'origin'. Default is now 'origin' with env
        override. This test builds a throwaway repo with a fake remote
        named 'foo' that has a `refs/remotes/foo/manager-worktree` ref,
        runs the script with MAS_REMOTE_NAME=foo, and asserts the
        'Creating local branch ... tracking foo/manager-worktree' path
        fires. Same setup with MAS_REMOTE_NAME=origin (which doesn't
        exist in this test repo) must NOT find the remote ref.
        """
        repo_dir = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", "user.name", "T"],
            check=True,
        )
        (repo_dir / "README.md").write_text("seed\n")
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "README.md"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-q", "-m", "seed"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "branch", "-m", "feat/manager-agent"],
            check=True,
        )
        # Fabricate a remote-tracking ref under refs/remotes/foo/
        # pointing at the seed commit, so the script's
        # `git show-ref --verify refs/remotes/foo/manager-worktree`
        # succeeds.
        head_sha = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(repo_dir), "update-ref",
             "refs/remotes/foo/manager-worktree", head_sha],
            check=True,
        )

        test_scripts_dir = repo_dir / "scripts"
        test_scripts_dir.mkdir()
        dest = test_scripts_dir / "setup-manager-worktree.sh"
        dest.write_text(SETUP_SCRIPT.read_text())
        dest.chmod(0o755)

        worktree_path = tmp_path / "mgr-wt"

        # With MAS_REMOTE_NAME=foo the remote-branch fallback fires.
        foo_result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "_TEST_DRY_RUN": "true",
                "MAS_MANAGER_WORKTREE": str(worktree_path),
                "MAS_REMOTE_NAME": "foo",
            },
        )
        assert foo_result.returncode == 0, foo_result.stderr
        combined_foo = foo_result.stdout + foo_result.stderr
        assert "foo/manager-worktree" in combined_foo, (
            "MAS_REMOTE_NAME=foo should route remote lookup through "
            "refs/remotes/foo/manager-worktree, got:\n" + combined_foo
        )

        # With MAS_REMOTE_NAME=origin (absent in this test repo) the
        # remote fallback must NOT fire — the script should land on
        # the fresh-fork branch path instead.
        origin_result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "_TEST_DRY_RUN": "true",
                "MAS_MANAGER_WORKTREE": str(worktree_path),
                "MAS_REMOTE_NAME": "origin",
            },
        )
        assert origin_result.returncode == 0, origin_result.stderr
        combined_origin = origin_result.stdout + origin_result.stderr
        assert "origin/manager-worktree" not in combined_origin, (
            "MAS_REMOTE_NAME=origin should not find origin/manager-worktree "
            "in this test repo; got:\n" + combined_origin
        )
        # Sanity: the fresh-fork path mentions feat/manager-agent.
        assert "feat/manager-agent" in combined_origin


class TestSetupScriptIdempotencyInTempRepo:
    """Spin up a throwaway git repo with ``feat/manager-agent`` as a
    ref, point the setup script at a tmp worktree path via
    ``MAS_MANAGER_WORKTREE``, run it twice, and verify:

    - first run creates the worktree (exit 0, dir exists)
    - second run is a no-op (exit 0, "already present" message)
    """

    def _run_setup(self, tmp_repo: Path, worktree_path: Path, cwd: Path):
        return subprocess.run(
            ["bash", str(SETUP_SCRIPT)],
            capture_output=True, text=True,
            cwd=str(cwd),
            env={
                **os.environ,
                "MAS_MANAGER_WORKTREE": str(worktree_path),
            },
        )

    def _make_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Test"],
            check=True,
        )
        (path / "README.md").write_text("seed\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "README.md"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True,
        )
        # Rename default branch to feat/manager-agent so the setup
        # script's base-branch lookup succeeds.
        subprocess.run(
            ["git", "-C", str(path), "branch", "-m", "feat/manager-agent"],
            check=True,
        )

    def test_idempotent_create_then_noop(self, tmp_path, monkeypatch):
        """The setup script must:

        1. Create the worktree on first invocation.
        2. Exit 0 with "already present" on the second.
        """
        # Lay out repos: a "repo" and a sibling worktree path.
        repo_dir = tmp_path / "repo"
        self._make_repo(repo_dir)
        worktree_path = tmp_path / "manager-worktree"

        # We can't call the real scripts/setup-manager-worktree.sh
        # directly because it resolves REPO_ROOT from its own
        # location — it would always target the real repo. Copy the
        # script into the test repo and run it from there.
        test_scripts_dir = repo_dir / "scripts"
        test_scripts_dir.mkdir()
        dest = test_scripts_dir / "setup-manager-worktree.sh"
        dest.write_text(SETUP_SCRIPT.read_text())
        dest.chmod(0o755)

        # First invocation — creates the worktree.
        first = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "MAS_MANAGER_WORKTREE": str(worktree_path),
            },
        )
        assert first.returncode == 0, (
            f"first run failed: {first.stderr}"
        )
        assert worktree_path.is_dir(), (
            "first run did not create the worktree directory"
        )
        # The worktree checkout should contain the seed README.
        assert (worktree_path / "README.md").is_file()

        # Second invocation — idempotent no-op.
        second = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "MAS_MANAGER_WORKTREE": str(worktree_path),
            },
        )
        assert second.returncode == 0, (
            f"second run failed (expected no-op): {second.stderr}"
        )
        combined = second.stdout + second.stderr
        assert "already present" in combined.lower() or "no-op" in combined.lower(), (
            f"second run should be a no-op, got:\n{combined}"
        )

    def test_fails_cleanly_when_path_exists_but_is_not_worktree(self, tmp_path):
        """If the target path exists but isn't a registered worktree,
        the script must refuse with a clear error (not blow away
        the directory or produce a confusing git error)."""
        repo_dir = tmp_path / "repo"
        self._make_repo(repo_dir)
        conflict_path = tmp_path / "manager-worktree"
        conflict_path.mkdir()
        (conflict_path / "stuff.txt").write_text("stale checkout\n")

        test_scripts_dir = repo_dir / "scripts"
        test_scripts_dir.mkdir()
        dest = test_scripts_dir / "setup-manager-worktree.sh"
        dest.write_text(SETUP_SCRIPT.read_text())
        dest.chmod(0o755)

        result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={
                **os.environ,
                "MAS_MANAGER_WORKTREE": str(conflict_path),
            },
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "exists" in combined.lower()
        # Conflict file must still be there (we refused to touch it).
        assert (conflict_path / "stuff.txt").is_file()


class TestStartShInvokesSetup:
    def test_start_sh_source_calls_setup_script(self):
        """scripts/start.sh must reference setup-manager-worktree.sh
        so the worktree is ensured on every start."""
        src = START_SCRIPT.read_text()
        assert "setup-manager-worktree.sh" in src, (
            "scripts/start.sh must invoke scripts/setup-manager-worktree.sh "
            "before spawning manager's claude pane (#45)."
        )

    def test_start_sh_dry_run_plans_worktree_setup(self):
        """A dry-run of start.sh should reveal the setup-manager-worktree
        call in the emitted plan."""
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        result = subprocess.run(
            [str(START_SCRIPT), "demo"],
            capture_output=True, text=True, env=env, timeout=30,
            cwd=str(REPO_ROOT),
        )
        combined = result.stdout + result.stderr
        assert "setup-manager-worktree.sh" in combined, (
            f"start.sh dry-run should print the worktree setup call; got:\n"
            f"{combined}"
        )


class TestConfigYaml:
    def test_manager_working_dir_points_at_worktree(self):
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        manager = cfg["agents"]["manager"]
        assert manager["working_dir"] == "../multi-agent-system-shell-manager", (
            "manager.working_dir must point at the sibling worktree "
            "after #45, got: " + repr(manager.get("working_dir"))
        )

    def test_manager_no_longer_points_at_shared_dot(self):
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        manager = cfg["agents"]["manager"]
        assert manager["working_dir"] != ".", (
            "manager.working_dir must NOT be '.' (the shared dir) — "
            "#45 isolates it onto the worktree."
        )

    def test_hub_still_in_workspace(self):
        """Regression: only manager's working_dir changed. Hub's
        working_dir must remain ./workspace so hub keeps hitting the
        shared primary working directory (by design)."""
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        hub = cfg["agents"]["hub"]
        assert hub["working_dir"] == "./workspace"
