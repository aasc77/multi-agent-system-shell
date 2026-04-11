#!/usr/bin/env python3
"""
Unit tests for the Idle Agent Watchdog.

Tests cover: multi-agent monitoring via assigned_agents, completion
indicator filtering, state machine fallback, cooldown, and alert logic.

Usage:
    cd /Users/angelserrano/Repositories/multi-agent-system-shell
    python3 -m pytest tests/test_watchdog.py -v
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.watchdog import (
    HeartbeatWatcher,
    IdleWatchdog,
    _AUTH_ERROR_PATTERN,
    _DEFAULT_AUTH_SCAN_EXCLUDES,
    _DONE_PATTERNS,
    _HEARTBEAT_SUBJECT_PREFIX,
    _HEARTBEAT_WILDCARD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_lifecycle():
    lc = MagicMock()
    lc.current_task = {"id": "dog-tracker-1", "title": "Dog tracker", "status": "in_progress"}
    return lc


@pytest.fixture
def mock_state_machine():
    sm = MagicMock()
    sm.current_state = "waiting_hub"
    sm.initial_state = "idle"
    sm._config = {"states": {}}
    return sm


@pytest.fixture
def mock_nats():
    nc = MagicMock()
    nc.publish_to_inbox = AsyncMock()
    return nc


@pytest.fixture
def mock_tmux():
    tmux = MagicMock()
    tmux.is_agent_idle = MagicMock(return_value=True)
    tmux.capture_pane = MagicMock(return_value="$ ")
    tmux.nudge = MagicMock()
    return tmux


@pytest.fixture
def mock_task_queue():
    tq = MagicMock()
    tq.tasks = [
        {
            "id": "dog-tracker-1",
            "status": "in_progress",
            "assigned_agents": {
                "hub": "code complete",
                "dgx2": "service running",
                "hassio": "camera credentials provided",
                "macmini": "E2E testing in progress",
            },
        }
    ]
    return tq


@pytest.fixture
def config():
    return {
        "watchdog": {
            "check_interval": 60,
            "idle_cooldown": 300,
        }
    }


@pytest.fixture
def watchdog(mock_lifecycle, mock_state_machine, mock_nats, mock_tmux, mock_task_queue, config):
    return IdleWatchdog(
        lifecycle=mock_lifecycle,
        state_machine=mock_state_machine,
        nats_client=mock_nats,
        tmux_comm=mock_tmux,
        config=config,
        task_queue=mock_task_queue,
    )


@pytest.fixture
def watchdog_no_tq(mock_lifecycle, mock_state_machine, mock_nats, mock_tmux, config):
    """Watchdog without task_queue (fallback to state machine)."""
    return IdleWatchdog(
        lifecycle=mock_lifecycle,
        state_machine=mock_state_machine,
        nats_client=mock_nats,
        tmux_comm=mock_tmux,
        config=config,
        task_queue=None,
    )


# ---------------------------------------------------------------------------
# Done Pattern Tests
# ---------------------------------------------------------------------------

class TestDonePatterns:
    def test_complete_matches(self):
        assert _DONE_PATTERNS.search("code complete")
        assert _DONE_PATTERNS.search("code complete — service.py, config.yaml")

    def test_completed_matches(self):
        assert _DONE_PATTERNS.search("task completed successfully")

    def test_provided_matches(self):
        assert _DONE_PATTERNS.search("camera credentials provided")

    def test_running_matches(self):
        assert _DONE_PATTERNS.search("service running — auto-start, scanning at 2fps")

    def test_done_matches(self):
        assert _DONE_PATTERNS.search("work done")

    def test_finished_matches(self):
        assert _DONE_PATTERNS.search("testing finished")

    def test_delivered_matches(self):
        assert _DONE_PATTERNS.search("artifacts delivered")

    def test_in_progress_no_match(self):
        assert not _DONE_PATTERNS.search("E2E testing in progress")

    def test_working_no_match(self):
        assert not _DONE_PATTERNS.search("working on deployment")

    def test_testing_no_match(self):
        assert not _DONE_PATTERNS.search("testing")


# ---------------------------------------------------------------------------
# Active Agents Tests
# ---------------------------------------------------------------------------

class TestGetActiveAgents:
    def test_returns_only_active_agents(self, watchdog):
        """Should filter out agents with completed/provided/running status."""
        active = watchdog._get_active_agents()
        agent_names = [a for a, _ in active]
        # Only macmini has "in progress" — others have done indicators
        assert "macmini" in agent_names
        assert "hub" not in agent_names
        assert "dgx2" not in agent_names
        assert "hassio" not in agent_names

    def test_returns_empty_without_task_queue(self, watchdog_no_tq):
        """Should return empty when no task_queue provided."""
        active = watchdog_no_tq._get_active_agents()
        assert active == []

    def test_returns_empty_for_no_in_progress_tasks(self, watchdog, mock_task_queue):
        """Should return empty when no tasks are in_progress."""
        mock_task_queue.tasks = [
            {"id": "test-1", "status": "completed", "assigned_agents": {"hub": "testing"}}
        ]
        active = watchdog._get_active_agents()
        assert active == []

    def test_returns_empty_when_no_assigned_agents(self, watchdog, mock_task_queue):
        """Should return empty (triggering fallback) when no assigned_agents field."""
        mock_task_queue.tasks = [
            {"id": "test-1", "status": "in_progress"}
        ]
        active = watchdog._get_active_agents()
        assert active == []

    def test_multiple_active_agents(self, watchdog, mock_task_queue):
        """Should return multiple agents when several are still working."""
        mock_task_queue.tasks = [
            {
                "id": "test-1",
                "status": "in_progress",
                "assigned_agents": {
                    "hub": "working on feature",
                    "macmini": "testing integration",
                    "dgx": "done",
                },
            }
        ]
        active = watchdog._get_active_agents()
        agent_names = [a for a, _ in active]
        assert "hub" in agent_names
        assert "macmini" in agent_names
        assert "dgx" not in agent_names


# ---------------------------------------------------------------------------
# Check Idle Agents Tests
# ---------------------------------------------------------------------------

class TestCheckIdleAgents:
    @pytest.mark.asyncio
    async def test_monitors_active_agents_from_tasks(self, watchdog, mock_tmux, mock_nats):
        """Should check only active agents from assigned_agents."""
        await watchdog._check_idle_agents()
        # macmini is the only active agent — should check it
        mock_tmux.is_agent_idle.assert_called_with("macmini")
        # Should alert manager since macmini is idle
        mock_nats.publish_to_inbox.assert_called_once()
        alert = mock_nats.publish_to_inbox.call_args[0]
        assert alert[0] == "manager"
        assert alert[1]["agent"] == "macmini"

    @pytest.mark.asyncio
    async def test_falls_back_to_state_machine(self, watchdog_no_tq, mock_tmux, mock_nats):
        """Should use state machine when no task_queue."""
        await watchdog_no_tq._check_idle_agents()
        # State machine says current agent is "hub" (waiting_hub)
        mock_tmux.is_agent_idle.assert_called_with("hub")
        mock_nats.publish_to_inbox.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_fallback_when_no_task(self, watchdog_no_tq, mock_lifecycle, mock_tmux):
        """Should skip fallback path when no current task."""
        mock_lifecycle.current_task = None
        await watchdog_no_tq._check_idle_agents()
        mock_tmux.is_agent_idle.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_idle_agents(self, watchdog, mock_tmux, mock_nats):
        """Should not alert for agents that are not idle."""
        mock_tmux.is_agent_idle.return_value = False
        await watchdog._check_idle_agents()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_cooldown(self, watchdog, mock_tmux, mock_nats):
        """Should not re-alert during cooldown window."""
        watchdog._last_alert["macmini"] = time.time()  # just alerted
        await watchdog._check_idle_agents()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_awaiting_response(self, watchdog, mock_tmux, mock_nats):
        """Should not re-alert while waiting for manager response within cooldown."""
        watchdog._awaiting_response["macmini"] = True
        watchdog._last_alert["macmini"] = time.time()  # recent alert, within cooldown
        await watchdog._check_idle_agents()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_alerts_after_cooldown_expires(self, watchdog, mock_tmux, mock_nats):
        """Should alert again once cooldown has expired."""
        watchdog._last_alert["macmini"] = time.time() - 400  # expired
        await watchdog._check_idle_agents()
        mock_nats.publish_to_inbox.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_when_assigned_agents_missing(self, watchdog, mock_task_queue, mock_tmux, mock_nats):
        """Should fall back to state machine when assigned_agents not in tasks."""
        mock_task_queue.tasks = [
            {"id": "test-1", "status": "in_progress"}  # no assigned_agents
        ]
        await watchdog._check_idle_agents()
        # Falls back to state machine -> "hub" from waiting_hub
        mock_tmux.is_agent_idle.assert_called_with("hub")


# ---------------------------------------------------------------------------
# Manager Response Tests
# ---------------------------------------------------------------------------

class TestHandleManagerResponse:
    @pytest.mark.asyncio
    async def test_nudge_action(self, watchdog, mock_tmux):
        """Manager response 'nudge' should nudge the agent."""
        watchdog._awaiting_response["macmini"] = True
        await watchdog.handle_manager_response({
            "type": "manager_idle_response",
            "agent": "macmini",
            "action": "nudge",
        })
        mock_tmux.nudge.assert_called_with("macmini", force=True)
        assert watchdog._awaiting_response["macmini"] is False

    @pytest.mark.asyncio
    async def test_expected_action(self, watchdog):
        """Manager response 'expected' should extend cooldown."""
        watchdog._awaiting_response["macmini"] = True
        await watchdog.handle_manager_response({
            "type": "manager_idle_response",
            "agent": "macmini",
            "action": "expected",
        })
        assert watchdog._awaiting_response["macmini"] is False
        assert watchdog._last_alert["macmini"] > 0


# ---------------------------------------------------------------------------
# Alert Manager Tests
# ---------------------------------------------------------------------------

class TestAlertManager:
    @pytest.mark.asyncio
    async def test_alert_includes_agent_info(self, watchdog, mock_nats, mock_tmux):
        """Alert should include agent name, task info, and pane capture."""
        await watchdog._alert_manager("macmini")

        mock_nats.publish_to_inbox.assert_called_once()
        call_args = mock_nats.publish_to_inbox.call_args[0]
        assert call_args[0] == "manager"
        alert = call_args[1]
        assert alert["agent"] == "macmini"
        assert alert["type"] == "idle_alert"
        assert "task_id" in alert
        assert "pane_capture" in alert

    @pytest.mark.asyncio
    async def test_alert_sets_cooldown_and_awaiting(self, watchdog, mock_nats, mock_tmux):
        """Alert should set last_alert time and awaiting_response flag."""
        await watchdog._alert_manager("macmini")
        assert watchdog._last_alert["macmini"] > 0
        assert watchdog._awaiting_response["macmini"] is True

    @pytest.mark.asyncio
    async def test_alert_nudges_manager_tmux(self, watchdog, mock_nats, mock_tmux):
        """Alert should nudge manager's tmux pane."""
        await watchdog._alert_manager("macmini")
        mock_tmux.nudge.assert_called_with("manager", force=True)


# ---------------------------------------------------------------------------
# Auth Failure Detection Tests (issue #28)
# ---------------------------------------------------------------------------

_AUTH_ERROR_LINE = "MCP error -32603: Authentication failed: Invalid token"
_AUTH_ERROR_PANE = (
    "some earlier output\n"
    "  ⎿  user called check_messages\n"
    f"  ⎿  {_AUTH_ERROR_LINE}\n"
    "❯ \n"
)


class TestAuthErrorPattern:
    # Positive matches — canonical MCP JSON-RPC auth failures

    def test_matches_full_error_line(self):
        assert _AUTH_ERROR_PATTERN.search(
            "MCP error -32603: Authentication failed: Invalid token",
        )

    def test_matches_decorated_tool_output(self):
        assert _AUTH_ERROR_PATTERN.search(
            "  ⎿  Error: MCP error -32603: Authentication failed: Invalid token",
        )

    def test_matches_invalid_token_variant(self):
        assert _AUTH_ERROR_PATTERN.search(
            "MCP error -32000: Invalid token",
        )

    def test_matches_case_insensitive_real_shape(self):
        assert _AUTH_ERROR_PATTERN.search(
            "mcp ERROR -32603: authentication FAILED",
        )

    def test_matches_wrapped_across_lines_at_52_cols(self):
        """Tmux word-wrap at ~52 cols splits the full line in two visual rows.

        The regex must still fire because `\\s` crosses the `\\n`.
        """
        wrapped = (
            "  ⎿  Error: MCP error -32603:\n"
            "     Authentication failed: Invalid token\n"
        )
        assert _AUTH_ERROR_PATTERN.search(wrapped)

    # Negative matches — prose, quotes, fragments (captured from real
    # smoke-test false positives before the pattern was tightened in #28).

    def test_rejects_bare_error_code(self):
        """macmini's scenario: backticked code reference in prose."""
        assert not _AUTH_ERROR_PATTERN.search(
            "the watchdog matches `-32603` in agent panes",
        )

    def test_rejects_prose_mentioning_auth_failure(self):
        """Discussion of the bug without the MCP SDK surface format."""
        assert not _AUTH_ERROR_PATTERN.search(
            'the real problem is "Authentication failed" in the error output',
        )

    def test_rejects_missing_prefix_structure(self):
        """Even the exact words — if the dash/colon prefix is missing, no match."""
        assert not _AUTH_ERROR_PATTERN.search(
            "mcp error 32603 authentication failed",
        )

    def test_rejects_hub_briefing_text(self):
        """Exact FP string from the #28 smoke-test first run on hub's pane."""
        assert not _AUTH_ERROR_PATTERN.search(
            "In the next ~1-2 minutes macmini will re-inject the `-32603` line into their pane.",
        )

    def test_rejects_quoted_matched_line_fragment(self):
        """Another FP captured from hub's pane: explanation of the bug."""
        assert not _AUTH_ERROR_PATTERN.search(
            "the literal -32603 string from messages I wrote",
        )

    def test_rejects_different_jsonrpc_error(self):
        """Non-auth JSON-RPC errors (like -32601 Method not found) must not match."""
        assert not _AUTH_ERROR_PATTERN.search(
            "MCP error -32601: Method not found: foobar",
        )

    def test_rejects_clean_check_messages_pane(self):
        pane = (
            "  ⎿  check_messages()\n"
            "  ⎿  []\n"
            "❯ \n"
        )
        assert not _AUTH_ERROR_PATTERN.search(pane)


class TestCheckAuthFailures:
    @pytest.fixture
    def auth_tmux(self, mock_tmux):
        """Tmux mock with pane_mapping hook and configurable captures."""
        mock_tmux.get_pane_mapping = MagicMock(
            return_value={"hub": 0, "RTX5090": 4},
        )
        mock_tmux.capture_pane = MagicMock(return_value="❯ \n")
        return mock_tmux

    @pytest.fixture
    def auth_watchdog(
        self, mock_lifecycle, mock_state_machine, mock_nats,
        auth_tmux, mock_task_queue, config,
    ):
        return IdleWatchdog(
            lifecycle=mock_lifecycle,
            state_machine=mock_state_machine,
            nats_client=mock_nats,
            tmux_comm=auth_tmux,
            config=config,
            task_queue=mock_task_queue,
        )

    @pytest.mark.asyncio
    async def test_no_match_no_alert(self, auth_watchdog, auth_tmux, mock_nats):
        """Clean panes never trigger a manager_directive."""
        auth_tmux.capture_pane.return_value = "  ⎿  check_messages()\n  ⎿  []\n❯ \n"
        await auth_watchdog._check_auth_failures()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_match_publishes_directive(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """A -32603 line triggers a manager_directive to the manager inbox."""
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()

        mock_nats.publish_to_inbox.assert_called_once()
        call = mock_nats.publish_to_inbox.call_args[0]
        assert call[0] == "manager"
        directive = call[1]
        assert directive["type"] == "manager_directive"
        assert directive["subtype"] == "auth_failure"
        assert directive["agent"] == "RTX5090"
        assert "-32603" in directive["matched_line"]

    @pytest.mark.asyncio
    async def test_matched_line_logged_as_flag_human(
        self, auth_watchdog, auth_tmux, mock_nats, caplog,
    ):
        """Watchdog writes a flag_human log entry with agent + matched line."""
        import logging
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "RTX5090" else "❯ \n"
        )
        with caplog.at_level(logging.WARNING, logger="orchestrator.watchdog"):
            await auth_watchdog._check_auth_failures()
        assert any(
            "flag_human" in rec.message and "RTX5090" in rec.message
            and "-32603" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_identical_burst_suppressed(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """Identical error line within cooldown does not re-alert."""
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        await auth_watchdog._check_auth_failures()
        assert mock_nats.publish_to_inbox.call_count == 1

    @pytest.mark.asyncio
    async def test_different_error_line_alerts_again(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """A different matched line re-alerts even inside the cooldown window."""
        panes = iter([
            _AUTH_ERROR_PANE,
            "  ⎿  Error: MCP error -32000: Invalid token\n❯ \n",
        ])

        def _capture(agent, lines):
            if agent != "RTX5090":
                return "❯ \n"
            return next(panes)

        auth_tmux.capture_pane.side_effect = _capture
        await auth_watchdog._check_auth_failures()
        await auth_watchdog._check_auth_failures()
        assert mock_nats.publish_to_inbox.call_count == 2

    @pytest.mark.asyncio
    async def test_cooldown_expires_allows_realert(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """After auth_alert_cooldown elapses, the same error re-alerts."""
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        # Rewind the remembered alert past the cooldown window.
        auth_watchdog._last_auth_alert["RTX5090"] = (
            time.time() - auth_watchdog._auth_alert_cooldown - 1
        )
        await auth_watchdog._check_auth_failures()
        assert mock_nats.publish_to_inbox.call_count == 2

    @pytest.mark.asyncio
    async def test_missing_get_pane_mapping_is_noop(
        self, mock_lifecycle, mock_state_machine, mock_nats, config,
    ):
        """Watchdog does not crash when tmux_comm has no get_pane_mapping."""
        class _BareTmux:
            def capture_pane(self, agent, lines=20):
                return "❯ \n"

        wd = IdleWatchdog(
            lifecycle=mock_lifecycle,
            state_machine=mock_state_machine,
            nats_client=mock_nats,
            tmux_comm=_BareTmux(),
            config=config,
            task_queue=None,
        )
        await wd._check_auth_failures()  # should not raise
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_excluded_hub_pane_is_not_scanned(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """Hub is in the default exclude list, so even a real-shape error
        in hub's pane must not publish a directive.
        """
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "hub" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_excluded_agent_still_fires(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """RTX5090 is NOT in the exclude list, so a real-shape error fires."""
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        mock_nats.publish_to_inbox.assert_called_once()

    @pytest.mark.asyncio
    async def test_config_overrides_exclude_list(
        self, mock_lifecycle, mock_state_machine, mock_nats, auth_tmux,
        mock_task_queue,
    ):
        """watchdog.auth_scan_excludes in config overrides the default set."""
        cfg = {
            "watchdog": {
                "check_interval": 60,
                "idle_cooldown": 300,
                "auth_scan_excludes": ["dgx"],  # explicit -- does NOT include hub
            }
        }
        auth_tmux.get_pane_mapping.return_value = {"hub": 0, "dgx": 2}
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "hub" else "❯ \n"
        )
        wd = IdleWatchdog(
            lifecycle=mock_lifecycle,
            state_machine=mock_state_machine,
            nats_client=mock_nats,
            tmux_comm=auth_tmux,
            config=cfg,
            task_queue=mock_task_queue,
        )
        await wd._check_auth_failures()
        # hub was NOT excluded here (config replaced the default) — alert fires
        mock_nats.publish_to_inbox.assert_called_once()
        assert mock_nats.publish_to_inbox.call_args[0][1]["agent"] == "hub"

    def test_default_exclude_list_has_hub_and_manager(self):
        """Contract test — the default exclusion set must hold hub + manager."""
        assert "hub" in _DEFAULT_AUTH_SCAN_EXCLUDES
        assert "manager" in _DEFAULT_AUTH_SCAN_EXCLUDES

    @pytest.mark.asyncio
    async def test_directive_body_shape_is_wire_ready(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """The watchdog builds the *body* of the directive — envelope
        fields (message_id, timestamp, from) are added downstream by
        NatsClient._envelope_wrap. This test pins the body shape the
        watchdog owns; the envelope contract lives in tests/test_nats_client.py
        per the #34 separation of concerns refactor.
        """
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            _AUTH_ERROR_PANE if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        mock_nats.publish_to_inbox.assert_called_once()
        directive = mock_nats.publish_to_inbox.call_args[0][1]

        # Watchdog-owned body fields — must be set before the library
        # wraps the envelope, so the mock sees them here.
        assert directive["type"] == "manager_directive"
        assert directive["subtype"] == "auth_failure"
        assert directive["agent"] == "RTX5090"
        assert "matched_line" in directive
        assert "-32603" in directive["matched_line"]
        assert directive["priority"] == "high"
        assert "message" in directive
        assert "auth" in directive["message"].lower()

        # Envelope fields MUST NOT be set by the watchdog itself — that
        # is the library's job now. If a future refactor reintroduces
        # a manual envelope block here, this assertion will catch it.
        assert "message_id" not in directive
        assert "timestamp" not in directive
        assert "from" not in directive

    @pytest.mark.asyncio
    async def test_matched_line_collapses_wrap_whitespace(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """When the match spans a tmux word-wrap, the reported matched_line
        collapses the embedded newlines/spaces into single spaces so the
        directive body and log line stay readable on one row.
        """
        wrapped_pane = (
            "  ⎿  Error: MCP error -32603:\n"
            "     Authentication failed: Invalid token\n"
            "❯ \n"
        )
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            wrapped_pane if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        mock_nats.publish_to_inbox.assert_called_once()
        matched = mock_nats.publish_to_inbox.call_args[0][1]["matched_line"]
        assert "\n" not in matched
        assert "MCP error -32603:" in matched
        assert "Authentication failed" in matched


# ---------------------------------------------------------------------------
# Heartbeat Watcher Tests (issue #32 — dev-agent self-health probe)
# ---------------------------------------------------------------------------


def _heartbeat_msg(agent: str, payload_type: str = "health_ok") -> MagicMock:
    """Build a fake core-NATS message whose `.data` decodes to a
    heartbeat JSON body for *agent*.
    """
    import json as _json
    m = MagicMock()
    body = {
        "type": payload_type,
        "agent": agent,
        "ts": "2026-04-11T20:00:00+00:00",
        "bridge_pid": 12345,
        "uptime_seconds": 42,
    }
    m.data = _json.dumps(body).encode("utf-8")
    return m


class TestHeartbeatConstants:
    def test_subject_prefix_outside_agents_wildcard(self):
        """Heartbeat subjects must NOT match the `agents.>` JetStream
        stream filter, otherwise every heartbeat would land in the
        stream and hit the 10k-msg retention budget.
        """
        assert _HEARTBEAT_SUBJECT_PREFIX == "system.heartbeat"
        assert not _HEARTBEAT_SUBJECT_PREFIX.startswith("agents.")
        assert _HEARTBEAT_WILDCARD == "system.heartbeat.>"


class TestHeartbeatWatcher:
    @pytest.fixture
    def hb_config(self):
        return {
            "watchdog": {
                "heartbeat_agents": ["hub"],
                "hub_heartbeat_staleness_seconds": 30,
                "hub_heartbeat_check_interval_seconds": 5,
                "hub_heartbeat_grace_multiplier": 2.0,
            }
        }

    @pytest.fixture
    def hb_nats(self):
        nc = MagicMock()
        nc.subscribe_core = AsyncMock(return_value=MagicMock())
        nc.publish_to_inbox = AsyncMock()
        return nc

    @pytest.fixture
    def watcher(self, hb_nats, hb_config):
        return HeartbeatWatcher(nats_client=hb_nats, config=hb_config)

    @pytest.mark.asyncio
    async def test_start_subscribes_to_wildcard(self, watcher, hb_nats):
        """start() subscribes to the `system.heartbeat.>` wildcard."""
        await watcher.start()
        hb_nats.subscribe_core.assert_called_once()
        args = hb_nats.subscribe_core.call_args[0]
        assert args[0] == "system.heartbeat.>"

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, watcher, hb_nats):
        """Calling start() twice must not subscribe twice."""
        await watcher.start()
        await watcher.start()
        assert hb_nats.subscribe_core.call_count == 1

    @pytest.mark.asyncio
    async def test_handle_heartbeat_records_timestamp(self, watcher):
        await watcher._handle_heartbeat(_heartbeat_msg("hub"))
        assert "hub" in watcher._last_heartbeat_seen
        assert (
            time.time() - watcher._last_heartbeat_seen["hub"] < 1.0
        ), "last_heartbeat_seen should be ~now"

    @pytest.mark.asyncio
    async def test_handle_heartbeat_drops_malformed_json(self, watcher):
        msg = MagicMock()
        msg.data = b"{not valid json"
        await watcher._handle_heartbeat(msg)
        assert "hub" not in watcher._last_heartbeat_seen

    @pytest.mark.asyncio
    async def test_handle_heartbeat_drops_wrong_type(self, watcher):
        await watcher._handle_heartbeat(_heartbeat_msg("hub", payload_type="other"))
        assert "hub" not in watcher._last_heartbeat_seen

    @pytest.mark.asyncio
    async def test_handle_heartbeat_drops_missing_agent(self, watcher):
        import json as _json
        msg = MagicMock()
        msg.data = _json.dumps({"type": "health_ok"}).encode("utf-8")
        await watcher._handle_heartbeat(msg)
        assert len(watcher._last_heartbeat_seen) == 0

    @pytest.mark.asyncio
    async def test_fresh_heartbeat_no_alert(self, watcher, hb_nats):
        """A heartbeat within the staleness window does not alert."""
        watcher._last_heartbeat_seen["hub"] = time.time()
        await watcher._check_staleness()
        hb_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_heartbeat_alerts(self, watcher, hb_nats):
        """A heartbeat older than the threshold fires a hub_unreachable."""
        watcher._last_heartbeat_seen["hub"] = time.time() - 35  # 5s past 30s threshold
        await watcher._check_staleness()
        hb_nats.publish_to_inbox.assert_called_once()
        call = hb_nats.publish_to_inbox.call_args[0]
        assert call[0] == "manager"
        directive = call[1]
        assert directive["type"] == "manager_directive"
        assert directive["subtype"] == "hub_unreachable"
        assert directive["agent"] == "hub"
        assert directive["staleness_seconds"] >= 30
        assert directive["threshold_seconds"] == 30
        assert directive["staleness_bucket"] == 1
        assert "last_heartbeat_seen" in directive
        assert directive["priority"] == "high"

    @pytest.mark.asyncio
    async def test_suppression_same_bucket_no_realert(self, watcher, hb_nats):
        """Two consecutive check_staleness calls inside the same bucket
        produce exactly one directive, not two.
        """
        watcher._last_heartbeat_seen["hub"] = time.time() - 35
        await watcher._check_staleness()
        await watcher._check_staleness()
        assert hb_nats.publish_to_inbox.call_count == 1

    @pytest.mark.asyncio
    async def test_suppression_next_bucket_realerts(self, watcher, hb_nats):
        """Crossing into the next threshold-multiple bucket fires again."""
        # Bucket 1: 30-59s stale
        watcher._last_heartbeat_seen["hub"] = time.time() - 35
        await watcher._check_staleness()
        assert hb_nats.publish_to_inbox.call_count == 1
        # Rewind last_seen to push us into bucket 2 (60-89s stale)
        watcher._last_heartbeat_seen["hub"] = time.time() - 65
        await watcher._check_staleness()
        assert hb_nats.publish_to_inbox.call_count == 2
        # Second directive has bucket=2
        second = hb_nats.publish_to_inbox.call_args_list[1][0][1]
        assert second["staleness_bucket"] == 2

    @pytest.mark.asyncio
    async def test_fresh_heartbeat_clears_suppression(self, watcher, hb_nats):
        """After alerting, a fresh heartbeat must clear the suppression
        bucket so the NEXT outage can alert from bucket 1 again.
        """
        watcher._last_heartbeat_seen["hub"] = time.time() - 35
        await watcher._check_staleness()
        assert hb_nats.publish_to_inbox.call_count == 1

        # Fresh heartbeat arrives — should clear bucket
        await watcher._handle_heartbeat(_heartbeat_msg("hub"))
        assert "hub" not in watcher._last_alert_bucket

        # New outage — bucket 1 re-alerts
        watcher._last_heartbeat_seen["hub"] = time.time() - 35
        await watcher._check_staleness()
        assert hb_nats.publish_to_inbox.call_count == 2

    @pytest.mark.asyncio
    async def test_cold_start_grace_suppresses_early_alerts(
        self, watcher, hb_nats,
    ):
        """When no heartbeat has ever been seen AND the orchestrator is
        younger than grace_multiplier * threshold, do NOT alert.
        Prevents spurious hub_unreachable at fresh-boot.
        """
        # Simulate a fresh boot — boot_time very recent
        watcher._boot_time = time.time() - 10  # 10s since boot
        await watcher._check_staleness()
        hb_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_cold_start_grace_expires_and_alerts(
        self, watcher, hb_nats,
    ):
        """After grace_multiplier * threshold has elapsed with no heartbeat
        ever seen, the watcher fires a hub_unreachable with
        last_heartbeat_seen = None.
        """
        # Simulate boot time past the grace window: grace = 2.0 * 30s = 60s
        watcher._boot_time = time.time() - 120  # 2 minutes since boot
        await watcher._check_staleness()
        hb_nats.publish_to_inbox.assert_called_once()
        directive = hb_nats.publish_to_inbox.call_args[0][1]
        assert directive["last_heartbeat_seen"] is None
        assert "never since orchestrator boot" in directive["message"]

    @pytest.mark.asyncio
    async def test_config_overrides_defaults(self, hb_nats):
        """Config values override all four knobs."""
        cfg = {
            "watchdog": {
                "heartbeat_agents": ["hub", "dgx"],
                "hub_heartbeat_staleness_seconds": 600,
                "hub_heartbeat_check_interval_seconds": 120,
                "hub_heartbeat_grace_multiplier": 3.5,
            }
        }
        w = HeartbeatWatcher(nats_client=hb_nats, config=cfg)
        assert w._heartbeat_agents == ("hub", "dgx")
        assert w._staleness_threshold == 600
        assert w._check_interval == 120
        assert w._grace_multiplier == 3.5

    @pytest.mark.asyncio
    async def test_defaults_when_config_missing(self, hb_nats):
        """Constructor tolerates a fully empty config."""
        w = HeartbeatWatcher(nats_client=hb_nats, config={})
        assert w._heartbeat_agents == ("hub",)
        assert w._staleness_threshold == 180
        assert w._check_interval == 30
        assert w._grace_multiplier == 2.0

    @pytest.mark.asyncio
    async def test_alert_inherits_envelope_via_publish_to_inbox(
        self, watcher, hb_nats,
    ):
        """The watcher calls nats_client.publish_to_inbox directly — it
        does NOT pre-wrap the envelope. The library is responsible for
        adding message_id/timestamp/from (per #34 refactor). If a future
        refactor makes the watcher build its own envelope, this test
        catches it.
        """
        watcher._last_heartbeat_seen["hub"] = time.time() - 35
        await watcher._check_staleness()
        directive = hb_nats.publish_to_inbox.call_args[0][1]
        assert "message_id" not in directive
        assert "timestamp" not in directive
        assert "from" not in directive

    @pytest.mark.asyncio
    async def test_multiple_heartbeat_agents_tracked_independently(
        self, hb_nats,
    ):
        """When `heartbeat_agents` lists multiple agents, each has its
        own last_seen / bucket state.
        """
        cfg = {
            "watchdog": {
                "heartbeat_agents": ["hub", "dgx"],
                "hub_heartbeat_staleness_seconds": 30,
                "hub_heartbeat_check_interval_seconds": 5,
                "hub_heartbeat_grace_multiplier": 2.0,
            }
        }
        w = HeartbeatWatcher(nats_client=hb_nats, config=cfg)
        # hub is fresh, dgx is stale
        w._last_heartbeat_seen["hub"] = time.time()
        w._last_heartbeat_seen["dgx"] = time.time() - 35
        await w._check_staleness()
        hb_nats.publish_to_inbox.assert_called_once()
        assert hb_nats.publish_to_inbox.call_args[0][1]["agent"] == "dgx"
