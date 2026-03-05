"""Task Lifecycle Manager for the Multi-Agent System Shell.

Drives task progression through the pipeline: picking tasks, firing state machine
triggers, executing actions (NATS publish + tmux nudge), handling completion,
retry, stuck, and all_done.

Requirements traced to PRD:
  - R5: Task Queue (task completion, retry, stuck, all_done)
  - R4: Config-Driven State Machine (triggers, transitions, actions)
  - R3: Communication Flow (NATS publish, all_done message schema)
  - R6: tmux Communication (nudging agents after assign_to_agent)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    """Raised when the lifecycle manager encounters an error."""


class TaskLifecycleManager:
    """Orchestrates task progression through the state machine pipeline.

    Args:
        task_queue: TaskQueue instance for managing task statuses.
        state_machine: StateMachine instance for handling triggers/transitions.
        nats_client: NatsClient instance for publishing messages.
        tmux_comm: TmuxComm instance for nudging agent panes.
        config: Merged config dict with 'tasks', 'agents', 'state_machine' keys.
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

        self._max_attempts: int = config.get("tasks", {}).get("max_attempts_per_task", 5)
        self._agents: dict[str, Any] = config.get("agents", {})

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

        Returns None/False if no pending tasks remain.
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
        transition = self._state_machine.handle_trigger(trigger="task_assigned")

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

        Args:
            agent: The agent role that sent the response.
            message: The parsed outbox message from the agent.
            transition_result: The TransitionResult from the state machine,
                or None if no transition matched.
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

        if to_state == initial and action != "assign_to_agent":
            # Task completed
            if self.current_task is not None:
                task_id = self.current_task["id"]
                self._task_queue.mark_completed(task_id)
                self._task_queue.save()

                # Check if all tasks are done
                await self._check_all_done()

                # Move to next pending task
                if not self._task_queue.all_done():
                    next_task = self._task_queue.get_next_pending()
                    if next_task is not None:
                        self.current_task = next_task
                    else:
                        self.current_task = None
                else:
                    self.current_task = None

    # ------------------------------------------------------------------
    # Handle unmatched fail (no matching transition)
    # ------------------------------------------------------------------

    async def handle_unmatched_fail(self, task_id: str) -> None:
        """Handle a fail status with no matching transition.

        Increments attempts, then either retries or marks stuck.
        """
        # Increment attempts
        self._task_queue.increment_attempts(task_id)
        self._task_queue.save()

        # Check if stuck
        if self._task_queue.is_stuck(task_id, self._max_attempts):
            # Mark stuck
            self._task_queue.mark_stuck(task_id)
            self._task_queue.save()
            logger.warning("Task %s marked as stuck (exceeded max attempts)", task_id)

            # Check all_done
            await self._check_all_done()

            # Move to next pending task (do NOT reset state machine here)
            if not self._task_queue.all_done():
                next_task = self._task_queue.get_next_pending()
                if next_task is not None:
                    self.current_task = next_task
                else:
                    self.current_task = None
            else:
                self.current_task = None
        else:
            # Retry: reset state machine and re-fire task_assigned
            self._state_machine.reset()
            transition = self._state_machine.handle_trigger(trigger="task_assigned")
            if transition is not None:
                await self._execute_action(transition)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _execute_action(self, transition: Any) -> None:
        """Execute the action from a transition result."""
        action = transition.action
        action_args = transition.action_args or {}

        if action == "assign_to_agent":
            target_agent = action_args.get("target_agent")
            if target_agent is not None:
                # Build task assignment message
                msg = {
                    "type": "task_assignment",
                    "task_id": self.current_task["id"] if self.current_task else "",
                    "title": self.current_task.get("title", "") if self.current_task else "",
                    "description": self.current_task.get("description", "") if self.current_task else "",
                }
                # Include message from action_args if present
                extra_message = action_args.get("message", "")
                if extra_message:
                    msg["message"] = extra_message

                # Publish to NATS inbox
                await self._nats_client.publish_to_inbox(target_agent, msg)

                # Nudge via tmux
                self._tmux_comm.nudge(target_agent)

    async def _check_all_done(self) -> None:
        """Check if all tasks are done and send all_done if so."""
        if self._task_queue.all_done() and not self.all_done_sent:
            # Calculate summary
            tasks = self._task_queue.tasks
            completed_count = sum(1 for t in tasks if t["status"] == "completed")
            stuck_count = sum(1 for t in tasks if t["status"] == "stuck")
            summary = f"All tasks processed: {completed_count} completed, {stuck_count} stuck"

            logger.info(summary)

            # Publish all_done
            await self._nats_client.publish_all_done(summary)
            self.all_done_sent = True
