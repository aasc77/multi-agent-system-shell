"""
Tests for orchestrator/tmux_comm.py -- tmux Communication Module

TDD Contract (RED phase):
These tests define the expected behavior of the tmux Communication Module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R6: tmux Communication (Orchestrator -> Agents)
  - R7: Interactive Orchestrator Console (msg command safe-nudge check)
  - Acceptance criteria from task rgr-6

Test categories:
  1. Pane index mapping -- agent name to 0-based pane index from config order
  2. Canonical target format -- <session_name>:agents.<pane_index>
  3. send-keys -- send text to agent pane with Enter
  4. Safe nudging -- check #{pane_current_command} before nudging
  5. Skip nudge -- foreground is not 'claude'
  6. Cooldown -- respect nudge_cooldown_seconds per agent
  7. Consecutive skip tracking -- per-agent skip counter
  8. Escalation -- flag_human after max_nudge_retries consecutive skips
  9. msg command -- same safe-nudge check, refuse with warning if busy
  10. Edge cases
"""

import time
import logging
import pytest
from unittest.mock import patch, MagicMock, call

# --- The import that MUST fail in RED phase ---
from orchestrator.tmux_comm import TmuxComm, TmuxCommError


# ---------------------------------------------------------------------------
# Fixtures
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

SINGLE_AGENT_CONFIG = {
    "tmux": {
        "session_name": "solo",
        "nudge_prompt": "Check messages now.",
        "nudge_cooldown_seconds": 10,
        "max_nudge_retries": 5,
    },
    "agents": {
        "writer": {"runtime": "claude_code", "working_dir": "/tmp/writer"},
    },
}


@pytest.fixture
def comm():
    """Create a TmuxComm instance with 3-agent config."""
    return TmuxComm(SAMPLE_CONFIG)


@pytest.fixture
def comm_two():
    """Create a TmuxComm instance with 2-agent config."""
    return TmuxComm(TWO_AGENT_CONFIG)


@pytest.fixture
def comm_single():
    """Create a TmuxComm instance with 1-agent config."""
    return TmuxComm(SINGLE_AGENT_CONFIG)


def _mock_pane_command(agent_target, process_name):
    """Helper: create a side_effect for subprocess.run to return a pane command."""
    def side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = process_name + "\n"
        result.returncode = 0
        return result
    return side_effect


# ===========================================================================
# 1. PANE INDEX MAPPING
# ===========================================================================


class TestPaneIndexMapping:
    """TmuxComm must build agent-name-to-pane-index mapping from config order."""

    def test_three_agents_mapping(self, comm):
        """3 agents in config order: qa=0, dev=1, refactor=2."""
        mapping = comm.get_pane_mapping()
        assert mapping["qa"] == 0
        assert mapping["dev"] == 1
        assert mapping["refactor"] == 2

    def test_two_agents_mapping(self, comm_two):
        """2 agents in config order: writer=0, executor=1."""
        mapping = comm_two.get_pane_mapping()
        assert mapping["writer"] == 0
        assert mapping["executor"] == 1

    def test_single_agent_mapping(self, comm_single):
        """1 agent maps to pane index 0."""
        mapping = comm_single.get_pane_mapping()
        assert mapping["writer"] == 0

    def test_mapping_has_correct_count(self, comm):
        """Mapping must contain exactly the same number of agents as config."""
        mapping = comm.get_pane_mapping()
        assert len(mapping) == 3

    def test_mapping_preserves_config_order(self):
        """Pane indices must follow the order agents appear in config."""
        # OrderedDict-style: first agent = 0, second = 1, etc.
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 30,
                "max_nudge_retries": 20,
            },
            "agents": {
                "alpha": {"runtime": "claude_code"},
                "beta": {"runtime": "claude_code"},
                "gamma": {"runtime": "claude_code"},
                "delta": {"runtime": "claude_code"},
            },
        }
        comm = TmuxComm(config)
        mapping = comm.get_pane_mapping()
        assert mapping["alpha"] == 0
        assert mapping["beta"] == 1
        assert mapping["gamma"] == 2
        assert mapping["delta"] == 3


# ===========================================================================
# 2. CANONICAL TARGET FORMAT
# ===========================================================================


class TestCanonicalTargetFormat:
    """Target format must be <session_name>:agents.<pane_index>."""

    def test_target_for_first_agent(self, comm):
        """First agent (qa) target: demo:agents.0."""
        target = comm.get_target("qa")
        assert target == "demo:agents.0"

    def test_target_for_second_agent(self, comm):
        """Second agent (dev) target: demo:agents.1."""
        target = comm.get_target("dev")
        assert target == "demo:agents.1"

    def test_target_for_third_agent(self, comm):
        """Third agent (refactor) target: demo:agents.2."""
        target = comm.get_target("refactor")
        assert target == "demo:agents.2"

    def test_target_uses_session_name_from_config(self):
        """Target must use the session_name from tmux config."""
        config = {
            "tmux": {
                "session_name": "my-project",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 30,
                "max_nudge_retries": 20,
            },
            "agents": {
                "agent1": {"runtime": "claude_code"},
            },
        }
        comm = TmuxComm(config)
        assert comm.get_target("agent1") == "my-project:agents.0"

    def test_target_for_unknown_agent_raises_error(self, comm):
        """Getting target for an unknown agent must raise an error."""
        with pytest.raises((TmuxCommError, KeyError)):
            comm.get_target("nonexistent_agent")


# ===========================================================================
# 3. SEND-KEYS (send text to agent pane)
# ===========================================================================


class TestSendKeys:
    """TmuxComm must send text to agent pane via tmux send-keys with Enter."""

    @patch("subprocess.run")
    def test_send_keys_calls_tmux(self, mock_run, comm):
        """send_keys must invoke tmux send-keys."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_keys("qa", "hello world")
        # At least one call to subprocess.run should contain 'send-keys'
        send_calls = [
            c for c in mock_run.call_args_list
            if "send-keys" in str(c)
        ]
        assert len(send_calls) >= 1

    @patch("subprocess.run")
    def test_send_keys_uses_correct_target(self, mock_run, comm):
        """send_keys must use the canonical target for the agent."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_keys("qa", "test text")
        # Should contain 'demo:agents.0' somewhere in subprocess calls
        all_args = str(mock_run.call_args_list)
        assert "demo:agents.0" in all_args

    @patch("subprocess.run")
    def test_send_keys_includes_enter(self, mock_run, comm):
        """send_keys must append Enter to the text."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_keys("qa", "some text")
        # Should have 'Enter' in the send-keys arguments
        all_args = str(mock_run.call_args_list)
        assert "Enter" in all_args

    @patch("subprocess.run")
    def test_send_keys_for_unknown_agent_raises_error(self, mock_run, comm):
        """send_keys must raise error for unknown agent name."""
        with pytest.raises((TmuxCommError, KeyError)):
            comm.send_keys("unknown_agent", "hello")


# ===========================================================================
# 4. SAFE NUDGING -- CHECK #{pane_current_command}
# ===========================================================================


class TestSafeNudging:
    """Before nudging, TmuxComm must check the foreground process."""

    @patch("subprocess.run")
    def test_nudge_checks_pane_current_command(self, mock_run, comm):
        """nudge must query #{pane_current_command} before sending."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa")
        # At least one call should contain 'pane_current_command' or 'display-message'
        all_args = str(mock_run.call_args_list)
        assert "pane_current_command" in all_args or "display-message" in all_args

    @patch("subprocess.run")
    def test_nudge_sends_when_foreground_is_claude(self, mock_run, comm):
        """Nudge must send text when foreground process is 'claude'."""
        # First call: check command -> claude; Second call: send-keys
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        result = comm.nudge("qa")
        assert result is True  # nudge was sent

    @patch("subprocess.run")
    def test_nudge_sends_configured_prompt(self, mock_run, comm):
        """Nudge must send the nudge_prompt text from config."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa")
        all_args = str(mock_run.call_args_list)
        assert "You have new messages" in all_args


# ===========================================================================
# 5. SKIP NUDGE WHEN FOREGROUND IS NOT 'claude'
# ===========================================================================


class TestSkipNudge:
    """Nudge must be skipped when foreground process is not 'claude'."""

    @patch("subprocess.run")
    def test_skip_when_foreground_is_node(self, mock_run, comm):
        """Must skip nudge when foreground process is 'node'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_python(self, mock_run, comm):
        """Must skip nudge when foreground process is 'python'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_python3(self, mock_run, comm):
        """Must skip nudge when foreground process is 'python3'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python3\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_git(self, mock_run, comm):
        """Must skip nudge when foreground process is 'git'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="git\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_npm(self, mock_run, comm):
        """Must skip nudge when foreground process is 'npm'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="npm\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_pytest(self, mock_run, comm):
        """Must skip nudge when foreground process is 'pytest'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="pytest\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_make(self, mock_run, comm):
        """Must skip nudge when foreground process is 'make'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="make\n")
        result = comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_no_send_keys_when_skipped(self, mock_run, comm):
        """When nudge is skipped, send-keys must NOT be called."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        comm.nudge("qa")
        # Only the display-message check call should exist, no send-keys
        send_keys_calls = [
            c for c in mock_run.call_args_list
            if "send-keys" in str(c)
        ]
        assert len(send_keys_calls) == 0


# ===========================================================================
# 6. COOLDOWN -- RESPECT nudge_cooldown_seconds
# ===========================================================================


class TestCooldown:
    """TmuxComm must track last-nudge timestamp per agent and respect cooldown."""

    @patch("subprocess.run")
    def test_first_nudge_always_sends(self, mock_run, comm):
        """First nudge for an agent should always be sent (no prior timestamp)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        result = comm.nudge("qa")
        assert result is True

    @patch("subprocess.run")
    def test_nudge_within_cooldown_is_skipped(self, mock_run, comm):
        """Second nudge within cooldown_seconds must be skipped."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa")  # First nudge
        result = comm.nudge("qa")  # Immediately after -- within cooldown
        assert result is False

    @patch("subprocess.run")
    def test_nudge_after_cooldown_is_sent(self, mock_run, comm):
        """Nudge after cooldown period has elapsed must be sent."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa")  # First nudge

        # Simulate time passing beyond cooldown
        comm._last_nudge_time["qa"] = time.time() - 31  # 31 seconds ago
        result = comm.nudge("qa")
        assert result is True

    @patch("subprocess.run")
    def test_cooldown_is_per_agent(self, mock_run, comm):
        """Cooldown must be tracked independently per agent."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa")  # Nudge qa

        # dev has not been nudged yet, should succeed
        result = comm.nudge("dev")
        assert result is True

    @patch("subprocess.run")
    def test_cooldown_uses_config_value(self, mock_run):
        """Cooldown must use the nudge_cooldown_seconds from config."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 5,  # Short cooldown
                "max_nudge_retries": 20,
            },
            "agents": {"a1": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")

        comm.nudge("a1")
        # Set last nudge to 6 seconds ago (beyond 5s cooldown)
        comm._last_nudge_time["a1"] = time.time() - 6
        result = comm.nudge("a1")
        assert result is True

    @patch("subprocess.run")
    def test_cooldown_boundary_exact(self, mock_run, comm):
        """At exactly the cooldown boundary, nudge should be allowed."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa")
        # Set last nudge to exactly cooldown seconds ago
        comm._last_nudge_time["qa"] = time.time() - 30
        result = comm.nudge("qa")
        assert result is True


# ===========================================================================
# 7. CONSECUTIVE SKIP TRACKING
# ===========================================================================


class TestConsecutiveSkipTracking:
    """TmuxComm must track consecutive skipped nudges per agent."""

    @patch("subprocess.run")
    def test_skip_increments_counter(self, mock_run, comm):
        """Each skipped nudge must increment the consecutive skip counter."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python\n")
        comm.nudge("qa")
        assert comm.get_consecutive_skips("qa") == 1

    @patch("subprocess.run")
    def test_multiple_skips_accumulate(self, mock_run, comm):
        """Multiple skips must accumulate."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        # Bypass cooldown by manipulating timestamps
        for i in range(5):
            comm._last_nudge_time["qa"] = 0  # Reset cooldown
            comm.nudge("qa")
        assert comm.get_consecutive_skips("qa") == 5

    @patch("subprocess.run")
    def test_successful_nudge_resets_counter(self, mock_run, comm):
        """A successful nudge must reset the consecutive skip counter to 0."""
        # First: accumulate some skips
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        for i in range(3):
            comm._last_nudge_time["qa"] = 0
            comm.nudge("qa")
        assert comm.get_consecutive_skips("qa") == 3

        # Then: successful nudge
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm._last_nudge_time["qa"] = 0
        comm.nudge("qa")
        assert comm.get_consecutive_skips("qa") == 0

    @patch("subprocess.run")
    def test_skip_counter_is_per_agent(self, mock_run, comm):
        """Skip counters must be independent per agent."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        comm._last_nudge_time["qa"] = 0
        comm.nudge("qa")
        assert comm.get_consecutive_skips("qa") == 1
        assert comm.get_consecutive_skips("dev") == 0

    @patch("subprocess.run")
    def test_initial_skip_count_is_zero(self, mock_run, comm):
        """Before any nudge attempt, skip count should be 0."""
        assert comm.get_consecutive_skips("qa") == 0
        assert comm.get_consecutive_skips("dev") == 0
        assert comm.get_consecutive_skips("refactor") == 0


# ===========================================================================
# 8. ESCALATION -- flag_human AFTER max_nudge_retries
# ===========================================================================


class TestEscalation:
    """After max_nudge_retries consecutive skips, must escalate to flag_human."""

    @patch("subprocess.run")
    def test_escalate_at_max_retries(self, mock_run):
        """Must call flag_human when consecutive skips reach max_nudge_retries."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 0,  # No cooldown for test
                "max_nudge_retries": 3,
            },
            "agents": {"writer": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")

        # Mock the flag_human callback
        flag_human_mock = MagicMock()
        comm.set_flag_human_callback(flag_human_mock)

        for _ in range(3):
            comm.nudge("writer")

        flag_human_mock.assert_called_once()

    @patch("subprocess.run")
    def test_no_escalation_below_max(self, mock_run):
        """Must NOT call flag_human when skips are below max_nudge_retries."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 0,
                "max_nudge_retries": 5,
            },
            "agents": {"writer": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")

        flag_human_mock = MagicMock()
        comm.set_flag_human_callback(flag_human_mock)

        for _ in range(4):
            comm.nudge("writer")

        flag_human_mock.assert_not_called()

    @patch("subprocess.run")
    def test_escalation_includes_agent_name(self, mock_run):
        """flag_human must be called with the stuck agent's name."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 0,
                "max_nudge_retries": 2,
            },
            "agents": {"myagent": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)
        mock_run.return_value = MagicMock(returncode=0, stdout="git\n")

        flag_human_mock = MagicMock()
        comm.set_flag_human_callback(flag_human_mock)

        for _ in range(2):
            comm.nudge("myagent")

        # Should be called with agent name
        call_args = flag_human_mock.call_args
        assert "myagent" in str(call_args)

    @patch("subprocess.run")
    def test_escalation_logs_warning(self, mock_run, caplog):
        """Escalation must emit a warning log about the stuck agent."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 0,
                "max_nudge_retries": 2,
            },
            "agents": {"writer": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)
        mock_run.return_value = MagicMock(returncode=0, stdout="npm\n")

        flag_human_mock = MagicMock()
        comm.set_flag_human_callback(flag_human_mock)

        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                comm.nudge("writer")

        # Check that a warning about the stuck agent was logged
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("writer" in msg and "stuck" in msg.lower() for msg in warning_messages), \
            f"Expected warning about 'writer' being stuck, got: {warning_messages}"

    @patch("subprocess.run")
    def test_escalation_only_fires_once(self, mock_run):
        """flag_human must be called only once, not on every subsequent skip."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 0,
                "max_nudge_retries": 2,
            },
            "agents": {"writer": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")

        flag_human_mock = MagicMock()
        comm.set_flag_human_callback(flag_human_mock)

        for _ in range(5):
            comm.nudge("writer")

        # Should only fire once at the threshold, not repeatedly
        assert flag_human_mock.call_count == 1

    @patch("subprocess.run")
    def test_escalation_resets_after_successful_nudge(self, mock_run):
        """After a successful nudge, the escalation state resets for future skips."""
        config = {
            "tmux": {
                "session_name": "test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 0,
                "max_nudge_retries": 2,
            },
            "agents": {"writer": {"runtime": "claude_code"}},
        }
        comm = TmuxComm(config)

        flag_human_mock = MagicMock()
        comm.set_flag_human_callback(flag_human_mock)

        # First: trigger escalation
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        for _ in range(2):
            comm.nudge("writer")
        assert flag_human_mock.call_count == 1

        # Then: successful nudge resets everything
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm._last_nudge_time["writer"] = 0
        comm.nudge("writer")

        # Then: accumulate skips again -- should trigger again
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        for _ in range(2):
            comm.nudge("writer")
        assert flag_human_mock.call_count == 2


# ===========================================================================
# 9. MSG COMMAND -- SAME SAFE-NUDGE CHECK
# ===========================================================================


class TestMsgCommand:
    """msg command must perform the same safe-nudge check as automated nudges."""

    @patch("subprocess.run")
    def test_msg_sends_when_foreground_is_claude(self, mock_run, comm):
        """msg must send text when foreground process is 'claude'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        result = comm.send_msg("qa", "fix the tests")
        assert result is True

    @patch("subprocess.run")
    def test_msg_refuses_when_foreground_is_not_claude(self, mock_run, comm):
        """msg must refuse when foreground process is not 'claude'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python\n")
        result = comm.send_msg("qa", "fix the tests")
        assert result is False

    @patch("subprocess.run")
    def test_msg_sends_custom_text(self, mock_run, comm):
        """msg must send the user-provided text, not the nudge_prompt."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_msg("qa", "please fix the tests")
        all_args = str(mock_run.call_args_list)
        assert "please fix the tests" in all_args

    @patch("subprocess.run")
    def test_msg_does_not_send_keys_when_busy(self, mock_run, comm):
        """When agent is busy, send_msg must NOT call tmux send-keys."""
        mock_run.return_value = MagicMock(returncode=0, stdout="git\n")
        comm.send_msg("qa", "fix the tests")
        send_keys_calls = [
            c for c in mock_run.call_args_list
            if "send-keys" in str(c)
        ]
        assert len(send_keys_calls) == 0

    @patch("subprocess.run")
    def test_msg_logs_warning_when_busy(self, mock_run, comm, caplog):
        """When agent is busy, msg must log a warning."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        with caplog.at_level(logging.WARNING):
            comm.send_msg("qa", "fix the tests")

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("busy" in msg.lower() or "not sent" in msg.lower() for msg in warning_messages), \
            f"Expected warning about agent being busy, got: {warning_messages}"

    @patch("subprocess.run")
    def test_msg_includes_enter(self, mock_run, comm):
        """msg command must append Enter to the text sent via send-keys."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_msg("qa", "run the tests")
        all_args = str(mock_run.call_args_list)
        assert "Enter" in all_args

    @patch("subprocess.run")
    def test_msg_for_unknown_agent_raises_error(self, mock_run, comm):
        """msg for unknown agent must raise error."""
        with pytest.raises((TmuxCommError, KeyError)):
            comm.send_msg("nonexistent_agent", "hello")

    @patch("subprocess.run")
    def test_msg_uses_correct_target(self, mock_run, comm):
        """msg must use the canonical target for the specified agent."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_msg("dev", "check this")
        all_args = str(mock_run.call_args_list)
        assert "demo:agents.1" in all_args


# ===========================================================================
# 10. EDGE CASES
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_tmux_comm_error_is_exception(self):
        """TmuxCommError must be a subclass of Exception."""
        assert issubclass(TmuxCommError, Exception)

    def test_tmux_comm_error_has_message(self):
        """TmuxCommError should accept and store a message."""
        err = TmuxCommError("test error")
        assert "test error" in str(err)

    def test_constructor_requires_config(self):
        """TmuxComm constructor must accept a config dict."""
        comm = TmuxComm(SAMPLE_CONFIG)
        assert comm is not None

    def test_missing_tmux_config_raises_error(self):
        """Missing 'tmux' key in config must raise an error."""
        with pytest.raises((TmuxCommError, KeyError)):
            TmuxComm({"agents": {"a": {}}})

    def test_missing_agents_config_raises_error(self):
        """Missing 'agents' key in config must raise an error."""
        with pytest.raises((TmuxCommError, KeyError)):
            TmuxComm({
                "tmux": {
                    "session_name": "test",
                    "nudge_prompt": "check",
                    "nudge_cooldown_seconds": 30,
                    "max_nudge_retries": 20,
                }
            })

    def test_empty_agents_config_raises_error(self):
        """Empty agents dict must raise an error."""
        with pytest.raises((TmuxCommError, ValueError)):
            TmuxComm({
                "tmux": {
                    "session_name": "test",
                    "nudge_prompt": "check",
                    "nudge_cooldown_seconds": 30,
                    "max_nudge_retries": 20,
                },
                "agents": {},
            })

    @patch("subprocess.run")
    def test_nudge_returns_bool(self, mock_run, comm):
        """nudge must return a boolean indicating success."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        result = comm.nudge("qa")
        assert isinstance(result, bool)

    @patch("subprocess.run")
    def test_send_msg_returns_bool(self, mock_run, comm):
        """send_msg must return a boolean indicating success."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        result = comm.send_msg("qa", "hello")
        assert isinstance(result, bool)

    @patch("subprocess.run")
    def test_tmux_command_failure_handled(self, mock_run, comm):
        """If tmux command fails (non-zero exit), TmuxComm should handle gracefully."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="session not found")
        # Should not crash -- either return False or raise TmuxCommError
        try:
            result = comm.nudge("qa")
            assert result is False
        except TmuxCommError:
            pass  # Also acceptable

    @patch("subprocess.run")
    def test_whitespace_in_pane_command_stripped(self, mock_run, comm):
        """Whitespace in pane_current_command output must be stripped."""
        mock_run.return_value = MagicMock(returncode=0, stdout="  claude  \n")
        result = comm.nudge("qa")
        assert result is True  # Should still recognize 'claude'

    @patch("subprocess.run")
    def test_nudge_for_different_agents_independent(self, mock_run, comm):
        """Nudging different agents should be independent operations."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")

        result_qa = comm.nudge("qa")
        assert result_qa is True

        result_dev = comm.nudge("dev")
        assert result_dev is True

        result_refactor = comm.nudge("refactor")
        assert result_refactor is True

    def test_get_pane_mapping_returns_dict(self, comm):
        """get_pane_mapping must return a dictionary."""
        mapping = comm.get_pane_mapping()
        assert isinstance(mapping, dict)

    def test_get_consecutive_skips_unknown_agent_returns_zero(self, comm):
        """get_consecutive_skips for untracked agent should return 0."""
        assert comm.get_consecutive_skips("qa") == 0

    @patch("subprocess.run")
    def test_msg_does_not_affect_nudge_cooldown(self, mock_run, comm):
        """send_msg should NOT update the nudge cooldown timestamp."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_msg("qa", "some text")

        # Nudge should still be possible (no cooldown from msg)
        result = comm.nudge("qa")
        assert result is True
