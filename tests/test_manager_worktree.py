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

    def test_fails_when_worktree_exists_on_wrong_branch(self, tmp_path):
        """#61 Observation 2: if the target path already IS a git
        worktree but on a different branch than `manager-worktree`,
        the setup script must error with an actionable message — not
        silently no-op (which would leave manager pointing at the
        wrong branch).

        Before #61 the idempotency check was path-based only: any
        worktree at the target path was treated as \"already set up\".
        After #61 it must also verify the branch.
        """
        repo_dir = tmp_path / "repo"
        self._make_repo(repo_dir)

        # Add a second branch so we can put a worktree on it.
        subprocess.run(
            ["git", "-C", str(repo_dir), "branch", "decoy-branch"],
            check=True,
        )
        worktree_path = tmp_path / "mgr-wt-wrong-branch"
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add",
             str(worktree_path), "decoy-branch"],
            check=True, capture_output=True,
        )
        assert (worktree_path / "README.md").is_file(), (
            "precondition: decoy worktree should have the seed README"
        )

        # Run setup against the existing-but-wrong-branch worktree.
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
                "MAS_MANAGER_WORKTREE": str(worktree_path),
            },
        )
        assert result.returncode != 0, (
            "setup must error non-zero when the worktree is on the "
            "wrong branch; got returncode=0"
        )
        combined = result.stdout + result.stderr
        assert "decoy-branch" in combined, (
            "error message must name the actual branch so the "
            "operator can identify the stale worktree: " + combined
        )
        assert "manager-worktree" in combined, (
            "error message must name the expected branch: " + combined
        )
        assert "git worktree remove" in combined, (
            "error must include the actionable remediation command: "
            + combined
        )
        # Worktree must not have been mutated — README still there,
        # still on decoy-branch, still has the same contents.
        assert (worktree_path / "README.md").is_file()
        branch_here = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse",
             "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert branch_here == "decoy-branch", (
            f"worktree branch must not have been touched; got {branch_here!r}"
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


class TestBaseBranchPriority:
    """#60: when forking `manager-worktree` from `feat/manager-agent`,
    setup-manager-worktree.sh must prefer the REMOTE ref over the
    local ref so the manager worktree tracks upstream state rather
    than whatever local work dev happens to have in progress.

    These tests all rebuild a throwaway repo with a known local +
    remote-tracking base, run setup against a fresh worktree path
    (so the `manager-worktree` branch doesn't yet exist and the
    fork-from-base code path fires), and inspect the resulting
    worktree HEAD and log messages.
    """

    def _make_repo_with_remote_base(
        self, path: Path, local_sha_msg: str, remote_sha_msg: str,
    ) -> tuple[str, str]:
        """Build a repo with:
        - a local ``feat/manager-agent`` branch pointing at a commit
          whose message is ``local_sha_msg``
        - a remote-tracking ref ``refs/remotes/origin/feat/manager-agent``
          pointing at a (different) commit whose message is
          ``remote_sha_msg``

        The two commits are unrelated siblings off the seed commit, so
        neither is an ancestor of the other. Returns (local_sha,
        remote_sha).

        Lets tests verify which base the script picked purely by the
        resulting worktree HEAD message — no external fetch, no live
        network.
        """
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "T"],
            check=True,
        )
        (path / "README.md").write_text("seed\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "README.md"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", "seed"],
            check=True,
        )
        # Rename default branch to BASE_BRANCH so "local base" exists.
        subprocess.run(
            ["git", "-C", str(path), "branch", "-m", "feat/manager-agent"],
            check=True,
        )
        # Add a second commit to feat/manager-agent — this is the
        # "stale local" state.
        (path / "local.txt").write_text("local\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "local.txt"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", local_sha_msg],
            check=True,
        )
        local_sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Now fabricate a remote-tracking ref that points at a
        # different commit (simulating "origin is ahead / diverged").
        # Create an orphan temporary branch, commit, capture the sha,
        # then update-ref the remote-tracking ref, and delete the
        # temp branch so the local feat/manager-agent remains at
        # local_sha.
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q",
             "-b", "__tmp_remote_base", "HEAD~1"],
            check=True,
        )
        (path / "remote.txt").write_text("remote-ahead\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "remote.txt"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", remote_sha_msg],
            check=True,
        )
        remote_sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(path), "update-ref",
             "refs/remotes/origin/feat/manager-agent", remote_sha],
            check=True,
        )
        # Restore the working tree to local.
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "feat/manager-agent"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "branch", "-qD", "__tmp_remote_base"],
            check=True,
        )
        return local_sha, remote_sha

    def _install_setup_script(self, repo_dir: Path) -> Path:
        scripts_dir = repo_dir / "scripts"
        scripts_dir.mkdir()
        dest = scripts_dir / "setup-manager-worktree.sh"
        dest.write_text(SETUP_SCRIPT.read_text())
        dest.chmod(0o755)
        return dest

    def test_prefers_remote_when_local_is_stale(self, tmp_path):
        """Local feat/manager-agent at commit A, remote at commit B
        (ahead). Setup must fork manager-worktree off B, and the log
        must call out that local is behind.
        """
        repo_dir = tmp_path / "repo"
        local_sha, remote_sha = self._make_repo_with_remote_base(
            repo_dir,
            local_sha_msg="LOCAL stale commit",
            remote_sha_msg="REMOTE fresh commit",
        )
        dest = self._install_setup_script(repo_dir)
        worktree_path = tmp_path / "mgr-wt"

        result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={**os.environ, "MAS_MANAGER_WORKTREE": str(worktree_path)},
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "Using remote base" in combined, (
            "log must announce remote was preferred:\n" + combined
        )
        # Divergence summary should name one of "behind" / "ahead" /
        # "diverged" — we only check "behind" here since that's
        # the shape for this test.
        assert "behind" in combined, (
            "log must describe local as behind when remote is ahead: "
            + combined
        )
        # Verify the worktree's HEAD points at the REMOTE sha, not
        # the local sha.
        head_here = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_here == remote_sha, (
            f"worktree HEAD must match remote ({remote_sha}), not "
            f"local ({local_sha}); got {head_here}"
        )

    def test_falls_back_to_local_when_remote_missing(self, tmp_path):
        """No remote-tracking ref configured. Setup must fall back to
        the local feat/manager-agent and log that the remote wasn't
        available.
        """
        repo_dir = tmp_path / "repo"
        # Reuse the helper then DROP the remote ref so only local
        # exists — that matches "remote not configured / unreachable".
        local_sha, _remote_sha = self._make_repo_with_remote_base(
            repo_dir,
            local_sha_msg="LOCAL only",
            remote_sha_msg="UNUSED",
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "update-ref", "-d",
             "refs/remotes/origin/feat/manager-agent"],
            check=True,
        )

        dest = self._install_setup_script(repo_dir)
        worktree_path = tmp_path / "mgr-wt-local-only"

        result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={**os.environ, "MAS_MANAGER_WORKTREE": str(worktree_path)},
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "Using local base" in combined, (
            "log must announce local-fallback path:\n" + combined
        )
        assert "remote" in combined.lower(), (
            "log must mention the remote is not configured / unreachable: "
            + combined
        )
        head_here = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_here == local_sha

    def test_prefers_remote_even_when_local_is_ahead(self, tmp_path):
        """Design decision (#60): if local `feat/manager-agent` is
        AHEAD of the remote (dev has unpushed commits), the manager
        worktree must STILL fork off the remote.

        Rationale: the manager worktree is a clean checkout for
        fun-mode writes and operational scripts. It should match
        what the rest of the fleet sees on origin, not dev's private
        unpushed work. The log should announce the divergence so dev
        isn't surprised.
        """
        repo_dir = tmp_path / "repo"
        # Build the repo so local is ahead of remote: create the
        # remote at the seed commit, then keep adding commits to
        # local.
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
        # Point the remote at the seed commit BEFORE adding local commits.
        seed_sha = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(repo_dir), "update-ref",
             "refs/remotes/origin/feat/manager-agent", seed_sha],
            check=True,
        )
        # Now add 2 local commits — local is ahead of remote by 2.
        for i in range(2):
            (repo_dir / f"local{i}.txt").write_text(f"ahead-{i}\n")
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", f"local{i}.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit",
                 "-q", "-m", f"LOCAL ahead {i}"],
                check=True,
            )
        local_sha = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert local_sha != seed_sha  # precondition: local IS ahead

        dest = self._install_setup_script(repo_dir)
        worktree_path = tmp_path / "mgr-wt-ahead"

        result = subprocess.run(
            ["bash", str(dest)],
            capture_output=True, text=True,
            env={**os.environ, "MAS_MANAGER_WORKTREE": str(worktree_path)},
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "Using remote base" in combined, (
            "log must announce remote was preferred even when local is "
            "ahead: " + combined
        )
        assert "ahead" in combined, (
            "log must describe local as ahead so dev sees the divergence: "
            + combined
        )
        head_here = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_here == seed_sha, (
            "worktree must match remote (seed), not the ahead local: "
            f"expected {seed_sha}, got {head_here}"
        )

    def _build_ahead_repo(self, tmp_path, ahead_count: int) -> tuple[Path, Path]:
        """Helper: build a repo with local feat/manager-agent exactly
        ``ahead_count`` commits ahead of origin/feat/manager-agent.
        Returns (repo_dir, worktree_path).
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
        seed_sha = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Pin remote at the seed commit.
        subprocess.run(
            ["git", "-C", str(repo_dir), "update-ref",
             "refs/remotes/origin/feat/manager-agent", seed_sha],
            check=True,
        )
        for i in range(ahead_count):
            (repo_dir / f"local{i}.txt").write_text(f"ahead-{i}\n")
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", f"local{i}.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit",
                 "-q", "-m", f"LOCAL ahead {i}"],
                check=True,
            )
        self._install_setup_script(repo_dir)
        return repo_dir, tmp_path / "mgr-wt"

    def test_log_text_no_duplicate_local_prefix(self, tmp_path):
        """#71: the log wrapper says \"(local '<branch>' is ${state})\",
        so the state string must NOT start with another \"local \" —
        otherwise operators see \"(local 'feat/manager-agent' is local
        11 commits behind)\" with the word duplicated.
        """
        repo_dir, worktree_path = self._build_ahead_repo(tmp_path, ahead_count=3)
        result = subprocess.run(
            ["bash", str(repo_dir / "scripts" / "setup-manager-worktree.sh")],
            capture_output=True, text=True,
            env={**os.environ, "MAS_MANAGER_WORKTREE": str(worktree_path)},
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "is local " not in combined, (
            "state string must not re-introduce the 'local' prefix "
            "that the surrounding log already provides; got:\n"
            + combined
        )
        # The correct phrasing is still present.
        assert "is 3 commits ahead" in combined, combined

    def test_log_text_singular_commit_when_ahead_by_one(self, tmp_path):
        """#71: N=1 must render as ``1 commit`` (singular), not
        ``1 commits`` (plural-s).
        """
        repo_dir, worktree_path = self._build_ahead_repo(tmp_path, ahead_count=1)
        result = subprocess.run(
            ["bash", str(repo_dir / "scripts" / "setup-manager-worktree.sh")],
            capture_output=True, text=True,
            env={**os.environ, "MAS_MANAGER_WORKTREE": str(worktree_path)},
        )
        assert result.returncode == 0, result.stderr
        combined = result.stdout + result.stderr
        assert "is 1 commit ahead" in combined, (
            "N=1 must render as singular 'commit' without plural-s; "
            "got:\n" + combined
        )
        assert "1 commits" not in combined, (
            "plural-s at N=1 is the exact bug this test is pinning"
        )


class TestStartShInvokesSetup:
    def test_start_sh_source_calls_setup_script(self):
        """scripts/start.sh must reference setup-manager-worktree.sh
        so the worktree is ensured on every start."""
        src = START_SCRIPT.read_text()
        assert "setup-manager-worktree.sh" in src, (
            "scripts/start.sh must invoke scripts/setup-manager-worktree.sh "
            "before spawning manager's claude pane (#45)."
        )

    def test_start_sh_dry_run_plans_worktree_setup(self, tmp_path):
        """A dry-run of start.sh must recurse into the setup script's
        dry-run so the operator sees the full git-ops plan inline (#61
        Observation 1).

        Without the recursion, `start.sh` dry-run just prints the
        `bash scripts/setup-manager-worktree.sh` *invocation* but not
        the `git worktree add ...` the setup would actually run. The
        point of a dry-run is \"show me every command before it runs\"
        — the recursion closes that gap.

        We redirect MAS_MANAGER_WORKTREE at a fresh tmp path so the
        setup produces a visible \"git worktree add\" plan line
        instead of the \"already present — no-op\" path it takes
        against the real sibling worktree on this machine.
        """
        env = os.environ.copy()
        env["_TEST_DRY_RUN"] = "true"
        env["MAS_MANAGER_WORKTREE"] = str(tmp_path / "fresh-mgr-wt")
        result = subprocess.run(
            [str(START_SCRIPT), "demo"],
            capture_output=True, text=True, env=env, timeout=30,
            cwd=str(REPO_ROOT),
        )
        combined = result.stdout + result.stderr
        # Invocation announcement — unchanged from the previous test.
        assert "setup-manager-worktree.sh" in combined, (
            f"start.sh dry-run should print the worktree setup call; got:\n"
            f"{combined}"
        )
        # #61 Observation 1: the recursive setup plan must also appear.
        assert "git worktree add" in combined, (
            "start.sh dry-run must recurse into setup-manager-worktree.sh "
            "and print its plan (#61). Expected a `git worktree add ...` "
            "line from the setup's dry-run in the output; got:\n"
            f"{combined}"
        )
        assert "manager-worktree" in combined


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
