"""Async NATS Client Wrapper for multi-agent orchestrator.

Wraps nats-py to provide publish/subscribe operations over NATS JetStream.

Requirements traced to PRD:
  - R3: Communication Flow (NATS details)
  - R5: Task Queue (all_done message)
  - Error Handling: NATS unavailability

Usage::

    client = NatsClient(config=nats_config, agents=agents)
    await client.connect()
    await client.publish_to_inbox("writer", {"type": "task_assignment", ...})
    await client.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import nats
from nats.js.api import StreamConfig, RetentionPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants -- single source of truth for config keys and defaults
# ---------------------------------------------------------------------------

# Config key names (avoids scattered magic strings)
_KEY_URL = "url"
_KEY_STREAM = "stream"
_KEY_SUBJECTS_PREFIX = "subjects_prefix"

# Default config values (PRD R3 — NATS details)
DEFAULT_STREAM_NAME = "AGENTS"
DEFAULT_SUBJECTS_PREFIX = "agents"

# Subject suffixes for the ``<prefix>.<role>.<suffix>`` convention
_INBOX_SUFFIX = "inbox"
_OUTBOX_SUFFIX = "outbox"

# JetStream stream defaults (PRD R3)
DEFAULT_MAX_MSGS = 10_000
DEFAULT_MAX_AGE_SECONDS = 3600  # 1 hour

# Message types
MSG_TYPE_ALL_DONE = "all_done"

# Setup script reference included in error messages
_SETUP_SCRIPT = "scripts/setup-nats.sh"

# Type alias for async message callbacks
MessageCallback = Callable[..., Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NatsClientError(Exception):
    """Raised when the NATS client encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class NatsClient:
    """Async NATS JetStream client for agent communication.

    Provides publish/subscribe over JetStream with durable consumers,
    automatic stream provisioning, and reconnection callbacks.

    Args:
        config: NATS configuration dict.  Must contain ``url``; optional
            keys ``stream`` and ``subjects_prefix`` override defaults.
        agents: Dict of agent definitions (name → agent config).

    Raises:
        NatsClientError: If *config* is missing the required ``url`` key.
    """

    def __init__(self, *, config: dict[str, Any], agents: dict[str, Any]) -> None:
        if _KEY_URL not in config:
            raise NatsClientError(
                f"Config missing required key: '{_KEY_URL}'"
            )
        self._config = config
        self._agents = agents
        self._url: str = config[_KEY_URL]
        self._stream_name: str = config.get(_KEY_STREAM, DEFAULT_STREAM_NAME)
        self._prefix: str = config.get(
            _KEY_SUBJECTS_PREFIX, DEFAULT_SUBJECTS_PREFIX,
        )
        self._conn: nats.NATS | None = None
        self._js = None

    # ------------------------------------------------------------------
    # Subject helpers
    # ------------------------------------------------------------------

    def inbox_subject(self, role: str) -> str:
        """Return the inbox subject for *role*."""
        return f"{self._prefix}.{role}.{_INBOX_SUFFIX}"

    def outbox_subject(self, role: str) -> str:
        """Return the outbox subject for *role*."""
        return f"{self._prefix}.{role}.{_OUTBOX_SUFFIX}"

    def wildcard_subject(self) -> str:
        """Return the stream wildcard subject (``<prefix>.>``)."""
        return f"{self._prefix}.>"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return ``True`` when the underlying NATS connection is live."""
        return self._conn is not None and self._conn.is_connected

    async def connect(self) -> None:
        """Connect to NATS and set up JetStream.

        The call is idempotent — calling it on an already-connected client
        is a no-op.

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
                f"Run: {_SETUP_SCRIPT}"
            ) from exc

        js = self._conn.jetstream()
        if asyncio.iscoroutine(js):
            js = await js
        self._js = js
        await self._ensure_stream()

    async def close(self) -> None:
        """Gracefully close the NATS connection.

        Safe to call even if the client was never connected.
        """
        if self._conn is not None:
            await self._conn.close()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish_to_inbox(self, role: str, message: dict[str, Any]) -> None:
        """Publish a JSON *message* to ``<prefix>.<role>.inbox``.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        subject = self.inbox_subject(role)
        payload = json.dumps(message).encode()
        await self._js.publish(subject, payload)

    async def publish_raw(self, subject: str, payload: bytes) -> None:
        """Publish raw bytes to an arbitrary NATS subject.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        await self._js.publish(subject, payload)

    async def publish_all_done(self, summary: str) -> None:
        """Publish an ``all_done`` message to every agent's inbox.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        message = {"type": MSG_TYPE_ALL_DONE, "summary": summary}
        for role in self._agents:
            await self.publish_to_inbox(role, message)

    # ------------------------------------------------------------------
    # Subscribing
    # ------------------------------------------------------------------

    async def subscribe_to_outbox(
        self,
        role: str,
        callback: MessageCallback,
    ) -> None:
        """Subscribe to ``<prefix>.<role>.outbox`` with a durable consumer.

        Args:
            role: Agent role name.
            callback: Async callable invoked for each received message.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        subject = self.outbox_subject(role)
        durable = f"{role}-{_OUTBOX_SUFFIX}"
        try:
            await self._js.subscribe(subject, cb=callback, durable=durable)
        except Exception:
            # Consumer may be stale from previous run -- delete and retry
            try:
                await self._js.delete_consumer(self._stream_name, durable)
            except Exception:
                pass
            await self._js.subscribe(subject, cb=callback, durable=durable)

    async def subscribe_all_outboxes(self, callback: MessageCallback) -> None:
        """Subscribe to every agent's outbox with a durable consumer.

        Args:
            callback: Async callable invoked for each received message.
        """
        for role in self._agents:
            await self.subscribe_to_outbox(role, callback)

    async def subscribe_to_inbox(
        self,
        role: str,
        callback: MessageCallback,
    ) -> None:
        """Subscribe to ``<prefix>.<role>.inbox`` with a durable consumer.

        Used by the orchestrator to watch for agent-to-agent messages
        and relay tmux nudges.
        """
        self._require_connected()
        subject = self.inbox_subject(role)
        durable = f"{role}-{_INBOX_SUFFIX}-relay"
        try:
            await self._js.subscribe(subject, cb=callback, durable=durable)
        except Exception:
            try:
                await self._js.delete_consumer(self._stream_name, durable)
            except Exception:
                pass
            await self._js.subscribe(subject, cb=callback, durable=durable)

    async def subscribe_all_inboxes(self, callback: MessageCallback) -> None:
        """Subscribe to every agent's inbox with a durable consumer."""
        for role in self._agents:
            await self.subscribe_to_inbox(role, callback)

    async def subscribe_ack(self, callback: MessageCallback) -> None:
        """Subscribe to delivery ACK subjects via core NATS.

        Listens on ``<prefix>.*.ack`` for delivery receipts published
        by MCP bridges when agents call ``check_messages``.  Uses core
        NATS (not JetStream) since ACKs are ephemeral.
        """
        self._require_connected()
        subject = f"{self._prefix}.*.ack"
        await self._conn.subscribe(subject, cb=callback)
        logger.info("Subscribed to delivery ACKs: %s", subject)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        """Raise :exc:`NatsClientError` if the JetStream context is absent."""
        if self._js is None:
            raise NatsClientError("Not connected. Call connect() first.")

    async def _ensure_stream(self) -> None:
        """Create or verify the JetStream stream (PRD R3).

        If the stream already exists (found by wildcard subject), this is a
        no-op.  Otherwise a new stream is created with the configured name,
        limits retention, and default capacity values.
        """
        try:
            await self._js.find_stream_name_by_subject(self.wildcard_subject())
        except Exception:
            # Stream does not exist -- create it
            await self._js.add_stream(
                config=StreamConfig(
                    name=self._stream_name,
                    subjects=[self.wildcard_subject()],
                    retention=RetentionPolicy.LIMITS,
                    max_msgs=DEFAULT_MAX_MSGS,
                    max_age=DEFAULT_MAX_AGE_SECONDS,
                ),
            )

    # ------------------------------------------------------------------
    # Reconnection callbacks
    # ------------------------------------------------------------------

    async def _on_reconnected(self) -> None:
        """Log successful reconnection to NATS."""
        logger.info("NATS reconnected")

    async def _on_disconnected(self) -> None:
        """Log NATS disconnection event."""
        logger.warning("NATS disconnected")

    async def _on_error(self, exc: Exception) -> None:
        """Log NATS transport errors."""
        logger.error("NATS error: %s", exc)
