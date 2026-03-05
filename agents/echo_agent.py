"""Echo Agent -- Example Script Agent for the Multi-Agent System Shell.

A minimal script agent that:
  - Accepts --role CLI argument to set its agent identity
  - Subscribes to agents.<role>.inbox via NATS
  - Responds with agent_complete / pass on task_assignment messages
  - Exits cleanly (code 0) on receiving all_done

Requirements traced to PRD:
  - R2: Config-Driven Agents (script runtime)
  - R3: Communication Flow (direct NATS for script agents)
  - R3: all_done message handling
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from nats.aio.client import Client as NATSClient

# Alias used in the module so tests can patch agents.echo_agent.nats_connect
nats_connect = NATSClient

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the echo agent.

    Args:
        argv: List of CLI arguments (defaults to sys.argv[1:]).

    Returns:
        Parsed namespace with ``role`` and ``nats_url``.
    """
    parser = argparse.ArgumentParser(description="Echo Agent -- example script agent")
    parser.add_argument("--role", required=True, help="Agent role identity")
    parser.add_argument(
        "--nats-url",
        default="nats://localhost:4222",
        help="NATS server URL (default: nats://localhost:4222)",
    )
    return parser.parse_args(argv)


class EchoAgent:
    """Echo agent that subscribes to NATS inbox and echoes task assignments.

    Args:
        role: Agent role identity (e.g. ``executor``).
        nats_url: NATS server URL.
    """

    def __init__(self, role: str, nats_url: str) -> None:
        self.role = role
        self.nats_url = nats_url
        self.inbox_subject = f"agents.{role}.inbox"
        self.outbox_subject = f"agents.{role}.outbox"
        self.should_exit = False
        self._nc: NATSClient | None = None

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def handle_task(self, msg: dict) -> dict:
        """Handle a task_assignment message and return an agent_complete response.

        Args:
            msg: Parsed task_assignment message dict.

        Returns:
            Dict with type=agent_complete, status=pass, and an echo summary.
        """
        task_id = msg.get("task_id", "unknown")
        title = msg.get("title", "")
        description = msg.get("description", "")
        summary = f"Echoed task: {task_id} - {title}"
        if description:
            summary += f" ({description})"
        return {
            "type": "agent_complete",
            "status": "pass",
            "summary": summary,
        }

    async def process_message(self, raw: bytes) -> None:
        """Process a raw NATS message payload.

        Handles:
          - task_assignment: respond with agent_complete on outbox
          - all_done: set should_exit flag and drain connection
          - Unknown/malformed: ignore gracefully

        Args:
            raw: Raw bytes from NATS subscription.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Received malformed message, ignoring")
            return

        msg_type = data.get("type")
        if msg_type is None:
            logger.warning("Message missing 'type' field, ignoring")
            return

        if msg_type == "task_assignment":
            response = await self.handle_task(data)
            if self._nc is not None:
                await self._nc.publish(
                    self.outbox_subject,
                    json.dumps(response).encode(),
                )
        elif msg_type == "all_done":
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

        async def _on_message(msg):
            await self.process_message(msg.data)

        await nc.subscribe(self.inbox_subject, cb=_on_message)

        # Block until should_exit is set
        while not self.should_exit:
            await asyncio.sleep(0.1)


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
