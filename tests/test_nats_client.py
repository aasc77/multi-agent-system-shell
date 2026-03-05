"""
Tests for orchestrator/nats_client.py -- Async NATS Client Wrapper

TDD Contract (RED phase):
These tests define the expected behavior of the NATS Client Wrapper module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R3: Communication Flow (NATS details)
  - R5: Task Queue (all_done message)
  - Error Handling: NATS unavailability
  - Acceptance criteria from task rgr-4

Test categories:
  1. Connection -- connect to NATS server at configured URL
  2. Publishing -- publish JSON messages to agents.<role>.inbox
  3. Subscribing -- subscribe to agents.<role>.outbox with callback
  4. JetStream setup -- AGENTS stream, limits retention, max 10k msgs, max age 1hr
  5. Durable consumers -- persist across restarts
  6. Health check -- exit code 1 if NATS unreachable
  7. all_done message -- publish to all agent inboxes
  8. Reconnection -- graceful handling via nats-py auto-reconnect
  9. Subject conventions -- correct subject naming
  10. Error handling and edge cases

NOTE: These tests use unittest.mock to patch nats-py internals since
we cannot rely on a live NATS server in unit tests. The NatsClient
wrapper must be structured to allow dependency injection or patching.
"""

import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

# --- The import that MUST fail in RED phase ---
from orchestrator.nats_client import NatsClient, NatsClientError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "url": "nats://localhost:4222",
    "stream": "AGENTS",
    "subjects_prefix": "agents",
}

SAMPLE_AGENTS = {
    "writer": {"runtime": "claude_code", "working_dir": "/tmp/demo"},
    "executor": {"runtime": "script", "command": "echo"},
}


@pytest.fixture
def nats_config():
    """Return default NATS config dict."""
    return DEFAULT_CONFIG.copy()


@pytest.fixture
def agents():
    """Return sample agents dict."""
    return SAMPLE_AGENTS.copy()


@pytest.fixture
def mock_nats_connection():
    """Create a mock nats.Connection object."""
    conn = AsyncMock()
    conn.is_connected = True
    conn.close = AsyncMock()

    # Mock JetStream context
    js = AsyncMock()
    conn.jetstream.return_value = js

    # Mock stream info (stream already exists)
    js.find_stream_name_by_subject = AsyncMock(return_value="AGENTS")

    return conn, js


# ===========================================================================
# 1. CONNECTION -- Connect to NATS server
# ===========================================================================


class TestConnection:
    """NatsClient must connect to NATS server at the configured URL."""

    @pytest.mark.asyncio
    async def test_creates_instance_with_config(self, nats_config, agents):
        """NatsClient should accept a config dict and agents dict."""
        client = NatsClient(config=nats_config, agents=agents)
        assert client is not None

    @pytest.mark.asyncio
    async def test_connect_calls_nats_connect(self, nats_config, agents):
        """connect() must call nats.connect() with the configured URL."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()
            mock_connect.assert_called_once()
            # The URL should be passed to connect
            call_args = mock_connect.call_args
            assert "nats://localhost:4222" in str(call_args)

    @pytest.mark.asyncio
    async def test_connect_stores_connection(self, nats_config, agents):
        """After connect(), the client should have an active connection."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.is_connected = True
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()
            assert client.is_connected is True

    @pytest.mark.asyncio
    async def test_close_disconnects(self, nats_config, agents):
        """close() must disconnect from the NATS server."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()
            await client.close()
            mock_conn.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_with_custom_url(self, agents):
        """NatsClient should use the URL from config, not hardcoded."""
        config = {
            "url": "nats://custom-host:5222",
            "stream": "AGENTS",
            "subjects_prefix": "agents",
        }
        client = NatsClient(config=config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()
            call_args = mock_connect.call_args
            assert "nats://custom-host:5222" in str(call_args)


# ===========================================================================
# 2. PUBLISHING -- Publish JSON messages to agent inbox
# ===========================================================================


class TestPublishing:
    """NatsClient must publish JSON messages to agents.<role>.inbox subjects."""

    @pytest.mark.asyncio
    async def test_publish_to_agent_inbox(self, nats_config, agents):
        """publish_to_inbox must publish to agents.<role>.inbox subject."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            message = {"type": "task_assignment", "task_id": "task-1", "title": "Test"}
            await client.publish_to_inbox("writer", message)

            mock_js.publish.assert_called_once()
            call_args = mock_js.publish.call_args
            # Subject should be agents.writer.inbox
            assert call_args[0][0] == "agents.writer.inbox" or \
                   call_args[1].get("subject") == "agents.writer.inbox" or \
                   "agents.writer.inbox" in str(call_args)

    @pytest.mark.asyncio
    async def test_publish_sends_json_encoded_payload(self, nats_config, agents):
        """Published message must be JSON-encoded bytes."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            message = {"type": "task_assignment", "task_id": "task-1"}
            await client.publish_to_inbox("writer", message)

            call_args = mock_js.publish.call_args
            # The payload (2nd positional or 'payload' kwarg) should be JSON bytes
            if len(call_args[0]) > 1:
                payload = call_args[0][1]
            else:
                payload = call_args[1].get("payload", b"")
            parsed = json.loads(payload)
            assert parsed["type"] == "task_assignment"
            assert parsed["task_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_publish_to_different_agents(self, nats_config, agents):
        """Must be able to publish to different agent inboxes."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            await client.publish_to_inbox("writer", {"type": "task_assignment"})
            await client.publish_to_inbox("executor", {"type": "task_assignment"})

            assert mock_js.publish.call_count == 2

    @pytest.mark.asyncio
    async def test_publish_uses_configured_prefix(self, agents):
        """Subject prefix should come from config, not be hardcoded."""
        config = {
            "url": "nats://localhost:4222",
            "stream": "MYSTREAM",
            "subjects_prefix": "myprefix",
        }
        client = NatsClient(config=config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            await client.publish_to_inbox("writer", {"type": "test"})

            call_args = mock_js.publish.call_args
            assert "myprefix.writer.inbox" in str(call_args)


# ===========================================================================
# 3. SUBSCRIBING -- Subscribe to agent outbox with callback
# ===========================================================================


class TestSubscribing:
    """NatsClient must subscribe to agents.<role>.outbox with a callback."""

    @pytest.mark.asyncio
    async def test_subscribe_to_agent_outbox(self, nats_config, agents):
        """subscribe_to_outbox must subscribe to agents.<role>.outbox."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            callback = AsyncMock()
            await client.subscribe_to_outbox("writer", callback)

            mock_js.subscribe.assert_called()
            call_args = mock_js.subscribe.call_args
            assert "agents.writer.outbox" in str(call_args)

    @pytest.mark.asyncio
    async def test_subscribe_all_agents(self, nats_config, agents):
        """subscribe_all_outboxes must subscribe to all agents' outboxes."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            callback = AsyncMock()
            await client.subscribe_all_outboxes(callback)

            # Should subscribe for both writer and executor
            assert mock_js.subscribe.call_count >= 2

    @pytest.mark.asyncio
    async def test_subscribe_with_callback(self, nats_config, agents):
        """Callback must be associated with the subscription."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            callback = AsyncMock()
            await client.subscribe_to_outbox("writer", callback)

            # The subscribe call should include the callback
            call_args = mock_js.subscribe.call_args
            assert callback in call_args[0] or "cb" in str(call_args) or \
                   callback == call_args[1].get("cb") or len(call_args[0]) > 1

    @pytest.mark.asyncio
    async def test_subscribe_uses_configured_prefix(self, agents):
        """Subscription subjects should use the configured prefix."""
        config = {
            "url": "nats://localhost:4222",
            "stream": "MYSTREAM",
            "subjects_prefix": "myprefix",
        }
        client = NatsClient(config=config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            callback = AsyncMock()
            await client.subscribe_to_outbox("writer", callback)

            call_args = mock_js.subscribe.call_args
            assert "myprefix.writer.outbox" in str(call_args)


# ===========================================================================
# 4. JETSTREAM SETUP -- Stream creation and configuration
# ===========================================================================


class TestJetStreamSetup:
    """NatsClient must create/ensure a JetStream stream with correct config."""

    @pytest.mark.asyncio
    async def test_creates_jetstream_context(self, nats_config, agents):
        """connect() must create a JetStream context."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()
            mock_conn.jetstream.assert_called()

    @pytest.mark.asyncio
    async def test_creates_agents_stream(self, nats_config, agents):
        """Must create or update stream named 'AGENTS'."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            # Simulate stream not found, so it needs to be created
            mock_js.find_stream_name_by_subject = AsyncMock(side_effect=Exception("not found"))
            mock_connect.return_value = mock_conn
            await client.connect()

            # Should have called add_stream or update_stream
            assert mock_js.add_stream.called or mock_js.update_stream.called

    @pytest.mark.asyncio
    async def test_stream_name_is_agents(self, nats_config, agents):
        """Stream name must be 'AGENTS' (or configurable via config)."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_js.find_stream_name_by_subject = AsyncMock(side_effect=Exception("not found"))
            mock_connect.return_value = mock_conn
            await client.connect()

            if mock_js.add_stream.called:
                call_args = mock_js.add_stream.call_args
                # Check stream config has name "AGENTS"
                assert "AGENTS" in str(call_args)

    @pytest.mark.asyncio
    async def test_stream_covers_agents_wildcard_subject(self, nats_config, agents):
        """Stream must cover 'agents.>' subjects."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_js.find_stream_name_by_subject = AsyncMock(side_effect=Exception("not found"))
            mock_connect.return_value = mock_conn
            await client.connect()

            if mock_js.add_stream.called:
                call_args = mock_js.add_stream.call_args
                # Subjects should include agents.>
                assert "agents.>" in str(call_args)

    @pytest.mark.asyncio
    async def test_stream_limits_retention_policy(self, nats_config, agents):
        """Stream must use 'limits' retention policy."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_js.find_stream_name_by_subject = AsyncMock(side_effect=Exception("not found"))
            mock_connect.return_value = mock_conn
            await client.connect()

            if mock_js.add_stream.called:
                call_args = mock_js.add_stream.call_args
                config_arg = call_args[1].get("config") if call_args[1] else call_args[0][0] if call_args[0] else None
                # The retention should be LIMITS
                assert "limits" in str(call_args).lower() or "RetentionPolicy.LIMITS" in str(call_args)

    @pytest.mark.asyncio
    async def test_stream_max_messages(self, nats_config, agents):
        """Stream must have max_msgs=10000."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_js.find_stream_name_by_subject = AsyncMock(side_effect=Exception("not found"))
            mock_connect.return_value = mock_conn
            await client.connect()

            if mock_js.add_stream.called:
                call_args = mock_js.add_stream.call_args
                assert "10000" in str(call_args) or 10000 in str(call_args)

    @pytest.mark.asyncio
    async def test_stream_max_age_one_hour(self, nats_config, agents):
        """Stream must have max_age of 1 hour (3600 seconds)."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_js.find_stream_name_by_subject = AsyncMock(side_effect=Exception("not found"))
            mock_connect.return_value = mock_conn
            await client.connect()

            if mock_js.add_stream.called:
                call_args = mock_js.add_stream.call_args
                # max_age should be 3600 seconds (or equivalent)
                assert "3600" in str(call_args) or "max_age" in str(call_args)


# ===========================================================================
# 5. DURABLE CONSUMERS -- Persist across restarts
# ===========================================================================


class TestDurableConsumers:
    """NatsClient must create durable consumers so messages persist across restarts."""

    @pytest.mark.asyncio
    async def test_subscribe_uses_durable_name(self, nats_config, agents):
        """Subscriptions must use a durable consumer name."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            callback = AsyncMock()
            await client.subscribe_to_outbox("writer", callback)

            call_args = mock_js.subscribe.call_args
            # Should include durable= parameter
            assert "durable" in str(call_args)

    @pytest.mark.asyncio
    async def test_durable_name_is_agent_specific(self, nats_config, agents):
        """Each agent's durable consumer must have a unique name."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            callback = AsyncMock()
            await client.subscribe_to_outbox("writer", callback)
            await client.subscribe_to_outbox("executor", callback)

            # The two subscribe calls should have different durable names
            calls = mock_js.subscribe.call_args_list
            assert len(calls) == 2
            durable_names = []
            for c in calls:
                durable_name = c[1].get("durable", "") if c[1] else ""
                durable_names.append(str(durable_name))
            # They should be different (agent-specific)
            assert durable_names[0] != durable_names[1]


# ===========================================================================
# 6. HEALTH CHECK -- Exit code 1 if NATS unreachable
# ===========================================================================


class TestHealthCheck:
    """Startup health check must exit with code 1 if NATS is unreachable."""

    @pytest.mark.asyncio
    async def test_health_check_fails_when_nats_unreachable(self, nats_config, agents):
        """connect() must raise NatsClientError when NATS server is unreachable."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.side_effect = Exception("Could not connect to NATS")
            with pytest.raises(NatsClientError) as exc_info:
                await client.connect()
            error_msg = str(exc_info.value)
            assert "setup-nats" in error_msg.lower() or "unreachable" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_health_check_error_includes_setup_instruction(self, nats_config, agents):
        """Error message must include 'Run: scripts/setup-nats.sh'."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.side_effect = Exception("Connection refused")
            with pytest.raises(NatsClientError) as exc_info:
                await client.connect()
            error_msg = str(exc_info.value)
            assert "scripts/setup-nats.sh" in error_msg

    @pytest.mark.asyncio
    async def test_health_check_succeeds_when_connected(self, nats_config, agents):
        """connect() should not raise when NATS is reachable."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.is_connected = True
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            # Should not raise
            await client.connect()
            assert client.is_connected is True

    @pytest.mark.asyncio
    async def test_nats_client_error_is_exception(self):
        """NatsClientError must be a subclass of Exception."""
        assert issubclass(NatsClientError, Exception)

    @pytest.mark.asyncio
    async def test_nats_client_error_has_message(self):
        """NatsClientError should accept and store a message."""
        err = NatsClientError("test nats error")
        assert "test nats error" in str(err)


# ===========================================================================
# 7. ALL_DONE MESSAGE -- Publish to all agent inboxes
# ===========================================================================


class TestAllDoneMessage:
    """NatsClient must publish all_done message to all agent inboxes."""

    @pytest.mark.asyncio
    async def test_publish_all_done_sends_to_all_agents(self, nats_config, agents):
        """publish_all_done must publish to every agent's inbox."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            summary = "All tasks processed: 3 completed, 0 stuck"
            await client.publish_all_done(summary)

            # Should publish to both agents' inboxes
            assert mock_js.publish.call_count == 2

    @pytest.mark.asyncio
    async def test_publish_all_done_correct_subjects(self, nats_config, agents):
        """all_done messages must go to agents.<role>.inbox for each agent."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            await client.publish_all_done("Done")

            subjects = set()
            for c in mock_js.publish.call_args_list:
                subject = c[0][0] if c[0] else c[1].get("subject", "")
                subjects.add(subject)

            assert "agents.writer.inbox" in subjects
            assert "agents.executor.inbox" in subjects

    @pytest.mark.asyncio
    async def test_publish_all_done_message_schema(self, nats_config, agents):
        """all_done message must follow the schema: type=all_done, summary=<text>."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            summary_text = "All tasks processed: 2 completed, 1 stuck"
            await client.publish_all_done(summary_text)

            # Check the payload of at least one published message
            call_args = mock_js.publish.call_args_list[0]
            if len(call_args[0]) > 1:
                payload = call_args[0][1]
            else:
                payload = call_args[1].get("payload", b"")
            parsed = json.loads(payload)
            assert parsed["type"] == "all_done"
            assert parsed["summary"] == summary_text

    @pytest.mark.asyncio
    async def test_publish_all_done_with_three_agents(self):
        """all_done must be sent to all agents, even with more than 2."""
        config = DEFAULT_CONFIG.copy()
        three_agents = {
            "writer": {"runtime": "claude_code"},
            "executor": {"runtime": "script", "command": "echo"},
            "reviewer": {"runtime": "claude_code"},
        }
        client = NatsClient(config=config, agents=three_agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            await client.publish_all_done("Done")

            assert mock_js.publish.call_count == 3


# ===========================================================================
# 8. RECONNECTION -- Graceful handling
# ===========================================================================


class TestReconnection:
    """NatsClient must handle reconnection gracefully via nats-py auto-reconnect."""

    @pytest.mark.asyncio
    async def test_connect_sets_reconnect_callbacks(self, nats_config, agents):
        """connect() should configure reconnection callbacks."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()

            call_args = mock_connect.call_args
            # nats.connect should be called with reconnect-related params
            # (reconnected_cb, disconnected_cb, or max_reconnect_attempts)
            call_str = str(call_args)
            has_reconnect_config = (
                "reconnected_cb" in call_str or
                "disconnected_cb" in call_str or
                "max_reconnect_attempts" in call_str or
                "error_cb" in call_str
            )
            assert has_reconnect_config, \
                f"connect() should configure reconnect callbacks. Got: {call_str}"

    @pytest.mark.asyncio
    async def test_is_connected_property(self, nats_config, agents):
        """is_connected property should reflect NATS connection state."""
        client = NatsClient(config=nats_config, agents=agents)
        # Before connect
        assert client.is_connected is False

        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.is_connected = True
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()
            assert client.is_connected is True


# ===========================================================================
# 9. SUBJECT CONVENTIONS -- Correct naming
# ===========================================================================


class TestSubjectConventions:
    """NATS subjects must follow the convention: <prefix>.<role>.inbox/outbox."""

    @pytest.mark.asyncio
    async def test_inbox_subject_format(self, nats_config, agents):
        """Inbox subject must be '<prefix>.<role>.inbox'."""
        client = NatsClient(config=nats_config, agents=agents)
        # Test the subject building method/property
        subject = client.inbox_subject("writer")
        assert subject == "agents.writer.inbox"

    @pytest.mark.asyncio
    async def test_outbox_subject_format(self, nats_config, agents):
        """Outbox subject must be '<prefix>.<role>.outbox'."""
        client = NatsClient(config=nats_config, agents=agents)
        subject = client.outbox_subject("writer")
        assert subject == "agents.writer.outbox"

    @pytest.mark.asyncio
    async def test_inbox_subject_with_custom_prefix(self, agents):
        """Inbox subject must use the configured prefix."""
        config = {
            "url": "nats://localhost:4222",
            "stream": "CUSTOM",
            "subjects_prefix": "custom",
        }
        client = NatsClient(config=config, agents=agents)
        assert client.inbox_subject("writer") == "custom.writer.inbox"

    @pytest.mark.asyncio
    async def test_outbox_subject_with_custom_prefix(self, agents):
        """Outbox subject must use the configured prefix."""
        config = {
            "url": "nats://localhost:4222",
            "stream": "CUSTOM",
            "subjects_prefix": "custom",
        }
        client = NatsClient(config=config, agents=agents)
        assert client.outbox_subject("executor") == "custom.executor.outbox"

    @pytest.mark.asyncio
    async def test_wildcard_subject_for_stream(self, nats_config, agents):
        """Stream wildcard subject must be '<prefix>.>'."""
        client = NatsClient(config=nats_config, agents=agents)
        # The stream should cover agents.>
        wild = client.wildcard_subject()
        assert wild == "agents.>"


# ===========================================================================
# 10. ERROR HANDLING AND EDGE CASES
# ===========================================================================


class TestErrorHandling:
    """Error handling and edge cases for the NATS client."""

    @pytest.mark.asyncio
    async def test_publish_before_connect_raises_error(self, nats_config, agents):
        """Publishing before connect() must raise an error."""
        client = NatsClient(config=nats_config, agents=agents)
        with pytest.raises((NatsClientError, RuntimeError)):
            await client.publish_to_inbox("writer", {"type": "test"})

    @pytest.mark.asyncio
    async def test_subscribe_before_connect_raises_error(self, nats_config, agents):
        """Subscribing before connect() must raise an error."""
        client = NatsClient(config=nats_config, agents=agents)
        callback = AsyncMock()
        with pytest.raises((NatsClientError, RuntimeError)):
            await client.subscribe_to_outbox("writer", callback)

    @pytest.mark.asyncio
    async def test_publish_all_done_before_connect_raises_error(self, nats_config, agents):
        """publish_all_done before connect() must raise an error."""
        client = NatsClient(config=nats_config, agents=agents)
        with pytest.raises((NatsClientError, RuntimeError)):
            await client.publish_all_done("Done")

    @pytest.mark.asyncio
    async def test_close_without_connect_is_safe(self, nats_config, agents):
        """Calling close() without connect() should not raise."""
        client = NatsClient(config=nats_config, agents=agents)
        # Should not raise
        await client.close()

    @pytest.mark.asyncio
    async def test_double_connect_is_safe(self, nats_config, agents):
        """Calling connect() twice should not raise or create duplicate connections."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_conn.is_connected = True
            mock_conn.jetstream.return_value = AsyncMock()
            mock_connect.return_value = mock_conn
            await client.connect()
            await client.connect()  # Second call should be safe

    @pytest.mark.asyncio
    async def test_publish_with_empty_message(self, nats_config, agents):
        """Publishing an empty dict should still work (valid JSON)."""
        client = NatsClient(config=nats_config, agents=agents)
        with patch("orchestrator.nats_client.nats.connect", new_callable=AsyncMock) as mock_connect:
            mock_conn = AsyncMock()
            mock_js = AsyncMock()
            mock_conn.jetstream.return_value = mock_js
            mock_connect.return_value = mock_conn
            await client.connect()

            await client.publish_to_inbox("writer", {})
            mock_js.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_constructor_requires_config_and_agents(self):
        """NatsClient constructor must require config and agents parameters."""
        with pytest.raises(TypeError):
            NatsClient()  # Missing required args

    @pytest.mark.asyncio
    async def test_missing_url_in_config_raises_error(self, agents):
        """Config without 'url' key should raise an error."""
        bad_config = {"stream": "AGENTS", "subjects_prefix": "agents"}
        with pytest.raises((NatsClientError, KeyError)):
            client = NatsClient(config=bad_config, agents=agents)
            # Or it may fail on connect
            await client.connect()
