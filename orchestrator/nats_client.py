"""
orchestrator/nats_client.py -- Async NATS Client Wrapper

Wraps nats-py to provide publish/subscribe operations for the
multi-agent orchestrator over NATS JetStream.

Requirements:
  - R3: Communication Flow (NATS details)
  - R5: Task Queue (all_done message)
  - Error Handling: NATS unavailability
"""

import asyncio
import json
import logging

import nats
from nats.js.api import StreamConfig, RetentionPolicy

logger = logging.getLogger(__name__)


class NatsClientError(Exception):
    """Raised when the NATS client encounters an unrecoverable error."""


class NatsClient:
    """Async NATS JetStream client for agent communication.

    Usage::

        client = NatsClient(config=nats_config, agents=agents)
        await client.connect()
        await client.publish_to_inbox("writer", {"type": "task_assignment", ...})
        await client.close()
    """

    def __init__(self, *, config: dict, agents: dict) -> None:
        if "url" not in config:
            raise NatsClientError("Config missing required key: 'url'")
        self._config = config
        self._agents = agents
        self._url: str = config["url"]
        self._stream_name: str = config.get("stream", "AGENTS")
        self._prefix: str = config.get("subjects_prefix", "agents")
        self._conn = None
        self._js = None

    # ------------------------------------------------------------------
    # Subject helpers
    # ------------------------------------------------------------------

    def inbox_subject(self, role: str) -> str:
        """Return the inbox subject for *role*."""
        return f"{self._prefix}.{role}.inbox"

    def outbox_subject(self, role: str) -> str:
        """Return the outbox subject for *role*."""
        return f"{self._prefix}.{role}.outbox"

    def wildcard_subject(self) -> str:
        """Return the stream wildcard subject."""
        return f"{self._prefix}.>"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return ``True`` when the underlying NATS connection is live."""
        if self._conn is None:
            return False
        return self._conn.is_connected

    async def connect(self) -> None:
        """Connect to NATS and set up JetStream.

        Raises:
            NatsClientError: If the server is unreachable.
        """
        if self._conn is not None and self._conn.is_connected:
            return  # already connected -- idempotent

        try:
            self._conn = await nats.connect(
                self._url,
                reconnected_cb=self._on_reconnected,
                disconnected_cb=self._on_disconnected,
                error_cb=self._on_error,
            )
        except Exception as exc:
            raise NatsClientError(
                f"NATS unreachable at {self._url}. "
                "Run: scripts/setup-nats.sh"
            ) from exc

        js = self._conn.jetstream()
        if asyncio.iscoroutine(js):
            js = await js
        self._js = js
        await self._ensure_stream()

    async def close(self) -> None:
        """Gracefully close the NATS connection."""
        if self._conn is not None:
            await self._conn.close()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish_to_inbox(self, role: str, message: dict) -> None:
        """Publish a JSON *message* to ``<prefix>.<role>.inbox``.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        subject = self.inbox_subject(role)
        payload = json.dumps(message).encode()
        await self._js.publish(subject, payload)

    async def publish_all_done(self, summary: str) -> None:
        """Publish an ``all_done`` message to every agent's inbox.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        message = {"type": "all_done", "summary": summary}
        for role in self._agents:
            await self.publish_to_inbox(role, message)

    # ------------------------------------------------------------------
    # Subscribing
    # ------------------------------------------------------------------

    async def subscribe_to_outbox(self, role: str, callback) -> None:
        """Subscribe to ``<prefix>.<role>.outbox`` with a durable consumer.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        subject = self.outbox_subject(role)
        durable = f"{role}-outbox"
        await self._js.subscribe(subject, cb=callback, durable=durable)

    async def subscribe_all_outboxes(self, callback) -> None:
        """Subscribe to every agent's outbox with a durable consumer."""
        for role in self._agents:
            await self.subscribe_to_outbox(role, callback)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._js is None:
            raise NatsClientError("Not connected. Call connect() first.")

    async def _ensure_stream(self) -> None:
        """Create or verify the JetStream stream."""
        try:
            await self._js.find_stream_name_by_subject(self.wildcard_subject())
        except Exception:
            # Stream does not exist -- create it
            await self._js.add_stream(
                config=StreamConfig(
                    name=self._stream_name,
                    subjects=[self.wildcard_subject()],
                    retention=RetentionPolicy.LIMITS,
                    max_msgs=10000,
                    max_age=3600,
                ),
            )

    # Reconnection callbacks
    async def _on_reconnected(self) -> None:
        logger.info("NATS reconnected")

    async def _on_disconnected(self) -> None:
        logger.warning("NATS disconnected")

    async def _on_error(self, exc: Exception) -> None:
        logger.error("NATS error: %s", exc)
