"""Shared test fixtures.

Auto-isolates tests from any live tmux session on the host machine so
that test session names (e.g. "remote-test") can collide with real
sessions a developer happens to be running without making tests pick
up arbitrary label state.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_tmux_pane_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``TmuxComm._scan_pane_labels`` to return an empty mapping
    during tests so agent -> pane-index resolution always falls back
    to the deterministic config-order default.
    """
    try:
        from orchestrator.tmux_comm import TmuxComm
    except Exception:
        return
    monkeypatch.setattr(
        TmuxComm, "_scan_pane_labels", lambda self, window: {}
    )
