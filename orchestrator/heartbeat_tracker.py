"""Per-agent heartbeat tracker (#80).

Each agent's MCP bridge (``mcp-bridge/index.js``) publishes a
``heartbeat`` message on ``agents.<role>.heartbeat`` at a
configurable interval (default 30s — core-NATS, no JetStream,
fire-and-forget). The orchestrator subscribes once and forwards
every observed heartbeat to this tracker.

``DeliveryProtocol._probe_neighbors`` then uses ``is_alive()`` to
gate UP/BUSY/DOWN determination: an agent whose bridge has not
published a heartbeat within 2× the configured interval is forced
to DOWN regardless of pane content. That closes the #80 silent-
logout gap — ssh-reconnect.sh can keep a tmux pane alive past a
real tunnel death, so pane-only liveness was a false-positive
vector. The heartbeat is the authoritative signal that the
bridge (and therefore the MCP plumbing the orchestrator talks
through) is still up.

## Thread safety

Same pattern as ``ActivityTracker`` / ``PaneStateCache``: a single
``threading.Lock`` guards all reads/writes. The writer is the
NATS heartbeat subscription callback (event-loop thread); readers
are the delivery probe loop and the orchestrator console's status
endpoint. ``threading.Lock`` (not ``asyncio.Lock``) keeps the
synchronous read path callable from non-asyncio contexts.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


_ORCHESTRATOR_SENDER = "orchestrator"


class HeartbeatTracker:
    """Thread-safe ``{agent: last_heartbeat_timestamp}`` map."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def touch(
        self,
        agent: Optional[str],
        now: Optional[float] = None,
    ) -> None:
        """Record that *agent*'s MCP bridge just heartbeated.

        No-op for empty names and the sentinel ``orchestrator``
        role. ``now`` is a test hook; real callers leave it ``None``
        and the tracker stamps ``time.time()``.
        """
        if not agent or agent == _ORCHESTRATOR_SENDER:
            return
        ts = now if now is not None else time.time()
        with self._lock:
            self._last[agent] = ts

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def last_seen(self, agent: str) -> Optional[float]:
        """Return the last heartbeat timestamp for *agent*, or
        ``None`` if the tracker has never observed one."""
        with self._lock:
            return self._last.get(agent)

    def age_seconds(
        self,
        agent: str,
        now: Optional[float] = None,
    ) -> Optional[float]:
        """Return seconds since *agent*'s last heartbeat, or
        ``None`` if the tracker has never observed one.

        ``None`` is the signal delivery uses to apply startup
        grace — a never-seen agent isn't DOWN, it just hasn't
        booted its bridge yet (or we just started the
        orchestrator).
        """
        reference = now if now is not None else time.time()
        with self._lock:
            ts = self._last.get(agent)
        if ts is None:
            return None
        return reference - ts

    def is_alive(
        self,
        agent: str,
        max_age_seconds: float,
        now: Optional[float] = None,
    ) -> bool:
        """``True`` if *agent* heartbeated within ``max_age_seconds``.

        ``False`` when the last heartbeat is older OR when the
        tracker has NEVER observed one. Callers that need the
        never-seen case for startup-grace decisions should use
        ``age_seconds()`` and check for ``None`` explicitly.
        """
        age = self.age_seconds(agent, now=now)
        if age is None:
            return False
        return age <= max_age_seconds

    def snapshot(self) -> dict[str, float]:
        """Return a detached copy of the ``{agent: timestamp}`` map."""
        with self._lock:
            return dict(self._last)
