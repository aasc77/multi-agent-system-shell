"""Echo Agent -- Example Script Agent for the Multi-Agent System Shell.

A minimal script agent that:
  - Accepts ``--role`` CLI argument to set its agent identity
  - Subscribes to ``agents.<role>.inbox`` via NATS
  - Responds with ``agent_complete`` / ``pass`` on ``task_assignment`` messages
  - Exits cleanly (code 0) on receiving ``all_done``

Requirements traced to PRD:
  - R2: Config-Driven Agents (script runtime)
  - R3: Communication Flow (direct NATS for script agents)
  - R3: all_done message handling

Usage::

    python3 agents/echo_agent.py --role executor
    python3 agents/echo_agent.py --role executor --nats-url nats://remote:4222
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from nats.aio.client import Client as NATSClient

# Alias used in the module so tests can patch agents.echo_agent.nats_connect
nats_connect = NATSClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants -- single source of truth for magic strings and defaults
# ---------------------------------------------------------------------------

# NATS subject convention (PRD R3): ``<prefix>.<role>.<suffix>``
_SUBJECTS_PREFIX = "agents"
_INBOX_SUFFIX = "inbox"
_OUTBOX_SUFFIX = "outbox"

# Default NATS server URL (PRD R3 — NATS details)
DEFAULT_NATS_URL = "nats://localhost:4222"

# Inbound message types (PRD R3 — Inbox message schema)
MSG_TYPE_TASK_ASSIGNMENT = "task_assignment"
MSG_TYPE_ALL_DONE = "all_done"

# Outbound message types and statuses (PRD R3 — Outbox message schema)
MSG_TYPE_AGENT_COMPLETE = "agent_complete"
STATUS_PASS = "pass"

# Message field keys
_KEY_TYPE = "type"
_KEY_TASK_ID = "task_id"
_KEY_TITLE = "title"
_KEY_DESCRIPTION = "description"
_KEY_STATUS = "status"
_KEY_SUMMARY = "summary"

# Default values for missing message fields
_DEFAULT_TASK_ID = "unknown"

# Run loop polling interval in seconds
_POLL_INTERVAL_SECONDS = 0.1


def _build_subject(role: str, suffix: str) -> str:
    """Build a NATS subject from the ``<prefix>.<role>.<suffix>`` convention.

    Args:
        role: Agent role identity (e.g. ``executor``).
        suffix: Subject suffix (``inbox`` or ``outbox``).

    Returns:
        Fully qualified NATS subject string.
    """
    return f"{_SUBJECTS_PREFIX}.{role}.{suffix}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the echo agent.

    Args:
        argv: List of CLI arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Parsed namespace with ``role`` and ``nats_url``.
    """
    parser = argparse.ArgumentParser(description="Echo Agent -- example script agent")
    parser.add_argument("--role", required=True, help="Agent role identity")
    parser.add_argument(
        "--nats-url",
        default=DEFAULT_NATS_URL,
        help=f"NATS server URL (default: {DEFAULT_NATS_URL})",
    )
    return parser.parse_args(argv)


class EchoAgent:
    """Echo agent that subscribes to NATS inbox and echoes task assignments.

    The agent follows the PRD R3 communication protocol: it listens on
    ``agents.<role>.inbox`` for ``task_assignment`` messages, responds with
    ``agent_complete`` on ``agents.<role>.outbox``, and exits cleanly on
    ``all_done``.

    Args:
        role: Agent role identity (e.g. ``executor``).
        nats_url: NATS server URL.
    """

    def __init__(self, role: str, nats_url: str) -> None:
        self.role = role
        self.nats_url = nats_url
        self.inbox_subject = _build_subject(role, _INBOX_SUFFIX)
        self.outbox_subject = _build_subject(role, _OUTBOX_SUFFIX)
        self.should_exit = False
        self._nc: NATSClient | None = None

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_task(self, msg: dict[str, Any]) -> dict[str, str]:
        """Handle a ``task_assignment`` message and return an ``agent_complete`` response.

        Args:
            msg: Parsed task_assignment message dict.

        Returns:
            Dict with ``type=agent_complete``, ``status=pass``, and an echo summary.
        """
        task_id = msg.get(_KEY_TASK_ID, _DEFAULT_TASK_ID)
        title = msg.get(_KEY_TITLE, "")
        description = msg.get(_KEY_DESCRIPTION, "")
        summary = f"Echoed task: {task_id} - {title}"
        if description:
            summary += f" ({description})"
        return {
            _KEY_TYPE: MSG_TYPE_AGENT_COMPLETE,
            _KEY_STATUS: STATUS_PASS,
            _KEY_SUMMARY: summary,
        }

    async def process_message(self, raw: bytes) -> None:
        """Process a raw NATS message payload.

        Handles:
          - ``task_assignment``: respond with ``agent_complete`` on outbox
          - ``all_done``: set ``should_exit`` flag and drain connection
          - Unknown/malformed: ignore gracefully

        Args:
            raw: Raw bytes from NATS subscription.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Received malformed message, ignoring")
            return

        msg_type = data.get(_KEY_TYPE)
        if msg_type is None:
            logger.warning("Message missing '%s' field, ignoring", _KEY_TYPE)
            return

        if msg_type == MSG_TYPE_TASK_ASSIGNMENT:
            response = await self.handle_task(data)
            if self._nc is not None:
                await self._nc.publish(
                    self.outbox_subject,
                    json.dumps(response).encode(),
                )
        elif msg_type == MSG_TYPE_ALL_DONE:
            self.should_exit = True
            if self._nc is not None:
                await self._nc.drain()
        else:
            logger.debug("Unknown message type '%s', ignoring", msg_type)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to NATS, subscribe to inbox, and process messages until exit."""
        nc = nats_connect()
        await nc.connect(self.nats_url)
        self._nc = nc

        async def _on_message(msg: Any) -> None:
            await self.process_message(msg.data)

        await nc.subscribe(self.inbox_subject, cb=_on_message)

        # Block until should_exit is set
        while not self.should_exit:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _async_main() -> None:
    """Async entry point."""
    args = parse_args()
    agent = EchoAgent(role=args.role, nats_url=args.nats_url)
    await agent.run()


def main() -> None:
    """Synchronous entry point for CLI invocation."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
