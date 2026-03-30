"""Message Router for the Multi-Agent System Shell.

Routes incoming NATS outbox messages to the state machine and lifecycle manager.

Requirements traced to PRD:
  - R3: Communication Flow (outbox message schema, unrecognized messages, field mapping)
  - R4: Config-Driven State Machine (trigger dispatch, transition matching)
  - R5: Task Queue (attempt counter must NOT increment for unrecognized/no-match)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# --- Outbox message payload keys (PRD R3 — "Outbox message schema") ---
_MSG_KEY_TYPE = "type"
_MSG_KEY_STATUS = "status"

# --- Transition config key (PRD R4) ---
_KEY_TRIGGER = "trigger"

# --- NATS subject parsing (subject format: agents.<role>.outbox) ---
_SUBJECT_SEPARATOR = "."
_SUBJECT_ROLE_INDEX = 1
_MIN_SUBJECT_PARTS = 2
_FALLBACK_ROLE = "unknown"

# --- Log message templates ---
_LOG_UNRECOGNIZED = "Unrecognized outbox message from %s: %s"
_LOG_NO_MATCHING_TRANSITION = (
    "No matching transition for %s from %s in state %s"
)
_LOG_HANDLER_ERROR = "Error handling message"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RouterError(Exception):
    """Raised when the message router encounters an error."""


# ---------------------------------------------------------------------------
# Message Router
# ---------------------------------------------------------------------------


class MessageRouter:
    """Routes incoming agent outbox messages to the state machine.

    Subscribes to all agent outbox NATS subjects, parses incoming JSON
    messages, validates required fields, dispatches to the state machine,
    and hands off transition results to the lifecycle manager.

    Also watches agent inboxes for ``agent_message`` types (sent via the
    ``send_to_agent`` MCP tool) and nudges the target agent's tmux pane
    so it knows to call ``check_messages``.

    Args:
        nats_client: NatsClient instance for subscribing to outbox subjects.
        state_machine: StateMachine instance for handling triggers/transitions.
        lifecycle_manager: TaskLifecycleManager for executing actions.
        tmux_comm: TmuxComm instance for nudging agent panes.
        agents: Dict of agent definitions (name -> agent config).
    """

    def __init__(
        self,
        nats_client: Any,
        state_machine: Any,
        lifecycle_manager: Any,
        agents: dict[str, Any],
        tmux_comm: Any = None,
        watchdog: Any = None,
    ) -> None:
        self._nats_client = nats_client
        self._state_machine = state_machine
        self._lifecycle_manager = lifecycle_manager
        self._tmux_comm = tmux_comm
        self._watchdog = watchdog
        self._agents = agents
        self._paused = False
        self._last_activity_time: float = time.time()

        # Build set of known trigger types from transitions
        self._known_triggers: set[str] = {
            t.get(_KEY_TRIGGER)
            for t in state_machine.transitions
            if t.get(_KEY_TRIGGER)
        }

    # ------------------------------------------------------------------
    # Pause / resume support (R7)
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Return whether the router is currently paused."""
        return self._paused

    @property
    def last_activity_time(self) -> float:
        """Return the timestamp of the last message activity."""
        return self._last_activity_time

    def _touch_activity(self) -> None:
        """Update the last activity timestamp."""
        self._last_activity_time = time.time()

    def pause(self) -> None:
        """Pause message processing."""
        self._paused = True

    def resume(self) -> None:
        """Resume message processing."""
        self._paused = False

    # ------------------------------------------------------------------
    # Subscription setup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to all agent outbox and inbox subjects."""
        await self._nats_client.subscribe_all_outboxes(self.handle_message)
        if self._tmux_comm is not None:
            await self._nats_client.subscribe_all_inboxes(self._handle_inbox_relay)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_message(self, msg: Any) -> None:
        """Handle an incoming NATS outbox message.

        Steps:
          1. Parse JSON from msg.data
          2. Validate required fields (type, status)
          3. Check if type is a known trigger
          4. Dispatch to state machine
          5. Hand off transition result to lifecycle manager
          6. ACK the message in all cases
        """
        self._touch_activity()

        if self._paused:
            return

        try:
            # --- Parse JSON ---
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                role = self._extract_role(msg.subject)
                await self._discard_unrecognized(msg, role, msg.data)
                return

            role = self._extract_role(msg.subject)
            msg_type = payload.get(_MSG_KEY_TYPE)
            status = payload.get(_MSG_KEY_STATUS)

            # --- Validate required fields (missing, empty, or None) ---
            if not msg_type or not status:
                await self._discard_unrecognized(msg, role, payload)
                return

            # --- Manager idle response → route to watchdog ---
            if msg_type == "manager_idle_response" and self._watchdog is not None:
                await self._watchdog.handle_manager_response(payload)
                await msg.ack()
                return

            # --- Check if type is a known trigger ---
            if msg_type not in self._known_triggers:
                await self._discard_unrecognized(msg, role, payload)
                return

            # --- Dispatch to state machine ---
            result = self._state_machine.handle_trigger(
                trigger=msg_type,
                source_agent=role,
                status=status,
            )

            if result is None:
                # Valid type but no matching transition for current state
                logger.warning(
                    _LOG_NO_MATCHING_TRANSITION,
                    msg_type,
                    role,
                    self._state_machine.current_state,
                )
                await msg.ack()
                return

            logger.info(
                "Transition: %s -> %s (trigger=%s, agent=%s, status=%s)",
                result.from_state, result.to_state, msg_type, role, status,
            )

            # --- Hand off to lifecycle manager ---
            await self._lifecycle_manager.execute_action(
                result.action, result.action_args, result
            )
            await msg.ack()

        except Exception:
            logger.exception(_LOG_HANDLER_ERROR)
            await msg.ack()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _discard_unrecognized(
        msg: Any, role: str, payload_or_data: Any,
    ) -> None:
        """Log a warning for an unrecognized message and ACK it.

        This covers three cases (PRD R3 — "Unrecognized messages"):
        - Invalid JSON (payload_or_data is raw ``msg.data``)
        - Missing/empty required fields (``type`` or ``status``)
        - Unknown trigger type not in any transition
        """
        logger.warning(_LOG_UNRECOGNIZED, role, payload_or_data)
        await msg.ack()

    @staticmethod
    def _extract_role(subject: str) -> str:
        """Extract the agent role from a NATS subject (``agents.<role>.outbox``)."""
        parts = subject.split(_SUBJECT_SEPARATOR)
        if len(parts) >= _MIN_SUBJECT_PARTS:
            return parts[_SUBJECT_ROLE_INDEX]
        return _FALLBACK_ROLE

    # ------------------------------------------------------------------
    # Inbox relay: nudge agents on agent-to-agent messages
    # ------------------------------------------------------------------

    async def _handle_inbox_relay(self, msg: Any) -> None:
        """Watch inbox messages for ``agent_message`` type and nudge the target.

        This enables agent-to-agent communication: when agent A uses
        ``send_to_agent`` to message agent B, the orchestrator sees the
        message land in B's inbox and nudges B's tmux pane so it calls
        ``check_messages``.

        Non-``agent_message`` types (e.g. ``task_assignment``) are ignored
        since the orchestrator already handles those via its own nudge path.
        """
        self._touch_activity()
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            await msg.ack()
            return

        if payload.get(_MSG_KEY_TYPE) != "agent_message":
            await msg.ack()
            return

        target_role = self._extract_role(msg.subject)
        sender = payload.get("from", "unknown")
        logger.info(
            "Relaying nudge to %s (agent_message from %s)", target_role, sender,
        )
        try:
            self._tmux_comm.nudge(target_role, force=True)
        except Exception:
            # Monitor agents (e.g. manager) have no pane in the agents window
            logger.debug("Skipping tmux nudge for %s (no pane)", target_role)
        await msg.ack()
