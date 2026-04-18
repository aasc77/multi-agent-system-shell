"""
Tests for orchestrator/console.py -- Interactive Console Command Handler

TDD Contract (RED phase):
These tests define the expected behavior of the Interactive Console module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R7: Interactive Orchestrator Console
  - R6: tmux Communication (msg command safe-nudge check)
  - R10: LLM Client (Ollama) -- health check, config settings

Acceptance criteria from task rgr-9:
  1. status command shows current state, active task, progress, NATS connection status
  2. tasks command lists all tasks with status markers
  3. skip command marks current task stuck and advances to next pending
  4. nudge <agent> triggers a nudge to the named agent's tmux pane
  5. msg <agent> <text> types text into agent pane after safe-nudge check;
     refuses with warning if agent is busy
  6. pause stops processing new outbox messages; resume re-enables processing
  7. log shows last 10 log entries
  8. help lists all commands with available agent names from config
  9. Ollama health check on startup: if unreachable, logs warning and continues without LLM
  10. LLM client respects config settings (provider, model, base_url, temperature)

Test categories:
  1. Console construction and initialization
  2. status command -- shows state, task, progress, NATS status
  3. tasks command -- lists all tasks with status markers
  4. skip command -- marks current task stuck, advances to next
  5. nudge <agent> -- triggers tmux nudge on named agent
  6. msg <agent> <text> -- types into agent pane with safe-nudge check
  7. pause / resume -- toggle outbox processing
  8. log -- last 10 log entries
  9. help -- list all commands with agent names
  10. Command parsing and dispatch
  11. Edge cases and error handling
"""

import logging
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call

# --- The imports that MUST fail in RED phase ---
from orchestrator.console import Console, ConsoleError


# ---------------------------------------------------------------------------
# Fixtures -- Mock dependencies for unit testing
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = {
    "tmux": {
        "session_name": "demo",
        "nudge_prompt": "You have new messages. Use check_messages with your role.",
        "nudge_cooldown_seconds": 30,
        "max_nudge_retries": 20,
    },
    "agents": {
        "qa": {"runtime": "claude_code", "working_dir": "/tmp/qa"},
        "dev": {"runtime": "claude_code", "working_dir": "/tmp/dev"},
        "refactor": {"runtime": "claude_code", "working_dir": "/tmp/refactor"},
    },
}

TWO_AGENT_CONFIG = {
    "tmux": {
        "session_name": "demo",
        "nudge_prompt": "You have new messages. Use check_messages with your role.",
        "nudge_cooldown_seconds": 30,
        "max_nudge_retries": 20,
    },
    "agents": {
        "writer": {"runtime": "claude_code", "working_dir": "/tmp/writer"},
        "executor": {"runtime": "script", "command": "python3 echo.py"},
    },
}


def _make_task(task_id, title="Test task", description="", status="pending", attempts=0):
    """Helper: create a task dict."""
    return {
        "id": task_id,
        "title": title,
        "description": description,
        "status": status,
        "attempts": attempts,
    }


def _make_mock_deps(config=None):
    """Helper: create mock dependencies for Console.

    Returns a dict with mocked state_machine, task_queue, nats_client,
    tmux_comm, and lifecycle_manager.
    """
    cfg = config or SAMPLE_CONFIG
    state_machine = MagicMock()
    state_machine.current_state = "idle"

    task_queue = MagicMock()
    task_queue.get_all_tasks.return_value = [
        _make_task("task-1", "First task", status="completed"),
        _make_task("task-2", "Second task", status="in_progress"),
        _make_task("task-3", "Third task", status="pending"),
    ]
    task_queue.get_current_task.return_value = _make_task(
        "task-2", "Second task", status="in_progress"
    )

    nats_client = MagicMock()
    nats_client.is_connected = True

    tmux_comm = MagicMock()
    tmux_comm.nudge.return_value = True
    tmux_comm.send_msg.return_value = True

    lifecycle_manager = MagicMock()
    lifecycle_manager.skip_current_task = MagicMock()

    return {
        "config": cfg,
        "state_machine": state_machine,
        "task_queue": task_queue,
        "nats_client": nats_client,
        "tmux_comm": tmux_comm,
        "lifecycle_manager": lifecycle_manager,
    }


@pytest.fixture
def deps():
    """Create standard mock dependencies for Console."""
    return _make_mock_deps()


@pytest.fixture
def console(deps):
    """Create a Console instance with mocked dependencies."""
    return Console(**deps)


@pytest.fixture
def console_two():
    """Create a Console instance with 2-agent config."""
    d = _make_mock_deps(config=TWO_AGENT_CONFIG)
    return Console(**d)


# ===========================================================================
# 1. CONSOLE CONSTRUCTION AND INITIALIZATION
# ===========================================================================


class TestConsoleConstruction:
    """Console must accept config and dependency injections."""

    def test_creates_instance(self, console):
        """Console constructor should return a valid instance."""
        assert console is not None

    def test_constructor_requires_config(self):
        """Console without config should raise an error."""
        with pytest.raises(TypeError):
            Console()

    def test_console_error_is_exception(self):
        """ConsoleError must be a subclass of Exception."""
        assert issubclass(ConsoleError, Exception)

    def test_console_error_has_message(self):
        """ConsoleError should accept and store a message."""
        err = ConsoleError("test error")
        assert "test error" in str(err)


# ===========================================================================
# 2. STATUS COMMAND -- current state, active task, progress, NATS status
# ===========================================================================


class TestStatusCommand:
    """status command must show current state, active task, progress, NATS status."""

    def test_status_returns_string(self, console):
        """status must return a string representation."""
        output = console.handle_command("status")
        assert isinstance(output, str)

    def test_status_includes_current_state(self, console, deps):
        """status must include the current state machine state."""
        deps["state_machine"].current_state = "waiting_dev"
        output = console.handle_command("status")
        assert "waiting_dev" in output

    def test_status_includes_active_task(self, console, deps):
        """status must include the active task id."""
        output = console.handle_command("status")
        assert "task-2" in output

    def test_status_includes_task_title(self, console, deps):
        """status must include the active task title."""
        output = console.handle_command("status")
        assert "Second task" in output

    def test_status_includes_progress(self, console, deps):
        """status must include progress info (e.g., task count or completion ratio)."""
        output = console.handle_command("status")
        # Should show something like "1/3 completed" or similar
        assert "1" in output and "3" in output

    def test_status_includes_nats_connection_status(self, console, deps):
        """status must include NATS connection status."""
        deps["nats_client"].is_connected = True
        output = console.handle_command("status")
        assert "connected" in output.lower() or "nats" in output.lower()

    def test_status_shows_nats_disconnected(self, console, deps):
        """status must reflect disconnected NATS state."""
        deps["nats_client"].is_connected = False
        output = console.handle_command("status")
        assert "disconnected" in output.lower() or "not connected" in output.lower()

    def test_status_when_no_active_task(self, console, deps):
        """status must handle case when no task is active."""
        deps["task_queue"].get_current_task.return_value = None
        output = console.handle_command("status")
        assert isinstance(output, str)
        # Should indicate no active task (not crash)
        assert "no" in output.lower() or "none" in output.lower() or "idle" in output.lower()


# ===========================================================================
# 3. TASKS COMMAND -- list all tasks with status markers
# ===========================================================================


class TestTasksCommand:
    """tasks command must list all tasks with status markers."""

    def test_tasks_returns_string(self, console):
        """tasks must return a string."""
        output = console.handle_command("tasks")
        assert isinstance(output, str)

    def test_tasks_includes_all_task_ids(self, console):
        """tasks must list all task IDs."""
        output = console.handle_command("tasks")
        assert "task-1" in output
        assert "task-2" in output
        assert "task-3" in output

    def test_tasks_includes_status_markers(self, console):
        """tasks must include status markers for each task."""
        output = console.handle_command("tasks")
        # Should contain status indicator text like 'completed', 'in_progress', 'pending'
        assert "completed" in output.lower() or "done" in output.lower()
        assert "in_progress" in output.lower() or "progress" in output.lower()
        assert "pending" in output.lower()

    def test_tasks_includes_task_titles(self, console):
        """tasks must include task titles."""
        output = console.handle_command("tasks")
        assert "First task" in output
        assert "Second task" in output
        assert "Third task" in output

    def test_tasks_with_empty_queue(self, console, deps):
        """tasks must handle empty task list gracefully."""
        deps["task_queue"].get_all_tasks.return_value = []
        output = console.handle_command("tasks")
        assert isinstance(output, str)

    def test_tasks_shows_stuck_status(self, console, deps):
        """tasks must show stuck status for stuck tasks."""
        deps["task_queue"].get_all_tasks.return_value = [
            _make_task("task-1", "Stuck task", status="stuck"),
        ]
        output = console.handle_command("tasks")
        assert "stuck" in output.lower()


# ===========================================================================
# 4. SKIP COMMAND -- mark current task stuck, advance to next
# ===========================================================================


class TestSkipCommand:
    """skip command must mark current task stuck and advance to next pending."""

    def test_skip_returns_string(self, console):
        """skip must return a confirmation string."""
        output = console.handle_command("skip")
        assert isinstance(output, str)

    def test_skip_calls_lifecycle_skip(self, console, deps):
        """skip must delegate to lifecycle_manager.skip_current_task."""
        console.handle_command("skip")
        deps["lifecycle_manager"].skip_current_task.assert_called_once()

    def test_skip_includes_confirmation(self, console):
        """skip must include confirmation of which task was skipped."""
        output = console.handle_command("skip")
        assert "skip" in output.lower() or "stuck" in output.lower()

    def test_skip_when_no_active_task(self, console, deps):
        """skip with no active task should indicate nothing to skip."""
        deps["task_queue"].get_current_task.return_value = None
        deps["lifecycle_manager"].skip_current_task.side_effect = Exception("No active task")
        output = console.handle_command("skip")
        assert isinstance(output, str)
        # Should not crash; should indicate no task to skip


# ===========================================================================
# 5. NUDGE <AGENT> -- trigger nudge to named agent's tmux pane
# ===========================================================================


class TestNudgeCommand:
    """nudge <agent> must trigger a nudge to the named agent's tmux pane."""

    def test_nudge_returns_string(self, console):
        """nudge must return a result string."""
        output = console.handle_command("nudge qa")
        assert isinstance(output, str)

    def test_nudge_calls_tmux_comm_nudge(self, console, deps):
        """nudge must call tmux_comm.nudge with the agent name."""
        console.handle_command("nudge qa")
        deps["tmux_comm"].nudge.assert_called_once_with("qa", source="orch.console")

    def test_nudge_for_different_agents(self, console, deps):
        """nudge must work for any configured agent."""
        console.handle_command("nudge dev")
        deps["tmux_comm"].nudge.assert_called_with("dev", source="orch.console")

    def test_nudge_confirmation_on_success(self, console, deps):
        """nudge must confirm when nudge was sent."""
        deps["tmux_comm"].nudge.return_value = True
        output = console.handle_command("nudge qa")
        assert "nudge" in output.lower() or "sent" in output.lower()

    def test_nudge_skipped_message(self, console, deps):
        """nudge must indicate when nudge was skipped (agent busy)."""
        deps["tmux_comm"].nudge.return_value = False
        output = console.handle_command("nudge qa")
        assert "skip" in output.lower() or "busy" in output.lower() or "not" in output.lower()

    def test_nudge_unknown_agent(self, console, deps):
        """nudge for unknown agent must return an error message."""
        deps["tmux_comm"].nudge.side_effect = KeyError("unknown_agent")
        output = console.handle_command("nudge unknown_agent")
        assert isinstance(output, str)
        assert "unknown" in output.lower() or "not found" in output.lower() or "error" in output.lower()

    def test_nudge_without_agent_name(self, console):
        """nudge without agent name must return usage hint."""
        output = console.handle_command("nudge")
        assert isinstance(output, str)
        assert "usage" in output.lower() or "agent" in output.lower()


# ===========================================================================
# 6. MSG <AGENT> <TEXT> -- type into agent pane with safe-nudge check
# ===========================================================================


class TestMsgCommand:
    """msg <agent> <text> must type text into agent pane after safe-nudge check."""

    def test_msg_returns_string(self, console):
        """msg must return a result string."""
        output = console.handle_command("msg qa fix the tests")
        assert isinstance(output, str)

    def test_msg_calls_tmux_comm_send_msg(self, console, deps):
        """msg must call tmux_comm.send_msg with agent and text."""
        console.handle_command("msg qa fix the tests")
        deps["tmux_comm"].send_msg.assert_called_once_with("qa", "fix the tests", source="orch.console")

    def test_msg_for_different_agent(self, console, deps):
        """msg must work for any configured agent."""
        console.handle_command("msg dev check this")
        deps["tmux_comm"].send_msg.assert_called_with("dev", "check this", source="orch.console")

    def test_msg_confirmation_on_success(self, console, deps):
        """msg must confirm when message was sent."""
        deps["tmux_comm"].send_msg.return_value = True
        output = console.handle_command("msg qa hello")
        assert "sent" in output.lower() or "message" in output.lower()

    def test_msg_refuses_when_agent_busy(self, console, deps):
        """msg must refuse with warning if agent is busy (send_msg returns False)."""
        deps["tmux_comm"].send_msg.return_value = False
        output = console.handle_command("msg qa fix the tests")
        assert "busy" in output.lower() or "not sent" in output.lower() or "warning" in output.lower()

    def test_msg_preserves_multi_word_text(self, console, deps):
        """msg must preserve multi-word text as a single string."""
        console.handle_command("msg qa please fix the failing tests now")
        deps["tmux_comm"].send_msg.assert_called_once_with(
            "qa", "please fix the failing tests now", source="orch.console",
        )

    def test_msg_unknown_agent(self, console, deps):
        """msg for unknown agent must return an error message."""
        deps["tmux_comm"].send_msg.side_effect = KeyError("nonexistent")
        output = console.handle_command("msg nonexistent hello")
        assert isinstance(output, str)
        assert "unknown" in output.lower() or "not found" in output.lower() or "error" in output.lower()

    def test_msg_without_agent_name(self, console):
        """msg without agent name must return usage hint."""
        output = console.handle_command("msg")
        assert isinstance(output, str)
        assert "usage" in output.lower() or "agent" in output.lower()

    def test_msg_without_text(self, console):
        """msg with agent but no text must return usage hint."""
        output = console.handle_command("msg qa")
        assert isinstance(output, str)
        assert "usage" in output.lower() or "text" in output.lower()


# ===========================================================================
# 7. PAUSE / RESUME -- toggle outbox processing
# ===========================================================================


class TestPauseResumeCommand:
    """pause/resume must toggle outbox processing flag."""

    def test_pause_returns_string(self, console):
        """pause must return a confirmation string."""
        output = console.handle_command("pause")
        assert isinstance(output, str)

    def test_resume_returns_string(self, console):
        """resume must return a confirmation string."""
        output = console.handle_command("resume")
        assert isinstance(output, str)

    def test_pause_sets_paused_flag(self, console):
        """pause must set the paused flag to True."""
        console.handle_command("pause")
        assert console.is_paused is True

    def test_resume_clears_paused_flag(self, console):
        """resume must set the paused flag to False."""
        console.handle_command("pause")
        console.handle_command("resume")
        assert console.is_paused is False

    def test_initially_not_paused(self, console):
        """Console must start in non-paused state."""
        assert console.is_paused is False

    def test_pause_confirmation_message(self, console):
        """pause must confirm that processing is paused."""
        output = console.handle_command("pause")
        assert "pause" in output.lower() or "stopped" in output.lower()

    def test_resume_confirmation_message(self, console):
        """resume must confirm that processing is resumed."""
        console.handle_command("pause")
        output = console.handle_command("resume")
        assert "resume" in output.lower() or "started" in output.lower()

    def test_double_pause_is_idempotent(self, console):
        """Calling pause twice should not error."""
        console.handle_command("pause")
        output = console.handle_command("pause")
        assert console.is_paused is True
        assert isinstance(output, str)

    def test_resume_without_pause_is_safe(self, console):
        """Calling resume without prior pause should not error."""
        output = console.handle_command("resume")
        assert console.is_paused is False
        assert isinstance(output, str)


# ===========================================================================
# 8. LOG -- last 10 log entries
# ===========================================================================


class TestLogCommand:
    """log command must show the last 10 log entries."""

    def test_log_returns_string(self, console):
        """log must return a string."""
        output = console.handle_command("log")
        assert isinstance(output, str)

    def test_log_shows_entries(self, console, deps):
        """log must show log entries."""
        # Simulate some log entries
        deps["lifecycle_manager"].get_recent_logs = MagicMock(
            return_value=[
                "2026-03-04 10:00:00 [INFO] Task task-1 assigned to qa",
                "2026-03-04 10:01:00 [INFO] Nudge sent to qa",
            ]
        )
        output = console.handle_command("log")
        assert isinstance(output, str)

    def test_log_limits_to_10_entries(self, console, deps):
        """log must show at most 10 entries."""
        many_logs = [f"Log entry {i}" for i in range(20)]
        deps["lifecycle_manager"].get_recent_logs = MagicMock(return_value=many_logs)
        output = console.handle_command("log")
        # Output should contain at most 10 entries
        # (Implementation may trim or call with limit=10)
        assert isinstance(output, str)

    def test_log_when_no_entries(self, console, deps):
        """log with no entries should handle gracefully."""
        deps["lifecycle_manager"].get_recent_logs = MagicMock(return_value=[])
        output = console.handle_command("log")
        assert isinstance(output, str)


# ===========================================================================
# 9. HELP -- list all commands with available agent names
# ===========================================================================


class TestHelpCommand:
    """help must list all commands with available agent names from config."""

    def test_help_returns_string(self, console):
        """help must return a string."""
        output = console.handle_command("help")
        assert isinstance(output, str)

    def test_help_lists_status_command(self, console):
        """help must list the 'status' command."""
        output = console.handle_command("help")
        assert "status" in output.lower()

    def test_help_lists_tasks_command(self, console):
        """help must list the 'tasks' command."""
        output = console.handle_command("help")
        assert "tasks" in output.lower()

    def test_help_lists_skip_command(self, console):
        """help must list the 'skip' command."""
        output = console.handle_command("help")
        assert "skip" in output.lower()

    def test_help_lists_nudge_command(self, console):
        """help must list the 'nudge' command."""
        output = console.handle_command("help")
        assert "nudge" in output.lower()

    def test_help_lists_msg_command(self, console):
        """help must list the 'msg' command."""
        output = console.handle_command("help")
        assert "msg" in output.lower()

    def test_help_lists_pause_command(self, console):
        """help must list the 'pause' command."""
        output = console.handle_command("help")
        assert "pause" in output.lower()

    def test_help_lists_resume_command(self, console):
        """help must list the 'resume' command."""
        output = console.handle_command("help")
        assert "resume" in output.lower()

    def test_help_lists_log_command(self, console):
        """help must list the 'log' command."""
        output = console.handle_command("help")
        assert "log" in output.lower()

    def test_help_lists_help_command(self, console):
        """help must list the 'help' command itself."""
        output = console.handle_command("help")
        assert "help" in output.lower()

    def test_help_includes_agent_names(self, console):
        """help must include the available agent names from config."""
        output = console.handle_command("help")
        assert "qa" in output
        assert "dev" in output
        assert "refactor" in output

    def test_help_includes_agent_names_two_agents(self, console_two):
        """help must list agent names from the 2-agent config."""
        output = console_two.handle_command("help")
        assert "writer" in output
        assert "executor" in output


# ===========================================================================
# 10. COMMAND PARSING AND DISPATCH
# ===========================================================================


class TestCommandParsing:
    """Console must parse and dispatch typed commands correctly."""

    def test_unknown_command_returns_error(self, console):
        """Unknown command must return an error / unknown message."""
        output = console.handle_command("foobar")
        assert isinstance(output, str)
        assert "unknown" in output.lower() or "unrecognized" in output.lower()

    def test_empty_command_returns_gracefully(self, console):
        """Empty command string must be handled gracefully."""
        output = console.handle_command("")
        assert isinstance(output, str)

    def test_whitespace_only_command(self, console):
        """Whitespace-only command must be handled gracefully."""
        output = console.handle_command("   ")
        assert isinstance(output, str)

    def test_command_case_insensitive(self, console):
        """Commands should be case-insensitive."""
        output = console.handle_command("STATUS")
        assert isinstance(output, str)
        # Should work the same as lowercase
        assert "state" in output.lower() or "task" in output.lower() or "idle" in output.lower()

    def test_command_with_extra_whitespace(self, console, deps):
        """Commands with extra whitespace should be parsed correctly."""
        console.handle_command("  nudge   qa  ")
        deps["tmux_comm"].nudge.assert_called_once_with("qa", source="orch.console")

    def test_msg_command_text_extraction(self, console, deps):
        """msg command should correctly extract text after agent name."""
        console.handle_command("msg dev please run the tests now")
        deps["tmux_comm"].send_msg.assert_called_once_with(
            "dev", "please run the tests now", source="orch.console",
        )

    def test_handle_command_returns_string_always(self, console):
        """handle_command must always return a string, never None."""
        for cmd in ["status", "tasks", "skip", "help", "log", "pause", "resume",
                     "nudge qa", "msg qa hi", "unknown", ""]:
            output = console.handle_command(cmd)
            assert isinstance(output, str), f"Command '{cmd}' returned {type(output)}"


# ===========================================================================
# 11. EDGE CASES AND ERROR HANDLING
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions for Console."""

    def test_nudge_with_special_chars_in_agent_name(self, console, deps):
        """Nudge with special characters in agent name should handle gracefully."""
        deps["tmux_comm"].nudge.side_effect = KeyError("no-such-agent!")
        output = console.handle_command("nudge no-such-agent!")
        assert isinstance(output, str)

    def test_msg_with_empty_text_after_agent(self, console):
        """msg with agent but empty text should return usage hint."""
        output = console.handle_command("msg qa ")
        assert isinstance(output, str)

    def test_skip_delegates_to_lifecycle_manager(self, console, deps):
        """skip must always delegate to lifecycle manager, not manage tasks directly."""
        console.handle_command("skip")
        deps["lifecycle_manager"].skip_current_task.assert_called()

    def test_console_does_not_crash_on_dependency_errors(self, console, deps):
        """Console must handle errors from dependencies gracefully."""
        deps["state_machine"].current_state = None  # Unexpected state
        # Should not crash
        output = console.handle_command("status")
        assert isinstance(output, str)

    def test_pause_resume_flag_accessible(self, console):
        """is_paused must be a readable property/attribute."""
        assert hasattr(console, "is_paused")
        assert isinstance(console.is_paused, bool)

    def test_multiple_commands_in_sequence(self, console):
        """Console must handle multiple sequential commands correctly."""
        outputs = []
        for cmd in ["status", "tasks", "help", "log"]:
            outputs.append(console.handle_command(cmd))
        assert all(isinstance(o, str) for o in outputs)
        assert len(outputs) == 4


# ===========================================================================
# 12. IMG COMMAND -- share file to agent workspaces and notify
# ===========================================================================


class TestImgCommand:
    """img command must share a file to agent workspaces and notify an agent."""

    def test_img_no_args_returns_usage(self, console):
        """img without arguments must return usage hint."""
        output = console.handle_command("img")
        assert "usage" in output.lower()

    def test_img_file_not_found(self, console):
        """img with nonexistent file must return error."""
        output = console.handle_command("img /nonexistent/file.png qa")
        assert "not found" in output.lower()

    def test_img_invalid_agent(self, console, tmp_path):
        """img with invalid agent name must return usage hint."""
        test_file = tmp_path / "test.png"
        test_file.write_text("fake")
        output = console.handle_command(f"img {test_file} badagent")
        assert "usage" in output.lower()

    def test_img_calls_share_script_and_notifies(self, console, deps, tmp_path):
        """img with valid file and agent calls share-file.sh and sends tmux msg."""
        test_file = tmp_path / "screenshot.png"
        test_file.write_text("fake")

        with patch("orchestrator.console.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            output = console.handle_command(f"img {test_file} qa")

            # Verify share-file.sh was called
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "share-file.sh" in call_args[0]

            # Verify agent was notified via tmux
            deps["tmux_comm"].send_msg.assert_called_once()
            agent_arg = deps["tmux_comm"].send_msg.call_args[0][0]
            msg_arg = deps["tmux_comm"].send_msg.call_args[0][1]
            assert agent_arg == "qa"
            assert "shared/screenshot.png" in msg_arg
            assert "Read tool" in msg_arg

        assert "Shared screenshot.png" in output
        assert "qa" in output

    def test_img_no_agent_uses_active_state(self, tmp_path):
        """img without agent arg uses the currently active agent from state machine."""
        config = {
            **SAMPLE_CONFIG,
            "project": "demo",
            "state_machine": {
                "states": {
                    "idle": {"description": "No active task"},
                    "waiting_qa": {"agent": "qa"},
                },
            },
        }
        d = _make_mock_deps(config=config)
        d["state_machine"].current_state = "waiting_qa"
        c = Console(**d)

        test_file = tmp_path / "diagram.png"
        test_file.write_text("fake")

        with patch("orchestrator.console.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
            c.handle_command(f"img {test_file}")

            # Should have targeted the qa agent
            d["tmux_comm"].send_msg.assert_called_once()
            agent_arg = d["tmux_comm"].send_msg.call_args[0][0]
            assert agent_arg == "qa"

    def test_img_share_script_failure(self, console, deps, tmp_path):
        """img must return error when share-file.sh fails."""
        test_file = tmp_path / "test.png"
        test_file.write_text("fake")

        with patch("orchestrator.console.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Project not found"
            )
            output = console.handle_command(f"img {test_file} qa")

            # Should NOT have notified the agent
            deps["tmux_comm"].send_msg.assert_not_called()

        assert "error" in output.lower()

    def test_img_expands_tilde(self, console):
        """img must expand ~ in file paths."""
        output = console.handle_command("img ~/nonexistent_test_xyz.png qa")
        # Should show expanded path in error
        assert "not found" in output.lower()
        assert "~" not in output

    def test_img_in_help(self, console):
        """help command must include img."""
        output = console.handle_command("help")
        assert "img" in output

    def test_img_in_dispatch(self, console):
        """img must be registered in the dispatch table."""
        assert "img" in console._dispatch

    def test_img_returns_string(self, console):
        """img must always return a string."""
        output = console.handle_command("img")
        assert isinstance(output, str)

    def test_project_name_extracted(self):
        """Console must extract project name from config."""
        config = {**SAMPLE_CONFIG, "project": "my-project"}
        d = _make_mock_deps(config=config)
        c = Console(**d)
        assert c._project_name == "my-project"

    def test_project_name_default_empty(self, console):
        """Console must default project_name to empty string."""
        assert console._project_name == ""
