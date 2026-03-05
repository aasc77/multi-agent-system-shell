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
from typing import Any

logger = logging.getLogger(__name__)


class RouterError(Exception):
    """Raised when the message router encounters an error."""


class MessageRouter:
    """Routes incoming agent outbox messages to the state machine.

    Args:
        nats_client: NatsClient instance for subscribing to outbox subjects.
        state_machine: StateMachine instance for handling triggers/transitions.
        lifecycle_manager: TaskLifecycleManager for executing actions.
        agents: Dict of agent definitions (name -> agent config).
    """

    def __init__(
        self,
        nats_client: Any,
        state_machine: Any,
        lifecycle_manager: Any,
        agents: dict[str, Any],
    ) -> None:
        self._nats_client = nats_client
        self._state_machine = state_machine
        self._lifecycle_manager = lifecycle_manager
        self._agents = agents
        self._paused = False

        # Build set of known trigger types from transitions
        self._known_triggers: set[str] = {
            t.get("trigger")
            for t in state_machine.transitions
            if t.get("trigger")
        }

    # ------------------------------------------------------------------
    # Pause / resume support (R7)
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Return whether the router is currently paused."""
        return self._paused

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
        """Subscribe to all agent outbox subjects."""
        await self._nats_client.subscribe_all_outboxes(self.handle_message)

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
        if self._paused:
            return

        try:
            # --- Parse JSON ---
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                role = self._extract_role(msg.subject)
                logger.warning(
                    "Unrecognized outbox message from %s: %s", role, msg.data
                )
                await msg.ack()
                return

            role = self._extract_role(msg.subject)
            msg_type = payload.get("type")
            status = payload.get("status")

            # --- Validate required fields (missing, empty, or None) ---
            if not msg_type or not status:
                logger.warning(
                    "Unrecognized outbox message from %s: %s", role, payload
                )
                await msg.ack()
                return

            # --- Check if type is a known trigger ---
            if msg_type not in self._known_triggers:
                logger.warning(
                    "Unrecognized outbox message from %s: %s", role, payload
                )
                await msg.ack()
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
                    "No matching transition for %s from %s in state %s",
                    msg_type,
                    role,
                    self._state_machine.current_state,
                )
                await msg.ack()
                return

            # --- Hand off to lifecycle manager ---
            await self._lifecycle_manager.execute_action(
                result.action, result.action_args, result
            )
            await msg.ack()

        except Exception:
            logger.exception("Error handling message")
            await msg.ack()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_role(subject: str) -> str:
        """Extract the agent role from a NATS subject (agents.<role>.outbox)."""
        parts = subject.split(".")
        if len(parts) >= 2:
            return parts[1]
        return "unknown"
