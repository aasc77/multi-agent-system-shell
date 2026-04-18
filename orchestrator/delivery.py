"""Reliable Message Delivery Protocol for the Multi-Agent System.

Per-agent mailbox flags with ACK, retransmit, and neighbor tracking:

  Neighbor Table (OSPF-style):
    agent -> state (UP/BUSY/DOWN) + mailbox flag

  Mailbox Flag (per agent):
    pending bool + attempt count + backoff

  ACK Flow:
    Agent calls check_messages -> MCP bridge publishes delivery_ack
    -> Orchestrator clears pending flag

  Soft ACK:
    Agent transitions BUSY -> UP -> pending cleared (was active, likely read mail)

  Protocol Loop:
    1. PROBE  -- capture panes, update neighbor states, soft ACK
    2. PROCESS -- re-nudge agents with pending mail after backoff
    3. EXPIRE -- dead letter after max attempts
    4. LOG    -- periodic route table dump

Runs as an async task inside the orchestrator's event loop.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
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
_DEFAULT_PROCESS_INTERVAL = 5       # seconds between mailbox processing
_DEFAULT_NEIGHBOR_TIMEOUT = 120     # seconds without ACK -> warning
_DEFAULT_TABLE_LOG_INTERVAL = 60    # log route table every N seconds
_STARTUP_GRACE_PERIOD = 5           # seconds to wait after agent comes UP
_ESCALATION_BACKOFF = 3600          # push notify when backoff reaches 1hr

# #80: heartbeat gate defaults.
#
# Each MCP bridge publishes on ``agents.<role>.heartbeat`` every
# ``MAS_HEARTBEAT_INTERVAL_SEC`` seconds (default 30 — matches the
# bridge default). The orchestrator gates UP/BUSY/DOWN on the last
# observed heartbeat being within ``2 * interval`` so one missed
# publish (packet loss, GC pause) does not flap the agent to DOWN
# on its own.
_DEFAULT_HEARTBEAT_INTERVAL_SEC = 30
# Demotion grace: if we have never seen a heartbeat AND the
# orchestrator has been up for less than this many seconds, do NOT
# force DOWN. A fresh orchestrator may be watching an agent whose
# bridge is still booting. The heartbeat publisher emits its first
# beat on-connect; this window covers the connect-to-first-publish
# lag (typically sub-second, but SSH / remote bridges can take a
# few seconds).
_DEFAULT_HEARTBEAT_STARTUP_GRACE_SEC = 60

# #80: proactive UP→DOWN alert defaults.
_DEFAULT_NEIGHBOR_DOWN_ALERT_COOLDOWN = 600  # 10 min burst suppression
# Env kill-switch so ``scripts/stop.sh`` and cognate paths can
# silence the alert spray during orderly shutdowns.
_ENV_SUPPRESS_DOWN_ALERTS = "MAS_SUPPRESS_DOWN_ALERTS"

# ACK message type published by MCP bridge
ACK_MESSAGE_TYPE = "delivery_ack"

# Heartbeat message type published by MCP bridge (#80).
HEARTBEAT_MESSAGE_TYPE = "heartbeat"

# Manager directive subtype for UP→DOWN transitions (#80).
DOWN_ALERT_SUBTYPE = "agent_logout"
_MSG_TYPE_MANAGER_DIRECTIVE = "manager_directive"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NeighborState(enum.Enum):
    """Agent reachability states (OSPF-inspired)."""
    UNKNOWN = "UNKNOWN"
    UP = "UP"       # idle at prompt -- ready to receive
    BUSY = "BUSY"   # processing -- nudge queued by Claude Code
    DOWN = "DOWN"   # pane unreachable -- nudge will fail


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MailboxFlag:
    """Per-agent pending-mail flag. Replaces per-message delivery records."""
    pending: bool = False
    last_nudge: float = 0.0
    attempt: int = 0
    escalated: bool = False
    last_reason: str = ""


@dataclass
class NeighborEntry:
    """Route table entry for a single agent neighbor."""
    agent: str
    state: NeighborState = NeighborState.UNKNOWN
    last_ack: float = 0.0
    last_probe: float = 0.0
    last_state_change: float = field(default_factory=time.time)
    rtt_ema: float = 0.0
    mailbox: MailboxFlag = field(default_factory=MailboxFlag)

    def transition(self, new_state: NeighborState) -> bool:
        """Update state, log on change. Returns True if changed."""
        if self.state == new_state:
            return False
        old = self.state
        self.state = new_state
        self.last_state_change = time.time()
        logger.info("NEIGHBOR %s %s -> %s", self.agent, old.value, new_state.value)
        return True


# Callback type for dead letter notifications: (agent_name, attempt_count)
DeadLetterCallback = Callable[[str, int], None]


# ---------------------------------------------------------------------------
# Delivery Protocol
# ---------------------------------------------------------------------------


class DeliveryProtocol:
    """Reliable message delivery with per-agent mailbox flags.

    Args:
        tmux_comm: TmuxComm for nudging and probing agent panes.
        config: Full config dict with optional ``routing`` section.
    """

    def __init__(
        self,
        tmux_comm: Any,
        config: dict[str, Any],
        pane_state_cache: Any = None,
        heartbeat_tracker: Any = None,
        nats_client: Any = None,
    ) -> None:
        self._tmux_comm = tmux_comm
        # #9: optional shared pane-state cache. When present, a
        # retransmit whose target is currently WORKING is deferred
        # (without incrementing the attempt counter) so we don't
        # interrupt an agent that is actively consuming the
        # previous input. ``None`` preserves the pre-#9 behavior:
        # nudge unconditionally.
        self._pane_state_cache = pane_state_cache
        # #80: optional heartbeat tracker + nats_client for the
        # UP→DOWN alerting path. ``None`` preserves the pre-#80
        # behavior: UP/DOWN determined by pane content alone, no
        # proactive alerts on transitions.
        self._heartbeat_tracker = heartbeat_tracker
        self._nats_client = nats_client

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

        # #80: heartbeat / alert config
        heartbeat_cfg = config.get("heartbeat", {}) or {}
        self._heartbeat_interval_sec: int = int(heartbeat_cfg.get(
            "interval_seconds", _DEFAULT_HEARTBEAT_INTERVAL_SEC,
        ))
        # Max age = 2 × interval gives one missed beat of slack
        # before DOWN demotion.
        self._heartbeat_max_age_sec: int = int(heartbeat_cfg.get(
            "max_age_seconds", 2 * self._heartbeat_interval_sec,
        ))
        self._heartbeat_startup_grace_sec: int = int(heartbeat_cfg.get(
            "startup_grace_seconds", _DEFAULT_HEARTBEAT_STARTUP_GRACE_SEC,
        ))
        self._orchestrator_started_at: float = time.time()

        self._alert_on_neighbor_down: bool = bool(heartbeat_cfg.get(
            "alert_on_neighbor_down", True,
        ))
        self._down_alert_cooldown: int = int(heartbeat_cfg.get(
            "neighbor_down_alert_cooldown_seconds",
            _DEFAULT_NEIGHBOR_DOWN_ALERT_COOLDOWN,
        ))
        self._last_down_alert: dict[str, float] = {}

        # Build neighbor table — ALL agents, including monitors.
        # Track which agents are monitors so we can skip delivery for them
        # (they're always in conversation with the user, the BUSY/UP probe
        # cycle doesn't apply, and they read messages naturally).
        agents = config.get("agents", {})
        self._neighbors: dict[str, NeighborEntry] = {}
        self._monitor_agents: set[str] = set()
        for name, cfg in agents.items():
            self._neighbors[name] = NeighborEntry(agent=name)
            if isinstance(cfg, dict) and cfg.get("role") == "monitor":
                self._monitor_agents.add(name)

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

    def deliver(self, agent: str, reason: str = "") -> None:
        """Request delivery to an agent. Always nudges immediately.

        No coalescing — if the agent already has pending mail, we still
        nudge right away (new message = new nudge attempt).

        Monitor agents (e.g. manager) get a one-shot nudge: we tap the
        pane once so they notice new mail, but we do NOT set pending,
        track attempts, or re-process them in the retransmit loop. This
        avoids the retransmit/escalation false alarms that fire when a
        monitor is legitimately idle in conversation with the user.
        """
        neighbor = self._neighbors.get(agent)
        if neighbor is None:
            # Dynamic agent — create entry on the fly
            neighbor = NeighborEntry(agent=agent)
            self._neighbors[agent] = neighbor

        # Monitor agents: one-shot nudge, no pending tracking, no retransmit
        if agent in self._monitor_agents:
            logger.info("DELIVER one-shot to %s monitor (%s)", agent, reason)
            try:
                self._tmux_comm.nudge(agent, force=True, source="orch.delivery")
            except Exception:
                logger.exception("monitor nudge failed for %s", agent)
            return

        neighbor.mailbox.pending = True
        neighbor.mailbox.last_reason = reason

        logger.info("DELIVER to %s (%s)", agent, reason)

        # Always nudge immediately
        self._attempt_nudge(neighbor)

    async def handle_heartbeat_message(self, msg: Any) -> None:
        """Handle a heartbeat publish from an agent's MCP bridge (#80).

        Records the ``agent`` field's timestamp in the
        ``HeartbeatTracker`` so the next ``_probe_neighbors`` cycle
        can gate UP/DOWN on it. Silently drops malformed payloads
        — one missing beat is absorbed by the ``2 × interval`` max-
        age window, a burst of malformed payloads still only misses
        heartbeats, not panics the loop.
        """
        if self._heartbeat_tracker is None:
            return
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if payload.get("type") != HEARTBEAT_MESSAGE_TYPE:
            return
        agent = payload.get("agent", "")
        if not agent:
            return
        self._heartbeat_tracker.touch(agent)

    async def handle_ack_message(self, msg: Any) -> None:
        """Handle a delivery ACK from the NATS ACK subscription."""
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
        if neighbor is None:
            return

        neighbor.last_ack = now
        neighbor.transition(NeighborState.UP)

        # RTT tracking
        if neighbor.mailbox.last_nudge > 0:
            rtt = now - neighbor.mailbox.last_nudge
            if rtt > 0:
                neighbor.rtt_ema = 0.7 * neighbor.rtt_ema + 0.3 * rtt
            logger.info(
                "ACK %s (RTT=%.1fs, msgs_read=%d)", agent, rtt, count,
            )
        else:
            logger.debug("ACK %s (no pending nudge, read %d)", agent, count)

        self._clear_mailbox(neighbor)

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
                "pending": n.mailbox.pending,
                "attempt": n.mailbox.attempt,
                "rtt": f"{n.rtt_ema:.1f}s" if n.rtt_ema > 0 else "-",
            }
            for name, n in self._neighbors.items()
        }

    def get_queue_status(self) -> list[dict]:
        """Return agents with pending mail for status reporting."""
        now = time.time()
        return [
            {
                "target": name,
                "pending": True,
                "attempt": f"{n.mailbox.attempt}/{self._max_attempts}",
                "reason": n.mailbox.last_reason,
                "last_nudge": (
                    f"{int(now - n.mailbox.last_nudge)}s ago"
                    if n.mailbox.last_nudge > 0
                    else "never"
                ),
            }
            for name, n in self._neighbors.items()
            if n.mailbox.pending
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

            # Process pending mailboxes
            self._process_mailboxes()

            # Periodic route table log
            if now - self._last_table_log >= self._table_log_interval:
                self._log_route_table()
                self._last_table_log = now

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    async def _probe_neighbors(self) -> None:
        """Probe all agent panes and update neighbor states.

        Includes soft ACK: if agent transitions BUSY -> UP while mail
        is pending, clear the flag (agent was active, likely read mail).

        #80: when a ``HeartbeatTracker`` is wired up, the heartbeat
        is the authoritative liveness signal. An agent whose MCP
        bridge has not published within ``2 × heartbeat_interval``
        is forced to DOWN regardless of pane content — this closes
        the ssh-reconnect-keeps-pane-alive false-positive.
        Monitors are exempt (they don't run an MCP bridge that
        publishes heartbeats the same way; pane state is the
        only signal we have). Startup grace suppresses DOWN
        demotion while the orchestrator is freshly booted and
        bridges are still coming up.
        """
        for name, neighbor in self._neighbors.items():
            old_state = neighbor.state
            try:
                if self._should_force_down_by_heartbeat(name):
                    neighbor.transition(NeighborState.DOWN)
                else:
                    idle = await asyncio.to_thread(
                        self._tmux_comm.is_agent_idle, name,
                    )
                    if idle:
                        changed = neighbor.transition(NeighborState.UP)
                        # Soft ACK: BUSY -> UP means agent completed
                        # a work cycle.
                        if changed and old_state == NeighborState.BUSY:
                            if neighbor.mailbox.pending:
                                logger.info(
                                    "SOFT_ACK %s — was BUSY, now UP. "
                                    "Clearing pending.",
                                    name,
                                )
                                self._clear_mailbox(neighbor)
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

            # #80: proactive alert on any transition INTO DOWN from
            # a reachable state (UP or BUSY). UNKNOWN→DOWN is the
            # boot path and doesn't alert. DOWN→DOWN (already
            # down) also doesn't alert.
            if (
                old_state in (NeighborState.UP, NeighborState.BUSY)
                and neighbor.state == NeighborState.DOWN
            ):
                await self._maybe_alert_neighbor_down(neighbor, old_state)

            neighbor.last_probe = time.time()

    def _should_force_down_by_heartbeat(self, agent: str) -> bool:
        """Return True when the heartbeat gate says *agent* is DOWN
        regardless of pane content.

        Conditions:
        - Tracker wired AND
        - agent is NOT a monitor (monitors don't publish heartbeats
          via the bridge pathway; pane signal is their only cue) AND
        - EITHER a previously-observed heartbeat is now older than
          ``max_age``, OR we have never seen a heartbeat AND the
          orchestrator has been running long enough that it should
          have arrived by now.
        """
        if self._heartbeat_tracker is None:
            return False
        if agent in self._monitor_agents:
            return False
        now = time.time()
        age = self._heartbeat_tracker.age_seconds(agent, now=now)
        if age is not None and age <= self._heartbeat_max_age_sec:
            # Fresh heartbeat — definitely not forcing DOWN.
            return False

        # Respect startup grace: even a never-seen agent stays UP
        # during the initial window so the bridge has time to boot.
        elapsed = now - self._orchestrator_started_at
        if elapsed < self._heartbeat_startup_grace_sec:
            return False

        # #86 (follow-up to #80): DEPLOY-SKEW FAIL-OPEN.
        #
        # If the tracker has observed ZERO heartbeats from ANY agent
        # in the fleet, the most likely explanation is that the MCP
        # bridges are running pre-#80 code that doesn't publish on
        # `agents.<role>.heartbeat` yet. Forcing everyone DOWN here
        # is a test-gap regression — #80's test suite covered the
        # single-agent stale case but never the global-empty case,
        # so a merge + bounce with un-redeployed bridges mass-
        # demoted the whole fleet.
        #
        # Fail-open rule: empty tracker past grace → return False,
        # fall through to the existing pane-based determination for
        # every agent. As soon as ANY agent's bridge is updated and
        # publishes even one heartbeat, the tracker becomes non-
        # empty and the gate re-engages for the remaining agents
        # (a stale-but-present entry IS a real silent-logout
        # signal; only the wholly-empty case is deploy skew).
        if not self._heartbeat_tracker.snapshot():
            return False

        # Stale or never-seen, past grace, tracker has at least one
        # observed heartbeat → legitimate silent-logout. Force DOWN.
        return True

    async def _maybe_alert_neighbor_down(
        self,
        neighbor: NeighborEntry,
        from_state: NeighborState,
    ) -> None:
        """Publish a ``manager_directive`` when a neighbor drops from
        UP/BUSY → DOWN. Cooldown-suppressed + env-kill-switchable.
        """
        if not self._alert_on_neighbor_down:
            return
        if self._nats_client is None:
            return
        if os.environ.get(_ENV_SUPPRESS_DOWN_ALERTS):
            return
        # Startup grace: don't spam alerts during bounce (the
        # grace window covers the orchestrator-start → first-probe
        # window when agents haven't had time to publish).
        now = time.time()
        elapsed = now - self._orchestrator_started_at
        if elapsed < self._heartbeat_startup_grace_sec:
            return
        # Cooldown
        last = self._last_down_alert.get(neighbor.agent, 0.0)
        if last > 0 and (now - last) < self._down_alert_cooldown:
            return

        logger.warning(
            "flag_human: neighbor %s transitioned %s → DOWN",
            neighbor.agent, from_state.value,
        )
        directive = {
            "type": _MSG_TYPE_MANAGER_DIRECTIVE,
            "subtype": DOWN_ALERT_SUBTYPE,
            "agent": neighbor.agent,
            "from_state": from_state.value,
            "message": (
                f"Neighbor '{neighbor.agent}' transitioned "
                f"{from_state.value} → DOWN. The agent's MCP bridge "
                f"has stopped heartbeating (no publish on "
                f"agents.{neighbor.agent}.heartbeat within "
                f"{self._heartbeat_max_age_sec}s). The tmux pane "
                f"may still appear alive because ssh-reconnect.sh "
                f"keeps it open past real tunnel death — the "
                f"heartbeat gap is authoritative. Check the agent "
                f"host and its bridge process."
            ),
            "priority": "high",
        }
        try:
            await self._nats_client.publish_to_inbox("manager", directive)
        except Exception:
            logger.exception(
                "Failed to publish agent_logout directive for %s",
                neighbor.agent,
            )
            return
        self._last_down_alert[neighbor.agent] = now

    # ------------------------------------------------------------------
    # Mailbox processing
    # ------------------------------------------------------------------

    def _process_mailboxes(self) -> None:
        """Re-nudge agents with pending mail after backoff elapses."""
        now = time.time()

        for name, neighbor in self._neighbors.items():
            # Skip monitor agents — they read messages naturally
            if name in self._monitor_agents:
                continue
            mb = neighbor.mailbox
            if not mb.pending:
                continue

            # Check backoff: is it time to re-nudge?
            backoff = _BACKOFF[min(mb.attempt, len(_BACKOFF) - 1)]
            if mb.last_nudge > 0 and (now - mb.last_nudge) < backoff:
                continue

            # Max attempts -> dead letter
            if mb.attempt >= self._max_attempts:
                logger.error(
                    "DEAD_LETTER %s — %d attempts (%s)",
                    name, mb.attempt, mb.last_reason,
                )
                if not mb.escalated:
                    self._push_notify(name, mb)
                if self._dead_letter_callback:
                    self._dead_letter_callback(name, mb.attempt)
                self._clear_mailbox(neighbor)
                continue

            # Re-nudge
            self._attempt_nudge(neighbor)

    def _attempt_nudge(self, neighbor: NeighborEntry) -> None:
        """Attempt to nudge an agent and update mailbox state."""
        now = time.time()
        mb = neighbor.mailbox

        # Agent is DOWN -> count attempt but skip the actual nudge
        if neighbor.state == NeighborState.DOWN:
            mb.attempt += 1
            mb.last_nudge = now
            backoff = _BACKOFF[min(mb.attempt, len(_BACKOFF) - 1)]
            logger.warning(
                "NUDGE %s SKIP (DOWN) attempt=%d/%d retry=%ds",
                neighbor.agent, mb.attempt, self._max_attempts, backoff,
            )
            return

        # Startup grace: if agent just came UP, defer (don't count as attempt)
        if (now - neighbor.last_state_change) < _STARTUP_GRACE_PERIOD:
            logger.info(
                "NUDGE %s GRACE (just came UP, wait %.0fs)",
                neighbor.agent,
                neighbor.last_state_change + _STARTUP_GRACE_PERIOD - now,
            )
            return

        # #9: if the shared pane-state cache reports the recipient
        # is actively rendering (WORKING), defer WITHOUT incrementing
        # the attempt counter. This is not a missed nudge — the agent
        # is mid-turn consuming the prior input. The retransmit clock
        # keeps ticking; we'll recheck on the next process cycle.
        #
        # Other states pass through to the normal nudge path:
        #   - IDLE           → nudge (❯ prompt visible, ready)
        #   - UNKNOWN        → nudge (#42 directive already fired;
        #                       may genuinely need wake-up)
        #   - CAPTURE_FAILED → nudge (can't tell state; fall back
        #                       to existing behavior)
        #   - None           → nudge (cache hasn't seen this agent
        #                       yet; fall back to existing behavior)
        if self._pane_state_cache is not None:
            try:
                pane_state = self._pane_state_cache.get(neighbor.agent)
            except Exception:
                pane_state = None
            if pane_state == "working":
                logger.info(
                    "NUDGE %s DEFER (pane WORKING) attempt=%d/%d",
                    neighbor.agent, mb.attempt, self._max_attempts,
                )
                return

        # Send nudge
        try:
            sent = self._tmux_comm.nudge(
                neighbor.agent, force=True, source="orch.delivery",
            )
        except Exception:
            sent = False

        mb.attempt += 1
        mb.last_nudge = now

        backoff = _BACKOFF[min(mb.attempt, len(_BACKOFF) - 1)]
        logger.info(
            "NUDGE %s %s attempt=%d/%d next=%ds (%s)",
            neighbor.agent, "SENT" if sent else "FAILED",
            mb.attempt, self._max_attempts, backoff, mb.last_reason,
        )

        # Escalate: push notification when backoff hits 1hr
        if backoff >= _ESCALATION_BACKOFF and not mb.escalated:
            mb.escalated = True
            self._push_notify(neighbor.agent, mb)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_mailbox(self, neighbor: NeighborEntry) -> None:
        """Reset an agent's mailbox to clean state."""
        neighbor.mailbox.pending = False
        neighbor.mailbox.attempt = 0
        neighbor.mailbox.escalated = False
        neighbor.mailbox.last_nudge = 0.0

    def _push_notify(self, agent: str, mb: MailboxFlag) -> None:
        """Send a push notification for an unresponsive agent."""
        msg = (
            f"Agent '{agent}' not responding after "
            f"{mb.attempt} delivery attempts. "
            f"Reason: {mb.last_reason}"
        )
        logger.warning("ESCALATE %s — push notification", agent)
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
                    agent, result.stderr.strip(),
                )
        except Exception:
            logger.exception("Push notification failed for %s", agent)

    def _log_route_table(self) -> None:
        """Log the current neighbor table and pending count."""
        entries = []
        for name, n in self._neighbors.items():
            flag = "*" if n.mailbox.pending else ""
            entries.append(f"{name}={n.state.value}{flag}")
        pending = sum(1 for n in self._neighbors.values() if n.mailbox.pending)
        logger.info(
            "ROUTE TABLE: %s | pending_ack=%d", " ".join(entries), pending,
        )
