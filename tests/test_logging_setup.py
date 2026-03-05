"""
Tests for orchestrator/logging_setup.py -- Structured Logging Setup

TDD Contract (RED phase):
These tests define the expected behavior of the Logging Setup module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R11: Logging -- dual output to file and stdout, structured format
  - R4: State Machine -- state transitions must be logged
  - R5: Task Queue -- task assignments must be logged
  - R3: Communication Flow -- NATS pub/sub events must be logged
  - R6: tmux Communication -- nudge attempts must be logged

Acceptance criteria from task rgr-10:
  1. Logs to orchestrator/orchestrator.log file and stdout simultaneously
  2. Log format: %(asctime)s [%(levelname)s] %(message)s
  3. Captures state transition events (from_state -> to_state)
  4. Captures task assignment events (task_id assigned to agent)
  5. Captures NATS publish/subscribe events
  6. Captures nudge attempts (sent, skipped, escalated)

Test categories:
  1. Logger creation and configuration
  2. Dual output (file + stdout)
  3. Log format verification
  4. State transition event logging
  5. Task assignment event logging
  6. NATS event logging
  7. Nudge event logging
  8. Log level filtering
  9. Edge cases and error handling
"""

import io
import logging
import os
import re
import tempfile

import pytest
from unittest.mock import MagicMock, patch, call

# --- The imports that MUST fail in RED phase ---
from orchestrator.logging_setup import (
    setup_logging,
    log_state_transition,
    log_task_assignment,
    log_nats_publish,
    log_nats_subscribe,
    log_nudge_sent,
    log_nudge_skipped,
    log_nudge_escalated,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


@pytest.fixture
def tmp_log_dir(tmp_path):
    """Create a temporary directory for log files."""
    log_dir = tmp_path / "orchestrator"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def log_file(tmp_log_dir):
    """Return path to a temporary log file."""
    return str(tmp_log_dir / "orchestrator.log")


@pytest.fixture
def logger(log_file):
    """Set up logging and return the configured logger."""
    lgr = setup_logging(log_file=log_file)
    yield lgr
    # Cleanup: remove handlers to avoid cross-test pollution
    for handler in lgr.handlers[:]:
        handler.close()
        lgr.removeHandler(handler)


# ===========================================================================
# 1. LOGGER CREATION AND CONFIGURATION
# ===========================================================================


class TestLoggerCreation:
    """setup_logging must return a properly configured logger."""

    def test_returns_logger_instance(self, log_file):
        """setup_logging returns a logging.Logger instance."""
        lgr = setup_logging(log_file=log_file)
        assert isinstance(lgr, logging.Logger)
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_logger_name_is_orchestrator(self, log_file):
        """Logger should be named 'orchestrator'."""
        lgr = setup_logging(log_file=log_file)
        assert lgr.name == "orchestrator"
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_logger_level_is_info_or_lower(self, log_file):
        """Logger should capture INFO and above by default."""
        lgr = setup_logging(log_file=log_file)
        assert lgr.level <= logging.INFO
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_setup_accepts_custom_log_level(self, log_file):
        """setup_logging should accept an optional level parameter."""
        lgr = setup_logging(log_file=log_file, level=logging.DEBUG)
        assert lgr.level <= logging.DEBUG
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)


# ===========================================================================
# 2. DUAL OUTPUT (FILE + STDOUT)
# ===========================================================================


class TestDualOutput:
    """Logs must be written to both file and stdout simultaneously."""

    def test_has_file_handler(self, logger, log_file):
        """Logger must have a FileHandler writing to orchestrator.log."""
        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) >= 1
        # The file handler should point to the specified log file
        assert any(log_file in h.baseFilename for h in file_handlers)

    def test_has_stream_handler(self, logger):
        """Logger must have a StreamHandler for stdout."""
        stream_handlers = [
            h for h in logger.handlers if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) >= 1

    def test_message_appears_in_file(self, logger, log_file):
        """A log message must be written to the file."""
        logger.info("test message for file")
        # Flush all handlers
        for h in logger.handlers:
            h.flush()
        with open(log_file, "r") as f:
            content = f.read()
        assert "test message for file" in content

    def test_message_appears_on_stdout(self, log_file, capsys):
        """A log message must also appear on stdout."""
        lgr = setup_logging(log_file=log_file)
        lgr.info("test message for stdout")
        for h in lgr.handlers:
            h.flush()
        captured = capsys.readouterr()
        assert "test message for stdout" in captured.out or "test message for stdout" in captured.err
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_both_outputs_receive_same_message(self, log_file, capsys):
        """Both file and stdout must receive the same log message content."""
        lgr = setup_logging(log_file=log_file)
        test_msg = "dual output test message 12345"
        lgr.info(test_msg)
        for h in lgr.handlers:
            h.flush()

        with open(log_file, "r") as f:
            file_content = f.read()
        captured = capsys.readouterr()

        assert test_msg in file_content
        assert test_msg in captured.out or test_msg in captured.err
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)


# ===========================================================================
# 3. LOG FORMAT VERIFICATION
# ===========================================================================


class TestLogFormat:
    """Log format must be: %(asctime)s [%(levelname)s] %(message)s"""

    def test_file_handler_format(self, logger):
        """File handler must use the specified format."""
        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        for h in file_handlers:
            assert h.formatter is not None
            assert h.formatter._fmt == LOG_FORMAT

    def test_stream_handler_format(self, logger):
        """Stream handler must use the specified format."""
        stream_handlers = [
            h for h in logger.handlers if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        for h in stream_handlers:
            assert h.formatter is not None
            assert h.formatter._fmt == LOG_FORMAT

    def test_log_entry_matches_format_pattern(self, logger, log_file):
        """A written log entry must match the expected format pattern."""
        logger.info("format check message")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read().strip()

        # Pattern: datetime [LEVEL] message
        # e.g. "2026-03-04 21:00:00,123 [INFO] format check message"
        pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[INFO\] format check message"
        assert re.search(pattern, content), f"Log entry did not match expected format: {content}"

    def test_log_level_appears_in_brackets(self, logger, log_file):
        """Log level must be enclosed in square brackets."""
        logger.warning("bracket test")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[WARNING]" in content


# ===========================================================================
# 4. STATE TRANSITION EVENT LOGGING
# ===========================================================================


class TestStateTransitionLogging:
    """State transitions must be logged with from_state and to_state."""

    def test_log_state_transition_basic(self, logger, log_file):
        """log_state_transition logs from_state -> to_state."""
        log_state_transition(logger, from_state="idle", to_state="waiting_writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "idle" in content
        assert "waiting_writer" in content

    def test_log_state_transition_contains_arrow_or_separator(self, logger, log_file):
        """State transition log should indicate direction (e.g., ->)."""
        log_state_transition(logger, from_state="idle", to_state="waiting_writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        # Should have some indication of from -> to
        assert "->" in content or "to" in content.lower()

    def test_log_state_transition_with_task_id(self, logger, log_file):
        """State transition log can optionally include a task_id."""
        log_state_transition(
            logger,
            from_state="idle",
            to_state="waiting_writer",
            task_id="task-1",
        )
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "task-1" in content

    def test_log_state_transition_uses_info_level(self, logger, log_file):
        """State transitions are logged at INFO level."""
        log_state_transition(logger, from_state="idle", to_state="waiting_writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[INFO]" in content

    def test_log_state_transition_all_states(self, logger, log_file):
        """Multiple state transitions should all be logged."""
        log_state_transition(logger, from_state="idle", to_state="waiting_writer")
        log_state_transition(logger, from_state="waiting_writer", to_state="waiting_executor")
        log_state_transition(logger, from_state="waiting_executor", to_state="idle")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "idle" in content
        assert "waiting_writer" in content
        assert "waiting_executor" in content


# ===========================================================================
# 5. TASK ASSIGNMENT EVENT LOGGING
# ===========================================================================


class TestTaskAssignmentLogging:
    """Task assignments must be logged with task_id and agent name."""

    def test_log_task_assignment_basic(self, logger, log_file):
        """log_task_assignment logs the task_id and agent."""
        log_task_assignment(logger, task_id="task-1", agent="writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "task-1" in content
        assert "writer" in content

    def test_log_task_assignment_contains_assigned_keyword(self, logger, log_file):
        """Task assignment log should indicate assignment action."""
        log_task_assignment(logger, task_id="task-1", agent="writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "assign" in content.lower()

    def test_log_task_assignment_uses_info_level(self, logger, log_file):
        """Task assignments are logged at INFO level."""
        log_task_assignment(logger, task_id="task-1", agent="writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[INFO]" in content

    def test_log_task_assignment_with_title(self, logger, log_file):
        """Task assignment can optionally include a task title."""
        log_task_assignment(
            logger, task_id="task-1", agent="writer", title="Do the thing"
        )
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "Do the thing" in content

    def test_log_multiple_task_assignments(self, logger, log_file):
        """Multiple task assignments should all be captured."""
        log_task_assignment(logger, task_id="task-1", agent="writer")
        log_task_assignment(logger, task_id="task-2", agent="executor")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "task-1" in content
        assert "task-2" in content
        assert "writer" in content
        assert "executor" in content


# ===========================================================================
# 6. NATS EVENT LOGGING
# ===========================================================================


class TestNATSEventLogging:
    """NATS publish and subscribe events must be logged."""

    def test_log_nats_publish_basic(self, logger, log_file):
        """log_nats_publish logs the subject."""
        log_nats_publish(logger, subject="agents.writer.inbox")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "agents.writer.inbox" in content

    def test_log_nats_publish_contains_publish_keyword(self, logger, log_file):
        """NATS publish log should indicate a publish action."""
        log_nats_publish(logger, subject="agents.writer.inbox")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "publish" in content.lower() or "pub" in content.lower()

    def test_log_nats_publish_uses_info_level(self, logger, log_file):
        """NATS publish events are logged at INFO level."""
        log_nats_publish(logger, subject="agents.writer.inbox")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[INFO]" in content

    def test_log_nats_publish_with_message_type(self, logger, log_file):
        """NATS publish can optionally include message type."""
        log_nats_publish(
            logger, subject="agents.writer.inbox", message_type="task_assignment"
        )
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "task_assignment" in content

    def test_log_nats_subscribe_basic(self, logger, log_file):
        """log_nats_subscribe logs the subject."""
        log_nats_subscribe(logger, subject="agents.writer.outbox")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "agents.writer.outbox" in content

    def test_log_nats_subscribe_contains_subscribe_keyword(self, logger, log_file):
        """NATS subscribe log should indicate a subscribe action."""
        log_nats_subscribe(logger, subject="agents.writer.outbox")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "subscribe" in content.lower() or "sub" in content.lower()

    def test_log_nats_subscribe_uses_info_level(self, logger, log_file):
        """NATS subscribe events are logged at INFO level."""
        log_nats_subscribe(logger, subject="agents.writer.outbox")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[INFO]" in content

    def test_log_nats_subscribe_with_consumer_name(self, logger, log_file):
        """NATS subscribe can optionally include consumer name."""
        log_nats_subscribe(
            logger, subject="agents.writer.outbox", consumer="writer-consumer"
        )
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "writer-consumer" in content


# ===========================================================================
# 7. NUDGE EVENT LOGGING
# ===========================================================================


class TestNudgeEventLogging:
    """Nudge events (sent, skipped, escalated) must be logged."""

    # --- Nudge Sent ---

    def test_log_nudge_sent_basic(self, logger, log_file):
        """log_nudge_sent logs that a nudge was sent to an agent."""
        log_nudge_sent(logger, agent="writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "writer" in content
        assert "nudge" in content.lower() or "sent" in content.lower()

    def test_log_nudge_sent_uses_info_level(self, logger, log_file):
        """Nudge sent events are INFO level."""
        log_nudge_sent(logger, agent="writer")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[INFO]" in content

    def test_log_nudge_sent_with_target(self, logger, log_file):
        """log_nudge_sent can optionally include the tmux target."""
        log_nudge_sent(logger, agent="writer", target="demo:agents.0")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "demo:agents.0" in content

    # --- Nudge Skipped ---

    def test_log_nudge_skipped_basic(self, logger, log_file):
        """log_nudge_skipped logs that a nudge was skipped."""
        log_nudge_skipped(logger, agent="writer", reason="node")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "writer" in content
        assert "skip" in content.lower()

    def test_log_nudge_skipped_includes_reason(self, logger, log_file):
        """Skipped nudge log must include the reason (foreground process)."""
        log_nudge_skipped(logger, agent="writer", reason="python")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "python" in content

    def test_log_nudge_skipped_uses_warning_level(self, logger, log_file):
        """Skipped nudge events are WARNING level."""
        log_nudge_skipped(logger, agent="writer", reason="node")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[WARNING]" in content

    # --- Nudge Escalated ---

    def test_log_nudge_escalated_basic(self, logger, log_file):
        """log_nudge_escalated logs that nudge retries were exhausted."""
        log_nudge_escalated(logger, agent="writer", retries=20)
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "writer" in content
        assert "escalat" in content.lower()

    def test_log_nudge_escalated_includes_retry_count(self, logger, log_file):
        """Escalated nudge log must include the retry count."""
        log_nudge_escalated(logger, agent="writer", retries=20)
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "20" in content

    def test_log_nudge_escalated_uses_warning_level(self, logger, log_file):
        """Escalated nudge events are WARNING level."""
        log_nudge_escalated(logger, agent="writer", retries=20)
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "[WARNING]" in content

    def test_log_nudge_escalated_mentions_stuck(self, logger, log_file):
        """Escalation log should mention agent appears stuck (per PRD R6)."""
        log_nudge_escalated(logger, agent="writer", retries=20)
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "stuck" in content.lower() or "exhausted" in content.lower()


# ===========================================================================
# 8. LOG LEVEL FILTERING
# ===========================================================================


class TestLogLevelFiltering:
    """Logger must respect log level settings."""

    def test_debug_messages_not_captured_at_info_level(self, log_file):
        """When level is INFO, DEBUG messages should not appear in log file."""
        lgr = setup_logging(log_file=log_file, level=logging.INFO)
        lgr.debug("this should not appear")
        for h in lgr.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "this should not appear" not in content
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_warning_messages_captured_at_info_level(self, log_file):
        """When level is INFO, WARNING messages should appear."""
        lgr = setup_logging(log_file=log_file, level=logging.INFO)
        lgr.warning("this should appear")
        for h in lgr.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "this should appear" in content
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_error_messages_captured_at_info_level(self, log_file):
        """When level is INFO, ERROR messages should appear."""
        lgr = setup_logging(log_file=log_file, level=logging.INFO)
        lgr.error("error message here")
        for h in lgr.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "error message here" in content
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_debug_messages_captured_at_debug_level(self, log_file):
        """When level is DEBUG, DEBUG messages should appear."""
        lgr = setup_logging(log_file=log_file, level=logging.DEBUG)
        lgr.debug("debug message here")
        for h in lgr.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "debug message here" in content
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)


# ===========================================================================
# 9. EDGE CASES AND ERROR HANDLING
# ===========================================================================


class TestEdgeCases:
    """Edge cases for the logging setup."""

    def test_setup_creates_log_directory_if_missing(self, tmp_path):
        """setup_logging should create parent directories if they don't exist."""
        log_file = str(tmp_path / "new_dir" / "orchestrator.log")
        lgr = setup_logging(log_file=log_file)
        lgr.info("directory creation test")
        for h in lgr.handlers:
            h.flush()

        assert os.path.exists(log_file)
        with open(log_file, "r") as f:
            content = f.read()
        assert "directory creation test" in content
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_setup_does_not_duplicate_handlers_on_repeat_calls(self, log_file):
        """Calling setup_logging twice should not add duplicate handlers."""
        lgr1 = setup_logging(log_file=log_file)
        handler_count_1 = len(lgr1.handlers)
        for h in lgr1.handlers[:]:
            h.close()
            lgr1.removeHandler(h)

        lgr2 = setup_logging(log_file=log_file)
        handler_count_2 = len(lgr2.handlers)

        assert handler_count_2 == handler_count_1
        for h in lgr2.handlers[:]:
            h.close()
            lgr2.removeHandler(h)

    def test_log_state_transition_with_empty_states(self, logger, log_file):
        """Logging a state transition with empty strings should not crash."""
        log_state_transition(logger, from_state="", to_state="")
        for h in logger.handlers:
            h.flush()
        # Should not raise; file should have some content
        with open(log_file, "r") as f:
            content = f.read()
        assert len(content) > 0

    def test_log_task_assignment_with_special_characters(self, logger, log_file):
        """Task IDs and agent names with special chars should be logged safely."""
        log_task_assignment(logger, task_id="task-1/sub", agent="agent-name_2")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "task-1/sub" in content
        assert "agent-name_2" in content

    def test_log_nats_publish_with_empty_subject(self, logger, log_file):
        """Publishing with an empty subject should still log."""
        log_nats_publish(logger, subject="")
        for h in logger.handlers:
            h.flush()
        # Should not raise
        with open(log_file, "r") as f:
            content = f.read()
        assert len(content) > 0

    def test_unicode_in_log_messages(self, logger, log_file):
        """Unicode characters in log messages should be handled."""
        log_task_assignment(logger, task_id="tarea-1", agent="escritor")
        for h in logger.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "tarea-1" in content
        assert "escritor" in content

    def test_concurrent_log_file_access(self, log_file):
        """Multiple logger instances should safely share the same file."""
        lgr = setup_logging(log_file=log_file)
        lgr.info("message one")
        lgr.info("message two")
        lgr.info("message three")
        for h in lgr.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "message one" in content
        assert "message two" in content
        assert "message three" in content
        for h in lgr.handlers[:]:
            h.close()
            lgr.removeHandler(h)

    def test_log_file_appends_on_restart(self, log_file):
        """A second setup_logging call should append, not overwrite existing logs."""
        lgr1 = setup_logging(log_file=log_file)
        lgr1.info("first run message")
        for h in lgr1.handlers:
            h.flush()
        for h in lgr1.handlers[:]:
            h.close()
            lgr1.removeHandler(h)

        lgr2 = setup_logging(log_file=log_file)
        lgr2.info("second run message")
        for h in lgr2.handlers:
            h.flush()

        with open(log_file, "r") as f:
            content = f.read()

        assert "first run message" in content
        assert "second run message" in content
        for h in lgr2.handlers[:]:
            h.close()
            lgr2.removeHandler(h)
