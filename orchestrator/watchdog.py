"""Idle Agent Watchdog for the Multi-Agent System Shell.

Periodically checks if agents assigned to the current task are idle
(at prompt). When detected, captures the pane and sends it to the
manager agent via NATS for review. The manager responds with
"expected" (leave alone) or "nudge" (re-nudge the agent).

Monitors all agents with active work via tasks.json assigned_agents,
falling back to the state machine's current agent if assigned_agents
is not present.

Runs as an async task inside the orchestrator's event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CHECK_INTERVAL = 60  # seconds between checks
_DEFAULT_IDLE_COOLDOWN = 300  # don't re-alert same agent within this window
_CAPTURE_LINES = 20  # lines to capture from pane

# Auth-failure detection (issue #28).
#
# Pattern design: must capture real bridge auth failures without
# false-positiving on prose that merely mentions the error code. Real
# MCP JSON-RPC auth failures render to claude panes as:
#
#     ⎿  Error: MCP error -32603: Authentication failed: Invalid token
#
# A strict "MCP error -NNNNN:" JSON-RPC prefix IMMEDIATELY followed by
# whitespace and an auth phrase is the canonical SDK surface format.
# Loose anchors (bare "-32603" or bare "Authentication failed")
# false-positive on dev chatter, briefings, PR descriptions, and
# anything that discusses the error in prose — see the #28 QA
# smoke-test run that self-triggered on hub's own pane when hub
# (dev) briefed another agent about the upcoming test by typing the
# error code in a message. The narrow proximity match here is the
# defense-in-depth against that.
#
# `\s{0,10}` handles tmux word-wrap between the code and the auth
# phrase — Python's `\s` already matches `\n` by default, so `re.DOTALL`
# is belt-and-braces (explicit intent, harmless).
#
# Known limitation: if the tmux pane is narrower than ~40 cols, tmux
# will split mid-word inside `Authentication` itself, and the pattern
# will miss the wrapped form entirely. No production pane runs that
# narrow in practice (we set pane geometry explicitly in start.sh),
# so fixing sub-40-col wrapping is out of scope for #28. If we ever
# shrink panes below that, see the follow-up tracked for dev-agent
# self-health (issue #32) which will replace pane-grep with a direct
# MCP-bridge probe anyway.
#
# Agent exclusion (see _DEFAULT_AUTH_SCAN_EXCLUDES) is the other half
# of the defense: agents that discuss errors as a matter of course
# (dev/hub, manager-monitor) are skipped because the pattern alone is
# not enough to rule out their prose. See the trade-off note on the
# hub exclusion below.
_DEFAULT_AUTH_ALERT_COOLDOWN = 900  # 15 min burst suppression
_AUTH_CAPTURE_LINES = 60
_AUTH_ERROR_PATTERN = re.compile(
    r"MCP error -\d+:\s{0,10}(?:Authentication failed|Invalid token)",
    re.IGNORECASE | re.DOTALL,
)

# Agents excluded by default from auth-failure pane scanning. Manager
# is already excluded via the role=monitor filter in TmuxComm's pane
# mapping, but we list it here as well so removing the monitor role
# doesn't silently open a cascade hole. Hub (dev) is excluded because
# it is the builder agent — it discusses auth failures in code,
# briefings, PR descriptions, and messages to other agents as a
# normal part of its work. Config override: set
# `watchdog.auth_scan_excludes: [list]` in the project config.
#
# Trade-off accepted for #28: with hub excluded, if hub ITSELF hits a
# real MCP auth failure the watchdog will not flag it from the pane.
# Hub's mitigation (self-report via send_to_agent) has a circular
# dependency — a bridge that cannot auth cannot send. This gap is
# tracked as the follow-up "dev-agent self-health probe" (issue #32,
# not yet filed as of #28 merge) which will add a direct MCP bridge
# probe independent of pane content.
_DEFAULT_AUTH_SCAN_EXCLUDES: frozenset[str] = frozenset({"hub", "manager"})

_MSG_TYPE_IDLE_ALERT = "idle_alert"
_MSG_TYPE_MANAGER_RESPONSE = "manager_idle_response"
_MSG_TYPE_MANAGER_DIRECTIVE = "manager_directive"
_MSG_TYPE_HEALTH_OK = "health_ok"

# Heartbeat watcher (issue #32) — backfills the #28 hub-exclusion gap.
#
# Hub's MCP bridge publishes a `health_ok` heartbeat to
# `system.heartbeat.<agent>` every 60s via core NATS (fire-and-forget,
# outside the `agents.>` JetStream wildcard so it is NOT stored in
# the AGENTS stream). The watchdog subscribes to the `>` wildcard and
# records the last-seen timestamp per agent. On staleness (no heartbeat
# within threshold), it publishes a `hub_unreachable` manager_directive
# — a signal that the pane-grep auth detection in #28 CANNOT produce
# for hub because hub is excluded from that scan.
#
# Absence IS the signal. A bridge with rotted OAuth credentials cannot
# send, so the heartbeat stops, and staleness crosses the threshold.
# The watchdog doesn't wait for an explicit "health_failed" event —
# that event could never be delivered reliably by a broken bridge.
_HEARTBEAT_SUBJECT_PREFIX = "system.heartbeat"
_HEARTBEAT_WILDCARD = f"{_HEARTBEAT_SUBJECT_PREFIX}.>"
_DEFAULT_HEARTBEAT_AGENTS: tuple[str, ...] = ("hub",)
_DEFAULT_HEARTBEAT_STALENESS = 180  # 3× the 60s bridge publish cadence
_DEFAULT_HEARTBEAT_CHECK_INTERVAL = 30  # staleness check cadence
_DEFAULT_HEARTBEAT_GRACE_MULTIPLIER = 2.0  # cold-start grace: 2× staleness

# Agent status values that indicate the agent's work is done
_DONE_PATTERNS = re.compile(
    r"\b(complete|completed|provided|running|done|finished|delivered)\b",
    re.IGNORECASE,
)


class IdleWatchdog:
    """Monitors agent panes and alerts the manager when an agent is idle
    while a task is assigned to it.

    Args:
        lifecycle: TaskLifecycleManager for checking current task state.
        state_machine: StateMachine for determining which agent owns current state.
        nats_client: NatsClient for sending alerts to manager.
        tmux_comm: TmuxComm for capturing panes and nudging.
        config: Merged config dict with agent and watchdog settings.
        task_queue: Optional TaskQueue for reading assigned_agents from tasks.json.
    """

    def __init__(
        self,
        lifecycle: Any,
        state_machine: Any,
        nats_client: Any,
        tmux_comm: Any,
        config: dict[str, Any],
        task_queue: Any = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._state_machine = state_machine
        self._nats_client = nats_client
        self._tmux_comm = tmux_comm
        self._task_queue = task_queue

        watchdog_cfg = config.get("watchdog", {})
        self._check_interval: int = watchdog_cfg.get(
            "check_interval", _DEFAULT_CHECK_INTERVAL,
        )
        self._idle_cooldown: int = watchdog_cfg.get(
            "idle_cooldown", _DEFAULT_IDLE_COOLDOWN,
        )
        self._auth_alert_cooldown: int = watchdog_cfg.get(
            "auth_alert_cooldown", _DEFAULT_AUTH_ALERT_COOLDOWN,
        )
        configured_excludes = watchdog_cfg.get("auth_scan_excludes")
        if configured_excludes is None:
            self._auth_scan_excludes: frozenset[str] = _DEFAULT_AUTH_SCAN_EXCLUDES
        else:
            self._auth_scan_excludes = frozenset(configured_excludes)

        # Track last alert time per agent to avoid spam
        self._last_alert: dict[str, float] = {}

        # Pending response from manager (agent -> True means waiting)
        self._awaiting_response: dict[str, bool] = {}

        # Auth-failure tracking: last alert time and last matched line per
        # agent. Suppression is keyed on (agent, matched_line) so a new
        # error surfaces immediately while a repeating burst of the same
        # line stays quiet until the cooldown expires.
        self._last_auth_alert: dict[str, float] = {}
        self._last_auth_match: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main watchdog loop. Call as an asyncio task."""
        logger.info("Idle watchdog started (interval=%ds)", self._check_interval)
        cycle = 0
        while True:
            await asyncio.sleep(self._check_interval)
            cycle += 1
            active = self._get_active_agents()
            logger.info("Watchdog cycle %d — %d active agents: %s", cycle, len(active),
                        [a for a, _ in active] if active else "none (fallback to state machine)")
            try:
                await self._check_idle_agents()
            except Exception:
                logger.exception("Watchdog check failed")
            try:
                await self._check_auth_failures()
            except Exception:
                logger.exception("Auth-failure check failed")

    async def handle_manager_response(self, message: dict[str, Any]) -> None:
        """Process a response from the manager about an idle alert.

        Expected message format::

            {"type": "manager_idle_response", "agent": "hub", "action": "expected"|"nudge"}
        """
        agent = message.get("agent", "")
        action = message.get("action", "")

        if not agent:
            return

        self._awaiting_response[agent] = False

        if action == "nudge":
            logger.info("Manager requested nudge for idle agent %s", agent)
            self._tmux_comm.nudge(agent, force=True)
        elif action == "expected":
            logger.info("Manager confirmed idle state for %s is expected", agent)
            # Extend cooldown so we don't re-alert soon
            self._last_alert[agent] = time.time()
        else:
            logger.warning("Unknown manager action for %s: %s", agent, action)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_current_agent(self) -> Optional[str]:
        """Determine which agent is responsible for the current state.

        Fallback method used when tasks.json has no assigned_agents.
        """
        current_state = self._state_machine.current_state
        initial = self._state_machine.initial_state

        if current_state == initial:
            return None

        # State names follow pattern: waiting_<agent>
        if current_state.startswith("waiting_"):
            return current_state[len("waiting_"):]

        # Fallback: check state config for agent field
        states = self._state_machine._config.get("states", {})
        state_cfg = states.get(current_state, {})
        if isinstance(state_cfg, dict):
            return state_cfg.get("agent")

        return None

    def _get_active_agents(self) -> list[tuple[str, str]]:
        """Return agents with active work from tasks.json assigned_agents.

        Reads all in_progress tasks from the task queue and returns agents
        whose status does NOT match a completion indicator.

        Returns:
            List of (agent_name, status_text) for agents still working.
            Empty list if no assigned_agents found (caller should fall back).
        """
        if self._task_queue is None:
            return []
        # Re-read tasks.json from disk to pick up external changes
        try:
            self._task_queue.reload()
        except Exception:
            pass

        active: list[tuple[str, str]] = []
        found_any = False

        for task in self._task_queue.tasks:
            if task.get("status") != "in_progress":
                continue
            assigned = task.get("assigned_agents")
            if not isinstance(assigned, dict):
                continue
            found_any = True
            for agent, status_text in assigned.items():
                if _DONE_PATTERNS.search(status_text or ""):
                    continue
                active.append((agent, status_text))

        if not found_any:
            return []  # signal caller to use fallback

        return active

    async def _check_idle_agents(self) -> None:
        """Check if assigned agents are idle.

        First tries multi-agent monitoring via tasks.json assigned_agents.
        Falls back to the state machine approach if no assigned_agents found.
        """
        # Try multi-agent monitoring from tasks.json first
        active_agents = self._get_active_agents()

        if active_agents:
            for agent, _status_text in active_agents:
                await self._check_single_agent(agent)
        elif self._lifecycle.current_task is not None:
            # Fallback: state machine single-agent monitoring
            agent = self._get_current_agent()
            if agent is not None:
                await self._check_single_agent(agent)

    async def _check_single_agent(self, agent: str) -> None:
        """Check if a single agent is idle and alert manager if so."""
        # Still waiting for manager response on this agent
        # Time out after idle_cooldown so we don't block forever
        if self._awaiting_response.get(agent, False):
            last = self._last_alert.get(agent, 0)
            if last > 0 and (time.time() - last) < self._idle_cooldown:
                return
            # Timed out waiting for manager — clear and re-alert
            self._awaiting_response[agent] = False

        # Cooldown — don't re-alert too soon
        last = self._last_alert.get(agent, 0)
        if last > 0 and (time.time() - last) < self._idle_cooldown:
            return

        # Check if agent pane is idle
        if not self._tmux_comm.is_agent_idle(agent):
            return

        # Agent is idle with a pending task — alert the manager
        await self._alert_manager(agent)

    async def _check_auth_failures(self) -> None:
        """Scan every known agent pane for MCP auth-failure signatures.

        Issue #28: when an agent's Claude Code OAuth or MCP bridge token
        rots, ``check_messages`` returns ``MCP error -32603: Authentication
        failed: Invalid token`` and the agent sits silently while messages
        pile up in its inbox. The watchdog greps each pane for that
        signature and flags the manager so the failure is visible within
        one cycle instead of appearing as mysterious idleness.
        """
        get_mapping = getattr(self._tmux_comm, "get_pane_mapping", None)
        if not callable(get_mapping):
            return
        try:
            agents = list(get_mapping().keys())
        except Exception:
            return
        for agent in agents:
            if agent in self._auth_scan_excludes:
                continue
            try:
                await self._check_agent_auth_failure(agent)
            except Exception:
                logger.exception("Auth-failure scan failed for %s", agent)

    async def _check_agent_auth_failure(self, agent: str) -> None:
        """Scan a single agent's pane for an auth-failure signature and,
        on first match of a new burst, publish a ``manager_directive`` to
        the manager inbox.

        Suppression is keyed on the exact matched line — identical
        repeating errors are quiet for ``auth_alert_cooldown`` seconds,
        but a *different* auth error on the same agent surfaces right
        away.
        """
        pane = self._tmux_comm.capture_pane(agent, lines=_AUTH_CAPTURE_LINES)
        if not pane:
            return
        match = _AUTH_ERROR_PATTERN.search(pane)
        if match is None:
            return

        # Report the full matched span, collapsing any tmux word-wrap
        # line breaks that fell inside the match into single spaces so
        # the directive/log line is readable on one line.
        matched_line = " ".join(match.group(0).split())
        if not matched_line:
            return

        now = time.time()
        last = self._last_auth_alert.get(agent, 0.0)
        last_line = self._last_auth_match.get(agent, "")
        within_cooldown = last > 0 and (now - last) < self._auth_alert_cooldown
        if within_cooldown and matched_line == last_line:
            return

        logger.warning(
            "flag_human: MCP auth failure on agent %s — %s",
            agent, matched_line,
        )

        # Envelope fields (message_id, timestamp, from) are filled in
        # automatically by NatsClient._envelope_wrap(). See #34.
        #
        # #28 originally generated a deterministic message_id keyed on
        # (agent, matched_line) so that identical re-observations
        # within the bridge cooldown would collapse by id. #34 drops
        # that: the watchdog's own `_last_auth_alert` / `_last_auth_match`
        # suppression (15 min keyed on matched_line) is the primary
        # dedup path and is unit-tested. If a suppression bug ever
        # produced duplicate publishes, the smoke test's observed-count
        # check would catch it and we could restore deterministic id
        # generation as a targeted patch.
        directive = {
            "type": _MSG_TYPE_MANAGER_DIRECTIVE,
            "subtype": "auth_failure",
            "agent": agent,
            "matched_line": matched_line,
            "message": (
                f"Watchdog flag: agent '{agent}' is hitting MCP auth failures "
                f"({matched_line!r}). Coordinate with dev/user to re-auth the "
                f"agent's Claude Code OAuth or MCP bridge token."
            ),
            "priority": "high",
        }
        try:
            await self._nats_client.publish_to_inbox("manager", directive)
        except Exception:
            logger.exception(
                "Failed to publish auth-failure directive to manager",
            )
            return

        self._last_auth_alert[agent] = now
        self._last_auth_match[agent] = matched_line

    async def _alert_manager(self, agent: str) -> None:
        """Capture pane and send idle alert to manager via NATS."""
        pane_text = self._tmux_comm.capture_pane(agent, lines=_CAPTURE_LINES)
        task = self._lifecycle.current_task

        alert = {
            "type": _MSG_TYPE_IDLE_ALERT,
            "agent": agent,
            "state": self._state_machine.current_state,
            "task_id": task.get("id", "") if task else "",
            "task_title": task.get("title", "") if task else "",
            "pane_capture": pane_text or "(capture failed)",
        }

        logger.info("Idle agent detected: %s — alerting manager", agent)
        await self._nats_client.publish_to_inbox("manager", alert)

        # Nudge manager's tmux pane so it checks messages
        self._tmux_comm.nudge("manager", force=True)

        self._last_alert[agent] = time.time()
        self._awaiting_response[agent] = True


class HeartbeatWatcher:
    """Watches ephemeral ``system.heartbeat.<agent>`` pings and flags
    staleness to the manager. Backfills the hub-exclusion gap from the
    #28 pane-grep auth detection (issue #32).

    How it works:

    1. On :meth:`start`, subscribes to ``system.heartbeat.>`` via core
       NATS. Each agent's MCP bridge publishes a ``health_ok`` signal
       at a fixed cadence (60s today) from the same NATS connection it
       already holds open.
    2. Every incoming heartbeat updates
       :attr:`_last_heartbeat_seen` ``[agent] = time.time()``. Messages
       with an unexpected shape are silently dropped.
    3. :meth:`run` ticks every ``check_interval`` seconds and, for each
       expected heartbeat agent, compares ``now - last_seen`` against
       the configured staleness threshold. On stale, a
       ``manager_directive`` with ``subtype='hub_unreachable'`` lands
       in ``agents.manager.inbox`` (inheriting the #34 envelope wrap).
    4. Suppression: alerts are bucketed on ``floor(staleness / threshold)``
       so we re-alert at 1×, 2×, 3×, … threshold — not every tick. A
       fresh heartbeat clears the bucket so the agent can re-alert
       cleanly on the next outage.
    5. Cold-start grace: if no heartbeat has ever been seen for an
       agent AND the orchestrator is younger than
       ``grace_multiplier × staleness_threshold``, alerts are
       suppressed. Prevents spurious ``hub_unreachable`` at boot when
       the bridge hasn't had time to publish its first heartbeat yet.

    Args:
        nats_client: NatsClient with :meth:`subscribe_core` and
            :meth:`publish_to_inbox` available.
        config: Merged config dict. Reads from ``config.watchdog``.
    """

    def __init__(self, nats_client: Any, config: dict[str, Any]) -> None:
        self._nats_client = nats_client

        watchdog_cfg = config.get("watchdog", {})
        configured_agents = watchdog_cfg.get("heartbeat_agents")
        if configured_agents is None:
            self._heartbeat_agents: tuple[str, ...] = tuple(_DEFAULT_HEARTBEAT_AGENTS)
        else:
            self._heartbeat_agents = tuple(configured_agents)

        self._staleness_threshold: int = int(
            watchdog_cfg.get(
                "hub_heartbeat_staleness_seconds",
                _DEFAULT_HEARTBEAT_STALENESS,
            ),
        )
        self._check_interval: int = int(
            watchdog_cfg.get(
                "hub_heartbeat_check_interval_seconds",
                _DEFAULT_HEARTBEAT_CHECK_INTERVAL,
            ),
        )
        self._grace_multiplier: float = float(
            watchdog_cfg.get(
                "hub_heartbeat_grace_multiplier",
                _DEFAULT_HEARTBEAT_GRACE_MULTIPLIER,
            ),
        )

        self._last_heartbeat_seen: dict[str, float] = {}
        self._last_alert_bucket: dict[str, int] = {}
        self._boot_time: float = time.time()
        self._subscription: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the heartbeat wildcard subject.

        Must be called before :meth:`run`. Idempotent — subscribing
        twice is a no-op.
        """
        if self._subscription is not None:
            return
        self._subscription = await self._nats_client.subscribe_core(
            _HEARTBEAT_WILDCARD,
            self._handle_heartbeat,
        )
        logger.info(
            "HeartbeatWatcher subscribed on %s (%d expected agents, "
            "staleness=%ds, check_interval=%ds, grace=%.1fx)",
            _HEARTBEAT_WILDCARD,
            len(self._heartbeat_agents),
            self._staleness_threshold,
            self._check_interval,
            self._grace_multiplier,
        )

    async def run(self) -> None:
        """Main staleness check loop. Call as an asyncio task after :meth:`start`."""
        logger.info(
            "HeartbeatWatcher loop started (check_interval=%ds)",
            self._check_interval,
        )
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                await self._check_staleness()
            except Exception:
                logger.exception("HeartbeatWatcher check failed")

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    async def _handle_heartbeat(self, msg: Any) -> None:
        """Record an incoming heartbeat's timestamp for the sending agent.

        Tolerates malformed payloads and unexpected message types
        gracefully — silent drop, no exception, no alert.
        """
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return
        if payload.get("type") != _MSG_TYPE_HEALTH_OK:
            return
        agent = payload.get("agent")
        if not agent or not isinstance(agent, str):
            return
        self._last_heartbeat_seen[agent] = time.time()
        # Clearing the alert bucket on heartbeat allows a subsequent
        # outage to re-alert cleanly from bucket 1 again. Without
        # this, the bucket would stay pinned at the last-alerted
        # value until the orchestrator restarted.
        if agent in self._last_alert_bucket:
            del self._last_alert_bucket[agent]

    # ------------------------------------------------------------------
    # Staleness check
    # ------------------------------------------------------------------

    async def _check_staleness(self) -> None:
        now = time.time()
        for agent in self._heartbeat_agents:
            await self._check_single_agent(agent, now)

    async def _check_single_agent(self, agent: str, now: float) -> None:
        last_seen = self._last_heartbeat_seen.get(agent)

        if last_seen is None:
            # Cold-start grace: if we have NEVER seen a heartbeat for
            # this agent and the orchestrator is still inside the
            # grace window, do not alert.
            grace_deadline = (
                self._boot_time
                + (self._staleness_threshold * self._grace_multiplier)
            )
            if now < grace_deadline:
                return
            staleness = now - self._boot_time
        else:
            staleness = now - last_seen

        if staleness < self._staleness_threshold:
            # Heartbeat fresh — no alert needed. Bucket was already
            # cleared by the heartbeat callback.
            return

        current_bucket = int(staleness // self._staleness_threshold)
        last_bucket = self._last_alert_bucket.get(agent, 0)
        if current_bucket <= last_bucket:
            # Same bucket already alerted; wait for the next threshold
            # multiple before re-alerting.
            return

        self._last_alert_bucket[agent] = current_bucket
        await self._alert_unreachable(agent, last_seen, staleness, current_bucket)

    async def _alert_unreachable(
        self,
        agent: str,
        last_seen: Optional[float],
        staleness: float,
        bucket: int,
    ) -> None:
        last_seen_iso: Optional[str]
        if last_seen is not None:
            last_seen_iso = datetime.fromtimestamp(
                last_seen, tz=timezone.utc,
            ).isoformat(timespec="seconds")
        else:
            last_seen_iso = None

        logger.warning(
            "flag_human: heartbeat stale on agent %s — "
            "last_seen=%s staleness=%ds threshold=%ds bucket=%d",
            agent,
            last_seen_iso or "never",
            int(staleness),
            self._staleness_threshold,
            bucket,
        )

        directive = {
            "type": _MSG_TYPE_MANAGER_DIRECTIVE,
            "subtype": "hub_unreachable",
            "agent": agent,
            "last_heartbeat_seen": last_seen_iso,
            "staleness_seconds": int(staleness),
            "staleness_bucket": bucket,
            "threshold_seconds": self._staleness_threshold,
            "message": (
                f"Watchdog flag: agent '{agent}' has not published a "
                f"heartbeat on system.heartbeat.{agent} for "
                f"{int(staleness)}s (threshold={self._staleness_threshold}s, "
                f"bucket={bucket}). Last seen: "
                f"{last_seen_iso or 'never since orchestrator boot'}. "
                f"The bridge may be down, the OAuth credential may have "
                f"expired, or the process itself may have died. "
                f"Coordinate with dev/user to investigate and re-auth "
                f"or restart."
            ),
            "priority": "high",
        }
        try:
            await self._nats_client.publish_to_inbox("manager", directive)
        except Exception:
            logger.exception(
                "Failed to publish hub_unreachable directive to manager",
            )


class InactivityAnnouncer:
    """Monitors NATS activity and alerts when all agents are idle.

    Args:
        nats_client: NatsClient for sending alerts.
        router: MessageRouter with last_activity_time attribute.
        config: watchdog.inactivity_announcer config dict.
    """

    def __init__(self, nats_client: Any, router: Any, config: dict[str, Any]) -> None:
        self._nats_client = nats_client
        self._router = router
        self._threshold = config.get("threshold_seconds", 300)
        self._escalate_after = config.get("escalate_after", 3)
        self._announce_on_speaker = config.get("announce_on_speaker", False)
        self._speaker_subject = "agents.hassio.speaker"
        self._count = 0
        self._escalated = False

    async def run(self) -> None:
        """Main announcer loop. Call as an asyncio task."""
        logger.info("Inactivity announcer started (threshold=%ds, escalate after %d)",
                     self._threshold, self._escalate_after)
        while True:
            await asyncio.sleep(30)
            idle_seconds = time.time() - self._router.last_activity_time

            if idle_seconds >= self._threshold:
                expected_count = int(idle_seconds // self._threshold)
                if expected_count > self._count:
                    self._count = expected_count
                    minutes = int(idle_seconds // 60)
                    await self._send_alert(minutes)
            else:
                if self._count > 0:
                    logger.info("Activity resumed after %d inactivity alerts — flags cleared", self._count)
                self._count = 0
                self._escalated = False

    async def _send_alert(self, minutes: int) -> None:
        """Send inactivity alert to manager, optionally to speaker."""
        logger.info("Inactivity alert #%d — no activity for %d minutes", self._count, minutes)

        notify = {
            "type": "agent_message",
            "from": "orchestrator",
            "message": f"Inactivity alert #{self._count}: no agent activity for {minutes} minutes. All agents appear idle.",
            "priority": "normal",
        }
        try:
            await self._nats_client.publish_to_inbox("manager", notify)
        except Exception as e:
            logger.warning("Failed to notify manager: %s", e)

        if self._announce_on_speaker:
            msg = json.dumps({
                "text": f"Orchestrator here. No agent activity for {minutes} minutes. Alert number {self._count}.",
                "from": "orchestrator",
            })
            try:
                await self._nats_client.publish_raw(self._speaker_subject, msg.encode())
            except Exception:
                pass

        if self._count >= self._escalate_after and not self._escalated:
            self._escalated = True
            logger.warning("Inactivity escalation — %d alerts, notifying manager to investigate", self._escalate_after)
            escalation = {
                "type": "agent_message",
                "from": "orchestrator",
                "message": f"ESCALATION: No agent activity for {self._count} consecutive checks. Investigate why agents are idle. Ask hub (QA) to run health tests.",
                "priority": "urgent",
            }
            try:
                await self._nats_client.publish_to_inbox("manager", escalation)
            except Exception as e:
                logger.warning("Failed to escalate: %s", e)
