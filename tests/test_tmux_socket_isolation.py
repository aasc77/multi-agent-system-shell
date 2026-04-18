"""Unit tests for the pytest tmux-socket isolation (#46).

These tests lock in the following invariants:

1. ``tests.conftest.TMUX_TEST_SOCKET == "mas-pytest"``.
2. Conftest sets ``MAS_TMUX_SOCKET`` in the test process env, so any
   module that reads the var at call time picks up the isolated
   socket automatically.
3. ``orchestrator.tmux_comm._tmux_cmd()`` prepends ``-L <socket>``
   when the env var is set and returns plain ``["tmux"]`` otherwise.
4. ``scripts/start.sh`` and ``scripts/stop.sh`` contain the
   ``_tmux()`` wrapper that honors the var (grep-based smoke — the
   scripts are too tightly coupled to real tmux to shell-exercise in
   a unit test).
5. Creating a session via the test socket is INVISIBLE to ``tmux ls``
   on the default socket (the socket-isolation acceptance criterion
   from issue #46).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.conftest import TMUX_TEST_SOCKET


REPO_ROOT = Path(__file__).resolve().parent.parent
START_SH = REPO_ROOT / "scripts" / "start.sh"
STOP_SH = REPO_ROOT / "scripts" / "stop.sh"
BOUNCE_SH = REPO_ROOT / "scripts" / "bounce-orchestrator.sh"
RESET_DEMO_SH = REPO_ROOT / "scripts" / "reset-demo.sh"
PASTE_IMAGE_SH = REPO_ROOT / "scripts" / "tmux-paste-image.sh"


def _tmux_installed() -> bool:
    return subprocess.run(
        ["which", "tmux"], capture_output=True,
    ).returncode == 0


# Matches a bare `tmux <subcommand>` token. Accepts:
#   - line start
#   - after whitespace
#   - after `$(` / `)` / `;` / `&&` / `||` / `|` / `` ` ``
# Rejects `_tmux`, `command tmux`, comment lines, and string-literal uses
# like `echo "... tmux ..."` (the double quote is the conservative signal).
_BARE_TMUX_RE = __import__("re").compile(
    r"""(?mx)                     # multiline, verbose
    ^                              # line start
    (?!\s*\#)                      # not a comment line
    (?:                            # allowed prefix before `tmux`
        [^"'\n]*?                  # ... no quote chars on this line yet
        (?:^|\s|\$\(|\(|;|\|\||&&|\|)  # token boundary
    )
    tmux                           # the bare command
    (?=\s|$)                       # followed by whitespace or EOL
    """,
)


def _assert_no_bare_tmux(path: Path) -> None:
    src = path.read_text()
    stripped_lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Allow `command tmux ...` inside the wrapper definition itself.
        if "command tmux" in stripped:
            continue
        stripped_lines.append(line)
    body = "\n".join(stripped_lines)
    # Find any bare `tmux <word>` that isn't prefixed by `_`.
    import re as _re
    offenders = []
    for match in _re.finditer(r"(?<![\w-])tmux\s+([a-zA-Z-]+)", body):
        # Allow documented substrings inside echo / string literals by
        # ignoring matches that sit inside a quoted string on the same
        # logical line.
        start = match.start()
        line_start = body.rfind("\n", 0, start) + 1
        line_end = body.find("\n", start)
        if line_end == -1:
            line_end = len(body)
        line = body[line_start:line_end]
        # Conservative: if the match appears after a `"`, it's a string.
        prefix = line[: start - line_start]
        if prefix.count('"') % 2 == 1 or prefix.count("'") % 2 == 1:
            continue
        # Ignore "_tmux" — the prefix ensures it's not _tmux already.
        if prefix.endswith("_"):
            continue
        offenders.append(line.strip())
    assert not offenders, (
        f"Found bare `tmux <subcommand>` in {path.name} — every call "
        f"must go through the `_tmux` wrapper so MAS_TMUX_SOCKET "
        f"isolation holds. Offending lines:\n  "
        + "\n  ".join(offenders)
    )


class TestSocketConstant:
    def test_constant_is_mas_pytest(self):
        assert TMUX_TEST_SOCKET == "mas-pytest"

    def test_env_var_set_during_tests(self):
        """Autouse conftest fixture should have set this before
        collection for the whole session."""
        assert os.environ.get("MAS_TMUX_SOCKET") == TMUX_TEST_SOCKET


class TestPythonHelper:
    """The ``orchestrator.tmux_comm._tmux_cmd()`` helper returns the
    argv prefix that every ``subprocess.run`` in the module uses."""

    def test_prepends_socket_when_env_set(self, monkeypatch):
        from orchestrator.tmux_comm import _tmux_cmd
        monkeypatch.setenv("MAS_TMUX_SOCKET", "foo")
        assert _tmux_cmd() == ["tmux", "-L", "foo"]

    def test_plain_tmux_when_env_unset(self, monkeypatch):
        from orchestrator.tmux_comm import _tmux_cmd
        monkeypatch.delenv("MAS_TMUX_SOCKET", raising=False)
        assert _tmux_cmd() == ["tmux"]

    def test_all_subprocess_calls_go_through_helper(self):
        """Defensive grep on tmux_comm.py — every subprocess.run with
        a tmux argv must use _tmux_cmd() as its prefix, not a bare
        string literal. Catches regressions where a new call site
        forgets the helper and bypasses the socket isolation.
        """
        src = (REPO_ROOT / "orchestrator" / "tmux_comm.py").read_text()
        # Strip out the helper function itself and docstring examples
        # so we're only grepping actual call sites.
        body_lines = []
        in_helper = False
        for line in src.splitlines():
            if "def _tmux_cmd" in line:
                in_helper = True
                continue
            if in_helper and line.strip().startswith("def "):
                in_helper = False
            if not in_helper:
                body_lines.append(line)
        body = "\n".join(body_lines)
        assert '"tmux",' not in body, (
            "Found a bare `\"tmux\",` in tmux_comm.py subprocess argv — "
            "every call site must use [*_tmux_cmd(), ...] so "
            "MAS_TMUX_SOCKET isolation is honored."
        )


class TestShellScriptWrappers:
    """Scripts wire the same isolation through ``_tmux()``."""

    def test_start_sh_defines_wrapper(self):
        src = START_SH.read_text()
        assert "_tmux()" in src, "scripts/start.sh must define _tmux() wrapper"
        assert 'MAS_TMUX_SOCKET' in src

    def test_stop_sh_defines_wrapper(self):
        src = STOP_SH.read_text()
        assert "_tmux()" in src, "scripts/stop.sh must define _tmux() wrapper"
        assert 'MAS_TMUX_SOCKET' in src

    def test_start_sh_tmux_bin_honors_socket(self):
        src = START_SH.read_text()
        # The TMUX_BIN single-string used for osascript/wt.exe commands
        # must append -L $MAS_TMUX_SOCKET when the env var is set.
        assert 'TMUX_BIN="${TMUX_BIN} -L ${MAS_TMUX_SOCKET}"' in src

    def test_reset_demo_sh_defines_wrapper(self):
        """#58: reset-demo.sh is a manual dev utility that drives tmux
        directly. Previously not exercised by pytest, so the default
        socket was safe — but if a test ever drove this script without
        the wrapper, isolation would break. Gate it in now."""
        src = RESET_DEMO_SH.read_text()
        assert "_tmux()" in src, "scripts/reset-demo.sh must define _tmux() wrapper (#58)"
        assert 'MAS_TMUX_SOCKET' in src

    def test_tmux_paste_image_sh_defines_wrapper(self):
        """#58: tmux-paste-image.sh is a tmux keybinding handler. At
        interactive runtime the env var is empty so `_tmux` falls
        through to plain tmux (preserving behavior). In any future
        test context it'll pick up the socket."""
        src = PASTE_IMAGE_SH.read_text()
        assert "_tmux()" in src, "scripts/tmux-paste-image.sh must define _tmux() wrapper (#58)"
        assert 'MAS_TMUX_SOCKET' in src

    def test_no_bare_tmux_calls_in_reset_demo_sh(self):
        """Regression guard: no bare `tmux <subcommand>` invocations
        left in reset-demo.sh. `_tmux` wrapper must intercept every
        call so MAS_TMUX_SOCKET isolation holds.
        """
        _assert_no_bare_tmux(RESET_DEMO_SH)

    def test_no_bare_tmux_calls_in_tmux_paste_image_sh(self):
        _assert_no_bare_tmux(PASTE_IMAGE_SH)


@pytest.mark.skipif(not _tmux_installed(), reason="tmux not installed")
class TestEndToEndIsolation:
    """A session created on the test socket must be invisible to
    ``tmux ls`` on the default socket — the core acceptance criterion."""

    def test_session_on_test_socket_invisible_to_default_socket(self):
        session = f"socketiso-{uuid.uuid4().hex[:8]}"

        # Snapshot the default socket BEFORE.
        before = subprocess.run(
            ["tmux", "ls"], capture_output=True, text=True,
        ).stdout

        # Create a session on the TEST socket only. If isolation is
        # broken (or a future regression drops the -L flag), this
        # session will leak onto the default socket and the after-
        # snapshot will differ.
        subprocess.run(
            ["tmux", "-L", TMUX_TEST_SOCKET, "new-session",
             "-d", "-s", session, "-n", "ctl"],
            check=True,
        )
        try:
            # Verify it IS visible on the test socket (sanity).
            test_ls = subprocess.run(
                ["tmux", "-L", TMUX_TEST_SOCKET, "ls"],
                capture_output=True, text=True,
            ).stdout
            assert session in test_ls, (
                f"Session not found on test socket — setup failed: "
                f"{test_ls!r}"
            )

            # Verify it's INVISIBLE on the default socket.
            after = subprocess.run(
                ["tmux", "ls"], capture_output=True, text=True,
            ).stdout
            assert session not in after, (
                f"LEAK: session {session!r} was created on the test "
                f"socket but appeared on the DEFAULT socket. "
                f"Isolation is broken. Default `tmux ls`: {after!r}"
            )
            # Stronger form: default-socket ls is byte-identical.
            assert after == before, (
                "Default socket tmux ls changed during the test — "
                "something leaked.\n"
                f"BEFORE: {before!r}\nAFTER:  {after!r}"
            )
        finally:
            subprocess.run(
                ["tmux", "-L", TMUX_TEST_SOCKET, "kill-session",
                 "-t", session],
                capture_output=True,
            )
