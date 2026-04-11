"""End-to-end integration smoke test for the #42 pane-diff
three-state liveness detection.

Drives the real ``TmuxComm.get_pane_state()`` against a live tmux
session. Spawns an isolated test session with a single detached
pane, writes controlled content into it via ``tmux send-keys``,
captures and state-derives across multiple cycles, and verifies
each of the three alert-eligible transitions fires in the right
sequence:

  1. Fresh pane with changing content → WORKING
  2. Same content between cycles WITH `❯` idle prompt → IDLE
  3. Same content between cycles WITHOUT idle prompt for
     ``_STALE_DEBOUNCE_CYCLES`` consecutive cycles → UNKNOWN

Also verifies the CAPTURE_FAILED debounce path by killing the
test pane mid-test and asserting the next two consecutive
get_pane_state() calls return WORKING (benefit of doubt) then
CAPTURE_FAILED.

Uses a unique test session name
(``pane-state-smoke-<epoch>-<uuid>``) so the test never collides
with the production ``remote-test`` session. Session is killed
in a ``try/finally`` so failed runs do not leave orphan tmux
sessions around.

Part of #42 acceptance criteria ("Integration (gated
``INTEGRATION=1``, per #37): spin up real pane, capture in each
of 3 states, verify detection"). Rolls alongside
``test_envelope_smoke.py`` and ``test_version_probe_smoke.py``
in the same harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.tmux_comm import (  # noqa: E402
    AgentPaneState,
    TmuxComm,
    _STALE_DEBOUNCE_CYCLES,
)


_TEST_AGENT = "smoke"


def _build_comm(session_name: str) -> TmuxComm:
    """Build a minimal TmuxComm instance wired to *session_name*.

    Uses a single-agent config with runtime=script so the ``smoke``
    agent maps to pane index 0 in the (single) window of the test
    session.
    """
    config = {
        "tmux": {
            "session_name": session_name,
            "nudge_prompt": "check_messages",
            "nudge_cooldown_seconds": 30,
            "max_nudge_retries": 5,
        },
        "agents": {
            _TEST_AGENT: {"runtime": "script", "command": "bash"},
        },
    }
    return TmuxComm(config)


def _kill_session(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        capture_output=True,
        check=False,
    )


def _send_line(session_name: str, pane_target: str, text: str) -> None:
    """Write *text* into the target pane as a literal, no Enter."""
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target, text],
        capture_output=True,
        check=False,
    )


def _send_keys_with_enter(
    session_name: str, pane_target: str, text: str,
) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target, text, "Enter"],
        capture_output=True,
        check=False,
    )


def _clear_pane(session_name: str, pane_target: str) -> None:
    """Clear the terminal in the pane so state is predictable."""
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target, "clear", "Enter"],
        capture_output=True,
        check=False,
    )
    time.sleep(0.2)


@pytest.fixture
def smoke_session():
    """Create an isolated tmux session, yield its name, kill on teardown.

    Window is named ``agents`` so the session matches TmuxComm's
    hardcoded ``<session>:agents.<pane_index>`` target convention
    (see ``tmux_comm._AGENTS_WINDOW``). Without the `-n agents`
    flag, TmuxComm.get_target() would produce a target pointing
    at a non-existent ``<session>:agents.0`` window and every
    capture_pane call would return None.
    """
    session_name = f"pane-state-smoke-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    # Create a detached session with a window named "agents" (the
    # hardcoded window name TmuxComm targets).
    result = subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", session_name,
            "-n", "agents",
            "-x", "100", "-y", "40",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"could not create test tmux session: {result.stderr.strip()}",
        )
    try:
        yield session_name
    finally:
        _kill_session(session_name)


def test_pane_state_working_on_changing_content(smoke_session):
    """Pane content changes between captures → WORKING.

    Uses `echo <token>` between captures to tick the pane
    content. First cycle is WORKING (no prior hash). Second
    cycle with different content is also WORKING (hash changed).
    """
    comm = _build_comm(smoke_session)
    pane_target = f"{smoke_session}:agents.0"

    _clear_pane(smoke_session, pane_target)
    _send_keys_with_enter(smoke_session, pane_target, "echo first")
    time.sleep(0.2)

    state1 = comm.get_pane_state(_TEST_AGENT)
    assert state1 == AgentPaneState.WORKING, (
        "first capture must return WORKING (no prior hash)"
    )

    _send_keys_with_enter(smoke_session, pane_target, "echo second")
    time.sleep(0.2)

    state2 = comm.get_pane_state(_TEST_AGENT)
    assert state2 == AgentPaneState.WORKING, (
        "second capture with changed content must return WORKING"
    )


def test_pane_state_idle_on_static_prompt_content(smoke_session):
    """Pane unchanged AND contains ❯ → IDLE.

    Writes a literal ❯ character into the pane, lets two captures
    run back-to-back with no intervening change.
    """
    comm = _build_comm(smoke_session)
    pane_target = f"{smoke_session}:agents.0"

    _clear_pane(smoke_session, pane_target)
    # Echo a line ending with the ❯ prompt marker. We use printf
    # so the bash heredoc doesn't try to interpret the character.
    _send_keys_with_enter(
        smoke_session, pane_target,
        "printf 'banner\\n\\u276f \\n'",
    )
    time.sleep(0.3)

    # Establish the baseline hash
    baseline = comm.get_pane_state(_TEST_AGENT)
    assert baseline == AgentPaneState.WORKING  # first cycle always WORKING

    # Second capture with no intervening activity
    time.sleep(0.3)
    state = comm.get_pane_state(_TEST_AGENT)
    assert state == AgentPaneState.IDLE, (
        f"unchanged pane with ❯ should be IDLE, got {state}"
    )


def test_pane_state_unknown_on_static_bash_prompt_after_debounce(smoke_session):
    """Pane unchanged for N consecutive cycles AND no ❯ → UNKNOWN.

    Leaves the bash prompt (no ❯) visible and captures enough
    times to exceed the debounce threshold. The shell prompt is
    the classic "pre-#42 blind spot" case — a dead claude process
    with a bash prompt would hit this.
    """
    comm = _build_comm(smoke_session)
    pane_target = f"{smoke_session}:agents.0"

    _clear_pane(smoke_session, pane_target)
    # Wait for the prompt to settle
    time.sleep(0.3)

    # First call: baseline, returns WORKING
    states = [comm.get_pane_state(_TEST_AGENT)]
    # Subsequent calls with no activity: debounce → eventually UNKNOWN
    for _ in range(_STALE_DEBOUNCE_CYCLES + 2):
        time.sleep(0.1)
        states.append(comm.get_pane_state(_TEST_AGENT))

    assert AgentPaneState.UNKNOWN in states, (
        f"debounced stale pane should eventually be UNKNOWN, "
        f"got sequence: {[s.value for s in states]}"
    )


def test_pane_state_capture_failed_when_session_dies(smoke_session):
    """Killing the session mid-test → capture_pane returns None →
    debounce → CAPTURE_FAILED on the second consecutive failure.
    """
    comm = _build_comm(smoke_session)

    # Establish baseline
    _clear_pane(smoke_session, f"{smoke_session}:agents.0")
    time.sleep(0.2)
    comm.get_pane_state(_TEST_AGENT)

    # Kill the session — subsequent captures must return None
    _kill_session(smoke_session)
    time.sleep(0.1)

    # First failure: benefit of doubt → WORKING
    state1 = comm.get_pane_state(_TEST_AGENT)
    assert state1 == AgentPaneState.WORKING, (
        f"first capture failure should be debounced (WORKING), got {state1}"
    )

    # Second consecutive failure → CAPTURE_FAILED
    state2 = comm.get_pane_state(_TEST_AGENT)
    assert state2 == AgentPaneState.CAPTURE_FAILED, (
        f"two consecutive capture failures should flag "
        f"CAPTURE_FAILED, got {state2}"
    )
