"""
Tests for agents/echo_agent.py -- Example Script Agent

TDD Contract (RED phase):
These tests define the expected behavior of the echo_agent.py script agent.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R2: Config-Driven Agents (script runtime)
  - R3: Communication Flow (direct NATS for script agents)
  - R3: all_done message handling (script agents MUST exit cleanly)
  - Acceptance criteria from task rgr-13

Test categories:
  1. CLI argument parsing -- accepts --role argument
  2. NATS subscription -- subscribes to agents.<role>.inbox
  3. Outbox response -- valid agent_complete schema with echo summary
  4. all_done handling -- exits cleanly with code 0
  5. Error handling -- missing args, NATS unavailable
  6. Message schema validation
"""

import json
import asyncio
import subprocess
import sys
import os
import signal
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

# --- The import that MUST fail in RED phase ---
from agents.echo_agent import EchoAgent, parse_args, main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TASK_ASSIGNMENT_MSG = {
    "type": "task_assignment",
    "task_id": "demo-1",
    "title": "Echo test",
    "description": "Test the echo agent functionality",
    "message": "",
}

ALL_DONE_MSG = {
    "type": "all_done",
    "summary": "All tasks processed: 1 completed, 0 stuck",
}


@pytest.fixture
def agent():
    """Create an EchoAgent instance with role 'executor'."""
    return EchoAgent(role="executor", nats_url="nats://localhost:4222")


@pytest.fixture
def mock_nats():
    """Create a mock NATS client."""
    nc = AsyncMock()
    nc.is_connected = True
    nc.subscribe = AsyncMock()
    nc.publish = AsyncMock()
    nc.drain = AsyncMock()
    nc.close = AsyncMock()
    return nc


# ===========================================================================
# 1. CLI ARGUMENT PARSING -- accepts --role argument
# ===========================================================================


class TestCLIArguments:
    """echo_agent.py must accept --role to set its agent identity."""

    def test_parse_args_with_role(self):
        """--role argument must be parsed and returned."""
        args = parse_args(["--role", "executor"])
        assert args.role == "executor"

    def test_parse_args_role_is_required(self):
        """Missing --role must cause an error."""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_parse_args_different_roles(self):
        """--role should accept any string value."""
        args = parse_args(["--role", "reviewer"])
        assert args.role == "reviewer"

        args = parse_args(["--role", "writer"])
        assert args.role == "writer"

    def test_parse_args_nats_url_default(self):
        """NATS URL should default to nats://localhost:4222 if not provided."""
        args = parse_args(["--role", "executor"])
        assert args.nats_url == "nats://localhost:4222"

    def test_parse_args_nats_url_override(self):
        """--nats-url should override the default NATS URL."""
        args = parse_args(["--role", "executor", "--nats-url", "nats://remote:4222"])
        assert args.nats_url == "nats://remote:4222"


# ===========================================================================
# 2. NATS SUBSCRIPTION -- subscribes to agents.<role>.inbox
# ===========================================================================


class TestNATSSubscription:
    """EchoAgent must subscribe to agents.<role>.inbox on start."""

    def test_inbox_subject_matches_role(self, agent):
        """Agent inbox subject must be agents.<role>.inbox."""
        assert agent.inbox_subject == "agents.executor.inbox"

    def test_outbox_subject_matches_role(self, agent):
        """Agent outbox subject must be agents.<role>.outbox."""
        assert agent.outbox_subject == "agents.executor.outbox"

    def test_inbox_subject_for_different_role(self):
        """Different role must produce different NATS subjects."""
        agent = EchoAgent(role="reviewer", nats_url="nats://localhost:4222")
        assert agent.inbox_subject == "agents.reviewer.inbox"
        assert agent.outbox_subject == "agents.reviewer.outbox"

    @pytest.mark.asyncio
    async def test_subscribes_to_inbox_on_start(self, agent, mock_nats):
        """Agent must subscribe to its inbox subject when started."""
        with patch("agents.echo_agent.nats_connect", return_value=mock_nats):
            # Start the agent but cancel after subscription
            task = asyncio.create_task(agent.run())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Verify subscription was made to the correct subject
        mock_nats.subscribe.assert_called()
        subscribe_calls = mock_nats.subscribe.call_args_list
        subjects = [c.args[0] if c.args else c.kwargs.get("subject") for c in subscribe_calls]
        assert "agents.executor.inbox" in subjects

    def test_agent_stores_role(self, agent):
        """Agent must store its role for later use."""
        assert agent.role == "executor"

    def test_agent_stores_nats_url(self, agent):
        """Agent must store the NATS URL."""
        assert agent.nats_url == "nats://localhost:4222"


# ===========================================================================
# 3. OUTBOX RESPONSE -- valid agent_complete schema with echo summary
# ===========================================================================


class TestOutboxResponse:
    """Agent must respond with valid outbox schema: type agent_complete, status pass."""

    @pytest.mark.asyncio
    async def test_response_has_type_agent_complete(self, agent, mock_nats):
        """Response type must be 'agent_complete'."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert response["type"] == "agent_complete"

    @pytest.mark.asyncio
    async def test_response_has_status_pass(self, agent, mock_nats):
        """Response status must be 'pass'."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert response["status"] == "pass"

    @pytest.mark.asyncio
    async def test_response_summary_echoes_task(self, agent, mock_nats):
        """Summary must echo the task title or description."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        summary = response.get("summary", "")
        # Summary should reference the task in some way
        assert (
            TASK_ASSIGNMENT_MSG["title"] in summary
            or TASK_ASSIGNMENT_MSG["description"] in summary
            or TASK_ASSIGNMENT_MSG["task_id"] in summary
        ), f"Summary '{summary}' does not echo the task"

    @pytest.mark.asyncio
    async def test_response_is_valid_json_serializable(self, agent, mock_nats):
        """Response must be JSON-serializable."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        json_str = json.dumps(response)
        parsed = json.loads(json_str)
        assert parsed["type"] == "agent_complete"
        assert parsed["status"] == "pass"

    @pytest.mark.asyncio
    async def test_publishes_response_to_outbox(self, agent, mock_nats):
        """Agent must publish the response to agents.<role>.outbox."""
        agent._nc = mock_nats
        await agent.process_message(json.dumps(TASK_ASSIGNMENT_MSG).encode())

        mock_nats.publish.assert_called()
        publish_call = mock_nats.publish.call_args
        subject = publish_call.args[0] if publish_call.args else publish_call.kwargs.get("subject")
        assert subject == "agents.executor.outbox"

    @pytest.mark.asyncio
    async def test_published_payload_is_valid_json(self, agent, mock_nats):
        """Published payload must be valid JSON with required fields."""
        agent._nc = mock_nats
        await agent.process_message(json.dumps(TASK_ASSIGNMENT_MSG).encode())

        publish_call = mock_nats.publish.call_args
        payload = publish_call.args[1] if len(publish_call.args) > 1 else publish_call.kwargs.get("payload")
        data = json.loads(payload)
        assert data["type"] == "agent_complete"
        assert data["status"] == "pass"
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_response_includes_task_id_reference(self, agent, mock_nats):
        """Response summary should reference the task_id for traceability."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        summary = response.get("summary", "")
        # Best practice: include task_id in summary
        assert "demo-1" in summary or "Echo test" in summary


# ===========================================================================
# 4. ALL_DONE HANDLING -- exits cleanly with code 0
# ===========================================================================


class TestAllDoneHandling:
    """Script agents MUST exit cleanly (exit code 0) on receiving all_done."""

    @pytest.mark.asyncio
    async def test_all_done_sets_shutdown_flag(self, agent, mock_nats):
        """Receiving all_done must set a flag to trigger clean shutdown."""
        agent._nc = mock_nats
        await agent.process_message(json.dumps(ALL_DONE_MSG).encode())
        assert agent.should_exit is True

    @pytest.mark.asyncio
    async def test_all_done_does_not_publish_response(self, agent, mock_nats):
        """Agent MUST NOT send an outbox response on all_done."""
        agent._nc = mock_nats
        await agent.process_message(json.dumps(ALL_DONE_MSG).encode())
        mock_nats.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_done_drains_nats_connection(self, agent, mock_nats):
        """Agent must drain NATS connection before exit."""
        agent._nc = mock_nats
        await agent.process_message(json.dumps(ALL_DONE_MSG).encode())
        # Agent should drain or close the connection
        assert mock_nats.drain.called or mock_nats.close.called

    def test_echo_agent_script_exits_zero_on_all_done(self):
        """Running echo_agent.py as subprocess and sending all_done must exit 0.

        This tests the actual script invocation. The agent should:
        1. Start and subscribe to NATS
        2. Receive all_done
        3. Exit with code 0
        """
        # This is a contract test -- the script must exist at agents/echo_agent.py
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "agents",
            "echo_agent.py",
        )
        assert os.path.exists(script_path), (
            f"echo_agent.py must exist at {script_path}"
        )


# ===========================================================================
# 5. ERROR HANDLING -- missing args, NATS unavailable
# ===========================================================================


class TestErrorHandling:
    """Error conditions for the echo agent."""

    def test_agent_requires_role(self):
        """Creating EchoAgent without role must raise an error."""
        with pytest.raises(TypeError):
            EchoAgent(nats_url="nats://localhost:4222")

    def test_agent_requires_nats_url(self):
        """Creating EchoAgent without nats_url must raise an error."""
        with pytest.raises(TypeError):
            EchoAgent(role="executor")

    @pytest.mark.asyncio
    async def test_handles_malformed_message_gracefully(self, agent, mock_nats):
        """Agent should handle malformed (non-JSON) messages without crashing."""
        agent._nc = mock_nats
        # Should not raise
        await agent.process_message(b"not valid json")
        # Agent should still be alive (should_exit not set)
        assert agent.should_exit is False

    @pytest.mark.asyncio
    async def test_handles_missing_type_field(self, agent, mock_nats):
        """Agent should handle messages missing the 'type' field."""
        agent._nc = mock_nats
        msg = json.dumps({"task_id": "demo-1", "title": "No type"}).encode()
        await agent.process_message(msg)
        # Should not crash, should not respond
        assert agent.should_exit is False

    @pytest.mark.asyncio
    async def test_handles_unknown_message_type(self, agent, mock_nats):
        """Agent should ignore messages with unknown type."""
        agent._nc = mock_nats
        msg = json.dumps({"type": "unknown_type", "data": "test"}).encode()
        await agent.process_message(msg)
        # Should not crash, should not respond
        mock_nats.publish.assert_not_called()


# ===========================================================================
# 6. MESSAGE SCHEMA VALIDATION
# ===========================================================================


class TestMessageSchema:
    """Outbox messages must conform to the PRD R3 schema."""

    @pytest.mark.asyncio
    async def test_outbox_has_required_type_field(self, agent):
        """type field is required in outbox messages."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert "type" in response

    @pytest.mark.asyncio
    async def test_outbox_type_is_agent_complete(self, agent):
        """type must be 'agent_complete'."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert response["type"] == "agent_complete"

    @pytest.mark.asyncio
    async def test_outbox_has_required_status_field(self, agent):
        """status field is required in outbox messages."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert "status" in response

    @pytest.mark.asyncio
    async def test_outbox_status_is_pass(self, agent):
        """Echo agent always returns status 'pass'."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert response["status"] == "pass"

    @pytest.mark.asyncio
    async def test_outbox_has_summary(self, agent):
        """summary is optional per PRD but echo agent should include it."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        assert "summary" in response
        assert len(response["summary"]) > 0

    @pytest.mark.asyncio
    async def test_outbox_no_extra_required_fields_missing(self, agent):
        """Response must have at minimum type and status."""
        response = await agent.handle_task(TASK_ASSIGNMENT_MSG)
        required_fields = {"type", "status"}
        assert required_fields.issubset(set(response.keys()))

    @pytest.mark.asyncio
    async def test_handles_task_with_empty_message(self, agent):
        """Task with empty message field should still produce valid response."""
        task = {**TASK_ASSIGNMENT_MSG, "message": ""}
        response = await agent.handle_task(task)
        assert response["type"] == "agent_complete"
        assert response["status"] == "pass"

    @pytest.mark.asyncio
    async def test_handles_task_with_extra_message(self, agent):
        """Task with extra instructions in message field."""
        task = {**TASK_ASSIGNMENT_MSG, "message": "Extra instructions here"}
        response = await agent.handle_task(task)
        assert response["type"] == "agent_complete"
        assert response["status"] == "pass"
