"""Idle Agent Watchdog for the Multi-Agent System Shell.

Periodically checks if the agent assigned to the current task is idle
(at prompt). When detected, captures the pane and sends it to the
manager agent via NATS for review. The manager responds with
"expected" (leave alone) or "nudge" (re-nudge the agent).

Runs as an async task inside the orchestrator's event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CHECK_INTERVAL = 60  # seconds between checks
_DEFAULT_IDLE_COOLDOWN = 300  # don't re-alert same agent within this window
_CAPTURE_LINES = 20  # lines to capture from pane

_MSG_TYPE_IDLE_ALERT = "idle_alert"
_MSG_TYPE_MANAGER_RESPONSE = "manager_idle_response"


class IdleWatchdog:
    """Monitors agent panes and alerts the manager when an agent is idle
    while a task is assigned to it.

    Args:
        lifecycle: TaskLifecycleManager for checking current task state.
        state_machine: StateMachine for determining which agent owns current state.
        nats_client: NatsClient for sending alerts to manager.
        tmux_comm: TmuxComm for capturing panes and nudging.
        config: Merged config dict with agent and watchdog settings.
    """

    def __init__(
        self,
        lifecycle: Any,
        state_machine: Any,
        nats_client: Any,
        tmux_comm: Any,
        config: dict[str, Any],
    ) -> None:
        self._lifecycle = lifecycle
        self._state_machine = state_machine
        self._nats_client = nats_client
        self._tmux_comm = tmux_comm

        watchdog_cfg = config.get("watchdog", {})
        self._check_interval: int = watchdog_cfg.get(
            "check_interval", _DEFAULT_CHECK_INTERVAL,
        )
        self._idle_cooldown: int = watchdog_cfg.get(
            "idle_cooldown", _DEFAULT_IDLE_COOLDOWN,
        )

        # Track last alert time per agent to avoid spam
        self._last_alert: dict[str, float] = {}

        # Pending response from manager (agent -> True means waiting)
        self._awaiting_response: dict[str, bool] = {}

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
            if cycle % 5 == 0:  # log heartbeat every 5 cycles
                logger.info("Watchdog heartbeat — cycle %d, monitoring agents", cycle)
            await self._check_idle_agents()

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
        """Determine which agent is responsible for the current state."""
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

    async def _check_idle_agents(self) -> None:
        """Check if the current assigned agent is idle."""
        # No task in progress — nothing to watch
        if self._lifecycle.current_task is None:
            return

        agent = self._get_current_agent()
        if agent is None:
            return

        # Still waiting for manager response on this agent
        if self._awaiting_response.get(agent, False):
            return

        # Cooldown — don't re-alert too soon
        last = self._last_alert.get(agent, 0)
        if last > 0 and (time.time() - last) < self._idle_cooldown:
            return

        # Check if agent pane is idle
        if not self._tmux_comm.is_agent_idle(agent):
            return

        # Agent is idle with a pending task — alert the manager
        await self._alert_manager(agent)

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
