"""
Tests for orchestrator/lifecycle.py -- Task Lifecycle Manager

TDD Contract (RED phase):
These tests define the expected behavior of the Task Lifecycle Manager module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R5: Task Queue (task completion, retry, stuck, all_done)
  - R4: Config-Driven State Machine (triggers, transitions, actions)
  - R3: Communication Flow (NATS publish, all_done message schema)
  - R6: tmux Communication (nudging agents after assign_to_agent)

Acceptance criteria from task rgr-7:
  1. Picks next pending task and marks it in_progress
  2. Fires task_assigned trigger into state machine for new tasks
  3. Completion rule: return to initial state + no assign_to_agent action = mark task completed
  4. Moves to next pending task after completing or marking stuck
  5. On fail with no matching fail transition: increments attempts counter
  6. Retry: if attempts < max_attempts, resets state to initial and re-fires task_assigned
  7. Stuck: if attempts >= max_attempts, marks task stuck and moves to next
  8. Sends all_done message to every agent inbox when all tasks completed or stuck
  9. Logs summary: 'All tasks processed: X completed, Y stuck'
  10. Orchestrator continues running (stays alive) after sending all_done

Test categories:
  1. Picking next pending task and marking in_progress
  2. Firing task_assigned trigger into state machine
  3. Executing assign_to_agent action (NATS publish + tmux nudge)
  4. Completion rule: return to initial + no assign_to_agent = completed
  5. Moving to next pending task after completion
  6. Handling fail with matched fail transition (state machine handles it)
  7. Handling fail with NO matching transition: increment attempts
  8. Retry: attempts < max_attempts -> reset + re-fire task_assigned
  9. Stuck: attempts >= max_attempts -> mark stuck + move to next
  10. all_done: publish to all agent inboxes when all tasks done
  11. Log summary: 'All tasks processed: X completed, Y stuck'
  12. Orchestrator stays alive after all_done
  13. Edge cases
"""

import json
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call, PropertyMock

# --- The import that MUST fail in RED phase ---
from orchestrator.lifecycle import TaskLifecycleManager, LifecycleError


# ---------------------------------------------------------------------------
# Fixtures -- Mock dependencies for unit testing
# ---------------------------------------------------------------------------


def _make_transition_result(
    from_state, to_state, trigger, action=None, action_args=None
):
    """Helper: create a mock transition result object matching StateMachine API."""
    result = MagicMock()
    result.from_state = from_state
    result.to_state = to_state
    result.trigger = trigger
    result.action = action
    result.action_args = action_args or {}
    return result


def _make_task(task_id, title="Test task", description="", status="pending", attempts=0):
    """Helper: create a task dict."""
    return {
        "id": task_id,
        "title": title,
        "description": description,
        "status": status,
        "attempts": attempts,
    }


@pytest.fixture
def mock_task_queue():
    """Create a mock TaskQueue with standard behavior."""
    tq = MagicMock()
    tq.tasks = [
        _make_task("task-1", "First task", "Do first"),
        _make_task("task-2", "Second task", "Do second"),
        _make_task("task-3", "Third task", "Do third"),
    ]
    tq.get_next_pending.return_value = tq.tasks[0]
    tq.all_done.return_value = False
    tq.save = MagicMock()
    return tq


@pytest.fixture
def mock_state_machine():
    """Create a mock StateMachine with standard behavior."""
    sm = MagicMock()
    sm.current_state = "idle"
    sm.initial_state = "idle"
    # Default: task_assigned from idle -> waiting_writer with assign_to_agent
    sm.handle_trigger.return_value = _make_transition_result(
        from_state="idle",
        to_state="waiting_writer",
        trigger="task_assigned",
        action="assign_to_agent",
        action_args={"target_agent": "writer"},
    )
    sm.reset = MagicMock()
    return sm


@pytest.fixture
def mock_nats_client():
    """Create a mock NatsClient."""
    nc = AsyncMock()
    nc.publish_to_inbox = AsyncMock()
    nc.publish_all_done = AsyncMock()
    nc.is_connected = True
    return nc


@pytest.fixture
def mock_tmux_comm():
    """Create a mock TmuxComm."""
    tc = MagicMock()
    tc.nudge = MagicMock(return_value=True)
    tc.send_keys = MagicMock()
    return tc


@pytest.fixture
def sample_config():
    """Return a minimal merged config dict for the lifecycle manager."""
    return {
        "tasks": {"max_attempts_per_task": 5},
        "agents": {
            "writer": {"runtime": "claude_code", "working_dir": "/tmp/demo"},
            "executor": {"runtime": "script", "command": "echo"},
        },
        "state_machine": {
            "initial": "idle",
        },
    }


@pytest.fixture
def lifecycle(mock_task_queue, mock_state_machine, mock_nats_client, mock_tmux_comm, sample_config):
    """Create a TaskLifecycleManager with all mocked dependencies."""
    return TaskLifecycleManager(
        task_queue=mock_task_queue,
        state_machine=mock_state_machine,
        nats_client=mock_nats_client,
        tmux_comm=mock_tmux_comm,
        config=sample_config,
    )


# ===========================================================================
# 1. PICKING NEXT PENDING TASK AND MARKING IN_PROGRESS
# ===========================================================================


class TestPickNextTask:
    """TaskLifecycleManager must pick the next pending task and mark it in_progress."""

    @pytest.mark.asyncio
    async def test_picks_next_pending_task(self, lifecycle, mock_task_queue):
        """Must call get_next_pending() on the task queue."""
        await lifecycle.process_next_task()
        mock_task_queue.get_next_pending.assert_called()

    @pytest.mark.asyncio
    async def test_marks_task_in_progress(self, lifecycle, mock_task_queue):
        """Must mark the picked task as in_progress."""
        await lifecycle.process_next_task()
        mock_task_queue.mark_in_progress.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_sets_current_task(self, lifecycle, mock_task_queue):
        """Must track the current task being processed."""
        await lifecycle.process_next_task()
        assert lifecycle.current_task is not None
        assert lifecycle.current_task["id"] == "task-1"

    @pytest.mark.asyncio
    async def test_saves_queue_after_marking(self, lifecycle, mock_task_queue):
        """Must persist the status change by calling save()."""
        await lifecycle.process_next_task()
        mock_task_queue.save.assert_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_pending(self, lifecycle, mock_task_queue):
        """When no pending tasks, must not crash and return None/False."""
        mock_task_queue.get_next_pending.return_value = None
        result = await lifecycle.process_next_task()
        assert result is None or result is False

    @pytest.mark.asyncio
    async def test_does_not_mark_in_progress_when_no_pending(self, lifecycle, mock_task_queue):
        """Must not call mark_in_progress when there are no pending tasks."""
        mock_task_queue.get_next_pending.return_value = None
        await lifecycle.process_next_task()
        mock_task_queue.mark_in_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_completed_and_stuck_tasks(self, lifecycle, mock_task_queue):
        """get_next_pending already skips completed/stuck -- lifecycle relies on it."""
        # task-1 is completed, task-2 is stuck, task-3 is pending
        pending_task = _make_task("task-3", status="pending")
        mock_task_queue.get_next_pending.return_value = pending_task
        await lifecycle.process_next_task()
        assert lifecycle.current_task["id"] == "task-3"


# ===========================================================================
# 2. FIRING task_assigned TRIGGER INTO STATE MACHINE
# ===========================================================================


class TestFireTaskAssigned:
    """Lifecycle must fire task_assigned trigger into the state machine for new tasks."""

    @pytest.mark.asyncio
    async def test_fires_task_assigned_trigger(self, lifecycle, mock_state_machine):
        """Must call handle_trigger with trigger='task_assigned'."""
        await lifecycle.process_next_task()
        mock_state_machine.handle_trigger.assert_called()
        # Verify the call included trigger="task_assigned"
        call_kwargs = mock_state_machine.handle_trigger.call_args
        assert call_kwargs[1].get("trigger") == "task_assigned" or \
               (call_kwargs[0] and call_kwargs[0][0] == "task_assigned") or \
               "task_assigned" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_task_assigned_fires_after_mark_in_progress(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """task_assigned must fire AFTER the task is marked in_progress."""
        call_order = []
        mock_task_queue.mark_in_progress.side_effect = lambda _: call_order.append("mark")
        mock_state_machine.handle_trigger.side_effect = lambda **kw: (
            call_order.append("trigger"),
            _make_transition_result("idle", "waiting_writer", "task_assigned", "assign_to_agent", {"target_agent": "writer"}),
        )[1]
        await lifecycle.process_next_task()
        assert call_order.index("mark") < call_order.index("trigger")

    @pytest.mark.asyncio
    async def test_does_not_fire_trigger_when_no_pending(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Must not fire task_assigned when there are no pending tasks."""
        mock_task_queue.get_next_pending.return_value = None
        await lifecycle.process_next_task()
        mock_state_machine.handle_trigger.assert_not_called()


# ===========================================================================
# 3. EXECUTING assign_to_agent ACTION (NATS PUBLISH + TMUX NUDGE)
# ===========================================================================


class TestAssignToAgent:
    """When transition action is assign_to_agent, must publish to NATS and nudge tmux."""

    @pytest.mark.asyncio
    async def test_publishes_task_to_agent_inbox(
        self, lifecycle, mock_nats_client, mock_state_machine
    ):
        """Must publish task assignment to target agent's NATS inbox."""
        await lifecycle.process_next_task()
        mock_nats_client.publish_to_inbox.assert_called()
        call_args = mock_nats_client.publish_to_inbox.call_args
        # First arg should be agent name "writer"
        assert call_args[0][0] == "writer" or call_args[1].get("agent") == "writer"

    @pytest.mark.asyncio
    async def test_published_message_has_task_assignment_type(
        self, lifecycle, mock_nats_client
    ):
        """Published message must have type='task_assignment'."""
        await lifecycle.process_next_task()
        call_args = mock_nats_client.publish_to_inbox.call_args
        # Second arg should be the message dict
        message = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("message", {})
        assert message["type"] == "task_assignment"

    @pytest.mark.asyncio
    async def test_published_message_includes_task_id(
        self, lifecycle, mock_nats_client
    ):
        """Published message must include the task_id."""
        await lifecycle.process_next_task()
        call_args = mock_nats_client.publish_to_inbox.call_args
        message = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("message", {})
        assert message["task_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_published_message_includes_title(
        self, lifecycle, mock_nats_client
    ):
        """Published message must include the task title."""
        await lifecycle.process_next_task()
        call_args = mock_nats_client.publish_to_inbox.call_args
        message = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("message", {})
        assert message["title"] == "First task"

    @pytest.mark.asyncio
    async def test_published_message_includes_description(
        self, lifecycle, mock_nats_client
    ):
        """Published message must include the task description."""
        await lifecycle.process_next_task()
        call_args = mock_nats_client.publish_to_inbox.call_args
        message = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("message", {})
        assert "description" in message

    @pytest.mark.asyncio
    async def test_nudges_target_agent_via_tmux(
        self, lifecycle, mock_tmux_comm
    ):
        """Must nudge the target agent's tmux pane after publishing."""
        await lifecycle.process_next_task()
        mock_tmux_comm.nudge.assert_called_with("writer", force=True)

    @pytest.mark.asyncio
    async def test_nudge_happens_after_nats_publish(
        self, lifecycle, mock_nats_client, mock_tmux_comm
    ):
        """Nudge must happen AFTER the NATS publish, not before."""
        call_order = []
        mock_nats_client.publish_to_inbox.side_effect = lambda *a, **kw: call_order.append("publish")
        mock_tmux_comm.nudge.side_effect = lambda *a, **kw: (call_order.append("nudge"), True)[1]
        await lifecycle.process_next_task()
        assert call_order.index("publish") < call_order.index("nudge")

    @pytest.mark.asyncio
    async def test_includes_message_from_action_args(
        self, lifecycle, mock_nats_client, mock_state_machine
    ):
        """When action_args includes a message, it must be in the published payload."""
        mock_state_machine.handle_trigger.return_value = _make_transition_result(
            from_state="waiting_executor",
            to_state="waiting_writer",
            trigger="agent_complete",
            action="assign_to_agent",
            action_args={"target_agent": "writer", "message": "Tests failed. Fix and re-send."},
        )
        await lifecycle.process_next_task()
        call_args = mock_nats_client.publish_to_inbox.call_args
        message = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("message", {})
        assert message.get("message") == "Tests failed. Fix and re-send."

    @pytest.mark.asyncio
    async def test_does_not_publish_when_action_is_not_assign_to_agent(
        self, lifecycle, mock_nats_client, mock_state_machine
    ):
        """Must NOT publish to NATS when action is flag_human or None."""
        mock_state_machine.handle_trigger.return_value = _make_transition_result(
            from_state="waiting_writer",
            to_state="idle",
            trigger="agent_complete",
            action="flag_human",
        )
        await lifecycle.process_next_task()
        mock_nats_client.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_nudge_when_action_is_not_assign_to_agent(
        self, lifecycle, mock_tmux_comm, mock_state_machine
    ):
        """Must NOT nudge when action is not assign_to_agent."""
        mock_state_machine.handle_trigger.return_value = _make_transition_result(
            from_state="waiting_writer",
            to_state="idle",
            trigger="agent_complete",
            action="flag_human",
        )
        await lifecycle.process_next_task()
        mock_tmux_comm.nudge.assert_not_called()


# ===========================================================================
# 4. COMPLETION RULE: RETURN TO INITIAL + NO assign_to_agent = COMPLETED
# ===========================================================================


class TestCompletionRule:
    """Task is completed when state returns to initial AND transition has no assign_to_agent action."""

    @pytest.mark.asyncio
    async def test_mark_completed_on_return_to_initial_no_action(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Transition to initial state with no assign_to_agent => mark task completed."""
        # Setup: task is in progress, state machine returns to idle with no action
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        transition = _make_transition_result(
            from_state="waiting_executor",
            to_state="idle",
            trigger="agent_complete",
            action=None,
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_task_queue.mark_completed.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_mark_completed_with_empty_string_action(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Transition to initial state with action='' => mark task completed."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        transition = _make_transition_result(
            from_state="waiting_executor",
            to_state="idle",
            trigger="agent_complete",
            action="",
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_task_queue.mark_completed.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_not_completed_when_action_is_assign_to_agent(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Return to initial WITH assign_to_agent action => NOT completed."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        transition = _make_transition_result(
            from_state="waiting_writer",
            to_state="idle",
            trigger="agent_complete",
            action="assign_to_agent",
            action_args={"target_agent": "writer"},
        )
        await lifecycle.handle_agent_response(
            agent="writer",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_task_queue.mark_completed.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_completed_when_not_returning_to_initial(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Transition to non-initial state => NOT completed."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "waiting_executor"
        mock_state_machine.initial_state = "idle"
        transition = _make_transition_result(
            from_state="waiting_writer",
            to_state="waiting_executor",
            trigger="agent_complete",
            action="assign_to_agent",
            action_args={"target_agent": "executor"},
        )
        await lifecycle.handle_agent_response(
            agent="writer",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_task_queue.mark_completed.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_with_flag_human_action_at_initial(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Return to initial with flag_human (not assign_to_agent) => mark completed."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        transition = _make_transition_result(
            from_state="waiting_writer",
            to_state="idle",
            trigger="agent_complete",
            action="flag_human",
        )
        await lifecycle.handle_agent_response(
            agent="writer",
            message={"type": "agent_complete", "status": "fail"},
            transition_result=transition,
        )
        mock_task_queue.mark_completed.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_saves_queue_after_completion(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Must persist the completed status by calling save()."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        transition = _make_transition_result(
            from_state="waiting_executor",
            to_state="idle",
            trigger="agent_complete",
            action=None,
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_task_queue.save.assert_called()


# ===========================================================================
# 5. MOVING TO NEXT PENDING TASK AFTER COMPLETION
# ===========================================================================


class TestMoveToNextTask:
    """After completing or marking stuck, lifecycle must move to the next pending task."""

    @pytest.mark.asyncio
    async def test_processes_next_after_completion(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """After marking task completed, must pick the next pending task."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = False
        mock_task_queue.get_next_pending.return_value = _make_task("task-2")

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        # Should have picked next task
        mock_task_queue.get_next_pending.assert_called()

    @pytest.mark.asyncio
    async def test_clears_current_task_when_no_more_pending(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """When no more pending tasks after completion, current_task should be None."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        assert lifecycle.current_task is None


# ===========================================================================
# 6. HANDLING FAIL WITH MATCHED FAIL TRANSITION
# ===========================================================================


class TestFailWithMatchedTransition:
    """When status=fail matches a transition, that transition fires normally."""

    @pytest.mark.asyncio
    async def test_matched_fail_transition_fires(
        self, lifecycle, mock_state_machine, mock_nats_client
    ):
        """If fail matches a transition (e.g., executor fail -> waiting_writer), fire it."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "waiting_writer"
        # The transition: executor fail -> waiting_writer with assign_to_agent
        transition = _make_transition_result(
            from_state="waiting_executor",
            to_state="waiting_writer",
            trigger="agent_complete",
            action="assign_to_agent",
            action_args={"target_agent": "writer", "message": "Tests failed. Fix and re-send."},
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "fail"},
            transition_result=transition,
        )
        # Should publish to writer (assign_to_agent action)
        mock_nats_client.publish_to_inbox.assert_called()

    @pytest.mark.asyncio
    async def test_matched_fail_does_not_increment_attempts(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """When fail matches a transition, attempts counter must NOT be incremented."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=0)
        transition = _make_transition_result(
            from_state="waiting_executor",
            to_state="waiting_writer",
            trigger="agent_complete",
            action="assign_to_agent",
            action_args={"target_agent": "writer"},
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "fail"},
            transition_result=transition,
        )
        mock_task_queue.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_matched_fail_does_not_mark_stuck(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """When fail matches a transition, task must NOT be marked stuck."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        transition = _make_transition_result(
            from_state="waiting_executor",
            to_state="waiting_writer",
            trigger="agent_complete",
            action="assign_to_agent",
            action_args={"target_agent": "writer"},
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "fail"},
            transition_result=transition,
        )
        mock_task_queue.mark_stuck.assert_not_called()


# ===========================================================================
# 7. HANDLING FAIL WITH NO MATCHING TRANSITION: INCREMENT ATTEMPTS
# ===========================================================================


class TestFailNoMatchingTransition:
    """When status=fail has NO matching transition, increment attempts and retry/stuck."""

    @pytest.mark.asyncio
    async def test_increments_attempts_on_unmatched_fail(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Must increment the task's attempts counter when fail has no matching transition."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=0)
        mock_task_queue.is_stuck.return_value = False

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_task_queue.increment_attempts.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_saves_after_incrementing_attempts(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Must persist the incremented attempts by calling save()."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=0)
        mock_task_queue.is_stuck.return_value = False

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_task_queue.save.assert_called()


# ===========================================================================
# 8. RETRY: ATTEMPTS < MAX_ATTEMPTS -> RESET + RE-FIRE task_assigned
# ===========================================================================


class TestRetryBehavior:
    """When attempts < max_attempts, reset state machine and re-fire task_assigned."""

    @pytest.mark.asyncio
    async def test_resets_state_machine_on_retry(
        self, lifecycle, mock_state_machine, mock_task_queue
    ):
        """Must reset state machine to initial state when retrying."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=1)
        mock_task_queue.is_stuck.return_value = False

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_state_machine.reset.assert_called()

    @pytest.mark.asyncio
    async def test_refires_task_assigned_on_retry(
        self, lifecycle, mock_state_machine, mock_task_queue
    ):
        """Must fire task_assigned trigger again after resetting."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=1)
        mock_task_queue.is_stuck.return_value = False

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        # handle_trigger should have been called with task_assigned
        calls = mock_state_machine.handle_trigger.call_args_list
        assert any("task_assigned" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_task_stays_in_progress_on_retry(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Task must remain in_progress during retry (not reset to pending)."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=1)
        mock_task_queue.is_stuck.return_value = False

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        # Should NOT be marked completed or stuck
        mock_task_queue.mark_completed.assert_not_called()
        mock_task_queue.mark_stuck.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_order_increment_then_check_then_reset_then_fire(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Retry order: increment -> check is_stuck -> reset -> fire task_assigned."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=0)
        mock_task_queue.is_stuck.return_value = False

        call_order = []
        mock_task_queue.increment_attempts.side_effect = lambda _: call_order.append("increment")
        mock_task_queue.is_stuck.side_effect = lambda *a, **kw: (call_order.append("check_stuck"), False)[1]
        mock_state_machine.reset.side_effect = lambda: call_order.append("reset")
        mock_state_machine.handle_trigger.side_effect = lambda **kw: (
            call_order.append("fire"),
            _make_transition_result("idle", "waiting_writer", "task_assigned", "assign_to_agent", {"target_agent": "writer"}),
        )[1]

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        assert "increment" in call_order
        assert "reset" in call_order
        assert call_order.index("increment") < call_order.index("reset")


# ===========================================================================
# 9. STUCK: ATTEMPTS >= MAX_ATTEMPTS -> MARK STUCK + MOVE TO NEXT
# ===========================================================================


class TestStuckBehavior:
    """When attempts >= max_attempts, mark task stuck and move to next."""

    @pytest.mark.asyncio
    async def test_marks_task_stuck_at_max_attempts(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Must mark task stuck when attempts >= max_attempts."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_task_queue.mark_stuck.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_saves_after_marking_stuck(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """Must persist the stuck status by calling save()."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_task_queue.save.assert_called()

    @pytest.mark.asyncio
    async def test_does_not_reset_state_machine_when_stuck_and_all_done(
        self, lifecycle, mock_state_machine, mock_task_queue
    ):
        """Must NOT reset state machine when task is stuck and no tasks remain."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True
        mock_task_queue.all_done.return_value = True

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_state_machine.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_moves_to_next_pending_after_stuck(
        self, lifecycle, mock_task_queue, mock_state_machine
    ):
        """After marking stuck, must pick the next pending task."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True
        mock_task_queue.all_done.return_value = False
        mock_task_queue.get_next_pending.return_value = _make_task("task-2")

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_task_queue.get_next_pending.assert_called()

    @pytest.mark.asyncio
    async def test_logs_warning_when_stuck(
        self, lifecycle, mock_task_queue, mock_state_machine, caplog
    ):
        """Must log a warning when a task is marked stuck."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True

        with caplog.at_level(logging.WARNING):
            await lifecycle.handle_unmatched_fail(task_id="task-1")

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("stuck" in msg.lower() for msg in warning_messages), \
            f"Expected warning about task being stuck, got: {warning_messages}"

    @pytest.mark.asyncio
    async def test_uses_max_attempts_from_config(
        self, mock_task_queue, mock_state_machine, mock_nats_client, mock_tmux_comm
    ):
        """Must pass max_attempts_per_task from config to is_stuck check."""
        config = {
            "tasks": {"max_attempts_per_task": 3},
            "agents": {"writer": {"runtime": "claude_code"}},
            "state_machine": {"initial": "idle"},
        }
        lm = TaskLifecycleManager(
            task_queue=mock_task_queue,
            state_machine=mock_state_machine,
            nats_client=mock_nats_client,
            tmux_comm=mock_tmux_comm,
            config=config,
        )
        lm.current_task = _make_task("task-1", status="in_progress", attempts=2)
        mock_task_queue.is_stuck.return_value = False

        await lm.handle_unmatched_fail(task_id="task-1")

        # Verify is_stuck was called with max_attempts=3
        call_args = mock_task_queue.is_stuck.call_args
        assert 3 in call_args[0] or call_args[1].get("max_attempts") == 3 or \
               "3" in str(call_args)


# ===========================================================================
# 10. ALL_DONE: PUBLISH TO ALL AGENT INBOXES
# ===========================================================================


class TestAllDone:
    """When all tasks completed or stuck, publish all_done to every agent."""

    @pytest.mark.asyncio
    async def test_publishes_all_done_when_all_tasks_finished(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """Must call publish_all_done when all tasks are completed or stuck."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_nats_client.publish_all_done.assert_called()

    @pytest.mark.asyncio
    async def test_does_not_publish_all_done_when_tasks_remain(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """Must NOT call publish_all_done when pending tasks remain."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = False
        mock_task_queue.get_next_pending.return_value = _make_task("task-2")

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_nats_client.publish_all_done.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_done_after_stuck_is_last_task(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """Must publish all_done when the last task is marked stuck."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_nats_client.publish_all_done.assert_called()


# ===========================================================================
# 11. LOG SUMMARY: 'All tasks processed: X completed, Y stuck'
# ===========================================================================


class TestLogSummary:
    """Must log 'All tasks processed: X completed, Y stuck' when all_done fires."""

    @pytest.mark.asyncio
    async def test_logs_summary_on_all_done(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine, caplog
    ):
        """Must log the summary when all tasks are processed."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        # Mock task list: 2 completed, 1 stuck
        mock_task_queue.tasks = [
            _make_task("task-1", status="completed"),
            _make_task("task-2", status="completed"),
            _make_task("task-3", status="stuck"),
        ]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        with caplog.at_level(logging.INFO):
            await lifecycle.handle_agent_response(
                agent="executor",
                message={"type": "agent_complete", "status": "pass"},
                transition_result=transition,
            )

        all_messages = [r.message for r in caplog.records]
        assert any(
            "all tasks processed" in msg.lower() and "2 completed" in msg.lower() and "1 stuck" in msg.lower()
            for msg in all_messages
        ), f"Expected summary log with '2 completed, 1 stuck', got: {all_messages}"

    @pytest.mark.asyncio
    async def test_summary_includes_correct_counts(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine, caplog
    ):
        """Summary must have accurate completed and stuck counts."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        # 1 completed, 2 stuck
        mock_task_queue.tasks = [
            _make_task("t1", status="completed"),
            _make_task("t2", status="stuck"),
            _make_task("t3", status="stuck"),
        ]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        with caplog.at_level(logging.INFO):
            await lifecycle.handle_agent_response(
                agent="executor",
                message={"type": "agent_complete", "status": "pass"},
                transition_result=transition,
            )

        all_messages = [r.message for r in caplog.records]
        assert any(
            "1 completed" in msg.lower() and "2 stuck" in msg.lower()
            for msg in all_messages
        ), f"Expected '1 completed, 2 stuck', got: {all_messages}"

    @pytest.mark.asyncio
    async def test_summary_passed_to_publish_all_done(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """The summary string must be passed to publish_all_done."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [
            _make_task("t1", status="completed"),
            _make_task("t2", status="completed"),
        ]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )

        call_args = mock_nats_client.publish_all_done.call_args
        summary = call_args[0][0] if call_args[0] else call_args[1].get("summary", "")
        assert "2 completed" in summary.lower()
        assert "0 stuck" in summary.lower()


# ===========================================================================
# 12. ORCHESTRATOR STAYS ALIVE AFTER all_done
# ===========================================================================


class TestOrchestratorStaysAlive:
    """Orchestrator must continue running (stays alive) after sending all_done."""

    @pytest.mark.asyncio
    async def test_does_not_raise_after_all_done(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """handle_agent_response must NOT raise or sys.exit after all_done."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [_make_task("t1", status="completed")]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        # This must NOT raise SystemExit or any exception
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )

    @pytest.mark.asyncio
    async def test_is_alive_property_true_after_all_done(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """Lifecycle manager must report it is alive after all_done."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [_make_task("t1", status="completed")]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        assert lifecycle.is_alive is True

    @pytest.mark.asyncio
    async def test_all_done_sent_flag_is_set(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """Lifecycle must track that all_done has been sent."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [_make_task("t1", status="completed")]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        assert lifecycle.all_done_sent is True

    @pytest.mark.asyncio
    async def test_all_done_not_sent_twice(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine
    ):
        """If all_done was already sent, must NOT send it again."""
        lifecycle.all_done_sent = True
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )
        mock_nats_client.publish_all_done.assert_not_called()


# ===========================================================================
# 13. EDGE CASES
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions for the Task Lifecycle Manager."""

    def test_lifecycle_error_is_exception(self):
        """LifecycleError must be a subclass of Exception."""
        assert issubclass(LifecycleError, Exception)

    def test_lifecycle_error_has_message(self):
        """LifecycleError should accept and store a message."""
        err = LifecycleError("test lifecycle error")
        assert "test lifecycle error" in str(err)

    def test_constructor_requires_all_dependencies(self):
        """Constructor must require task_queue, state_machine, nats_client, tmux_comm, config."""
        with pytest.raises(TypeError):
            TaskLifecycleManager()

    def test_constructor_accepts_dependencies(
        self, mock_task_queue, mock_state_machine, mock_nats_client, mock_tmux_comm, sample_config
    ):
        """Constructor must accept all required dependencies."""
        lm = TaskLifecycleManager(
            task_queue=mock_task_queue,
            state_machine=mock_state_machine,
            nats_client=mock_nats_client,
            tmux_comm=mock_tmux_comm,
            config=sample_config,
        )
        assert lm is not None

    def test_initial_current_task_is_none(self, lifecycle):
        """Before processing, current_task must be None."""
        # Reset for clean state -- freshly created lifecycle should have no current task
        lm = TaskLifecycleManager(
            task_queue=MagicMock(),
            state_machine=MagicMock(),
            nats_client=AsyncMock(),
            tmux_comm=MagicMock(),
            config={"tasks": {"max_attempts_per_task": 5}, "agents": {}, "state_machine": {"initial": "idle"}},
        )
        assert lm.current_task is None

    def test_initial_all_done_sent_is_false(self, lifecycle):
        """Before any processing, all_done_sent must be False."""
        lm = TaskLifecycleManager(
            task_queue=MagicMock(),
            state_machine=MagicMock(),
            nats_client=AsyncMock(),
            tmux_comm=MagicMock(),
            config={"tasks": {"max_attempts_per_task": 5}, "agents": {}, "state_machine": {"initial": "idle"}},
        )
        assert lm.all_done_sent is False

    def test_initial_is_alive_is_true(self, lifecycle):
        """Lifecycle manager should be alive at construction."""
        lm = TaskLifecycleManager(
            task_queue=MagicMock(),
            state_machine=MagicMock(),
            nats_client=AsyncMock(),
            tmux_comm=MagicMock(),
            config={"tasks": {"max_attempts_per_task": 5}, "agents": {}, "state_machine": {"initial": "idle"}},
        )
        assert lm.is_alive is True

    @pytest.mark.asyncio
    async def test_process_next_task_with_empty_queue(
        self, lifecycle, mock_task_queue
    ):
        """Processing when task queue is empty must be handled gracefully."""
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.all_done.return_value = True
        # Should not crash
        result = await lifecycle.process_next_task()
        assert result is None or result is False

    @pytest.mark.asyncio
    async def test_handle_agent_response_with_none_transition(
        self, lifecycle, mock_task_queue
    ):
        """When transition_result is None (no matching transition), must handle gracefully."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_task_queue.is_stuck.return_value = False

        # transition_result=None means no transition matched
        await lifecycle.handle_agent_response(
            agent="writer",
            message={"type": "agent_complete", "status": "fail"},
            transition_result=None,
        )
        # Should increment attempts (unmatched fail)
        mock_task_queue.increment_attempts.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_max_attempts_defaults_to_config_value(self, lifecycle):
        """max_attempts must come from config['tasks']['max_attempts_per_task']."""
        assert lifecycle.max_attempts == 5

    @pytest.mark.asyncio
    async def test_single_task_full_lifecycle(
        self, lifecycle, mock_task_queue, mock_state_machine, mock_nats_client
    ):
        """Single task: pending -> in_progress -> completed -> all_done."""
        # Step 1: Process first task
        await lifecycle.process_next_task()
        mock_task_queue.mark_in_progress.assert_called_with("task-1")

        # Step 2: Agent completes, transition returns to initial
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [_make_task("t1", status="completed")]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )

        mock_task_queue.mark_completed.assert_called_with("task-1")
        mock_nats_client.publish_all_done.assert_called()

    @pytest.mark.asyncio
    async def test_multiple_tasks_sequential_processing(
        self, lifecycle, mock_task_queue, mock_state_machine, mock_nats_client
    ):
        """Multiple tasks processed sequentially: task-1 completed, then task-2."""
        # Process task-1
        await lifecycle.process_next_task()
        mock_task_queue.mark_in_progress.assert_called_with("task-1")

        # Complete task-1
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = False
        mock_task_queue.get_next_pending.return_value = _make_task("task-2")

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        await lifecycle.handle_agent_response(
            agent="executor",
            message={"type": "agent_complete", "status": "pass"},
            transition_result=transition,
        )

        mock_task_queue.mark_completed.assert_called_with("task-1")
        # Should have moved to next task
        mock_task_queue.get_next_pending.assert_called()

    @pytest.mark.asyncio
    async def test_stuck_then_next_task_processed(
        self, lifecycle, mock_task_queue, mock_state_machine, mock_nats_client
    ):
        """After a task is stuck, the next pending task gets processed."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True
        mock_task_queue.all_done.return_value = False
        next_task = _make_task("task-2")
        mock_task_queue.get_next_pending.return_value = next_task
        mock_state_machine.handle_trigger.return_value = _make_transition_result(
            "idle", "waiting_writer", "task_assigned", "assign_to_agent", {"target_agent": "writer"}
        )

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_task_queue.mark_stuck.assert_called_with("task-1")

    @pytest.mark.asyncio
    async def test_retry_resets_state_machine_to_initial(
        self, lifecycle, mock_state_machine, mock_task_queue
    ):
        """On retry, state machine must be reset to the initial state specifically."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=0)
        mock_task_queue.is_stuck.return_value = False
        mock_state_machine.reset = MagicMock()

        await lifecycle.handle_unmatched_fail(task_id="task-1")

        mock_state_machine.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_done_with_zero_stuck(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine, caplog
    ):
        """Summary with 0 stuck tasks should still include '0 stuck'."""
        lifecycle.current_task = _make_task("task-1", status="in_progress")
        mock_state_machine.current_state = "idle"
        mock_state_machine.initial_state = "idle"
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [
            _make_task("t1", status="completed"),
            _make_task("t2", status="completed"),
            _make_task("t3", status="completed"),
        ]

        transition = _make_transition_result(
            from_state="waiting_executor", to_state="idle", trigger="agent_complete"
        )
        with caplog.at_level(logging.INFO):
            await lifecycle.handle_agent_response(
                agent="executor",
                message={"type": "agent_complete", "status": "pass"},
                transition_result=transition,
            )

        all_messages = [r.message for r in caplog.records]
        assert any(
            "3 completed" in msg.lower() and "0 stuck" in msg.lower()
            for msg in all_messages
        ), f"Expected '3 completed, 0 stuck', got: {all_messages}"

    @pytest.mark.asyncio
    async def test_all_done_with_all_stuck(
        self, lifecycle, mock_task_queue, mock_nats_client, mock_state_machine, caplog
    ):
        """Summary when all tasks are stuck."""
        lifecycle.current_task = _make_task("task-1", status="in_progress", attempts=4)
        mock_task_queue.is_stuck.return_value = True
        mock_task_queue.all_done.return_value = True
        mock_task_queue.get_next_pending.return_value = None
        mock_task_queue.tasks = [
            _make_task("t1", status="stuck"),
            _make_task("t2", status="stuck"),
        ]

        with caplog.at_level(logging.INFO):
            await lifecycle.handle_unmatched_fail(task_id="task-1")

        all_messages = [r.message for r in caplog.records]
        assert any(
            "0 completed" in msg.lower() and "2 stuck" in msg.lower()
            for msg in all_messages
        ), f"Expected '0 completed, 2 stuck', got: {all_messages}"
