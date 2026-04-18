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
from orchestrator.tmux_comm import (
    AgentPaneState,
    TmuxComm,
    TmuxCommError,
    _AGENT_STATE_CAPTURE_LINES,
    _STALE_DEBOUNCE_CYCLES,
)


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

SCRIPT_AGENTS_CONFIG = {
    "tmux": {
        "session_name": "demo",
        "nudge_prompt": "You have new messages. Use check_messages with your role.",
        "nudge_cooldown_seconds": 30,
        "max_nudge_retries": 20,
    },
    "agents": {
        "qa": {"runtime": "script", "command": "python3 agent.py --role qa"},
        "dev": {"runtime": "script", "command": "python3 agent.py --role dev"},
        "refactor": {"runtime": "script", "command": "python3 agent.py --role refactor"},
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


@pytest.fixture
def script_comm():
    """Create a TmuxComm instance with script-only agents (busy-check applies)."""
    return TmuxComm(SCRIPT_AGENTS_CONFIG)


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

    def test_monitor_agent_excluded_from_pane_mapping(self):
        """Manager agent with role=monitor must not get a pane index.

        start.sh launches monitor agents in the control window, not the
        agents window.  If TmuxComm includes them in the mapping the
        indices for all subsequent agents are off by one.
        """
        config = {
            "tmux": {
                "session_name": "remote-test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 30,
                "max_nudge_retries": 20,
            },
            "agents": {
                "manager": {"runtime": "claude_code", "role": "monitor"},
                "hub": {"runtime": "claude_code"},
                "dgx": {"runtime": "claude_code", "ssh_host": "dgx@10.0.0.1"},
                "macmini": {"runtime": "claude_code", "ssh_host": "user@10.0.0.2"},
                "hassio": {"runtime": "claude_code", "ssh_host": "hassio@10.0.0.3"},
            },
        }
        comm = TmuxComm(config)
        mapping = comm.get_pane_mapping()
        assert "manager" not in mapping
        assert mapping["hub"] == 0
        assert mapping["dgx"] == 1
        assert mapping["macmini"] == 2
        assert mapping["hassio"] == 3


# ===========================================================================
# 1b. REGULAR-AGENT @LABEL RESOLUTION (#51 / #68)
# ===========================================================================


class TestRegularAgentLabelResolution:
    """#68: regular-agent pane mapping must come from tmux @labels on
    the agents window, not config-order enumeration. Config-order
    silently misrouted every NUDGE once the live tmux layout drifted
    from config order (continuum restore, manual splits, etc.).

    Each test mocks ``_scan_window_pane_labels`` to simulate a given
    live tmux state and asserts the resulting pane mapping.
    """

    def _base_config(self):
        return {
            "tmux": {
                "session_name": "remote-test",
                "nudge_prompt": "check",
                "nudge_cooldown_seconds": 30,
                "max_nudge_retries": 20,
            },
            "agents": {
                "hub": {"runtime": "claude_code", "label": "dev"},
                "macmini": {"runtime": "claude_code", "label": "macmini (qa)"},
                "hassio": {"runtime": "claude_code"},
                "RTX5090": {"runtime": "claude_code", "label": "RTX5090"},
                "dgx": {"runtime": "claude_code", "label": "dgx1"},
                "dgx2": {"runtime": "claude_code"},
            },
        }

    def test_label_field_wins_over_name(self):
        """When agent has a configured ``label`` and a pane with that
        @label exists, that pane index wins — regardless of the
        agent's position in config or the agent's own name."""
        from unittest.mock import patch
        label_map = {
            # Simulate the drifted real-world layout from #68:
            # [hub, hassio, dgx1, macmini (qa), RTX5090, dgx2]
            "dev": 0,
            "hassio": 1,
            "dgx1": 2,
            "macmini (qa)": 3,
            "RTX5090": 4,
            "dgx2": 5,
        }
        with patch.object(
            TmuxComm, "_scan_window_pane_labels",
            return_value=label_map,
        ):
            comm = TmuxComm(self._base_config())
        mapping = comm.get_pane_mapping()
        # This is the exact misrouting scenario from #68 — macmini
        # must land on pane 3, not pane 1 (hassio's pane).
        assert mapping["macmini"] == 3, (
            "macmini must resolve to its real @label pane index 3, "
            f"not config-order index 1; got {mapping['macmini']}"
        )
        assert mapping["hub"] == 0
        assert mapping["hassio"] == 1
        assert mapping["dgx"] == 2
        assert mapping["RTX5090"] == 4
        assert mapping["dgx2"] == 5

    def test_name_fallback_when_no_label_field(self):
        """An agent without a configured ``label`` falls back to
        matching against its own name in the label map. Exercises
        the hassio + dgx2 case from the real config."""
        from unittest.mock import patch
        label_map = {
            "dev": 0,
            "hassio": 1,
            "dgx1": 2,
            "macmini (qa)": 3,
            "RTX5090": 4,
            "dgx2": 5,
        }
        with patch.object(
            TmuxComm, "_scan_window_pane_labels",
            return_value=label_map,
        ):
            comm = TmuxComm(self._base_config())
        mapping = comm.get_pane_mapping()
        # hassio and dgx2 have no `label` — must match on name.
        assert mapping["hassio"] == 1
        assert mapping["dgx2"] == 5

    def test_config_order_fallback_when_tmux_query_fails(self):
        """No live tmux (unit tests, pre-session boot): scan returns
        empty dict → every agent falls back to config-order index.
        This preserves the pre-#68 test contract."""
        from unittest.mock import patch
        with patch.object(
            TmuxComm, "_scan_window_pane_labels",
            return_value={},
        ):
            comm = TmuxComm(self._base_config())
        mapping = comm.get_pane_mapping()
        assert mapping["hub"] == 0
        assert mapping["macmini"] == 1
        assert mapping["hassio"] == 2
        assert mapping["RTX5090"] == 3
        assert mapping["dgx"] == 4
        assert mapping["dgx2"] == 5

    def test_partial_label_match_uses_label_for_some_falls_back_for_others(self):
        """If only some agents' labels appear in the map, those get
        their live index and everybody else falls back to config
        order. Covers mid-boot partial-registration edge cases."""
        from unittest.mock import patch
        # Only macmini's label is registered.
        label_map = {"macmini (qa)": 3}
        with patch.object(
            TmuxComm, "_scan_window_pane_labels",
            return_value=label_map,
        ):
            comm = TmuxComm(self._base_config())
        mapping = comm.get_pane_mapping()
        assert mapping["macmini"] == 3       # from label
        assert mapping["hub"] == 0            # config order
        assert mapping["hassio"] == 2         # config order
        assert mapping["RTX5090"] == 3        # config order — collides with macmini-by-label, by design (map is name→idx, not idx→name)
        assert mapping["dgx"] == 4
        assert mapping["dgx2"] == 5

    def test_monitors_still_excluded_from_pane_mapping(self):
        """Regression guard: #68's label resolution must not change
        the #42/#51 monitors-excluded-from-agents-window invariant."""
        from unittest.mock import patch
        cfg = self._base_config()
        cfg["agents"] = {
            "manager": {"runtime": "claude_code", "role": "monitor"},
            **cfg["agents"],
        }
        with patch.object(
            TmuxComm, "_scan_window_pane_labels",
            return_value={"manager": 0, "dev": 0},
        ):
            comm = TmuxComm(cfg)
        mapping = comm.get_pane_mapping()
        assert "manager" not in mapping, (
            "monitors must NOT appear in the regular-agent pane mapping"
        )

    def test_scan_agents_uses_agents_window(self):
        """Regression guard: the agents-window scanner must target
        ``<session>:agents``, not the control window."""
        from unittest.mock import patch, MagicMock
        cfg = self._base_config()
        captured = {}
        def _fake_scan(self, window):
            captured["window"] = window
            return {}
        with patch.object(
            TmuxComm, "_scan_window_pane_labels", _fake_scan,
        ):
            TmuxComm(cfg)
        # __init__ calls both scans (agents + control). The agents
        # scan should be first; we assert the last captured window
        # is either "control" or "agents", then separately check
        # the agents method exists by invoking it.
        comm2 = TmuxComm(cfg)
        # Direct: the agents-window scanner must call the underlying
        # helper with "agents".
        with patch.object(
            comm2, "_scan_window_pane_labels", return_value={},
        ) as mock_scan:
            comm2._scan_agents_pane_labels()
            mock_scan.assert_called_once_with("agents")


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
# 3b. NUDGE TRACEABILITY PREFIX (#66)
# ===========================================================================


class TestNudgeTraceabilityPrefix:
    """#66: every outbound pane nudge must prepend
    ``[<ISO-8601 UTC> <source>] `` to the text tmux actually sees,
    so operators can correlate a pane event to the exact caller.
    """

    # #82: timestamp resolution is now seconds-only. NUDGE retries on
    # the same agent are spaced ≥ 1 second apart by cooldown, so ms
    # precision never disambiguated anything in practice and made
    # each pane line ~4 characters longer.
    _PREFIX_RE = (
        r"^\[20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "
        r"[a-z0-9._-]+\] "
    )

    @patch("subprocess.run")
    def test_send_keys_prepends_timestamp_and_source(self, mock_run, comm):
        import re
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_keys("qa", "hello", source="orch.delivery")
        # Extract the text argument passed to subprocess.run for
        # the send-keys call (tmux args = [tmux, send-keys, -t,
        # target, <text>]).
        send_calls = [
            c for c in mock_run.call_args_list
            if any("send-keys" in str(a) for a in c[0])
        ]
        assert send_calls, "no send-keys call observed"
        # Find the first call whose argv includes our expected text
        # prefix form.
        texts = []
        for call in send_calls:
            argv = call[0][0]
            if "send-keys" in argv and len(argv) >= 5:
                texts.append(argv[-1])
        assert any(
            re.match(self._PREFIX_RE, t) and "hello" in t
            for t in texts
        ), (
            "expected a send-keys text matching "
            f"{self._PREFIX_RE!r} + 'hello'; got {texts!r}"
        )
        assert any("orch.delivery" in t for t in texts), (
            f"source tag not in outgoing text; got {texts!r}"
        )

    @patch("subprocess.run")
    def test_send_keys_default_source_is_unknown(self, mock_run, comm):
        """Backwards-compat: callers that don't pass ``source``
        get ``[<ts> unknown]`` so the prefix is always present
        without breaking legacy tests that call
        ``send_keys(agent, text)`` positionally."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_keys("qa", "legacy")
        texts = [
            call[0][0][-1]
            for call in mock_run.call_args_list
            if "send-keys" in str(call) and len(call[0][0]) >= 5
        ]
        assert any("unknown" in t and "legacy" in t for t in texts), (
            f"default source must be 'unknown'; got {texts!r}"
        )

    @patch("subprocess.run")
    def test_nudge_forwards_source_to_send_keys(self, mock_run, comm):
        """``nudge(source=...)`` must thread the tag all the way
        through to the composed pane text."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.nudge("qa", force=True, source="orch.watchdog")
        texts = [
            call[0][0][-1]
            for call in mock_run.call_args_list
            if "send-keys" in str(call) and len(call[0][0]) >= 5
        ]
        assert any("orch.watchdog" in t for t in texts), (
            f"nudge source must appear in the pane text; got {texts!r}"
        )

    @patch("subprocess.run")
    def test_send_msg_forwards_source_to_send_keys(self, mock_run, comm):
        """Same wiring for ``send_msg``."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        comm.send_msg("qa", "note", source="orch.console")
        texts = [
            call[0][0][-1]
            for call in mock_run.call_args_list
            if "send-keys" in str(call) and len(call[0][0]) >= 5
        ]
        assert any(
            "orch.console" in t and "note" in t for t in texts
        ), f"send_msg source must appear in the pane text; got {texts!r}"

    def test_format_nudge_prefix_is_iso8601_seconds_utc(self):
        """``_format_nudge_prefix`` must produce the exact #82
        format: ``[YYYY-MM-DDTHH:MM:SSZ <source>] `` (seconds-only,
        no millisecond component)."""
        import re
        from orchestrator.tmux_comm import _format_nudge_prefix
        prefix = _format_nudge_prefix("orch.delivery")
        assert re.match(
            r"^\[20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "
            r"orch\.delivery\] $",
            prefix,
        ), f"prefix shape wrong: {prefix!r}"

    def test_format_nudge_prefix_has_no_millisecond_component(self):
        """#82 regression guard: the prefix must NOT contain a
        ``.mmm`` decimal component before the ``Z``. Pre-#82 the
        format was ``HH:MM:SS.mmmZ`` — this test pins the rollback
        so a well-meaning future change can't silently re-introduce
        the ms component (which bloated every operator-facing log
        line by ~4 characters)."""
        import re
        from orchestrator.tmux_comm import _format_nudge_prefix
        prefix = _format_nudge_prefix("orch.watchdog")
        # No `.<digits>Z` fragment allowed anywhere in the prefix.
        assert not re.search(r"\.\d+Z", prefix), (
            f"prefix must not carry ms precision; got {prefix!r}"
        )

    @patch("subprocess.run")
    def test_send_keys_logs_composed_message(self, mock_run, comm, caplog):
        """#66 debuggability payoff: the composed pane text must
        be logged alongside the nudge so ``grep <timestamp>`` in
        ``orchestrator.log`` surfaces the exact outgoing message.
        """
        import logging as _logging
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        with caplog.at_level(_logging.INFO, logger="orchestrator.tmux_comm"):
            comm.send_keys("qa", "hello", source="orch.delivery")
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "orch.delivery" in logged
        assert "hello" in logged
        assert "source=orch.delivery" in logged


# ===========================================================================
# 4. SAFE NUDGING -- CHECK #{pane_current_command}
# ===========================================================================


class TestSafeNudging:
    """Before nudging, TmuxComm must check the foreground process (script agents)."""

    @patch("subprocess.run")
    def test_nudge_checks_pane_current_command(self, mock_run, script_comm):
        """nudge must query #{pane_current_command} before sending (script agents)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        script_comm.nudge("qa")
        # At least one call should contain 'pane_current_command' or 'display-message'
        all_args = str(mock_run.call_args_list)
        assert "pane_current_command" in all_args or "display-message" in all_args

    @patch("subprocess.run")
    def test_nudge_sends_when_foreground_is_claude(self, mock_run, script_comm):
        """Nudge must send text when foreground process is 'claude' (script agents)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        result = script_comm.nudge("qa")
        assert result is True  # nudge was sent

    @patch("subprocess.run")
    def test_nudge_sends_configured_prompt(self, mock_run, script_comm):
        """Nudge must send the nudge_prompt text from config."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        script_comm.nudge("qa")
        all_args = str(mock_run.call_args_list)
        assert "You have new messages" in all_args

    @patch("subprocess.run")
    def test_claude_code_agents_skip_busy_check(self, mock_run, comm):
        """Claude Code agents must skip busy-check and always nudge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        result = comm.nudge("qa")  # qa is claude_code in SAMPLE_CONFIG
        assert result is True  # nudge sent despite node foreground


# ===========================================================================
# 5. SKIP NUDGE WHEN FOREGROUND IS NOT 'claude'
# ===========================================================================


class TestSkipNudge:
    """Nudge must be skipped when foreground process is not 'claude' (script agents)."""

    @patch("subprocess.run")
    def test_skip_when_foreground_is_node(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'node'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_python(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'python'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_python3(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'python3'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python3\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_git(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'git'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="git\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_npm(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'npm'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="npm\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_pytest(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'pytest'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="pytest\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_skip_when_foreground_is_make(self, mock_run, script_comm):
        """Must skip nudge when foreground process is 'make'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="make\n")
        result = script_comm.nudge("qa")
        assert result is False

    @patch("subprocess.run")
    def test_no_send_keys_when_skipped(self, mock_run, script_comm):
        """When nudge is skipped, send-keys must NOT be called."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        script_comm.nudge("qa")
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
    """TmuxComm must track consecutive skipped nudges per agent (script agents)."""

    @patch("subprocess.run")
    def test_skip_increments_counter(self, mock_run, script_comm):
        """Each skipped nudge must increment the consecutive skip counter."""
        mock_run.return_value = MagicMock(returncode=0, stdout="python\n")
        script_comm.nudge("qa")
        assert script_comm.get_consecutive_skips("qa") == 1

    @patch("subprocess.run")
    def test_multiple_skips_accumulate(self, mock_run, script_comm):
        """Multiple skips must accumulate."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        # Bypass cooldown by manipulating timestamps
        for i in range(5):
            script_comm._last_nudge_time["qa"] = 0  # Reset cooldown
            script_comm.nudge("qa")
        assert script_comm.get_consecutive_skips("qa") == 5

    @patch("subprocess.run")
    def test_successful_nudge_resets_counter(self, mock_run, script_comm):
        """A successful nudge must reset the consecutive skip counter to 0."""
        # First: accumulate some skips
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        for i in range(3):
            script_comm._last_nudge_time["qa"] = 0
            script_comm.nudge("qa")
        assert script_comm.get_consecutive_skips("qa") == 3

        # Then: successful nudge
        mock_run.return_value = MagicMock(returncode=0, stdout="claude\n")
        script_comm._last_nudge_time["qa"] = 0
        script_comm.nudge("qa")
        assert script_comm.get_consecutive_skips("qa") == 0

    @patch("subprocess.run")
    def test_skip_counter_is_per_agent(self, mock_run, script_comm):
        """Skip counters must be independent per agent."""
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n")
        script_comm._last_nudge_time["qa"] = 0
        script_comm.nudge("qa")
        assert script_comm.get_consecutive_skips("qa") == 1
        assert script_comm.get_consecutive_skips("dev") == 0

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
            "agents": {"writer": {"runtime": "script", "command": "echo"}},
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
            "agents": {"writer": {"runtime": "script", "command": "echo"}},
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
            "agents": {"myagent": {"runtime": "script", "command": "echo"}},
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
            "agents": {"writer": {"runtime": "script", "command": "echo"}},
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
            "agents": {"writer": {"runtime": "script", "command": "echo"}},
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
            "agents": {"writer": {"runtime": "script", "command": "echo"}},
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
    def test_tmux_command_failure_handled(self, mock_run, script_comm):
        """If tmux command fails (non-zero exit), TmuxComm should handle gracefully."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="session not found")
        # Should not crash -- either return False or raise TmuxCommError
        try:
            result = script_comm.nudge("qa")
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


# ---------------------------------------------------------------------------
# 11. Agent pane state (issue #42)
# ---------------------------------------------------------------------------


def _patch_capture(comm: TmuxComm, captures):
    """Replace comm.capture_pane with a side_effect sequence.

    *captures* is an iterable of return values. Each call to
    ``capture_pane(agent, lines=...)`` yields the next value. When
    the iterable is exhausted, subsequent calls raise StopIteration
    — tests should provide enough values for all expected calls.
    """
    it = iter(captures)

    def _side_effect(agent, lines=10):
        return next(it)

    comm.capture_pane = MagicMock(side_effect=_side_effect)


class TestGetPaneStateWorking:
    """Hash-changed → WORKING branch."""

    def test_first_cycle_returns_working_no_prior_hash(self, comm):
        """Very first call for an agent has no prior hash to compare
        against. Must return WORKING as the least-alarming default,
        NOT UNKNOWN — we have no information to flag on.
        """
        _patch_capture(comm, ["✻ Scurrying… (3s · ↓ 12 tokens)\n"])
        state = comm.get_pane_state("qa")
        assert state == AgentPaneState.WORKING

    def test_changed_hash_returns_working(self, comm):
        """Two consecutive captures with different content → WORKING
        on the second (hash changed = active work ticking).
        """
        _patch_capture(comm, [
            "✻ Scurrying… (3s · ↓ 12 tokens)\n",
            "✻ Scurrying… (4s · ↓ 18 tokens)\n",
        ])
        comm.get_pane_state("qa")
        state = comm.get_pane_state("qa")
        assert state == AgentPaneState.WORKING

    def test_tool_exec_running_animation_ticks_as_working(self, comm):
        """Real-world bash-tool-call phase: Running… (Xs · timeout Ys)
        time counter increments → pane hash changes each cycle →
        WORKING.
        """
        _patch_capture(comm, [
            "⎿  Running… (3s · timeout 15s)\n  (ctrl+b ctrl+b hint)\n",
            "⎿  Running… (4s · timeout 15s)\n  (ctrl+b ctrl+b hint)\n",
        ])
        comm.get_pane_state("qa")
        state = comm.get_pane_state("qa")
        assert state == AgentPaneState.WORKING


class TestGetPaneStateIdle:
    """Hash-unchanged + ❯ present → IDLE branch."""

    def test_unchanged_pane_with_prompt_returns_idle(self, comm):
        """Agent parked at the ❯ prompt, pane unchanged between
        cycles → IDLE (existing watchdog behavior preserved).
        """
        idle_pane = "banner\n❯ \n  footer line\n"
        _patch_capture(comm, [idle_pane, idle_pane])
        comm.get_pane_state("qa")
        state = comm.get_pane_state("qa")
        assert state == AgentPaneState.IDLE

    def test_idle_then_activity_returns_working(self, comm):
        """Idle → activity transition: prior IDLE cycle, then the
        hash changes → WORKING. Pins the reset-on-change semantics.
        """
        _patch_capture(comm, [
            "❯ \n",
            "❯ \n",
            "⎿ Running… (1s)\n❯ \n",
        ])
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING  # cycle 1: first, no prior
        assert comm.get_pane_state("qa") == AgentPaneState.IDLE     # cycle 2: unchanged + prompt
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING  # cycle 3: hash changed


class TestGetPaneStateUnknown:
    """Hash-unchanged + no ❯ + debounced → UNKNOWN branch."""

    def test_first_stale_cycle_returns_working_benefit_of_doubt(self, comm):
        """One stale capture is debounced. Single cycle where the
        pane is unchanged and has no prompt → WORKING, not UNKNOWN.
        """
        # bash prompt, no ❯, repeated. First call establishes the
        # baseline hash; second call is the first stale observation.
        _patch_capture(comm, [
            "user@host:~$ \n",
            "user@host:~$ \n",
        ])
        comm.get_pane_state("qa")  # baseline
        state = comm.get_pane_state("qa")  # first stale — still WORKING
        assert state == AgentPaneState.WORKING

    def test_second_consecutive_stale_returns_unknown(self, comm):
        """Two consecutive stale cycles → flagged UNKNOWN."""
        _patch_capture(comm, [
            "user@host:~$ \n",  # baseline
            "user@host:~$ \n",  # stale 1 → WORKING (benefit of doubt)
            "user@host:~$ \n",  # stale 2 → UNKNOWN
        ])
        comm.get_pane_state("qa")  # baseline
        comm.get_pane_state("qa")  # stale 1 (WORKING)
        state = comm.get_pane_state("qa")  # stale 2 (UNKNOWN)
        assert state == AgentPaneState.UNKNOWN

    def test_crashed_agent_with_stack_trace_detected(self, comm):
        """Real-world failure: python agent crashed with stack trace
        visible in pane but no ❯. Pre-#42 is_agent_idle would return
        False here and the old watchdog would silently consider this
        'working'. Post-#42 must flag it UNKNOWN after debounce.
        """
        crash_pane = (
            "Traceback (most recent call last):\n"
            '  File "agent.py", line 42\n'
            "KeyError: 'missing'\n"
        )
        _patch_capture(comm, [crash_pane, crash_pane, crash_pane])
        comm.get_pane_state("qa")
        comm.get_pane_state("qa")
        assert comm.get_pane_state("qa") == AgentPaneState.UNKNOWN

    def test_recovery_after_unknown_returns_working(self, comm):
        """After flagging UNKNOWN, if the pane starts changing again,
        next cycle must return WORKING and the stale counter resets.
        """
        _patch_capture(comm, [
            "stuck\n",
            "stuck\n",
            "stuck\n",  # UNKNOWN
            "moving! 1s\n",  # recovery
            "moving! 2s\n",  # still moving
        ])
        comm.get_pane_state("qa")
        comm.get_pane_state("qa")
        assert comm.get_pane_state("qa") == AgentPaneState.UNKNOWN
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING

    def test_unknown_then_prompt_returns_idle(self, comm):
        """Agent stuck (UNKNOWN) → pane updates to show a prompt →
        next cycle is IDLE, stale counter resets.
        """
        _patch_capture(comm, [
            "stuck\n",
            "stuck\n",
            "stuck\n",  # UNKNOWN
            "recovered\n❯ \n",  # new content with prompt — WORKING
            "recovered\n❯ \n",  # unchanged with prompt — IDLE
        ])
        comm.get_pane_state("qa")
        comm.get_pane_state("qa")
        assert comm.get_pane_state("qa") == AgentPaneState.UNKNOWN
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING
        assert comm.get_pane_state("qa") == AgentPaneState.IDLE


class TestGetPaneStateCaptureFailed:
    """capture_pane() returned None → debounced → CAPTURE_FAILED."""

    def test_first_capture_failure_is_debounced(self, comm):
        """A single None from capture_pane does NOT immediately
        return CAPTURE_FAILED — debounced.
        """
        _patch_capture(comm, [None])
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING

    def test_second_consecutive_capture_failure_flags_failed(self, comm):
        """Two consecutive None captures → CAPTURE_FAILED."""
        _patch_capture(comm, [None, None])
        comm.get_pane_state("qa")
        assert comm.get_pane_state("qa") == AgentPaneState.CAPTURE_FAILED

    def test_capture_recovery_resets_failure_counter(self, comm):
        """After a single failure, a successful capture resets the
        counter so a future single failure does NOT immediately
        flag. Prevents accumulating failures across long-running
        orchestrators.
        """
        _patch_capture(comm, [
            None,            # failure 1 (benefit of doubt)
            "normal pane\n",  # recovery, counter reset
            None,            # failure 1 again (benefit of doubt)
        ])
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING


class TestGetPaneStateAgentIsolation:
    """State tracking is per-agent — one agent going stale must not
    affect another agent's counter.
    """

    def test_staleness_is_per_agent(self, comm):
        """Agent A goes stale for 2 cycles (UNKNOWN), agent B is
        active. B must not inherit A's staleness.
        """
        call_count = {"qa": 0, "dev": 0}

        def _capture(agent, lines=10):
            call_count[agent] += 1
            if agent == "qa":
                return "stuck\n"  # always same → goes stale
            return f"dev cycle {call_count[agent]}\n"  # always changing

        comm.capture_pane = MagicMock(side_effect=_capture)
        for _ in range(3):
            qa_state = comm.get_pane_state("qa")
            dev_state = comm.get_pane_state("dev")
        assert qa_state == AgentPaneState.UNKNOWN
        assert dev_state == AgentPaneState.WORKING


class TestGetPaneStateEmptyCapture:
    """Empty or whitespace-only pane edge cases."""

    def test_empty_string_capture_returns_working_first_cycle(self, comm):
        """A legitimate empty string from capture_pane (not None —
        successful capture of an empty pane) behaves like any other
        content: first cycle is WORKING, repeated identical → stale.
        """
        _patch_capture(comm, ["", "", ""])
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING
        # Second call: hash of "" matches prior → no prompt → stale 1
        assert comm.get_pane_state("qa") == AgentPaneState.WORKING
        # Third call: stale 2 → UNKNOWN
        assert comm.get_pane_state("qa") == AgentPaneState.UNKNOWN


class TestIsAgentIdleBackcompat:
    """Pure prompt-detection function — NO side effects on the
    pane-state debounce machinery owned by :meth:`get_pane_state`.

    Revert from the earlier #42 draft that made this a thin
    wrapper around ``get_pane_state``. Caught in #43 review by
    macmini: the wrapper leaked state advances into three
    independent call sites and broke the 2-cycle debounce.
    """

    def test_returns_true_for_idle_pane(self, comm):
        """A pane ending in ``❯`` returns True — the original
        contract, preserved.
        """
        comm.capture_pane = MagicMock(return_value="banner\n❯ \n")
        assert comm.is_agent_idle("qa") is True

    def test_returns_false_for_busy_pane(self, comm):
        """A pane without ``❯`` returns False."""
        comm.capture_pane = MagicMock(return_value="⎿  Running…\n")
        assert comm.is_agent_idle("qa") is False

    def test_returns_false_on_capture_failure(self, comm):
        """Transient capture failure (None) returns False — matches
        pre-#42 behavior. get_pane_state owns the capture-failure
        debounce; is_agent_idle just conservatively says "not idle".
        """
        comm.capture_pane = MagicMock(return_value=None)
        assert comm.is_agent_idle("qa") is False

    def test_requests_10_line_capture_not_30(self, comm):
        """is_agent_idle continues to request 10 lines (the pre-#42
        default), NOT the 30-line hash window get_pane_state uses.
        Preserves the existing fast-path for the delivery probe.
        """
        comm.capture_pane = MagicMock(return_value="❯ \n")
        comm.is_agent_idle("qa")
        call_kwargs = comm.capture_pane.call_args.kwargs
        assert call_kwargs.get("lines") == 10

    def test_does_not_advance_capture_failed_debounce(self, comm):
        """**Regression test for the #43 review finding.**

        ``is_agent_idle`` must NOT mutate
        ``_consecutive_capture_failures``. Only ``get_pane_state``
        (called once per watchdog cycle from
        ``_check_unknown_agents``) is allowed to advance the
        CAPTURE_FAILED debounce counter.

        Without this guard, ``is_agent_idle`` could be called from
        multiple code paths and threads (watchdog idle check,
        delivery probe thread pool) and each call would advance
        the counter, firing CAPTURE_FAILED on the first capture
        failure instead of the second. Spurious directive on every
        transient tmux hiccup.
        """
        comm.capture_pane = MagicMock(return_value=None)

        # Call is_agent_idle many times with failing captures
        for _ in range(10):
            comm.is_agent_idle("qa")

        # The state machine dicts MUST be untouched
        assert comm._consecutive_capture_failures.get("qa", 0) == 0, (
            "is_agent_idle leaked into the CAPTURE_FAILED debounce "
            "counter — the #43 review finding has regressed"
        )
        assert comm._consecutive_stale_cycles.get("qa", 0) == 0
        assert "qa" not in comm._last_pane_hash

    def test_does_not_advance_unknown_debounce(self, comm):
        """Same contract for UNKNOWN / ``_consecutive_stale_cycles``.

        ``is_agent_idle`` called many times with a static non-idle
        pane must not advance the stale-cycle counter, even though
        that's structurally a "stale" capture from the perspective
        of ``get_pane_state``.
        """
        comm.capture_pane = MagicMock(return_value="busy bash\n")
        for _ in range(10):
            comm.is_agent_idle("qa")
        assert comm._consecutive_stale_cycles.get("qa", 0) == 0
        assert "qa" not in comm._last_pane_hash

    def test_interleaved_calls_preserve_get_pane_state_debounce(self, comm):
        """End-to-end gate: interleave ``is_agent_idle`` and
        ``get_pane_state`` calls the way production does (watchdog
        idle check → delivery probe → watchdog unknown check) and
        verify ``get_pane_state`` still honors the exact 2-cycle
        CAPTURE_FAILED debounce.
        """
        comm.capture_pane = MagicMock(return_value=None)

        # Simulate one watchdog cycle: is_agent_idle (idle check) +
        # delivery probe (is_agent_idle via thread pool) + unknown check
        # (get_pane_state). Only the get_pane_state call should
        # advance the counter.
        comm.is_agent_idle("qa")   # idle check
        comm.is_agent_idle("qa")   # delivery probe
        state1 = comm.get_pane_state("qa")  # unknown check, counter = 1
        assert state1 == AgentPaneState.WORKING, (
            "first capture failure in first cycle should be debounced"
        )

        # Second watchdog cycle, same pattern
        comm.is_agent_idle("qa")
        comm.is_agent_idle("qa")
        state2 = comm.get_pane_state("qa")  # counter = 2
        assert state2 == AgentPaneState.CAPTURE_FAILED, (
            "second capture failure in second cycle should flag"
        )


class TestGetStaleCycleCount:
    """#56: `get_stale_cycle_count(agent)` exposes the debounce
    counter used by `get_pane_state` without mutating it, so the
    watchdog cycle log can show ``pane-stuck: dgx(N cycles)``
    without re-computing any pane hashes.
    """

    def test_returns_zero_for_unknown_agent(self, comm):
        assert comm.get_stale_cycle_count("never-seen") == 0

    def test_returns_zero_after_working_reset(self, comm):
        """Advancing through WORKING clears the counter."""
        with patch.object(comm, "capture_pane",
                          side_effect=["frame-a\n", "frame-b\n"]):
            comm.get_pane_state("hub")  # first capture → WORKING (no prior)
            comm.get_pane_state("hub")  # hash changed → WORKING
        assert comm.get_stale_cycle_count("hub") == 0

    def test_increments_as_pane_stays_stale(self, comm):
        """The counter increments once per consecutive stale cycle."""
        # No-prompt frame, repeated.
        stale_frame = "Running a tool call...\n"
        with patch.object(comm, "capture_pane", return_value=stale_frame):
            comm.get_pane_state("hub")  # first capture → WORKING, counter 0
            comm.get_pane_state("hub")  # stale 1 → WORKING, counter 1
            comm.get_pane_state("hub")  # stale 2 → UNKNOWN, counter 2
        assert comm.get_stale_cycle_count("hub") == 2

    def test_read_is_non_mutating(self, comm):
        """Reading via get_stale_cycle_count must NOT advance the
        counter — otherwise the watchdog's log-format read path
        would corrupt the #42 state machine."""
        stale_frame = "Running a tool call...\n"
        with patch.object(comm, "capture_pane", return_value=stale_frame):
            comm.get_pane_state("hub")
            comm.get_pane_state("hub")
        before = comm.get_stale_cycle_count("hub")
        comm.get_stale_cycle_count("hub")
        comm.get_stale_cycle_count("hub")
        assert comm.get_stale_cycle_count("hub") == before


class TestStateTransitionTable:
    """Table-driven test covering a full state-machine walk:
    WORKING → IDLE → WORKING → stale×2 → UNKNOWN → recover → WORKING.
    """

    def test_full_lifecycle(self, comm):
        seq = [
            ("⎿ Running 1s\n", AgentPaneState.WORKING),   # first cycle (no prior hash)
            ("⎿ Running 2s\n", AgentPaneState.WORKING),   # hash changed
            ("❯ \n",           AgentPaneState.WORKING),   # hash changed
            ("❯ \n",           AgentPaneState.IDLE),      # unchanged + prompt
            ("⎿ Running 1s\n", AgentPaneState.WORKING),   # hash changed
            ("stuck\n",        AgentPaneState.WORKING),   # hash changed
            ("stuck\n",        AgentPaneState.WORKING),   # stale 1 (benefit of doubt)
            ("stuck\n",        AgentPaneState.UNKNOWN),   # stale 2
            ("stuck\n",        AgentPaneState.UNKNOWN),   # stale 3 (still UNKNOWN)
            ("recovered\n",    AgentPaneState.WORKING),   # hash changed — reset
            ("recovered\n",    AgentPaneState.WORKING),   # stale 1 again
        ]
        captures = [c for c, _ in seq]
        _patch_capture(comm, captures)
        for i, (_, expected) in enumerate(seq):
            got = comm.get_pane_state("qa")
            assert got == expected, (
                f"cycle {i}: expected {expected}, got {got}"
            )


class TestCaptureLineCount:
    """Pin the AGENT_STATE_CAPTURE_LINES constant so a future
    refactor that changes the hash window size is caught in review.
    """

    def test_hash_window_is_30_lines(self):
        assert _AGENT_STATE_CAPTURE_LINES == 30

    def test_debounce_is_2_cycles(self):
        assert _STALE_DEBOUNCE_CYCLES == 2

    def test_get_pane_state_requests_correct_line_count(self, comm):
        """Verify get_pane_state asks capture_pane for the hash-window
        size, not the legacy 10-line default. Regression guard
        against accidental reversion.
        """
        comm.capture_pane = MagicMock(return_value="test\n")
        comm.get_pane_state("qa")
        call_kwargs = comm.capture_pane.call_args.kwargs
        assert call_kwargs.get("lines") == _AGENT_STATE_CAPTURE_LINES
