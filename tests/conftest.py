"""Top-level pytest configuration for the MAS test suite.

The main job of this conftest is to isolate pytest's tmux ops on a
dedicated socket via ``tmux -L <socket>``. See issue #46.

## Why

Several tests (``test_start_script.py``, ``test_stop_script.py``,
``test_orchestrator_singleton.py``, some integration smoke tests)
shell out to real ``tmux`` against named sessions. When those ops
run on the default tmux socket they share a namespace with any live
user sessions, so a buggy fixture can prefix-match and kill user
work. That nearly happened: a per-test teardown ran ``tmux
kill-session -t remote`` and prefix-matched the user's live
``remote-test`` session.

We patched the prefix-match by switching to exact-match
(``-t =name``), but the underlying risk — shared-server namespace
collision — was still there. Fix: set ``MAS_TMUX_SOCKET`` at session
start so every tmux invocation from shell scripts
(``scripts/start.sh``, ``scripts/stop.sh``) and Python code
(``orchestrator/tmux_comm.py``) prepends ``-L $MAS_TMUX_SOCKET``.
Dedicated socket = dedicated server = full namespace isolation.

## Scope

- Env var set at collection time via ``_mas_tmux_socket_isolation``
  autouse fixture (session-scoped) so subprocesses inherit it
  without each test having to remember.
- Cleanup: ``tmux -L <socket> kill-server`` at session teardown.
- Smoke test: ``test_default_socket_unchanged`` verifies ``tmux ls``
  on the default socket is byte-identical before and after pytest —
  acceptance criterion from the issue.

## Out of scope (per #46 scope note)

- Does NOT rewrite the existing ``_TEST_DRY_RUN`` dry-run path in
  ``scripts/start.sh`` / ``scripts/stop.sh``. That path exists to
  skip tmux entirely for dry-run tests and is orthogonal to socket
  isolation.
- Does NOT modify ``scripts/lint-destructive-patterns.sh`` —
  pattern-match lint is the secondary defense and stays in place.
- Does NOT change which tests are gated on ``INTEGRATION=1``.
"""

from __future__ import annotations

import os
import subprocess

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Dedicated tmux socket name used by pytest. A separate socket name
#: means ``tmux`` spawns a distinct server with its own namespace —
#: no list-sessions overlap, no kill-session collision with the
#: user's default-socket sessions.
TMUX_TEST_SOCKET = "mas-pytest"


# ---------------------------------------------------------------------------
# Socket isolation (autouse, session-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _mas_tmux_socket_isolation():
    """Set ``MAS_TMUX_SOCKET`` in the test-process env, then kill-server
    on the test socket at session teardown.

    Autouse + session scope: runs exactly once, before any test
    collection that might subprocess out to ``tmux``. Sets the env
    var on ``os.environ`` so ``subprocess.run`` calls inherit it
    without each test having to pass ``env={...}`` explicitly.
    """
    previous = os.environ.get("MAS_TMUX_SOCKET")
    os.environ["MAS_TMUX_SOCKET"] = TMUX_TEST_SOCKET
    try:
        yield
    finally:
        # Tear down every session the test suite created by killing
        # the entire test-socket server. Using -L keeps us strictly
        # off the default socket — this can NEVER touch user work.
        subprocess.run(
            ["tmux", "-L", TMUX_TEST_SOCKET, "kill-server"],
            capture_output=True,
        )
        if previous is None:
            os.environ.pop("MAS_TMUX_SOCKET", None)
        else:
            os.environ["MAS_TMUX_SOCKET"] = previous


# ---------------------------------------------------------------------------
# Default-socket safety net (acceptance criterion #2 in the issue)
# ---------------------------------------------------------------------------

def _tmux_installed() -> bool:
    return subprocess.run(
        ["which", "tmux"], capture_output=True,
    ).returncode == 0


def _default_socket_ls() -> str:
    """Return ``tmux ls`` output for the DEFAULT socket (no ``-L``).

    This deliberately does not prepend ``-L $MAS_TMUX_SOCKET`` — the
    whole point is to see whether any test leaked a session onto the
    user's real tmux server. Returns stdout verbatim; a failure to
    contact tmux (no server running) produces empty output, which
    compares equal to "empty before and after".
    """
    result = subprocess.run(
        ["tmux", "ls"], capture_output=True, text=True,
    )
    return result.stdout


@pytest.fixture(scope="session", autouse=True)
def _default_socket_before_after(request, _mas_tmux_socket_isolation):
    """Snapshot ``tmux ls`` on the default socket at session start and
    compare against a fresh snapshot at session teardown. If the test
    suite leaked a session onto the default socket, this asserts and
    the user gets a clear error pointing at the regression.

    Skips silently when tmux is not installed (CI runners without
    tmux can still collect and run the non-tmux tests).

    Depends on ``_mas_tmux_socket_isolation`` so our env var is set
    before the first snapshot — any subprocess kicked off during
    snapshot collection is also socket-isolated.
    """
    if not _tmux_installed():
        yield
        return
    before = _default_socket_ls()
    yield
    after = _default_socket_ls()
    if before != after:
        raise AssertionError(
            "Pytest leaked a tmux session onto the DEFAULT socket. "
            "MAS_TMUX_SOCKET isolation was bypassed somewhere — audit "
            "any test that shells out to `tmux` without the `-L` flag.\n"
            f"Default socket BEFORE pytest:\n{before!r}\n"
            f"Default socket AFTER pytest:\n{after!r}"
        )
