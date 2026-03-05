"""
Tests for orchestrator/router.py -- Message Router

TDD Contract (RED phase):
These tests define the expected behavior of the Message Router module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R3: Communication Flow (outbox message schema, unrecognized messages, field mapping)
  - R4: Config-Driven State Machine (trigger dispatch, transition matching)
  - R5: Task Queue (attempt counter must NOT increment for unrecognized/no-match)

Acceptance criteria from task rgr-8:
  1. Subscribes to agents.<role>.outbox for all configured agents
  2. Parses incoming JSON messages from outbox subjects
  3. Maps message fields to state machine lookup: type->trigger, role->source_agent, status->status
  4. Dispatches valid messages to state machine and triggers transitions
  5. Unrecognized type: logs 'Unrecognized outbox message from <role>: <payload>', ACKs, no state change
  6. Missing required fields (type or status): treated as unrecognized, same handling
  7. Valid type but no matching transition: logs 'no matching transition', ACKs, no state change
  8. Neither unrecognized nor no-matching-transition increments the task attempt counter
  9. Passes transition result (action, action_args) to lifecycle manager for execution

Test categories:
  1. Subscription setup -- subscribes to all agent outbox subjects
  2. Message parsing -- JSON deserialization from NATS messages
  3. Field mapping -- type->trigger, role->source_agent, status->status
  4. Valid message dispatch -- triggers state machine transitions
  5. Unrecognized type handling -- unknown type, log + ACK + no state change
  6. Missing required fields -- treated as unrecognized
  7. No matching transition -- valid type but wrong state/agent, distinct log
  8. Attempt counter protection -- neither case increments attempts
  9. Lifecycle manager handoff -- passes action/action_args to lifecycle manager
  10. Edge cases and error handling
"""

import json
import logging
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call, PropertyMock

# --- The import that MUST fail in RED phase ---
from orchestrator.router import MessageRouter, RouterError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_AGENTS = {
    "writer": {"runtime": "claude_code", "working_dir": "/tmp/demo"},
    "executor": {"runtime": "script", "command": "python3 echo_agent.py"},
}

THREE_AGENTS = {
    "qa": {"runtime": "claude_code", "working_dir": "/tmp/qa"},
    "dev": {"runtime": "claude_code", "working_dir": "/tmp/dev"},
    "refactor": {"runtime": "claude_code", "working_dir": "/tmp/refactor"},
}

# Transitions that the router should know about for validation
SAMPLE_TRANSITIONS = [
    {
        "from": "idle",
        "to": "waiting_writer",
        "trigger": "task_assigned",
        "action": "assign_to_agent",
        "action_args": {"target_agent": "writer"},
    },
    {
        "from": "waiting_writer",
        "to": "waiting_executor",
        "trigger": "agent_complete",
        "source_agent": "writer",
        "status": "pass",
        "action": "assign_to_agent",
        "action_args": {"target_agent": "executor"},
    },
    {
        "from": "waiting_writer",
        "to": "idle",
        "trigger": "agent_complete",
        "source_agent": "writer",
        "status": "fail",
        "action": "flag_human",
    },
    {
        "from": "waiting_executor",
        "to": "idle",
        "trigger": "agent_complete",
        "source_agent": "executor",
        "status": "pass",
    },
    {
        "from": "waiting_executor",
        "to": "waiting_writer",
        "trigger": "agent_complete",
        "source_agent": "executor",
        "status": "fail",
        "action": "assign_to_agent",
        "action_args": {
            "target_agent": "writer",
            "message": "Tests failed. Fix and re-send.",
        },
    },
]


def _make_nats_msg(payload: dict, subject: str = "agents.writer.outbox") -> MagicMock:
    """Create a mock NATS message with JSON data and ack method."""
    msg = MagicMock()
    msg.data = json.dumps(payload).encode("utf-8")
    msg.subject = subject
    msg.ack = AsyncMock()
    return msg


def _make_nats_msg_raw(raw_bytes: bytes, subject: str = "agents.writer.outbox") -> MagicMock:
    """Create a mock NATS message with raw bytes (possibly invalid JSON)."""
    msg = MagicMock()
    msg.data = raw_bytes
    msg.subject = subject
    msg.ack = AsyncMock()
    return msg


@pytest.fixture
def mock_nats_client():
    """Create a mock NATS client."""
    client = AsyncMock()
    client.subscribe_to_outbox = AsyncMock()
    client.subscribe_all_outboxes = AsyncMock()
    return client


@pytest.fixture
def mock_state_machine():
    """Create a mock state machine."""
    sm = MagicMock()
    sm.current_state = "waiting_writer"
    sm.transitions = SAMPLE_TRANSITIONS

    # Default: handle_trigger returns a successful transition result
    result = MagicMock()
    result.action = "assign_to_agent"
    result.action_args = {"target_agent": "executor"}
    result.from_state = "waiting_writer"
    result.to_state = "waiting_executor"
    result.trigger = "agent_complete"
    sm.handle_trigger = MagicMock(return_value=result)
    return sm


@pytest.fixture
def mock_lifecycle_manager():
    """Create a mock lifecycle manager."""
    lm = AsyncMock()
    lm.execute_action = AsyncMock()
    lm.increment_attempts = MagicMock()
    return lm


@pytest.fixture
def router(mock_nats_client, mock_state_machine, mock_lifecycle_manager):
    """Create a MessageRouter with mocked dependencies."""
    return MessageRouter(
        nats_client=mock_nats_client,
        state_machine=mock_state_machine,
        lifecycle_manager=mock_lifecycle_manager,
        agents=SAMPLE_AGENTS,
    )


@pytest.fixture
def router_three(mock_nats_client, mock_state_machine, mock_lifecycle_manager):
    """Create a MessageRouter with 3 agents."""
    return MessageRouter(
        nats_client=mock_nats_client,
        state_machine=mock_state_machine,
        lifecycle_manager=mock_lifecycle_manager,
        agents=THREE_AGENTS,
    )


# ===========================================================================
# 1. SUBSCRIPTION SETUP -- Subscribe to all agent outbox subjects
# ===========================================================================


class TestSubscriptionSetup:
    """MessageRouter must subscribe to agents.<role>.outbox for all configured agents."""

    @pytest.mark.asyncio
    async def test_subscribes_to_all_agent_outboxes(self, router, mock_nats_client):
        """start() must subscribe to outbox for every configured agent."""
        await router.start()

        # Should have set up subscriptions for both writer and executor
        # Either via subscribe_all_outboxes or individual subscribe_to_outbox calls
        if mock_nats_client.subscribe_all_outboxes.called:
            mock_nats_client.subscribe_all_outboxes.assert_called_once()
        else:
            assert mock_nats_client.subscribe_to_outbox.call_count >= 2
            subjects = [str(c) for c in mock_nats_client.subscribe_to_outbox.call_args_list]
            assert any("writer" in s for s in subjects)
            assert any("executor" in s for s in subjects)

    @pytest.mark.asyncio
    async def test_subscribes_for_three_agents(self, router_three, mock_nats_client):
        """With 3 agents, must subscribe to all 3 outbox subjects."""
        await router_three.start()

        if mock_nats_client.subscribe_all_outboxes.called:
            mock_nats_client.subscribe_all_outboxes.assert_called_once()
        else:
            assert mock_nats_client.subscribe_to_outbox.call_count >= 3

    @pytest.mark.asyncio
    async def test_subscription_registers_message_handler(self, router, mock_nats_client):
        """Subscription must register a callback/handler for incoming messages."""
        await router.start()

        # The subscription must have a handler function
        if mock_nats_client.subscribe_all_outboxes.called:
            call_args = mock_nats_client.subscribe_all_outboxes.call_args
            # A callback function should be passed
            assert call_args is not None
            assert len(call_args[0]) > 0 or len(call_args[1]) > 0
        else:
            for c in mock_nats_client.subscribe_to_outbox.call_args_list:
                assert c is not None


# ===========================================================================
# 2. MESSAGE PARSING -- JSON deserialization from NATS messages
# ===========================================================================


class TestMessageParsing:
    """MessageRouter must parse incoming JSON messages from outbox subjects."""

    @pytest.mark.asyncio
    async def test_parses_valid_json_message(self, router, mock_state_machine):
        """Must correctly parse a valid JSON outbox message."""
        payload = {
            "type": "agent_complete",
            "status": "pass",
            "summary": "All tests passed",
        }
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        # State machine should be called (message was parsed successfully)
        mock_state_machine.handle_trigger.assert_called()

    @pytest.mark.asyncio
    async def test_handles_invalid_json_gracefully(self, router, mock_state_machine, caplog):
        """Invalid JSON must be handled gracefully -- log, ACK, no crash."""
        msg = _make_nats_msg_raw(b"this is not json", subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        # Message should be ACKed (don't let it re-deliver)
        msg.ack.assert_called_once()
        # State machine should NOT be called
        mock_state_machine.handle_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_role_from_subject(self, router, mock_state_machine):
        """Must extract the agent role from the NATS subject (agents.<role>.outbox)."""
        payload = {
            "type": "agent_complete",
            "status": "pass",
        }
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        await router.handle_message(msg)

        # The handle_trigger call should include source_agent="executor"
        call_args = mock_state_machine.handle_trigger.call_args
        assert call_args is not None
        # Check source_agent was extracted correctly
        if call_args[1]:  # kwargs
            assert call_args[1].get("source_agent") == "executor"
        else:
            assert "executor" in str(call_args)


# ===========================================================================
# 3. FIELD MAPPING -- type->trigger, role->source_agent, status->status
# ===========================================================================


class TestFieldMapping:
    """MessageRouter must map outbox fields to state machine lookup parameters."""

    @pytest.mark.asyncio
    async def test_maps_type_to_trigger(self, router, mock_state_machine):
        """Message 'type' field must map to state machine 'trigger' parameter."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_state_machine.handle_trigger.call_args
        # trigger should be "agent_complete"
        if call_args[1]:  # kwargs
            assert call_args[1].get("trigger") == "agent_complete"
        elif call_args[0]:
            assert call_args[0][0] == "agent_complete"

    @pytest.mark.asyncio
    async def test_maps_role_to_source_agent(self, router, mock_state_machine):
        """Agent role (from subject) must map to state machine 'source_agent' parameter."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_state_machine.handle_trigger.call_args
        if call_args[1]:  # kwargs
            assert call_args[1].get("source_agent") == "writer"
        else:
            assert "writer" in str(call_args)

    @pytest.mark.asyncio
    async def test_maps_status_to_status(self, router, mock_state_machine):
        """Message 'status' field must map to state machine 'status' parameter."""
        payload = {"type": "agent_complete", "status": "fail"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_state_machine.handle_trigger.call_args
        if call_args[1]:  # kwargs
            assert call_args[1].get("status") == "fail"
        else:
            assert "fail" in str(call_args)

    @pytest.mark.asyncio
    async def test_maps_all_three_fields_together(self, router, mock_state_machine):
        """All three mappings must be applied simultaneously."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        await router.handle_message(msg)

        mock_state_machine.handle_trigger.assert_called_once_with(
            trigger="agent_complete",
            source_agent="executor",
            status="pass",
        )

    @pytest.mark.asyncio
    async def test_extra_fields_ignored(self, router, mock_state_machine):
        """Extra fields (summary, files_changed, error) must not affect dispatch."""
        payload = {
            "type": "agent_complete",
            "status": "pass",
            "summary": "All good",
            "files_changed": ["src/foo.py"],
            "error": None,
        }
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        # State machine should be called with only the mapped fields
        mock_state_machine.handle_trigger.assert_called_once_with(
            trigger="agent_complete",
            source_agent="writer",
            status="pass",
        )


# ===========================================================================
# 4. VALID MESSAGE DISPATCH -- Triggers state machine transitions
# ===========================================================================


class TestValidMessageDispatch:
    """MessageRouter must dispatch valid messages to the state machine."""

    @pytest.mark.asyncio
    async def test_dispatches_valid_message_to_state_machine(
        self, router, mock_state_machine
    ):
        """Valid outbox message must be passed to state machine's handle_trigger."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_state_machine.handle_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_acks_valid_message_after_dispatch(
        self, router, mock_state_machine
    ):
        """Valid message must be ACKed after successful dispatch."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_with_pass_status(self, router, mock_state_machine):
        """Dispatch with status=pass must correctly trigger state machine."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_state_machine.handle_trigger.call_args
        assert "pass" in str(call_args)

    @pytest.mark.asyncio
    async def test_dispatch_with_fail_status(self, router, mock_state_machine):
        """Dispatch with status=fail must correctly trigger state machine."""
        payload = {"type": "agent_complete", "status": "fail"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_state_machine.handle_trigger.call_args
        assert "fail" in str(call_args)


# ===========================================================================
# 5. UNRECOGNIZED TYPE HANDLING
# ===========================================================================


class TestUnrecognizedType:
    """Unrecognized type: log warning, ACK, no state change, no attempt increment."""

    @pytest.mark.asyncio
    async def test_unrecognized_type_logs_warning(
        self, router, mock_state_machine, caplog
    ):
        """Unknown type must log 'Unrecognized outbox message from <role>: <payload>'."""
        payload = {"type": "random_garbage_type", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        # State machine returns None for unrecognized triggers
        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() and "writer" in msg
            for msg in warning_messages
        ), f"Expected 'Unrecognized outbox message from writer' warning, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_unrecognized_type_includes_payload_in_log(
        self, router, mock_state_machine, caplog
    ):
        """Log message must include the full payload for debugging."""
        payload = {"type": "totally_unknown", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        # Payload should appear in the log message
        assert any(
            "totally_unknown" in msg for msg in warning_messages
        ), f"Expected payload in log, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_unrecognized_type_acks_message(
        self, router, mock_state_machine
    ):
        """Unrecognized type must ACK the message (prevent re-delivery)."""
        payload = {"type": "unknown_action", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_unrecognized_type_no_state_change(
        self, router, mock_state_machine
    ):
        """Unrecognized type must NOT change the state machine's state."""
        original_state = mock_state_machine.current_state
        payload = {"type": "bogus_type", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        # Make the router detect this as unrecognized (type not in any transition trigger)
        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        # State should not have changed
        assert mock_state_machine.current_state == original_state

    @pytest.mark.asyncio
    async def test_unrecognized_type_no_attempt_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Unrecognized type must NOT increment the task attempt counter."""
        payload = {"type": "unknown_stuff", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        # Lifecycle manager's increment method should NOT be called
        mock_lifecycle_manager.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_type_not_matching_any_transitions_trigger(
        self, router, mock_state_machine, caplog
    ):
        """A type value that doesn't match ANY transition's trigger field = unrecognized."""
        payload = {"type": "deploy_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        ), f"Expected 'unrecognized' in warning, got: {warning_messages}"


# ===========================================================================
# 6. MISSING REQUIRED FIELDS -- Treated as unrecognized
# ===========================================================================


class TestMissingRequiredFields:
    """Missing required fields (type or status) must be treated as unrecognized."""

    @pytest.mark.asyncio
    async def test_missing_type_field_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Message missing 'type' must be treated as unrecognized."""
        payload = {"status": "pass", "summary": "Did something"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() and "writer" in msg
            for msg in warning_messages
        ), f"Expected unrecognized warning for missing type, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_missing_status_field_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Message missing 'status' must be treated as unrecognized."""
        payload = {"type": "agent_complete", "summary": "Did stuff"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() and "writer" in msg
            for msg in warning_messages
        ), f"Expected unrecognized warning for missing status, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_missing_both_fields_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Message missing both 'type' and 'status' must be treated as unrecognized."""
        payload = {"summary": "Did something", "extra": "data"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        ), f"Expected unrecognized warning, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_missing_type_acks_message(self, router, mock_state_machine):
        """Message with missing type must still be ACKed."""
        payload = {"status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_status_acks_message(self, router, mock_state_machine):
        """Message with missing status must still be ACKed."""
        payload = {"type": "agent_complete"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_type_no_state_machine_call(
        self, router, mock_state_machine
    ):
        """Message missing 'type' must NOT be dispatched to state machine."""
        payload = {"status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_state_machine.handle_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_status_no_state_machine_call(
        self, router, mock_state_machine
    ):
        """Message missing 'status' must NOT be dispatched to state machine."""
        payload = {"type": "agent_complete"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_state_machine.handle_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_fields_no_attempt_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Missing required fields must NOT increment the task attempt counter."""
        payload = {"summary": "just a summary"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_type_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Empty string for type must be treated as unrecognized."""
        payload = {"type": "", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        ), f"Expected unrecognized warning for empty type, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_empty_status_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Empty string for status must be treated as unrecognized."""
        payload = {"type": "agent_complete", "status": ""}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        ), f"Expected unrecognized warning for empty status, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_null_type_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Null/None type must be treated as unrecognized."""
        payload = {"type": None, "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        ), f"Expected unrecognized warning for null type, got: {warning_messages}"


# ===========================================================================
# 7. NO MATCHING TRANSITION -- Valid type but no match for current state
# ===========================================================================


class TestNoMatchingTransition:
    """Valid type but no matching transition: distinct log message, ACK, no state change."""

    @pytest.mark.asyncio
    async def test_no_matching_transition_logs_distinct_message(
        self, router, mock_state_machine, caplog
    ):
        """Valid type with no matching transition must log 'no matching transition'."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        # State machine returns None because no transition matches current state
        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "no matching transition" in msg.lower() for msg in warning_messages
        ), f"Expected 'no matching transition' warning, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_no_matching_transition_distinct_from_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Log for no-matching-transition must be different from 'unrecognized'."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        # agent_complete IS a valid trigger type, but state machine returns None
        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        # Should say "no matching transition" NOT "unrecognized"
        matching_msgs = [
            m for m in warning_messages if "no matching transition" in m.lower()
        ]
        unrecognized_msgs = [
            m for m in warning_messages if "unrecognized" in m.lower()
        ]
        assert len(matching_msgs) > 0, (
            f"Expected 'no matching transition' message, got: {warning_messages}"
        )
        assert len(unrecognized_msgs) == 0, (
            f"Should NOT log 'unrecognized' for valid type, got: {warning_messages}"
        )

    @pytest.mark.asyncio
    async def test_no_matching_transition_acks_message(
        self, router, mock_state_machine
    ):
        """No matching transition must ACK the message."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_matching_transition_no_state_change(
        self, router, mock_state_machine
    ):
        """No matching transition must NOT change the state machine's state."""
        original_state = mock_state_machine.current_state
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        assert mock_state_machine.current_state == original_state

    @pytest.mark.asyncio
    async def test_no_matching_transition_no_attempt_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """No matching transition must NOT increment the task attempt counter."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        mock_lifecycle_manager.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_type_wrong_source_agent(
        self, router, mock_state_machine, caplog
    ):
        """agent_complete from wrong source_agent = no matching transition."""
        payload = {"type": "agent_complete", "status": "pass"}
        # executor sends while state machine expects writer
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "no matching transition" in msg.lower() for msg in warning_messages
        ), f"Expected 'no matching transition', got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_valid_type_wrong_status(
        self, router, mock_state_machine, caplog
    ):
        """agent_complete with an unexpected status = no matching transition."""
        payload = {"type": "agent_complete", "status": "partial"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "no matching transition" in msg.lower() for msg in warning_messages
        ), f"Expected 'no matching transition', got: {warning_messages}"


# ===========================================================================
# 8. ATTEMPT COUNTER PROTECTION
# ===========================================================================


class TestAttemptCounterProtection:
    """Neither unrecognized nor no-matching-transition increments the attempt counter."""

    @pytest.mark.asyncio
    async def test_unrecognized_does_not_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Unrecognized type must NOT increment attempts."""
        payload = {"type": "random_noise", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        mock_lifecycle_manager.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_match_does_not_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """No matching transition must NOT increment attempts."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        mock_lifecycle_manager.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_fields_does_not_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Missing required fields must NOT increment attempts."""
        payload = {"summary": "orphan message"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_does_not_increment(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Invalid JSON must NOT increment attempts."""
        msg = _make_nats_msg_raw(b"not json at all", subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.increment_attempts.assert_not_called()


# ===========================================================================
# 9. LIFECYCLE MANAGER HANDOFF -- Pass action/action_args
# ===========================================================================


class TestLifecycleManagerHandoff:
    """Router must pass transition result (action, action_args) to lifecycle manager."""

    @pytest.mark.asyncio
    async def test_passes_action_to_lifecycle_manager(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """When transition succeeds, action must be passed to lifecycle manager."""
        result = MagicMock()
        result.action = "assign_to_agent"
        result.action_args = {"target_agent": "executor"}
        result.from_state = "waiting_writer"
        result.to_state = "waiting_executor"
        mock_state_machine.handle_trigger.return_value = result

        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.execute_action.assert_called_once()
        call_args = mock_lifecycle_manager.execute_action.call_args
        # Should pass the action name
        assert "assign_to_agent" in str(call_args)

    @pytest.mark.asyncio
    async def test_passes_action_args_to_lifecycle_manager(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """When transition succeeds, action_args must be passed to lifecycle manager."""
        result = MagicMock()
        result.action = "assign_to_agent"
        result.action_args = {"target_agent": "executor"}
        result.from_state = "waiting_writer"
        result.to_state = "waiting_executor"
        mock_state_machine.handle_trigger.return_value = result

        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_lifecycle_manager.execute_action.call_args
        assert "executor" in str(call_args)

    @pytest.mark.asyncio
    async def test_passes_full_transition_result(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Lifecycle manager should receive the full transition result."""
        result = MagicMock()
        result.action = "assign_to_agent"
        result.action_args = {
            "target_agent": "writer",
            "message": "Tests failed. Fix and re-send.",
        }
        result.from_state = "waiting_executor"
        result.to_state = "waiting_writer"
        mock_state_machine.handle_trigger.return_value = result

        payload = {"type": "agent_complete", "status": "fail"}
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.execute_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_handoff_when_no_transition(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """No transition match = no lifecycle manager handoff."""
        mock_state_machine.handle_trigger.return_value = None

        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.execute_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_handoff_when_unrecognized(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Unrecognized type = no lifecycle manager handoff."""
        payload = {"type": "garbage_type", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        mock_state_machine.handle_trigger.return_value = None

        await router.handle_message(msg)

        mock_lifecycle_manager.execute_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_handoff_with_flag_human_action(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """flag_human action must also be passed to lifecycle manager."""
        result = MagicMock()
        result.action = "flag_human"
        result.action_args = None
        result.from_state = "waiting_writer"
        result.to_state = "idle"
        mock_state_machine.handle_trigger.return_value = result

        payload = {"type": "agent_complete", "status": "fail"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_lifecycle_manager.execute_action.assert_called_once()
        call_args = mock_lifecycle_manager.execute_action.call_args
        assert "flag_human" in str(call_args)

    @pytest.mark.asyncio
    async def test_handoff_with_no_action_transition(
        self, router, mock_state_machine, mock_lifecycle_manager
    ):
        """Transition with no action (action=None) should still notify lifecycle manager."""
        result = MagicMock()
        result.action = None
        result.action_args = None
        result.from_state = "waiting_executor"
        result.to_state = "idle"
        mock_state_machine.handle_trigger.return_value = result

        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.executor.outbox")

        await router.handle_message(msg)

        # Lifecycle manager should still be notified even with no action
        # so it can check completion rules (return to initial state)
        mock_lifecycle_manager.execute_action.assert_called_once()


# ===========================================================================
# 10. EDGE CASES AND ERROR HANDLING
# ===========================================================================


class TestEdgeCases:
    """Edge cases and error handling for the Message Router."""

    def test_router_error_is_exception(self):
        """RouterError must be a subclass of Exception."""
        assert issubclass(RouterError, Exception)

    def test_router_error_has_message(self):
        """RouterError should accept and store a message."""
        err = RouterError("test router error")
        assert "test router error" in str(err)

    def test_constructor_requires_dependencies(self):
        """MessageRouter constructor must accept nats_client, state_machine, lifecycle_manager, agents."""
        with pytest.raises(TypeError):
            MessageRouter()  # Missing required args

    def test_constructor_accepts_all_dependencies(
        self, mock_nats_client, mock_state_machine, mock_lifecycle_manager
    ):
        """Constructor with all dependencies must succeed."""
        router = MessageRouter(
            nats_client=mock_nats_client,
            state_machine=mock_state_machine,
            lifecycle_manager=mock_lifecycle_manager,
            agents=SAMPLE_AGENTS,
        )
        assert router is not None

    @pytest.mark.asyncio
    async def test_empty_payload_treated_as_unrecognized(
        self, router, mock_state_machine, caplog
    ):
        """Empty JSON object must be treated as unrecognized (missing type and status)."""
        payload = {}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(msg)

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        ), f"Expected unrecognized warning for empty payload, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_empty_payload_acks(self, router, mock_state_machine):
        """Empty payload must still be ACKed."""
        payload = {}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_message_from_different_agents(
        self, router, mock_state_machine
    ):
        """Must correctly handle messages from different agents in sequence."""
        payload = {"type": "agent_complete", "status": "pass"}

        msg1 = _make_nats_msg(payload, subject="agents.writer.outbox")
        msg2 = _make_nats_msg(payload, subject="agents.executor.outbox")

        await router.handle_message(msg1)
        await router.handle_message(msg2)

        assert mock_state_machine.handle_trigger.call_count == 2

        # First call should have source_agent=writer
        first_call = mock_state_machine.handle_trigger.call_args_list[0]
        assert "writer" in str(first_call)

        # Second call should have source_agent=executor
        second_call = mock_state_machine.handle_trigger.call_args_list[1]
        assert "executor" in str(second_call)

    @pytest.mark.asyncio
    async def test_role_extraction_handles_prefix(
        self, router, mock_state_machine
    ):
        """Must correctly extract role from subjects with standard prefix."""
        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        call_args = mock_state_machine.handle_trigger.call_args
        if call_args[1]:
            assert call_args[1].get("source_agent") == "writer"

    @pytest.mark.asyncio
    async def test_handles_large_payload(self, router, mock_state_machine):
        """Must handle messages with large optional fields."""
        payload = {
            "type": "agent_complete",
            "status": "pass",
            "summary": "A" * 10000,
            "files_changed": [f"file_{i}.py" for i in range(100)],
            "error": None,
        }
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        await router.handle_message(msg)

        mock_state_machine.handle_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_messages_handled_independently(
        self, router, mock_state_machine
    ):
        """Each message must be handled independently."""
        payload1 = {"type": "agent_complete", "status": "pass"}
        payload2 = {"type": "agent_complete", "status": "fail"}

        msg1 = _make_nats_msg(payload1, subject="agents.writer.outbox")
        msg2 = _make_nats_msg(payload2, subject="agents.writer.outbox")

        await router.handle_message(msg1)
        await router.handle_message(msg2)

        assert mock_state_machine.handle_trigger.call_count == 2

    @pytest.mark.asyncio
    async def test_ack_even_on_handler_exception(
        self, router, mock_state_machine
    ):
        """If handle_trigger raises, message should still be ACKed to prevent re-delivery."""
        mock_state_machine.handle_trigger.side_effect = Exception("unexpected error")

        payload = {"type": "agent_complete", "status": "pass"}
        msg = _make_nats_msg(payload, subject="agents.writer.outbox")

        # Should not crash
        try:
            await router.handle_message(msg)
        except Exception:
            pass

        # Message should still be ACKed
        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_paused_router_skips_processing(
        self, router, mock_state_machine
    ):
        """When router is paused, incoming messages should not be processed."""
        # If router supports pause/resume (from R7)
        if hasattr(router, 'pause') and hasattr(router, 'is_paused'):
            router.pause()
            payload = {"type": "agent_complete", "status": "pass"}
            msg = _make_nats_msg(payload, subject="agents.writer.outbox")

            await router.handle_message(msg)

            mock_state_machine.handle_trigger.assert_not_called()
        else:
            # If pause isn't implemented on router, this is acceptable
            pytest.skip("Router does not support pause/resume")

    @pytest.mark.asyncio
    async def test_multiple_messages_different_types(
        self, router, mock_state_machine, caplog
    ):
        """Handling valid and invalid messages in sequence."""
        # Valid message
        valid_payload = {"type": "agent_complete", "status": "pass"}
        valid_msg = _make_nats_msg(valid_payload, subject="agents.writer.outbox")

        # Invalid message (missing required fields)
        invalid_payload = {"summary": "no type or status"}
        invalid_msg = _make_nats_msg(invalid_payload, subject="agents.writer.outbox")

        with caplog.at_level(logging.WARNING):
            await router.handle_message(valid_msg)
            await router.handle_message(invalid_msg)

        # First message should trigger state machine, second should not
        assert mock_state_machine.handle_trigger.call_count == 1

        # Second message should generate warning
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "unrecognized" in msg.lower() for msg in warning_messages
        )
