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

from orchestrator.watchdog import IdleWatchdog, _DONE_PATTERNS


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
