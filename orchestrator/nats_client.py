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
import time
import uuid
from datetime import datetime, timezone
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

        The *message* is envelope-wrapped before publish — ``message_id``,
        ``timestamp``, and ``from`` are filled in with sensible defaults
        if the caller has not set them. The envelope enables the
        mas-bridge ``handleCheckMessages`` dedup path to correctly
        collapse the push-subscription buffer copy against the durable
        JetStream pull copy; without an envelope the bridge fails open
        and delivers every orchestrator-generated message twice to
        the target agent.

        The caller's *message* dict is NOT mutated — a shallow copy is
        made before envelope fields are added, so callers that reuse
        the same dict across multiple publish calls (e.g.
        ``publish_all_done``) get a fresh envelope on every publish
        without accidentally sharing ``message_id`` across recipients.

        Callers that set any of ``message_id``, ``timestamp``, or
        ``from`` themselves retain their values — the library never
        clobbers caller intent.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        subject = self.inbox_subject(role)
        enveloped = self._envelope_wrap(message)
        payload = json.dumps(enveloped).encode()
        await self._js.publish(subject, payload)

    def _envelope_wrap(self, message: dict[str, Any]) -> dict[str, Any]:
        """Return a shallow copy of *message* with envelope fields filled in.

        Fields added if missing: ``message_id``, ``timestamp``, ``from``.
        Caller-set values are preserved. The original dict is never
        mutated so callers can safely reuse it across publish calls.

        Shallow-copy caveat: the envelope fields are all top-level scalars
        (strings), so a shallow ``dict(message)`` is sufficient to keep
        the caller's dict isolated from our mutations. Do NOT mutate
        nested containers on *enveloped* — any nested dict/list is still
        shared with the caller via reference, and in-place edits there
        would propagate back to the caller and silently break fanout.
        """
        enveloped = dict(message)
        if not enveloped.get("message_id"):
            enveloped["message_id"] = self._generate_message_id(enveloped)
        if not enveloped.get("timestamp"):
            enveloped["timestamp"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            )
        if not enveloped.get("from"):
            enveloped["from"] = "orchestrator"
        return enveloped

    @staticmethod
    def _generate_message_id(message: dict[str, Any]) -> str:
        """Generate a unique ``message_id`` for *message*.

        Format: ``<type>-<epoch>-<short-uuid>``. Uses the message's
        ``type`` field (falling back to ``orchestrator``) as the
        prefix so each publisher namespace stays distinct in traces
        and the mas-bridge dedup cache has a wider hash space across
        concurrent senders.
        """
        raw_type = str(message.get("type") or "orchestrator")
        msg_type = raw_type.replace(" ", "_")
        epoch = int(time.time())
        short_uuid = uuid.uuid4().hex[:8]
        return f"{msg_type}-{epoch}-{short_uuid}"

    async def publish_raw(self, subject: str, payload: bytes) -> None:
        """Publish raw bytes to an arbitrary NATS subject.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        await self._js.publish(subject, payload)

    async def subscribe_core(
        self,
        subject: str,
        callback: MessageCallback,
    ) -> Any:
        """Register a core-NATS push subscription on *subject*.

        Use for core-NATS request/reply or ephemeral-signal
        subscriptions on subjects OUTSIDE the JetStream ``agents.>``
        wildcard. For durable agent-addressed messages use
        :meth:`subscribe_to_inbox` / :meth:`subscribe_to_outbox`.

        Unlike the JetStream-backed subscribe methods, this is a
        plain push subscription with no durable consumer, no ack
        tracking, and no redelivery on disconnect. Messages missed
        while the subscriber is down are lost by design — that is
        exactly the semantic you want for request/reply handlers
        (the request is scoped to a single call and replies go back
        over the inbox-subject the request was made on) and for
        loss-tolerant diagnostic signals.

        First caller: issue #31 orchestrator version probe, which
        registers a handler on ``system.orchestrator.version`` that
        responds via ``msg.respond(bytes)`` on the incoming message.

        Returns:
            The underlying ``nats.aio.subscription.Subscription``
            object, useful for ``.unsubscribe()`` on shutdown.

        Raises:
            NatsClientError: If not connected.
        """
        self._require_connected()
        return await self._conn.subscribe(subject, cb=callback)

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
