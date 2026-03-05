"""Task Lifecycle Manager for the Multi-Agent System Shell.

Drives task progression through the pipeline: picking tasks, firing state machine
triggers, executing actions (NATS publish + tmux nudge), handling completion,
retry, stuck, and all_done.

Requirements traced to PRD:
  - R5: Task Queue (task completion, retry, stuck, all_done)
  - R4: Config-Driven State Machine (triggers, transitions, actions)
  - R3: Communication Flow (NATS publish, all_done message schema)
  - R6: tmux Communication (nudging agents after assign_to_agent)

Example usage::

    manager = TaskLifecycleManager(task_queue, state_machine, nats, tmux, config)
    await manager.process_next_task()          # picks pending → in_progress
    await manager.handle_agent_response(...)   # completion / retry / stuck
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from orchestrator.task_queue import STATUS_COMPLETED, STATUS_STUCK

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Orchestrator-generated trigger (PRD R4 — "Orchestrator-generated triggers")
TRIGGER_TASK_ASSIGNED = "task_assigned"

# Built-in action name (PRD R4 — "Built-in actions")
ACTION_ASSIGN_TO_AGENT = "assign_to_agent"

# Inbox message type (PRD R3 — "Inbox message schema")
MSG_TYPE_TASK_ASSIGNMENT = "task_assignment"

# --- Message payload keys ---
_MSG_KEY_TYPE = "type"
_MSG_KEY_TASK_ID = "task_id"
_MSG_KEY_TITLE = "title"
_MSG_KEY_DESCRIPTION = "description"
_MSG_KEY_MESSAGE = "message"

# --- Config key names (avoids scattered magic strings) ---
_CFG_TASKS = "tasks"
_CFG_MAX_ATTEMPTS = "max_attempts_per_task"
_CFG_AGENTS = "agents"

# Default max attempts per task (PRD R5)
_DEFAULT_MAX_ATTEMPTS = 5

# Summary template for all_done logging (PRD R5)
_ALL_DONE_SUMMARY_TEMPLATE = "All tasks processed: {completed} completed, {stuck} stuck"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LifecycleError(Exception):
    """Raised when the lifecycle manager encounters an error."""


# ---------------------------------------------------------------------------
# Task Lifecycle Manager
# ---------------------------------------------------------------------------


class TaskLifecycleManager:
    """Orchestrates task progression through the state machine pipeline.

    Coordinates the task queue, state machine, NATS client, and tmux
    communicator to drive each task from ``pending`` through to
    ``completed`` or ``stuck``.

    Args:
        task_queue: TaskQueue instance for managing task statuses.
        state_machine: StateMachine instance for handling triggers/transitions.
        nats_client: NatsClient instance for publishing messages.
        tmux_comm: TmuxComm instance for nudging agent panes.
        config: Merged config dict with ``'tasks'``, ``'agents'``, and
            ``'state_machine'`` keys.
    """

    def __init__(
        self,
        task_queue: Any,
        state_machine: Any,
        nats_client: Any,
        tmux_comm: Any,
        config: dict[str, Any],
    ) -> None:
        self._task_queue = task_queue
        self._state_machine = state_machine
        self._nats_client = nats_client
        self._tmux_comm = tmux_comm
        self._config = config

        self._max_attempts: int = (
            config.get(_CFG_TASKS, {}).get(_CFG_MAX_ATTEMPTS, _DEFAULT_MAX_ATTEMPTS)
        )
        self._agents: dict[str, Any] = config.get(_CFG_AGENTS, {})

        self.current_task: Optional[dict] = None
        self.all_done_sent: bool = False
        self.is_alive: bool = True

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def max_attempts(self) -> int:
        """Return the configured max attempts per task."""
        return self._max_attempts

    # ------------------------------------------------------------------
    # Core lifecycle: process next task
    # ------------------------------------------------------------------

    async def process_next_task(self) -> Optional[bool]:
        """Pick the next pending task, mark it in_progress, and fire task_assigned.

        Returns:
            ``True`` if a task was picked and processed, ``None`` if no
            pending tasks remain.
        """
        task = self._task_queue.get_next_pending()
        if task is None:
            return None

        # Mark in_progress
        task_id = task["id"]
        self._task_queue.mark_in_progress(task_id)
        self.current_task = task
        self._task_queue.save()

        # Fire task_assigned trigger into state machine
        transition = self._state_machine.handle_trigger(trigger=TRIGGER_TASK_ASSIGNED)

        # Execute action if present
        if transition is not None:
            await self._execute_action(transition)

        return True

    # ------------------------------------------------------------------
    # Handle agent response
    # ------------------------------------------------------------------

    async def handle_agent_response(
        self,
        agent: str,
        message: dict[str, Any],
        transition_result: Any,
    ) -> None:
        """Process an agent's response after a state machine transition.

        Applies the completion rule (PRD R5): if the transition returns to
        the initial state without an ``assign_to_agent`` action, the current
        task is marked ``completed`` and the manager advances to the next
        pending task.

        Args:
            agent: The agent role that sent the response.
            message: The parsed outbox message from the agent.
            transition_result: The ``TransitionResult`` from the state
                machine, or ``None`` if no transition matched.
        """
        if transition_result is None:
            # No matching transition -- treat as unmatched fail
            if self.current_task is not None:
                await self.handle_unmatched_fail(task_id=self.current_task["id"])
            return

        # Execute action from the transition
        await self._execute_action(transition_result)

        # Check completion rule: return to initial + no assign_to_agent = completed
        to_state = transition_result.to_state
        action = transition_result.action
        initial = self._state_machine.initial_state

        if to_state == initial and action != ACTION_ASSIGN_TO_AGENT:
            # Task completed
            if self.current_task is not None:
                task_id = self.current_task["id"]
                self._task_queue.mark_completed(task_id)
                self._task_queue.save()

                await self._advance_to_next_task()

    # ------------------------------------------------------------------
    # Handle unmatched fail (no matching transition)
    # ------------------------------------------------------------------

    async def handle_unmatched_fail(self, task_id: str) -> None:
        """Handle a fail status with no matching transition.

        Increments the task's attempts counter, then either retries
        (resets state machine + re-fires ``task_assigned``) or marks
        the task ``stuck`` if max attempts are exceeded (PRD R5).
        """
        # Increment attempts
        self._task_queue.increment_attempts(task_id)
        self._task_queue.save()

        # Check if stuck
        if self._task_queue.is_stuck(task_id, self._max_attempts):
            self._task_queue.mark_stuck(task_id)
            self._task_queue.save()
            logger.warning("Task %s marked as stuck (exceeded max attempts)", task_id)

            await self._advance_to_next_task()
        else:
            # Retry: reset state machine and re-fire task_assigned
            self._state_machine.reset()
            transition = self._state_machine.handle_trigger(
                trigger=TRIGGER_TASK_ASSIGNED,
            )
            if transition is not None:
                await self._execute_action(transition)

    # ------------------------------------------------------------------
    # Public helpers (used by Console and Router)
    # ------------------------------------------------------------------

    async def execute_action(self, action: str, action_args: dict, transition: Any) -> None:
        """Public entry point for the router to execute a transition's action."""
        await self._execute_action(transition)

        # Check completion rule: return to initial + no assign_to_agent = completed
        to_state = transition.to_state
        initial = self._state_machine.initial_state

        if to_state == initial and action != ACTION_ASSIGN_TO_AGENT:
            if self.current_task is not None:
                task_id = self.current_task["id"]
                logger.info("Task %s completed", task_id)
                self._task_queue.mark_completed(task_id)
                self._task_queue.save()
                await self._advance_to_next_task()

    def skip_current_task(self) -> None:
        """Mark the current task as stuck and clear it."""
        if self.current_task is None:
            raise LifecycleError("No active task to skip")
        task_id = self.current_task["id"]
        self._task_queue.mark_stuck(task_id)
        self._task_queue.save()
        self._state_machine.reset()
        self.current_task = None

    def get_recent_logs(self, count: int = 10) -> list[str]:
        """Return recent log entries (placeholder -- reads from log file)."""
        return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _advance_to_next_task(self) -> None:
        """Check for all_done, then start the next pending task.

        If all tasks are finished (``completed`` or ``stuck``), sends the
        ``all_done`` message.  Otherwise resets the state machine and
        processes the next pending task through the full lifecycle.
        """
        await self._check_all_done()

        if not self._task_queue.all_done():
            self._state_machine.reset()
            await self.process_next_task()
        else:
            self.current_task = None

    async def _execute_action(self, transition: Any) -> None:
        """Execute the action from a transition result.

        Currently supports the ``assign_to_agent`` built-in action:
        publishes a task assignment message to the target agent's NATS
        inbox and nudges the agent's tmux pane.
        """
        action = transition.action
        action_args = transition.action_args or {}

        if action == ACTION_ASSIGN_TO_AGENT:
            target_agent = action_args.get("target_agent")
            if target_agent is not None:
                msg = self._build_task_assignment_message(action_args)
                await self._nats_client.publish_to_inbox(target_agent, msg)
                self._tmux_comm.nudge(target_agent)

    def _build_task_assignment_message(
        self, action_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the inbox task_assignment message payload (PRD R3).

        Args:
            action_args: The action_args dict from the transition, which
                may contain an optional ``message`` field.

        Returns:
            A dict ready for JSON serialisation and NATS publishing.
        """
        task = self.current_task
        msg: dict[str, Any] = {
            _MSG_KEY_TYPE: MSG_TYPE_TASK_ASSIGNMENT,
            _MSG_KEY_TASK_ID: task["id"] if task else "",
            _MSG_KEY_TITLE: task.get(_MSG_KEY_TITLE, "") if task else "",
            _MSG_KEY_DESCRIPTION: task.get(_MSG_KEY_DESCRIPTION, "") if task else "",
        }
        extra_message = action_args.get(_MSG_KEY_MESSAGE, "")
        if extra_message:
            msg[_MSG_KEY_MESSAGE] = extra_message
        return msg

    async def _check_all_done(self) -> None:
        """Check if all tasks are done and send ``all_done`` if so.

        Builds a summary string with completed/stuck counts, logs it,
        publishes via NATS, and sets the ``all_done_sent`` flag to
        prevent duplicate sends.
        """
        if self._task_queue.all_done() and not self.all_done_sent:
            tasks = self._task_queue.tasks
            completed_count = sum(
                1 for t in tasks if t["status"] == STATUS_COMPLETED
            )
            stuck_count = sum(
                1 for t in tasks if t["status"] == STATUS_STUCK
            )
            summary = _ALL_DONE_SUMMARY_TEMPLATE.format(
                completed=completed_count, stuck=stuck_count,
            )

            logger.info(summary)
            await self._nats_client.publish_all_done(summary)
            self.all_done_sent = True
