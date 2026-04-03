"""Interactive Console Command Handler for the Multi-Agent System Shell.

Handles typed commands from the orchestrator's interactive console pane.

Requirements traced to PRD:
  - R7: Interactive Orchestrator Console
  - R6: tmux Communication (msg command safe-nudge check)
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_LOG_ENTRIES = 10

# --- Command names (used as dispatch keys and in help text) ---
_CMD_STATUS = "status"
_CMD_TASKS = "tasks"
_CMD_SKIP = "skip"
_CMD_NUDGE = "nudge"
_CMD_MSG = "msg"
_CMD_PAUSE = "pause"
_CMD_RESUME = "resume"
_CMD_LOG = "log"
_CMD_IMG = "img"
_CMD_BROADCAST = "broadcast"
_CMD_CONVERSATION = "conversation"
_CMD_HELP = "help"

_ALL_COMMANDS = [
    _CMD_STATUS,
    _CMD_TASKS,
    _CMD_SKIP,
    _CMD_NUDGE,
    _CMD_MSG,
    _CMD_BROADCAST,
    _CMD_IMG,
    _CMD_PAUSE,
    _CMD_RESUME,
    _CMD_LOG,
    _CMD_HELP,
]

# --- Response message templates ---
_MSG_EMPTY_CMD = "Empty command. Type 'help' for available commands."
_MSG_UNKNOWN_CMD = "Unknown command: '{}'. Type 'help' for available commands."
_MSG_NO_ACTIVE_TASK = "No active task"
_MSG_NO_TASKS = "No tasks in queue."
_MSG_SKIP_OK = "Current task marked as stuck and skipped."
_MSG_SKIP_FAIL = "Skip failed: {}"
_MSG_NUDGE_USAGE = "Usage: nudge <agent>. Available agents: {}"
_MSG_NUDGE_OK = "Nudge sent to {}."
_MSG_NUDGE_SKIP = "Nudge skipped for {} (agent busy or not ready)."
_MSG_MSG_USAGE = "Usage: msg <agent> <text>"
_MSG_MSG_NO_TEXT = "Usage: msg <agent> <text>. No text provided for {}."
_MSG_MSG_OK = "Message sent to {}."
_MSG_MSG_BUSY = "Agent {} is busy -- message not sent. Warning: try again later."
_MSG_TMUX_ERROR = "Error: agent not found or {} failed - {}"
_MSG_BROADCAST_USAGE = "Usage: broadcast <text>"
_MSG_BROADCAST_OK = "Broadcast sent to: {}"
_MSG_IMG_USAGE = "Usage: img <file-path> [agent]. Available agents: {}"
_MSG_IMG_NOT_FOUND = "Error: File not found: {}"
_MSG_IMG_OK = "Shared {} and notified {}."
_MSG_IMG_DIST_FAIL = "Error distributing file: {}"
_MSG_PAUSE_OK = "Outbox processing paused."
_MSG_RESUME_OK = "Outbox processing resumed."
_MSG_NO_LOGS = "No log entries."


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

        # Build dispatch table once (all handlers are stable bound methods)
        self._project_name: str = config.get("project", "")

        self._dispatch: dict[str, Any] = {
            _CMD_STATUS: self._cmd_status,
            _CMD_TASKS: self._cmd_tasks,
            _CMD_SKIP: self._cmd_skip,
            _CMD_NUDGE: self._cmd_nudge,
            _CMD_MSG: self._cmd_msg,
            _CMD_BROADCAST: self._cmd_broadcast,
            _CMD_IMG: self._cmd_img,
            _CMD_PAUSE: self._cmd_pause,
            _CMD_RESUME: self._cmd_resume,
            _CMD_LOG: self._cmd_log,
            _CMD_CONVERSATION: self._cmd_conversation,
            _CMD_HELP: self._cmd_help,
        }

        # Conversation mode subprocess
        self._conversation_proc: subprocess.Popen | None = None

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
            return _MSG_EMPTY_CMD

        command = parts[0].lower()
        args = parts[1:]

        handler = self._dispatch.get(command)
        if handler is None:
            return _MSG_UNKNOWN_CMD.format(command)

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

        task_line = self._format_active_task(current_task)

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
            return _MSG_NO_TASKS

        lines = [
            f"  [{t.get('status', 'unknown')}] {t.get('id', 'unknown')} - {t.get('title', '')}"
            for t in all_tasks
        ]
        return "Tasks:\n" + "\n".join(lines)

    def _cmd_skip(self, args: list[str]) -> str:
        """Mark current task as stuck and advance to next pending."""
        try:
            self._lifecycle_manager.skip_current_task()
            return _MSG_SKIP_OK
        except Exception as exc:
            return _MSG_SKIP_FAIL.format(exc)

    def _cmd_nudge(self, args: list[str]) -> str:
        """Trigger a nudge to the named agent's tmux pane."""
        if not args:
            return _MSG_NUDGE_USAGE.format(", ".join(self._agent_names))

        agent_name = args[0]
        return self._safe_tmux_call(
            operation="nudge",
            call=lambda: self._tmux_comm.nudge(agent_name),
            success_msg=_MSG_NUDGE_OK.format(agent_name),
            skipped_msg=_MSG_NUDGE_SKIP.format(agent_name),
        )

    def _cmd_msg(self, args: list[str]) -> str:
        """Type text into an agent's pane after safe-nudge check."""
        if not args:
            return _MSG_MSG_USAGE
        if len(args) < 2:
            return _MSG_MSG_NO_TEXT.format(args[0])

        agent_name = args[0]
        text = " ".join(args[1:])

        return self._safe_tmux_call(
            operation="message",
            call=lambda: self._tmux_comm.send_msg(agent_name, text),
            success_msg=_MSG_MSG_OK.format(agent_name),
            skipped_msg=_MSG_MSG_BUSY.format(agent_name),
        )

    def _cmd_broadcast(self, args: list[str]) -> str:
        """Send a message to all agent panes."""
        if not args:
            return _MSG_BROADCAST_USAGE

        text = " ".join(args)
        sent = []
        for agent in self._agent_names:
            try:
                if self._tmux_comm.send_msg(agent, text):
                    sent.append(agent)
            except Exception:
                pass  # skip agents not in pane mapping (e.g. monitor)
        return _MSG_BROADCAST_OK.format(", ".join(sent)) if sent else "No agents received the broadcast."

    def _cmd_img(self, args: list[str]) -> str:
        """Share an image/file to agent workspaces and notify an agent."""
        if not args:
            return _MSG_IMG_USAGE.format(", ".join(self._agent_names))

        file_path = os.path.expanduser(args[0])
        if not os.path.isfile(file_path):
            return _MSG_IMG_NOT_FOUND.format(file_path)

        # Determine target agent
        if len(args) >= 2:
            agent = args[1]
            if agent not in self._agent_names:
                return _MSG_IMG_USAGE.format(", ".join(self._agent_names))
        else:
            # Use the currently active agent from the state machine
            current_state = self._state_machine.current_state
            states_cfg = self._config.get("state_machine", {}).get("states", {})
            state_info = states_cfg.get(current_state, {})
            agent = state_info.get("agent", self._agent_names[0] if self._agent_names else "")
            if not agent:
                return _MSG_IMG_USAGE.format(", ".join(self._agent_names))

        # Run share-file.sh
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        share_script = os.path.join(script_dir, "scripts", "share-file.sh")
        result = subprocess.run(
            [share_script, self._project_name, file_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return _MSG_IMG_DIST_FAIL.format(result.stderr.strip())

        # Notify the agent
        filename = os.path.basename(file_path)
        msg = f"Look at the image at shared/{filename} using your Read tool"
        self._safe_tmux_call(
            operation="message",
            call=lambda: self._tmux_comm.send_msg(agent, msg),
            success_msg=_MSG_MSG_OK.format(agent),
            skipped_msg=_MSG_MSG_BUSY.format(agent),
        )
        return _MSG_IMG_OK.format(filename, agent)

    def _cmd_pause(self, args: list[str]) -> str:
        """Pause outbox processing."""
        self._paused = True
        return _MSG_PAUSE_OK

    def _cmd_resume(self, args: list[str]) -> str:
        """Resume outbox processing."""
        self._paused = False
        return _MSG_RESUME_OK

    def _cmd_log(self, args: list[str]) -> str:
        """Show last N log entries (default: 10)."""
        try:
            logs = self._lifecycle_manager.get_recent_logs(_MAX_LOG_ENTRIES)
        except AttributeError:
            logs = []

        if not logs:
            return _MSG_NO_LOGS

        return "\n".join(logs[-_MAX_LOG_ENTRIES:])

    def _cmd_conversation(self, args: list[str]) -> str:
        """Toggle conversation mode on/off."""
        if not args:
            is_on = self._conversation_proc is not None and self._conversation_proc.poll() is None
            return f"Conversation mode is {'ON' if is_on else 'OFF'}. Usage: conversation on|off"

        action = args[0].lower()
        if action == "on":
            if self._conversation_proc and self._conversation_proc.poll() is None:
                return "Conversation mode is already ON."
            script = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "scripts", "conversation-mode.py",
            )
            nats_url = self._config.get("nats", {}).get("url", "nats://192.168.1.37:4222")
            self._conversation_proc = subprocess.Popen(
                ["python3", script, "--nats-url", nats_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            logger.info("Conversation mode started (PID %d)", self._conversation_proc.pid)
            return f"Conversation mode ON (PID {self._conversation_proc.pid})"

        elif action == "off":
            if not self._conversation_proc or self._conversation_proc.poll() is not None:
                return "Conversation mode is already OFF."
            self._conversation_proc.send_signal(signal.SIGTERM)
            self._conversation_proc.wait(timeout=5)
            pid = self._conversation_proc.pid
            self._conversation_proc = None
            logger.info("Conversation mode stopped (PID %d)", pid)
            return f"Conversation mode OFF (stopped PID {pid})"

        return "Usage: conversation on|off"

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
            f"  broadcast <text>  - Send text to all agent panes\n"
            f"  img <file> [agent] - Share file to workspaces and notify agent. Agents: {agent_list}\n"
            "  conversation on|off - Toggle conversation mode (hear agents on speakers)\n"
            "  pause           - Pause outbox processing\n"
            "  resume          - Resume outbox processing\n"
            "  log             - Show last 10 log entries\n"
            "  help            - Show this help message\n"
            f"\nAvailable agents: {agent_list}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_active_task(task: dict[str, Any] | None) -> str:
        """Format the active task line for the status display.

        Returns a human-readable string for the current task, or a
        'no active task' message if *task* is ``None``.
        """
        if task:
            task_id = task.get("id", "unknown")
            task_title = task.get("title", "")
            return f"Active task: {task_id} - {task_title}"
        return _MSG_NO_ACTIVE_TASK

    @staticmethod
    def _safe_tmux_call(
        *,
        operation: str,
        call: Any,
        success_msg: str,
        skipped_msg: str,
    ) -> str:
        """Execute a tmux operation with unified error handling.

        Args:
            operation: Human-readable name (e.g. ``"nudge"``, ``"message"``).
            call: Zero-arg callable that performs the tmux action.
            success_msg: Returned when *call* returns a truthy value.
            skipped_msg: Returned when *call* returns a falsy value.
        """
        try:
            result = call()
            return success_msg if result else skipped_msg
        except Exception as exc:
            return _MSG_TMUX_ERROR.format(operation, exc)
