"""Per-agent MCP activity tracker (#55).

Every call through mcp-bridge — ``send_to_agent``, ``check_messages``,
``send_message`` — lands on NATS as an outbox or inbox publish. The
orchestrator's :class:`~orchestrator.router.MessageRouter` already
subscribes to both traffic paths; it surfaces the participating
agent name(s) to this module so the watchdog can report ad-hoc MCP
activity alongside the task-queue picture in its per-cycle log line.

## Why a separate module (not in ``delivery.py``)

The delivery protocol's concern is reliable handoff with
``ACK``/retransmit. The activity tracker's concern is
observability-only — it never acts on its own state, just reports
it. Entangling the two would make ``delivery.py`` concurrent-writer
state that both the delivery loop AND every NATS subscription
mutates. Keeping ownership narrow avoids that.

## Thread safety

The map is read from the watchdog's asyncio loop (``run()`` cycle)
and written from whichever event-loop thread handles NATS
subscription callbacks. A single ``threading.Lock`` covers both
paths — every store/load is O(1) and the critical section is short,
so contention is negligible in practice.

The lock is a ``threading.Lock`` rather than an ``asyncio.Lock`` so
the synchronous public API stays callable from non-asyncio contexts
(e.g. unit tests). Asyncio callers get the same guarantee because
the lock acquisition is non-blocking relative to the event loop's
own scheduling.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


_ORCHESTRATOR_SENDER = "orchestrator"


class ActivityTracker:
    """Thread-safe ``{agent: last_activity_timestamp}`` map.

    ``touch(agent)`` records the current time for that agent.
    ``snapshot()`` returns a detached copy of the whole map.
    ``active_within(window_s)`` returns the subset within the window,
    sorted by recency (freshest first).
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def touch(self, agent: Optional[str], now: Optional[float] = None) -> None:
        """Record that *agent* just had MCP traffic.

        No-op for empty names and for the sentinel ``orchestrator``
        sender so the tracker never reports orchestrator-generated
        housekeeping messages as an "agent is active" signal.
        ``now`` is a test hook; real callers leave it ``None`` and
        the tracker stamps ``time.time()``.
        """
        if not agent or agent == _ORCHESTRATOR_SENDER:
            return
        ts = now if now is not None else time.time()
        with self._lock:
            self._last[agent] = ts

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, float]:
        """Return a detached copy of the ``{agent: timestamp}`` map.

        Caller can iterate / mutate freely without racing against
        concurrent ``touch()`` calls.
        """
        with self._lock:
            return dict(self._last)

    def active_within(
        self,
        window_seconds: float,
        now: Optional[float] = None,
    ) -> list[tuple[str, float]]:
        """Return agents active within *window_seconds*, freshest first.

        Each element is ``(agent_name, age_seconds)``. An agent whose
        last activity is older than ``window_seconds`` is omitted — it
        belongs on the idle list the watchdog computes separately.

        ``now`` is a test hook; real callers leave it ``None``.
        """
        reference = now if now is not None else time.time()
        with self._lock:
            items = list(self._last.items())
        pairs = [
            (agent, reference - ts)
            for agent, ts in items
            if (reference - ts) <= window_seconds
        ]
        pairs.sort(key=lambda pair: pair[1])
        return pairs
