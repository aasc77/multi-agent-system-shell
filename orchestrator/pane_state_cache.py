"""Shared per-agent pane-state cache (#9).

#42 owns the pane-hash computation via ``TmuxComm.get_pane_state``.
#56 caches each cycle's return inside ``IdleWatchdog`` so the cycle
log can read it without re-computing. #9 generalizes that cache
into its own module so the delivery protocol can also read it —
specifically, so ``DeliveryProtocol._attempt_nudge`` can defer a
retransmit when the recipient is actively rendering something
(``WORKING``) instead of interrupting their current turn.

## Design

Thread-safe ``{agent: pane_state_string}`` map, same pattern as
``ActivityTracker`` (#55 / PR #72). The cache is write-from-the-
watchdog-asyncio-loop and read-from-the-delivery-asyncio-loop;
both run in the same event loop, but a ``threading.Lock``
guarantees correctness even if a future caller moves into a
separate thread.

The values are the string form of ``AgentPaneState.value``
(``"working"`` / ``"idle"`` / ``"unknown"`` / ``"capture_failed"``).
Keeping it as strings avoids an import cycle between the cache and
``tmux_comm`` and matches the shape ``IdleWatchdog`` already
stores in ``_last_pane_state_by_agent``.

## Ownership

- **Writer**: ``IdleWatchdog`` (during ``_check_single_agent_pane_state``).
- **Reader**: ``DeliveryProtocol`` (during ``_attempt_nudge``).
- **Orthogonal to ActivityTracker (#55)**: one tracks MCP
  message recency per agent, the other tracks tmux pane state
  per agent. Both feed delivery/watchdog decisions but stay
  separate — different signals, different semantics.
"""

from __future__ import annotations

import threading
from typing import Optional


class PaneStateCache:
    """Thread-safe ``{agent: pane_state_string}`` map.

    ``set(agent, state)`` records the latest observed pane state
    (typically called from the watchdog right after
    ``get_pane_state`` returns). ``get(agent)`` returns the last
    recorded value or ``None`` if the agent has never been seen.
    ``snapshot()`` returns a detached copy for callers that want
    to iterate without holding the lock.
    """

    def __init__(self) -> None:
        self._state: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, agent: str, state: Optional[str]) -> None:
        """Record *agent*'s latest pane state.

        ``state`` should be one of ``"working"``, ``"idle"``,
        ``"unknown"``, ``"capture_failed"`` — the string form of
        ``AgentPaneState.value``. ``None`` or empty values are
        treated as a no-op so callers can pass whatever they get
        from ``getattr(state, 'value', state)`` without guarding.
        """
        if not agent or not state:
            return
        with self._lock:
            self._state[agent] = state

    def get(self, agent: str) -> Optional[str]:
        """Return the last recorded pane state for *agent*, or
        ``None`` if the cache has never seen this agent (e.g.
        first cycle, or agent not yet observed).
        """
        with self._lock:
            return self._state.get(agent)

    def snapshot(self) -> dict[str, str]:
        """Return a detached copy of the full map."""
        with self._lock:
            return dict(self._state)
