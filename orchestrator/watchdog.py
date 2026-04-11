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
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CHECK_INTERVAL = 60  # seconds between checks
_DEFAULT_IDLE_COOLDOWN = 300  # don't re-alert same agent within this window
_CAPTURE_LINES = 20  # lines to capture from pane

# Auth-failure detection (issue #28). Deeper capture because the error
# often scrolls above the prompt footer before the watchdog sees it.
_DEFAULT_AUTH_ALERT_COOLDOWN = 900  # 15 min burst suppression
_AUTH_CAPTURE_LINES = 60
_AUTH_ERROR_PATTERN = re.compile(r"-32603|Authentication failed", re.IGNORECASE)

_MSG_TYPE_IDLE_ALERT = "idle_alert"
_MSG_TYPE_MANAGER_RESPONSE = "manager_idle_response"
_MSG_TYPE_MANAGER_DIRECTIVE = "manager_directive"

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
        if not _AUTH_ERROR_PATTERN.search(pane):
            return

        matched_line = ""
        for line in pane.splitlines():
            if _AUTH_ERROR_PATTERN.search(line):
                matched_line = line.strip()
                break
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
