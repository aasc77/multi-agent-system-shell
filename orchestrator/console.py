"""Interactive Console Command Handler for the Multi-Agent System Shell.

Handles typed commands from the orchestrator's interactive console pane.

Requirements traced to PRD:
  - R7: Interactive Orchestrator Console
  - R6: tmux Communication (msg command safe-nudge check)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_LOG_ENTRIES = 10

_CMD_STATUS = "status"
_CMD_TASKS = "tasks"
_CMD_SKIP = "skip"
_CMD_NUDGE = "nudge"
_CMD_MSG = "msg"
_CMD_PAUSE = "pause"
_CMD_RESUME = "resume"
_CMD_LOG = "log"
_CMD_HELP = "help"

_ALL_COMMANDS = [
    _CMD_STATUS,
    _CMD_TASKS,
    _CMD_SKIP,
    _CMD_NUDGE,
    _CMD_MSG,
    _CMD_PAUSE,
    _CMD_RESUME,
    _CMD_LOG,
    _CMD_HELP,
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConsoleError(Exception):
    """Raised when the console encounters an error."""


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------


class Console:
    """Interactive console command handler for the orchestrator.

    Args:
        config: Merged config dict with 'tmux' and 'agents' keys.
        state_machine: StateMachine instance for reading current state.
        task_queue: TaskQueue instance for listing/reading tasks.
        nats_client: NatsClient instance for connection status.
        tmux_comm: TmuxComm instance for nudge/msg operations.
        lifecycle_manager: TaskLifecycleManager for skip and log operations.
    """

    def __init__(
        self,
        config: dict[str, Any],
        state_machine: Any,
        task_queue: Any,
        nats_client: Any,
        tmux_comm: Any,
        lifecycle_manager: Any,
    ) -> None:
        self._config = config
        self._state_machine = state_machine
        self._task_queue = task_queue
        self._nats_client = nats_client
        self._tmux_comm = tmux_comm
        self._lifecycle_manager = lifecycle_manager
        self._paused = False

        # Extract agent names from config
        self._agent_names = list(config.get("agents", {}).keys())

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Return whether outbox processing is currently paused."""
        return self._paused

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def handle_command(self, cmd: str) -> str:
        """Parse and dispatch a typed command, returning a result string."""
        parts = cmd.strip().split()
        if not parts:
            return "Empty command. Type 'help' for available commands."

        command = parts[0].lower()
        args = parts[1:]

        dispatch = {
            _CMD_STATUS: self._cmd_status,
            _CMD_TASKS: self._cmd_tasks,
            _CMD_SKIP: self._cmd_skip,
            _CMD_NUDGE: self._cmd_nudge,
            _CMD_MSG: self._cmd_msg,
            _CMD_PAUSE: self._cmd_pause,
            _CMD_RESUME: self._cmd_resume,
            _CMD_LOG: self._cmd_log,
            _CMD_HELP: self._cmd_help,
        }

        handler = dispatch.get(command)
        if handler is None:
            return f"Unknown command: '{command}'. Type 'help' for available commands."

        return handler(args)

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    def _cmd_status(self, args: list[str]) -> str:
        """Show current state, active task, progress, NATS status."""
        state = self._state_machine.current_state
        current_task = self._task_queue.get_current_task()
        all_tasks = self._task_queue.get_all_tasks()

        completed = sum(1 for t in all_tasks if t.get("status") == "completed")
        total = len(all_tasks)

        nats_status = (
            "NATS: connected" if self._nats_client.is_connected else "NATS: disconnected"
        )

        if current_task:
            task_id = current_task.get("id", "unknown")
            task_title = current_task.get("title", "")
            task_line = f"Active task: {task_id} - {task_title}"
        else:
            task_line = "No active task"

        return (
            f"State: {state}\n"
            f"{task_line}\n"
            f"Progress: {completed}/{total} completed\n"
            f"{nats_status}"
        )

    def _cmd_tasks(self, args: list[str]) -> str:
        """List all tasks with status markers."""
        all_tasks = self._task_queue.get_all_tasks()
        if not all_tasks:
            return "No tasks in queue."

        lines = []
        for task in all_tasks:
            task_id = task.get("id", "unknown")
            title = task.get("title", "")
            status = task.get("status", "unknown")
            lines.append(f"  [{status}] {task_id} - {title}")

        return "Tasks:\n" + "\n".join(lines)

    def _cmd_skip(self, args: list[str]) -> str:
        """Mark current task as stuck and advance to next pending."""
        try:
            self._lifecycle_manager.skip_current_task()
            return "Current task marked as stuck and skipped."
        except Exception as exc:
            return f"Skip failed: {exc}"

    def _cmd_nudge(self, args: list[str]) -> str:
        """Trigger a nudge to the named agent's tmux pane."""
        if not args:
            return "Usage: nudge <agent>. Available agents: " + ", ".join(self._agent_names)

        agent_name = args[0]
        try:
            result = self._tmux_comm.nudge(agent_name)
            if result:
                return f"Nudge sent to {agent_name}."
            else:
                return f"Nudge skipped for {agent_name} (agent busy or not ready)."
        except (KeyError, Exception) as exc:
            return f"Error: agent not found or nudge failed - {exc}"

    def _cmd_msg(self, args: list[str]) -> str:
        """Type text into an agent's pane after safe-nudge check."""
        if not args:
            return "Usage: msg <agent> <text>"
        if len(args) < 2:
            # Only agent name, no text
            agent_name = args[0]
            # Check if there's trailing whitespace that was stripped
            return f"Usage: msg <agent> <text>. No text provided for {agent_name}."

        agent_name = args[0]
        text = " ".join(args[1:])

        try:
            result = self._tmux_comm.send_msg(agent_name, text)
            if result:
                return f"Message sent to {agent_name}."
            else:
                return f"Agent {agent_name} is busy -- message not sent. Warning: try again later."
        except (KeyError, Exception) as exc:
            return f"Error: agent not found or message failed - {exc}"

    def _cmd_pause(self, args: list[str]) -> str:
        """Pause outbox processing."""
        self._paused = True
        return "Outbox processing paused."

    def _cmd_resume(self, args: list[str]) -> str:
        """Resume outbox processing."""
        self._paused = False
        return "Outbox processing resumed."

    def _cmd_log(self, args: list[str]) -> str:
        """Show last 10 log entries."""
        try:
            logs = self._lifecycle_manager.get_recent_logs(_MAX_LOG_ENTRIES)
        except AttributeError:
            logs = []

        if not logs:
            return "No log entries."

        entries = logs[-_MAX_LOG_ENTRIES:]
        return "\n".join(entries)

    def _cmd_help(self, args: list[str]) -> str:
        """List all commands with available agent names."""
        agent_list = ", ".join(self._agent_names) if self._agent_names else "none"
        return (
            "Available commands:\n"
            "  status          - Show current state, active task, progress, NATS status\n"
            "  tasks           - List all tasks with status markers\n"
            "  skip            - Skip current task (mark as stuck)\n"
            f"  nudge <agent>   - Nudge an agent. Agents: {agent_list}\n"
            f"  msg <agent> <text> - Send text to agent pane. Agents: {agent_list}\n"
            "  pause           - Pause outbox processing\n"
            "  resume          - Resume outbox processing\n"
            "  log             - Show last 10 log entries\n"
            "  help            - Show this help message\n"
            f"\nAvailable agents: {agent_list}"
        )
