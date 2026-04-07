"""Reliable Message Delivery Protocol for the Multi-Agent System.

Implements routing-protocol-inspired reliable delivery over NATS + tmux:

  Neighbor Table (OSPF-style):
    agent -> state (UP/BUSY/DOWN) + last_ack + RTT

  Delivery Queue (TCP-style):
    seq -> target, state, attempts, backoff

  ACK Flow:
    Agent calls check_messages -> MCP bridge publishes delivery_ack
    -> Orchestrator receives ACK -> clears pending delivery

  Protocol Loop:
    1. PROBE  -- capture panes, update neighbor states
    2. PROCESS -- retry unacked deliveries with exponential backoff
    3. EXPIRE -- dead letter after max attempts, alert manager
    4. LOG    -- periodic route table dump

Runs as an async task inside the orchestrator's event loop.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Exponential backoff schedule (seconds) indexed by attempt number.
# Attempt:   1    2     3     4      5      6
_BACKOFF = [0, 15, 60, 300, 3600, 3600]

_DEFAULT_MAX_ATTEMPTS = 6
_DEFAULT_PROBE_INTERVAL = 10        # seconds between neighbor probes
_DEFAULT_PROCESS_INTERVAL = 5       # seconds between queue processing
_DEFAULT_NEIGHBOR_TIMEOUT = 120     # seconds without ACK -> warning
_DEFAULT_TABLE_LOG_INTERVAL = 60    # log route table every N seconds
_STARTUP_GRACE_PERIOD = 5           # seconds to wait after agent comes UP
_ESCALATION_BACKOFF = 3600          # push notify when backoff reaches 1hr

# ACK message type published by MCP bridge
ACK_MESSAGE_TYPE = "delivery_ack"

# Push notification script
_PUSH_NOTIFY_SCRIPT = "python3 scripts/push-notify.py"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NeighborState(enum.Enum):
    """Agent reachability states (OSPF-inspired)."""
    UNKNOWN = "UNKNOWN"
    UP = "UP"       # idle at prompt -- ready to receive
    BUSY = "BUSY"   # processing -- nudge queued by Claude Code
    DOWN = "DOWN"   # pane unreachable -- nudge will fail


class DeliveryState(enum.Enum):
    """Delivery record lifecycle (TCP-inspired)."""
    QUEUED = "QUEUED"   # waiting for first attempt
    SENT = "SENT"       # nudge sent, awaiting ACK
    ACKED = "ACKED"     # agent confirmed receipt
    DEAD = "DEAD"       # max attempts exhausted


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class NeighborEntry:
    """Route table entry for a single agent neighbor."""
    agent: str
    state: NeighborState = NeighborState.UNKNOWN
    last_ack: float = 0.0
    last_probe: float = 0.0
    last_state_change: float = field(default_factory=time.time)
    pending: int = 0
    rtt_ema: float = 0.0  # exponential moving average of ACK RTT

    def transition(self, new_state: NeighborState) -> bool:
        """Update state, log on change. Returns True if changed."""
        if self.state == new_state:
            return False
        old = self.state
        self.state = new_state
        self.last_state_change = time.time()
        logger.info("NEIGHBOR %s %s -> %s", self.agent, old.value, new_state.value)
        return True


@dataclass
class DeliveryRecord:
    """A pending delivery: we need this agent to check their inbox."""
    seq: int
    target: str
    reason: str = ""
    state: DeliveryState = DeliveryState.QUEUED
    attempts: int = 0
    next_retry: float = 0.0
    created: float = field(default_factory=time.time)
    last_sent: float = 0.0
    escalated: bool = False  # push notification sent


# Callback type for dead letter notifications
DeadLetterCallback = Callable[[str, int], None]


# ---------------------------------------------------------------------------
# Delivery Protocol
# ---------------------------------------------------------------------------


class DeliveryProtocol:
    """Reliable message delivery with ACK, retransmit, and neighbor tracking.

    Args:
        tmux_comm: TmuxComm for nudging and probing agent panes.
        config: Full config dict with optional ``routing`` section.
    """

    def __init__(
        self,
        tmux_comm: Any,
        config: dict[str, Any],
    ) -> None:
        self._tmux_comm = tmux_comm

        routing_cfg = config.get("routing", {})
        self._probe_interval: int = routing_cfg.get(
            "probe_interval", _DEFAULT_PROBE_INTERVAL,
        )
        self._process_interval: int = routing_cfg.get(
            "process_interval", _DEFAULT_PROCESS_INTERVAL,
        )
        self._neighbor_timeout: int = routing_cfg.get(
            "neighbor_timeout", _DEFAULT_NEIGHBOR_TIMEOUT,
        )
        self._max_attempts: int = routing_cfg.get(
            "max_attempts", _DEFAULT_MAX_ATTEMPTS,
        )
        self._table_log_interval: int = routing_cfg.get(
            "table_log_interval", _DEFAULT_TABLE_LOG_INTERVAL,
        )

        # Build neighbor table (skip monitor agents like manager)
        agents = config.get("agents", {})
        self._neighbors: dict[str, NeighborEntry] = {}
        for name, cfg in agents.items():
            if isinstance(cfg, dict) and cfg.get("role") == "monitor":
                continue
            self._neighbors[name] = NeighborEntry(agent=name)

        # Delivery queue
        self._queue: list[DeliveryRecord] = []
        self._seq_counter: int = 0

        # Timing
        self._last_probe: float = 0.0
        self._last_table_log: float = 0.0

        # Callbacks
        self._dead_letter_callback: Optional[DeadLetterCallback] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_dead_letter_callback(self, fn: DeadLetterCallback) -> None:
        """Register callback for dead letter notifications."""
        self._dead_letter_callback = fn

    def deliver(self, agent: str, reason: str = "") -> int:
        """Request reliable delivery to an agent.

        Coalesces with any existing pending delivery for the same agent
        (one nudge covers all queued messages). Returns the sequence
        number, or 0 if coalesced with an existing delivery.
        """
        # Coalesce: if already pending for this agent, don't double-nudge
        for record in self._queue:
            if record.target == agent and record.state in (
                DeliveryState.QUEUED, DeliveryState.SENT,
            ):
                logger.debug(
                    "DELIVER to %s coalesced with seq=%d (%s)",
                    agent, record.seq, reason,
                )
                return 0

        self._seq_counter += 1
        seq = self._seq_counter
        record = DeliveryRecord(seq=seq, target=agent, reason=reason)
        self._queue.append(record)

        neighbor = self._neighbors.get(agent)
        if neighbor:
            neighbor.pending += 1

        logger.info("DELIVER seq=%d to %s QUEUED (%s)", seq, agent, reason)

        # Immediate first attempt
        self._attempt_delivery(record)

        return seq

    async def handle_ack_message(self, msg: Any) -> None:
        """Handle a delivery ACK from the NATS ACK subscription.

        Called by core NATS subscription on ``agents.*.ack``.
        """
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if payload.get("type") != ACK_MESSAGE_TYPE:
            return

        agent = payload.get("agent", "")
        count = payload.get("count", 0)
        if not agent:
            return

        now = time.time()
        neighbor = self._neighbors.get(agent)

        # ACK received -> agent is UP and responsive
        if neighbor:
            neighbor.last_ack = now
            neighbor.transition(NeighborState.UP)

        # Clear all pending deliveries for this agent
        acked_count = 0
        for record in self._queue:
            if record.target != agent:
                continue
            if record.state not in (DeliveryState.QUEUED, DeliveryState.SENT):
                continue

            rtt = now - record.last_sent if record.last_sent > 0 else 0
            record.state = DeliveryState.ACKED
            acked_count += 1

            if neighbor:
                neighbor.pending = max(0, neighbor.pending - 1)
                if rtt > 0:
                    neighbor.rtt_ema = 0.7 * neighbor.rtt_ema + 0.3 * rtt

            logger.info(
                "DELIVER seq=%d to %s ACKED (RTT=%.1fs, msgs_read=%d)",
                record.seq, agent, rtt, count,
            )

        if acked_count == 0:
            # Agent checked messages on its own (no pending delivery)
            logger.debug("ACK from %s — no pending delivery (read %d)", agent, count)

        self._cleanup_queue()

    def get_neighbor_table(self) -> dict[str, dict]:
        """Return neighbor table for status reporting."""
        now = time.time()
        return {
            name: {
                "state": n.state.value,
                "last_ack": (
                    f"{int(now - n.last_ack)}s ago"
                    if n.last_ack > 0
                    else "never"
                ),
                "pending": n.pending,
                "rtt": f"{n.rtt_ema:.1f}s" if n.rtt_ema > 0 else "-",
            }
            for name, n in self._neighbors.items()
        }

    def get_queue_status(self) -> list[dict]:
        """Return pending delivery queue for status reporting."""
        now = time.time()
        return [
            {
                "seq": r.seq,
                "target": r.target,
                "state": r.state.value,
                "attempts": f"{r.attempts}/{self._max_attempts}",
                "reason": r.reason,
                "age": f"{int(now - r.created)}s",
            }
            for r in self._queue
            if r.state in (DeliveryState.QUEUED, DeliveryState.SENT)
        ]

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main protocol loop. Call as an asyncio task."""
        logger.info(
            "Delivery protocol started "
            "(probe=%ds, process=%ds, max_attempts=%d)",
            self._probe_interval,
            self._process_interval,
            self._max_attempts,
        )

        # Initial probe
        await self._probe_neighbors()
        self._log_route_table()
        self._last_probe = time.time()
        self._last_table_log = time.time()

        while True:
            await asyncio.sleep(self._process_interval)
            now = time.time()

            # Probe neighbors on schedule
            if now - self._last_probe >= self._probe_interval:
                await self._probe_neighbors()
                self._last_probe = now

            # Process delivery queue
            self._process_queue()

            # Periodic route table log
            if now - self._last_table_log >= self._table_log_interval:
                self._log_route_table()
                self._last_table_log = now

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    async def _probe_neighbors(self) -> None:
        """Probe all agent panes and update neighbor states.

        Runs blocking tmux subprocess calls in a thread to avoid stalling
        the async event loop.
        """
        for name, neighbor in self._neighbors.items():
            try:
                idle = await asyncio.to_thread(
                    self._tmux_comm.is_agent_idle, name,
                )
                if idle:
                    neighbor.transition(NeighborState.UP)
                else:
                    pane = await asyncio.to_thread(
                        self._tmux_comm.capture_pane, name, 1,
                    )
                    if pane is None:
                        neighbor.transition(NeighborState.DOWN)
                    else:
                        neighbor.transition(NeighborState.BUSY)
            except Exception:
                neighbor.transition(NeighborState.DOWN)

            neighbor.last_probe = time.time()

    # ------------------------------------------------------------------
    # Queue processing
    # ------------------------------------------------------------------

    def _process_queue(self) -> None:
        """Process the delivery queue: retry unacked, dead-letter expired."""
        now = time.time()
        dead: list[DeliveryRecord] = []

        for record in self._queue:
            if record.state in (DeliveryState.ACKED, DeliveryState.DEAD):
                continue

            # Not time for retry yet
            if record.next_retry > now:
                continue

            # Max attempts -> dead letter
            if record.attempts >= self._max_attempts:
                record.state = DeliveryState.DEAD
                logger.error(
                    "DEAD LETTER seq=%d to %s — %d attempts in %ds (%s)",
                    record.seq, record.target, record.attempts,
                    int(now - record.created), record.reason,
                )
                dead.append(record)
                continue

            # Retry delivery
            self._attempt_delivery(record)

        # Handle dead letters
        for record in dead:
            neighbor = self._neighbors.get(record.target)
            if neighbor:
                neighbor.pending = max(0, neighbor.pending - 1)
            if self._dead_letter_callback:
                self._dead_letter_callback(record.target, record.seq)

        self._cleanup_queue()

    def _attempt_delivery(self, record: DeliveryRecord) -> None:
        """Attempt to nudge the target agent and update the record."""
        now = time.time()
        neighbor = self._neighbors.get(record.target)

        # Agent is DOWN -> skip nudge but count the attempt
        if neighbor and neighbor.state == NeighborState.DOWN:
            record.attempts += 1
            backoff = _BACKOFF[min(record.attempts, len(_BACKOFF) - 1)]
            record.next_retry = now + backoff
            logger.warning(
                "DELIVER seq=%d to %s SKIP (DOWN) attempt=%d/%d retry=%ds",
                record.seq, record.target, record.attempts,
                self._max_attempts, backoff,
            )
            return

        # Startup grace: if agent just transitioned to UP, wait before nudging
        if neighbor and (now - neighbor.last_state_change) < _STARTUP_GRACE_PERIOD:
            record.next_retry = neighbor.last_state_change + _STARTUP_GRACE_PERIOD
            logger.info(
                "DELIVER seq=%d to %s GRACE (agent just came UP, wait %.0fs)",
                record.seq, record.target,
                record.next_retry - now,
            )
            return

        # Send nudge
        try:
            sent = self._tmux_comm.nudge(record.target, force=True)
        except Exception:
            sent = False

        record.attempts += 1
        record.last_sent = now
        record.state = DeliveryState.SENT

        backoff = _BACKOFF[min(record.attempts, len(_BACKOFF) - 1)]
        record.next_retry = now + backoff

        logger.info(
            "DELIVER seq=%d to %s %s attempt=%d/%d next=%ds",
            record.seq, record.target,
            "SENT" if sent else "NUDGE_FAILED",
            record.attempts, self._max_attempts, backoff,
        )

        # Escalate: push notification when backoff hits 1hr threshold
        if backoff >= _ESCALATION_BACKOFF and not record.escalated:
            record.escalated = True
            self._push_notify(record)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _push_notify(self, record: DeliveryRecord) -> None:
        """Send a push notification for an unresponsive agent."""
        msg = (
            f"Agent '{record.target}' not responding after "
            f"{record.attempts} delivery attempts "
            f"({int(time.time() - record.created)}s). "
            f"Reason: {record.reason}"
        )
        logger.warning("ESCALATE seq=%d to %s — push notification", record.seq, record.target)
        try:
            result = subprocess.run(
                [
                    "python3", "scripts/push-notify.py",
                    "-t", "MAS: Agent Unresponsive",
                    msg,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.error(
                    "Push notification failed for %s: %s",
                    record.target, result.stderr.strip(),
                )
        except Exception:
            logger.exception("Push notification failed for %s", record.target)

    def _cleanup_queue(self) -> None:
        """Remove completed and dead records from the queue."""
        self._queue = [
            r for r in self._queue
            if r.state not in (DeliveryState.ACKED, DeliveryState.DEAD)
        ]

    def _log_route_table(self) -> None:
        """Log the current neighbor table and queue depth."""
        entries = []
        for name, n in self._neighbors.items():
            entries.append(f"{name}={n.state.value}({n.pending})")
        pending = len([
            r for r in self._queue
            if r.state in (DeliveryState.QUEUED, DeliveryState.SENT)
        ])
        logger.info("ROUTE TABLE: %s | queue=%d", " ".join(entries), pending)
