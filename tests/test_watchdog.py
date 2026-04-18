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
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.watchdog import (
    IdleWatchdog,
    _AUTH_ERROR_PATTERN,
    _CONTEXT_BEFORE_MAX_CHARS,
    _DEFAULT_AUTH_SCAN_EXCLUDES,
    _DONE_PATTERNS,
    _MATCHED_LINE_MAX_CHARS,
    _extract_full_line_and_context,
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
# Cycle Log Line Tests (#27)
# ---------------------------------------------------------------------------

class TestCycleLogLine:
    """Verifies the per-cycle log line phrasing from #27.

    The old "N active agents: ... (fallback to state machine)" wording
    read as "no agents are doing anything" but actually only reflected
    tasks.json assigned_agents — invisible to ad-hoc send_to_agent
    traffic. New wording disambiguates that.
    """

    @pytest.mark.asyncio
    async def test_log_when_agents_assigned(self, watchdog, caplog):
        calls = {"n": 0}
        async def _cancel_after_first(_delay):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError
        with patch("asyncio.sleep", side_effect=_cancel_after_first):
            with caplog.at_level(logging.INFO, logger="orchestrator.watchdog"):
                with pytest.raises(asyncio.CancelledError):
                    await watchdog.run()
        messages = [r.getMessage() for r in caplog.records]
        assert any("agents assigned to in_progress tasks" in m for m in messages)
        assert not any("active agents" in m for m in messages)
        assert not any("fallback to state machine" in m for m in messages)

    @pytest.mark.asyncio
    async def test_log_when_pipeline_idle(self, watchdog_no_tq, caplog):
        calls = {"n": 0}
        async def _cancel_after_first(_delay):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError
        with patch("asyncio.sleep", side_effect=_cancel_after_first):
            with caplog.at_level(logging.INFO, logger="orchestrator.watchdog"):
                with pytest.raises(asyncio.CancelledError):
                    await watchdog_no_tq.run()
        messages = [r.getMessage() for r in caplog.records]
        assert any("pipeline idle (no task-queue work)" in m for m in messages)
        assert not any("active agents" in m for m in messages)
        assert not any("fallback to state machine" in m for m in messages)


# ---------------------------------------------------------------------------
# Composite Cycle Log with MCP-Active Block (#55)
# ---------------------------------------------------------------------------

class TestCycleLogMcpActive:
    """#55: per-cycle log line augments the #27 task-queue block with
    an ``MCP-active: ...`` section when the ``ActivityTracker`` has
    any agent inside the configured window, and an ``idle: ...``
    section for every regular agent that isn't in either block.
    """

    def _make_watchdog(
        self,
        tracker=None,
        task_queue=None,
        monitored_agents=("hub", "macmini", "hassio", "RTX5090", "dgx", "dgx2"),
        mcp_window=60,
    ):
        from orchestrator.watchdog import IdleWatchdog
        cfg = {
            "agents": {
                name: {"runtime": "claude_code"}
                for name in monitored_agents
            },
            "watchdog": {
                "check_interval": 60,
                "idle_cooldown": 300,
                "mcp_activity_window_seconds": mcp_window,
            },
        }
        return IdleWatchdog(
            lifecycle=MagicMock(current_task=None),
            state_machine=MagicMock(current_state="idle", initial_state="idle"),
            nats_client=AsyncMock(),
            tmux_comm=MagicMock(),
            config=cfg,
            task_queue=task_queue,
            activity_tracker=tracker,
        )

    def test_no_tracker_emits_original_pipeline_idle_line(self):
        """Backwards-compat: without a tracker we still emit the
        #27 phrasing. #55's MCP-active block is opt-in via tracker."""
        wd = self._make_watchdog(tracker=None)
        msg = wd._format_cycle_log(cycle=5, active=[])
        assert "pipeline idle (no task-queue work)" in msg
        assert "MCP-active" not in msg

    def test_empty_tracker_omits_mcp_active_block(self):
        """Tracker wired but empty → no MCP-active block, full idle
        list of every monitored agent."""
        from orchestrator.activity_tracker import ActivityTracker
        wd = self._make_watchdog(tracker=ActivityTracker())
        msg = wd._format_cycle_log(cycle=1, active=[])
        assert "MCP-active" not in msg
        # Every monitored agent should be in the idle list.
        assert "idle: hub macmini hassio RTX5090 dgx dgx2" in msg

    def test_single_agent_activity_populates_mcp_active(self):
        from orchestrator.activity_tracker import ActivityTracker
        tracker = ActivityTracker()
        now = 1000.0
        tracker.touch("dev", now=now - 3)  # 3s ago
        wd = self._make_watchdog(tracker=tracker)
        with patch("orchestrator.activity_tracker.time.time", return_value=now):
            msg = wd._format_cycle_log(cycle=2, active=[])
        assert "MCP-active: dev(3s)" in msg
        # Idle list excludes dev (because it's MCP-active).
        assert "idle: " in msg
        assert " dev" not in msg.split("idle:")[1]

    def test_multi_agent_activity_sorted_by_recency(self):
        from orchestrator.activity_tracker import ActivityTracker
        tracker = ActivityTracker()
        now = 1000.0
        tracker.touch("dev", now=now - 3)
        tracker.touch("RTX5090", now=now - 8)
        tracker.touch("macmini", now=now - 45)
        wd = self._make_watchdog(tracker=tracker)
        with patch("orchestrator.activity_tracker.time.time", return_value=now):
            msg = wd._format_cycle_log(cycle=3, active=[])
        # Freshest first.
        assert "MCP-active: dev(3s) RTX5090(8s) macmini(45s)" in msg
        # Idle list only has agents not in MCP-active: hassio, dgx, dgx2.
        idle_part = msg.split("idle:")[1]
        assert "hassio" in idle_part
        assert "dgx" in idle_part
        assert "dgx2" in idle_part
        assert "dev" not in idle_part
        assert "macmini" not in idle_part

    def test_activity_older_than_window_ages_out(self):
        """An agent last active longer ago than the window is in the
        idle list, not the MCP-active list. Tests the aging-out
        behavior at the log-formatter level (the tracker's own
        active_within is unit-tested separately)."""
        from orchestrator.activity_tracker import ActivityTracker
        tracker = ActivityTracker()
        now = 1000.0
        tracker.touch("macmini", now=now - 10)     # inside 60s window
        tracker.touch("stale", now=now - 1000)    # outside — ages out
        wd = self._make_watchdog(
            tracker=tracker,
            monitored_agents=("macmini", "stale", "hub"),
            mcp_window=60,
        )
        with patch("orchestrator.activity_tracker.time.time", return_value=now):
            msg = wd._format_cycle_log(cycle=4, active=[])
        assert "MCP-active: macmini(10s)" in msg
        assert "stale" not in msg.split("MCP-active:")[1].split("|")[0]
        idle_part = msg.split("idle:")[1]
        assert "stale" in idle_part
        assert "hub" in idle_part

    def test_assigned_agent_not_reported_as_idle_even_with_mcp_activity(self):
        """An agent listed in tasks.json assigned_agents appears in
        the task-queue block and must NOT also appear in the idle
        block — even when it also has fresh MCP activity within the
        window. Dual-reporting with MCP-active is intentional (same
        composite-log philosophy documented on other tests in this
        class), so this test only pins the idle-exclusion invariant.
        The old name ``takes_priority_over_mcp_active`` implied the
        test asserted a priority that the assertions don't actually
        enforce — see #73.
        """
        from orchestrator.activity_tracker import ActivityTracker
        tracker = ActivityTracker()
        now = 1000.0
        tracker.touch("dev", now=now - 2)
        wd = self._make_watchdog(tracker=tracker)
        active = [("dev", "coding")]
        with patch("orchestrator.activity_tracker.time.time", return_value=now):
            msg = wd._format_cycle_log(cycle=6, active=active)
        # Task-queue block names dev.
        assert "agents assigned to in_progress tasks" in msg
        assert "['dev']" in msg
        # Not reported in MCP-active even though tracker is fresh.
        mcp_section = (
            msg.split("MCP-active:")[1].split("|")[0]
            if "MCP-active:" in msg else ""
        )
        # (Acceptable: dev may or may not show up in MCP-active. The
        # critical invariant is that it does NOT appear in the idle
        # block — because the operator already knows it's working.)
        idle_section = msg.split("idle:")[1] if "idle:" in msg else ""
        assert " dev" not in idle_section, (
            "dev must not appear in the idle block while assigned: " + msg
        )

    def test_monitors_excluded_from_idle_list(self):
        """#55 regression guard: the idle list is for *regular* agents
        only. Monitors live in the control window and shouldn't be
        reported as idle in the agents-window status line."""
        wd = self._make_watchdog(
            tracker=None,
            monitored_agents=(),  # override below
        )
        # Rebuild with a monitor + regulars.
        from orchestrator.watchdog import IdleWatchdog
        cfg = {
            "agents": {
                "manager": {"runtime": "claude_code", "role": "monitor"},
                "hub": {"runtime": "claude_code"},
                "macmini": {"runtime": "claude_code"},
            },
            "watchdog": {"check_interval": 60, "idle_cooldown": 300},
        }
        wd = IdleWatchdog(
            lifecycle=MagicMock(current_task=None),
            state_machine=MagicMock(current_state="idle", initial_state="idle"),
            nats_client=AsyncMock(),
            tmux_comm=MagicMock(),
            config=cfg,
            task_queue=None,
            activity_tracker=None,
        )
        msg = wd._format_cycle_log(cycle=7, active=[])
        assert "manager" not in msg, (
            "monitor 'manager' must not appear in the cycle log: " + msg
        )
        assert "idle: hub macmini" in msg


# ---------------------------------------------------------------------------
# Composite Cycle Log with Pane-Diff Block (#56)
# ---------------------------------------------------------------------------

class TestCycleLogPaneDiff:
    """#56: surfaces the #42 pane-state signal in the cycle log as
    ``pane-active: ...`` (WORKING) and ``pane-stuck: ...(N cycles)``
    (UNKNOWN), without duplicating the hash computation. The
    watchdog populates ``_last_pane_state_by_agent`` during
    ``_check_unknown_agents`` (which runs BEFORE the log in the #56
    reordered run loop), and the log formatter reads it.

    Dual-reporting with the task-queue and MCP-active blocks is
    intentional per the issue body — same philosophy as #72.
    """

    def _make_watchdog(
        self,
        pane_states: dict[str, str] | None = None,
        stale_counts: dict[str, int] | None = None,
        tracker=None,
        monitored_agents=("hub", "macmini", "hassio", "RTX5090", "dgx", "dgx2"),
    ):
        """Construct an IdleWatchdog pre-populated with a given
        pane-state cache + stale-cycle-count map (as TmuxComm
        would have exposed them via `get_stale_cycle_count`).
        """
        from orchestrator.watchdog import IdleWatchdog
        cfg = {
            "agents": {
                name: {"runtime": "claude_code"}
                for name in monitored_agents
            },
            "watchdog": {"check_interval": 60, "idle_cooldown": 300},
        }
        stale_counts = stale_counts or {}
        tmux = MagicMock()
        tmux.get_stale_cycle_count = lambda a: stale_counts.get(a, 0)
        wd = IdleWatchdog(
            lifecycle=MagicMock(current_task=None),
            state_machine=MagicMock(current_state="idle", initial_state="idle"),
            nats_client=AsyncMock(),
            tmux_comm=tmux,
            config=cfg,
            task_queue=None,
            activity_tracker=tracker,
        )
        if pane_states is not None:
            wd._last_pane_state_by_agent = dict(pane_states)
        return wd

    def test_no_pane_state_cache_emits_no_pane_blocks(self):
        """Backward-compat: without pane-state data, the log omits
        both pane-active and pane-stuck sections."""
        wd = self._make_watchdog(pane_states=None)
        msg = wd._format_cycle_log(cycle=1, active=[])
        assert "pane-active" not in msg
        assert "pane-stuck" not in msg

    def test_working_state_populates_pane_active(self):
        wd = self._make_watchdog(pane_states={"hub": "working", "macmini": "working"})
        msg = wd._format_cycle_log(cycle=2, active=[])
        assert "pane-active: " in msg
        pane_active_section = msg.split("pane-active:")[1].split("|")[0]
        assert "hub" in pane_active_section
        assert "macmini" in pane_active_section
        idle_part = msg.split("idle:")[1] if "idle:" in msg else ""
        assert "hub" not in idle_part
        assert "macmini" not in idle_part

    def test_unknown_state_populates_pane_stuck_with_cycle_count(self):
        wd = self._make_watchdog(
            pane_states={"dgx": "unknown"},
            stale_counts={"dgx": 5},
        )
        msg = wd._format_cycle_log(cycle=3, active=[])
        assert "pane-stuck: dgx(5 cycles)" in msg
        idle_part = msg.split("idle:")[1] if "idle:" in msg else ""
        idle_names = idle_part.split()
        assert "dgx" not in idle_names, (
            "dgx (stuck) must not also appear in idle list; idle: "
            + repr(idle_names)
        )

    def test_idle_state_falls_through_to_idle_block(self):
        """#42 IDLE (hash unchanged + prompt visible) must NOT be
        counted as pane-stuck — that would misreport a happy
        waiting-at-prompt agent as a frozen process. Preserves the
        #42 positive-signal distinction."""
        wd = self._make_watchdog(pane_states={"hub": "idle"})
        msg = wd._format_cycle_log(cycle=4, active=[])
        assert "pane-active" not in msg
        assert "pane-stuck" not in msg
        assert "idle:" in msg
        assert "hub" in msg.split("idle:")[1]

    def test_capture_failed_falls_through_to_idle_block(self):
        """CAPTURE_FAILED is a tmux-session health issue, not an
        agent-pane signal. The #42 machinery emits a separate
        directive for it; the cycle log shouldn't misreport it as
        pane-stuck.
        """
        wd = self._make_watchdog(pane_states={"hassio": "capture_failed"})
        msg = wd._format_cycle_log(cycle=5, active=[])
        assert "pane-active" not in msg
        assert "pane-stuck" not in msg
        assert "hassio" in msg.split("idle:")[1]

    def test_pre_debounce_counts_as_working_not_stuck(self):
        """Per-#42 semantics: an agent that's been stale for less
        than `_STALE_DEBOUNCE_CYCLES` still returns `WORKING` from
        `get_pane_state`. That flows through to our cache as
        `"working"` so the cycle log calls it pane-active, NOT
        pane-stuck. This test locks the 2-cycle debounce boundary
        at the log-formatter level.
        """
        wd = self._make_watchdog(pane_states={"RTX5090": "working"})
        msg = wd._format_cycle_log(cycle=6, active=[])
        assert "pane-active: " in msg
        assert "RTX5090" in msg.split("pane-active:")[1].split("|")[0]
        assert "pane-stuck" not in msg

    def test_dual_report_task_queue_and_pane_active(self):
        """An agent listed in tasks.json AND whose pane is currently
        WORKING should appear in BOTH the task-queue block and the
        pane-active block. Same dual-reporting philosophy as #72.
        """
        wd = self._make_watchdog(pane_states={"hub": "working"})
        msg = wd._format_cycle_log(cycle=7, active=[("hub", "writing code")])
        # Task-queue block names hub.
        assert "agents assigned to in_progress tasks" in msg
        assert "['hub']" in msg
        # Pane-active block also names hub.
        assert "pane-active: hub" in msg
        # Idle block does NOT.
        idle_part = msg.split("idle:")[1] if "idle:" in msg else ""
        assert " hub" not in idle_part

    def test_pane_stuck_with_mcp_activity_dual_reports(self):
        """#44 guidance: a pane-stuck agent that also has recent
        MCP activity is likely running a long blocking tool call.
        The log should surface BOTH signals so the operator has the
        context — do not auto-suppress either.
        """
        from orchestrator.activity_tracker import ActivityTracker
        tracker = ActivityTracker()
        now = 1000.0
        tracker.touch("dgx", now=now - 2)
        wd = self._make_watchdog(
            pane_states={"dgx": "unknown"},
            stale_counts={"dgx": 3},
            tracker=tracker,
        )
        with patch("orchestrator.activity_tracker.time.time", return_value=now):
            msg = wd._format_cycle_log(cycle=8, active=[])
        # Both signals surface for dgx.
        assert "MCP-active: dgx(2s)" in msg
        assert "pane-stuck: dgx(3 cycles)" in msg
        idle_part = msg.split("idle:")[1] if "idle:" in msg else ""
        idle_names = idle_part.split()
        assert "dgx" not in idle_names, (
            "dgx (stuck + mcp-active) must not appear in idle list; "
            "idle: " + repr(idle_names)
        )

    def test_missing_get_stale_cycle_count_gracefully_degrades(self):
        """Older TmuxComm implementations (tests with bare MagicMock)
        don't expose `get_stale_cycle_count`. The log must still
        render the pane-stuck block — just without the cycle count
        suffix — instead of crashing or emitting a 0-cycles line.
        """
        from orchestrator.watchdog import IdleWatchdog
        cfg = {
            "agents": {
                "hub": {"runtime": "claude_code"},
                "dgx": {"runtime": "claude_code"},
            },
            "watchdog": {"check_interval": 60, "idle_cooldown": 300},
        }
        tmux = MagicMock(spec=[])  # strict: no attributes surface
        wd = IdleWatchdog(
            lifecycle=MagicMock(current_task=None),
            state_machine=MagicMock(current_state="idle", initial_state="idle"),
            nats_client=AsyncMock(),
            tmux_comm=tmux,
            config=cfg,
            task_queue=None,
            activity_tracker=None,
        )
        wd._last_pane_state_by_agent = {"dgx": "unknown"}
        msg = wd._format_cycle_log(cycle=9, active=[])
        # Bare "pane-stuck: dgx" (no cycle count suffix) is fine.
        assert "pane-stuck: dgx" in msg


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
        mock_tmux.nudge.assert_called_with(
            "macmini", force=True, source="orch.watchdog",
        )
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
        mock_tmux.nudge.assert_called_with(
            "manager", force=True, source="orch.watchdog",
        )


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
# Full Physical Line Extraction Tests (issue #33)
# ---------------------------------------------------------------------------


class TestExtractFullLineAndContext:
    """Unit tests for the `_extract_full_line_and_context` helper.

    These tests drive the #33 behavior directly: they feed synthetic
    pane captures into the helper and assert on the returned
    (matched_line, context_before) tuple, without going through the
    full `_check_agent_auth_failure` async path.
    """

    def _match(self, pane: str):
        m = _AUTH_ERROR_PATTERN.search(pane)
        assert m is not None, f"expected regex to match in pane: {pane!r}"
        return m

    def test_single_line_preserves_trailing_invalid_token(self):
        """Default #28 test case — the trailing `: Invalid token`
        suffix was being dropped by `match.group(0)`. After #33 it
        must be preserved in `matched_line`.
        """
        pane = (
            "some earlier output\n"
            "  ⎿  Error: MCP error -32603: Authentication failed: Invalid token\n"
            "❯ \n"
        )
        matched, ctx = _extract_full_line_and_context(pane, self._match(pane))
        assert "MCP error -32603:" in matched
        assert "Authentication failed" in matched
        assert "Invalid token" in matched, (
            f"trailing `: Invalid token` must survive #33 extraction, "
            f"got: {matched!r}"
        )

    def test_single_line_preserves_trailing_expired_jwt_context(self):
        """Real-world variant — operators might see richer trailing
        context like `: expired JWT` or `: revoked by admin`. Those
        must all survive the extraction.
        """
        pane = (
            "  ⎿  Error: MCP error -32603: Authentication failed: "
            "token expired 2h ago, see https://example/re-auth\n"
            "❯ \n"
        )
        matched, _ = _extract_full_line_and_context(pane, self._match(pane))
        assert "token expired 2h ago" in matched
        assert "https://example/re-auth" in matched

    def test_wrapped_line_at_52_cols_returns_full_content(self):
        """Narrow-pane wrap case — `MCP error -32603: Authentication
        failed: Invalid token` gets split across two visual lines at
        ~52 col width. The match span crosses the `\\n`, so extraction
        must walk backward to the prior `\\n` AND forward to the next
        `\\n` to capture both visual lines, then collapse the embedded
        newline into a space.
        """
        pane = (
            "prior line\n"
            "  ⎿  Error: MCP error -32603:\n"
            "     Authentication failed: Invalid token\n"
            "❯ \n"
        )
        matched, _ = _extract_full_line_and_context(pane, self._match(pane))
        assert "\n" not in matched
        assert "MCP error -32603:" in matched
        assert "Authentication failed" in matched
        assert "Invalid token" in matched

    def test_context_before_captured_when_error_on_its_own_line(self):
        """Claude's tool output sometimes renders `⎿ Error:` on one
        line and the actual error payload on the next. `context_before`
        should capture the preceding physical line so operators get
        the rendering context.
        """
        pane = (
            "  ⎿  Error:\n"
            "     MCP error -32603: Authentication failed: Invalid token\n"
            "❯ \n"
        )
        matched, ctx = _extract_full_line_and_context(pane, self._match(pane))
        assert "MCP error -32603" in matched
        assert "Error:" in ctx, (
            f"preceding line should be captured as context_before, "
            f"got ctx={ctx!r}"
        )

    def test_context_before_empty_when_match_on_first_line(self):
        """If the match is on the first physical line of the capture,
        there is no preceding line to capture — context_before must
        be an empty string, not None or a bogus substring.
        """
        pane = "MCP error -32603: Authentication failed: Invalid token\n"
        matched, ctx = _extract_full_line_and_context(pane, self._match(pane))
        assert "MCP error -32603" in matched
        assert ctx == ""

    def test_context_before_empty_when_match_at_pane_start_no_newline(self):
        """Pane has no newline before the match at all (single-line
        capture). context_before should be empty.
        """
        pane = "MCP error -32603: Authentication failed: Invalid token"
        matched, ctx = _extract_full_line_and_context(pane, self._match(pane))
        assert matched == (
            "MCP error -32603: Authentication failed: Invalid token"
        )
        assert ctx == ""

    def test_matched_line_capped_at_max_chars(self):
        """Defensive: a pathologically long pane line should be
        truncated to `_MATCHED_LINE_MAX_CHARS` (with an ellipsis
        suffix) so directive payloads don't balloon on garbage input.
        """
        long_suffix = "x" * 2000
        pane = (
            f"  ⎿  Error: MCP error -32603: Authentication failed: {long_suffix}\n"
        )
        matched, _ = _extract_full_line_and_context(pane, self._match(pane))
        assert len(matched) <= _MATCHED_LINE_MAX_CHARS
        assert matched.endswith("...")
        # The leading MCP prefix must still be present — we truncate
        # from the tail, not the head.
        assert matched.startswith("⎿ Error: MCP error -32603") or (
            "MCP error -32603:" in matched
        )

    def test_context_before_capped_at_max_chars(self):
        """Same cap rule for `context_before`."""
        long_context = "y" * 500
        pane = (
            f"  ⎿  {long_context}\n"
            "     MCP error -32603: Authentication failed: Invalid token\n"
        )
        _, ctx = _extract_full_line_and_context(pane, self._match(pane))
        assert len(ctx) <= _CONTEXT_BEFORE_MAX_CHARS
        assert ctx.endswith("...")

    def test_collapsed_whitespace_single_spaces(self):
        """Multiple consecutive spaces within a matched line should
        collapse to single spaces (same behavior as the original
        `" ".join(raw.split())` normalization).
        """
        pane = (
            "  ⎿  Error:    MCP error -32603:     Authentication failed\n"
        )
        matched, _ = _extract_full_line_and_context(pane, self._match(pane))
        assert "  " not in matched, (
            f"consecutive spaces should be collapsed, got: {matched!r}"
        )


class TestCheckAuthFailuresContextBefore:
    """Integration-style tests that drive #33 through the full
    `_check_agent_auth_failure` publish path and verify the directive
    payload carries both `matched_line` (with full trailing context)
    and `context_before` (when available).
    """

    @pytest.fixture
    def auth_tmux(self, mock_tmux):
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
    async def test_directive_carries_full_trailing_context(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """The published directive's matched_line must contain the
        full trailing context (`: Invalid token`), not the truncated
        `match.group(0)` form that #28 originally shipped.
        """
        pane = (
            "  ⎿  Error: MCP error -32603: Authentication failed: Invalid token\n"
            "❯ \n"
        )
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            pane if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        mock_nats.publish_to_inbox.assert_called_once()
        directive = mock_nats.publish_to_inbox.call_args[0][1]
        assert "MCP error -32603:" in directive["matched_line"]
        assert "Authentication failed" in directive["matched_line"]
        assert "Invalid token" in directive["matched_line"]

    @pytest.mark.asyncio
    async def test_directive_populates_context_before(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """When the match is preceded by a physical pane line, the
        directive must include it in `context_before` so operators
        can see the surrounding tool-output rendering.
        """
        pane = (
            "  ⎿  Error:\n"
            "     MCP error -32603: Authentication failed: Invalid token\n"
            "❯ \n"
        )
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            pane if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        directive = mock_nats.publish_to_inbox.call_args[0][1]
        assert "context_before" in directive
        assert "Error:" in directive["context_before"]

    @pytest.mark.asyncio
    async def test_directive_context_before_empty_when_no_prior_line(
        self, auth_watchdog, auth_tmux, mock_nats,
    ):
        """When the match is the first line of the pane, context_before
        is present as an empty string (schema stability for consumers).
        """
        pane = "MCP error -32603: Authentication failed: Invalid token\n"
        auth_tmux.capture_pane.side_effect = lambda agent, lines: (
            pane if agent == "RTX5090" else "❯ \n"
        )
        await auth_watchdog._check_auth_failures()
        directive = mock_nats.publish_to_inbox.call_args[0][1]
        assert "context_before" in directive
        assert directive["context_before"] == ""


# ---------------------------------------------------------------------------
# Pane-State Detection Tests (issue #42)
# ---------------------------------------------------------------------------


class TestCheckUnknownAgents:
    """Integration-style tests for IdleWatchdog._check_unknown_agents
    that drive the new #42 pane-state path end-to-end. Uses string
    sentinels ("working", "idle", "unknown", "capture_failed") from
    mock tmux_comm so the tests do not have to import the real
    AgentPaneState enum — the watchdog late-binds via `.value`.
    """

    @pytest.fixture
    def state_tmux(self, mock_tmux):
        mock_tmux.get_pane_mapping = MagicMock(
            return_value={"hub": 0, "macmini": 1, "RTX5090": 4},
        )
        mock_tmux.get_pane_state = MagicMock(return_value="working")
        return mock_tmux

    @pytest.fixture
    def state_watchdog(
        self, mock_lifecycle, mock_state_machine, mock_nats,
        state_tmux, mock_task_queue, config,
    ):
        return IdleWatchdog(
            lifecycle=mock_lifecycle,
            state_machine=mock_state_machine,
            nats_client=mock_nats,
            tmux_comm=state_tmux,
            config=config,
            task_queue=mock_task_queue,
        )

    @pytest.mark.asyncio
    async def test_all_working_no_alerts(self, state_watchdog, state_tmux, mock_nats):
        """Every agent WORKING → no directive published."""
        state_tmux.get_pane_state.return_value = "working"
        await state_watchdog._check_unknown_agents()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_idle_no_unknown_alerts(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """Every agent IDLE → no agent_pane_unknown directive
        (IDLE is handled by the existing _check_idle_agents path,
        not by _check_unknown_agents).
        """
        state_tmux.get_pane_state.return_value = "idle"
        await state_watchdog._check_unknown_agents()
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_state_publishes_directive(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """One agent in UNKNOWN state → one manager_directive with
        subtype agent_pane_unknown.
        """
        def _state(agent):
            return "unknown" if agent == "RTX5090" else "working"
        state_tmux.get_pane_state.side_effect = _state

        await state_watchdog._check_unknown_agents()

        mock_nats.publish_to_inbox.assert_called_once()
        call = mock_nats.publish_to_inbox.call_args[0]
        assert call[0] == "manager"
        directive = call[1]
        assert directive["type"] == "manager_directive"
        assert directive["subtype"] == "agent_pane_unknown"
        assert directive["agent"] == "RTX5090"
        assert directive["priority"] == "high"
        assert "message" in directive
        # #44: directive must name the long-tool-call false-positive
        # scenario and point operators at tasks.json / outbox activity
        # before assuming the agent crashed.
        message = directive["message"]
        assert "ticking" in message
        assert "tasks.json" in message
        assert "outbox" in message

    @pytest.mark.asyncio
    async def test_capture_failed_publishes_different_subtype(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """CAPTURE_FAILED must produce `agent_pane_capture_failed`
        subtype, not `agent_pane_unknown`. Operators troubleshoot
        these differently (tmux session vs agent health).
        """
        def _state(agent):
            return "capture_failed" if agent == "macmini" else "working"
        state_tmux.get_pane_state.side_effect = _state

        await state_watchdog._check_unknown_agents()

        mock_nats.publish_to_inbox.assert_called_once()
        directive = mock_nats.publish_to_inbox.call_args[0][1]
        assert directive["subtype"] == "agent_pane_capture_failed"
        assert directive["agent"] == "macmini"

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeat_unknown(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """Two consecutive scans with the same UNKNOWN agent →
        only ONE directive (cooldown suppression).
        """
        state_tmux.get_pane_state.return_value = "unknown"
        await state_watchdog._check_unknown_agents()
        await state_watchdog._check_unknown_agents()
        # 3 agents × 1 alert each, not 3 × 2 = 6
        assert mock_nats.publish_to_inbox.call_count == 3

    @pytest.mark.asyncio
    async def test_unknown_cooldown_isolated_from_capture_failed(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """A UNKNOWN alert on agent A must not suppress a subsequent
        CAPTURE_FAILED alert on agent A — different subtypes, different
        suppression tracks.
        """
        # First pass: unknown on hub
        state_tmux.get_pane_state.side_effect = (
            lambda a: "unknown" if a == "hub" else "working"
        )
        await state_watchdog._check_unknown_agents()

        # Second pass: capture_failed on hub (different suppression track)
        state_tmux.get_pane_state.side_effect = (
            lambda a: "capture_failed" if a == "hub" else "working"
        )
        await state_watchdog._check_unknown_agents()

        assert mock_nats.publish_to_inbox.call_count == 2
        first = mock_nats.publish_to_inbox.call_args_list[0][0][1]
        second = mock_nats.publish_to_inbox.call_args_list[1][0][1]
        assert first["subtype"] == "agent_pane_unknown"
        assert second["subtype"] == "agent_pane_capture_failed"

    @pytest.mark.asyncio
    async def test_cooldown_expires_allows_realert(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """After unknown_alert_cooldown seconds, a still-UNKNOWN
        agent re-alerts.
        """
        state_tmux.get_pane_state.return_value = "unknown"
        await state_watchdog._check_unknown_agents()
        assert mock_nats.publish_to_inbox.call_count == 3

        # Rewind the per-agent cooldown so the next call re-alerts
        for agent in ("hub", "macmini", "RTX5090"):
            state_watchdog._last_unknown_alert[agent] = (
                time.time() - state_watchdog._unknown_alert_cooldown - 1
            )

        await state_watchdog._check_unknown_agents()
        assert mock_nats.publish_to_inbox.call_count == 6

    @pytest.mark.asyncio
    async def test_hub_is_scanned_unlike_auth_failure_path(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """#42 pane-diff detection does NOT exclude hub (unlike #28
        auth-failure scan which had to exclude hub due to the
        self-reference trap). Pane-diff is immune to the trap —
        hub gets the same UNKNOWN detection as every other agent.
        This backfills the #28 hub blind spot.
        """
        state_tmux.get_pane_state.side_effect = (
            lambda a: "unknown" if a == "hub" else "working"
        )
        await state_watchdog._check_unknown_agents()
        mock_nats.publish_to_inbox.assert_called_once()
        directive = mock_nats.publish_to_inbox.call_args[0][1]
        assert directive["agent"] == "hub"

    @pytest.mark.asyncio
    async def test_missing_get_pane_state_is_noop(
        self, mock_lifecycle, mock_state_machine, mock_nats, config,
    ):
        """Watchdog tolerates a tmux_comm without get_pane_state —
        e.g. legacy tests, partial mocks. Must not raise.
        """
        class _BareTmux:
            def get_pane_mapping(self):
                return {"hub": 0}
            # No get_pane_state attribute at all

        wd = IdleWatchdog(
            lifecycle=mock_lifecycle,
            state_machine=mock_state_machine,
            nats_client=mock_nats,
            tmux_comm=_BareTmux(),
            config=config,
            task_queue=None,
        )
        await wd._check_unknown_agents()  # must not raise
        mock_nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_directive_body_shape_is_wire_ready(
        self, state_watchdog, state_tmux, mock_nats,
    ):
        """Pin the body shape owned by the watchdog. Envelope
        fields (message_id, timestamp, from) are added downstream
        by NatsClient._envelope_wrap per #34, not here.
        """
        state_tmux.get_pane_state.side_effect = (
            lambda a: "unknown" if a == "RTX5090" else "working"
        )
        await state_watchdog._check_unknown_agents()
        directive = mock_nats.publish_to_inbox.call_args[0][1]

        # Body fields owned by watchdog
        assert directive["type"] == "manager_directive"
        assert directive["subtype"] == "agent_pane_unknown"
        assert directive["agent"] == "RTX5090"
        assert directive["priority"] == "high"
        assert "message" in directive

        # Envelope fields must NOT be set here — library's job
        assert "message_id" not in directive
        assert "timestamp" not in directive
        assert "from" not in directive


class TestStateCouplingRegression:
    """**Regression tests for the #43 review finding.**

    macmini caught a bug where the watchdog's idle-check path and
    unknown-check path both advanced :class:`TmuxComm`'s pane-state
    debounce counters because :meth:`is_agent_idle` was wrapped
    around :meth:`get_pane_state`. CAPTURE_FAILED fired on the
    first capture failure instead of the second; UNKNOWN
    effective debounce was ~1 cycle instead of 2.

    These tests use a **real TmuxComm** (not a mock) with a mocked
    ``capture_pane`` method, to exercise the actual state-machine
    interactions between ``is_agent_idle`` (called from
    ``_check_idle_agents``) and ``get_pane_state`` (called from
    ``_check_unknown_agents``). Unit-test mocks of ``is_agent_idle``
    cannot see this coupling because they short-circuit the real
    method.
    """

    @pytest.fixture
    def real_tmux_config(self):
        return {
            "tmux": {
                "session_name": "regression-test",
                "nudge_prompt": "nudge",
                "nudge_cooldown_seconds": 30,
                "max_nudge_retries": 5,
            },
            "agents": {
                "hub": {"runtime": "claude_code", "working_dir": "."},
                "qa": {"runtime": "claude_code", "working_dir": "."},
            },
        }

    @pytest.fixture
    def real_tmux(self, real_tmux_config):
        from orchestrator.tmux_comm import TmuxComm
        return TmuxComm(real_tmux_config)

    @pytest.fixture
    def real_watchdog(
        self, mock_lifecycle, mock_state_machine, mock_nats,
        real_tmux, real_tmux_config,
    ):
        return IdleWatchdog(
            lifecycle=mock_lifecycle,
            state_machine=mock_state_machine,
            nats_client=mock_nats,
            tmux_comm=real_tmux,
            config={
                "watchdog": {"check_interval": 15, "idle_cooldown": 60},
            },
        )

    @pytest.mark.asyncio
    async def test_idle_check_does_not_leak_into_unknown_debounce(
        self, real_watchdog, real_tmux, mock_nats,
    ):
        """Full watchdog cycle with capture failing: idle check fires
        first (via is_agent_idle), then unknown check fires second
        (via get_pane_state). After ONE full cycle,
        ``_consecutive_capture_failures`` must be exactly 1 (not 2),
        and no CAPTURE_FAILED directive must have been published.

        Pre-fix behavior: ``is_agent_idle`` advanced the counter
        to 1, then ``get_pane_state`` advanced it to 2 and fired
        CAPTURE_FAILED on the first cycle.
        """
        real_tmux.capture_pane = MagicMock(return_value=None)

        # Simulate one watchdog cycle: _check_idle_agents runs first
        # (calls is_agent_idle for each active agent), then
        # _check_unknown_agents runs (calls get_pane_state for each
        # agent from get_pane_mapping).
        await real_watchdog._check_idle_agents()
        await real_watchdog._check_unknown_agents()

        # After ONE cycle of capture-failure: counter should be 1
        # per agent (advanced only by get_pane_state), not 2.
        # CAPTURE_FAILED has _STALE_DEBOUNCE_CYCLES = 2, so NO alert.
        for agent in ("hub", "qa"):
            assert real_tmux._consecutive_capture_failures.get(agent, 0) == 1, (
                f"{agent}: expected counter=1 after one cycle (get_pane_state "
                f"advance only), got "
                f"{real_tmux._consecutive_capture_failures.get(agent, 0)} "
                f"— is_agent_idle is leaking into the debounce counter again"
            )

        # Verify no CAPTURE_FAILED directive was published
        for call in mock_nats.publish_to_inbox.call_args_list:
            directive = call[0][1]
            assert directive.get("subtype") != "agent_pane_capture_failed", (
                "CAPTURE_FAILED fired on the first capture-failure cycle "
                "— the debounce contract has regressed"
            )

    @pytest.mark.asyncio
    async def test_two_cycles_of_capture_failure_fires_exactly_once(
        self, real_watchdog, real_tmux, mock_nats,
    ):
        """After TWO full watchdog cycles of capture failure, exactly
        one CAPTURE_FAILED directive per agent must fire.

        This is the contract the debounce was designed around and
        the test that pre-fix would have failed (it would have fired
        after cycle 1 and again after cycle 2, producing two alerts
        per agent).
        """
        real_tmux.capture_pane = MagicMock(return_value=None)

        # Cycle 1: counter → 1, no alert
        await real_watchdog._check_idle_agents()
        await real_watchdog._check_unknown_agents()
        assert mock_nats.publish_to_inbox.call_count == 0

        # Cycle 2: counter → 2, CAPTURE_FAILED fires once per agent
        await real_watchdog._check_idle_agents()
        await real_watchdog._check_unknown_agents()

        subtypes = [
            call[0][1].get("subtype")
            for call in mock_nats.publish_to_inbox.call_args_list
        ]
        capture_failed_count = sum(
            1 for s in subtypes if s == "agent_pane_capture_failed"
        )
        assert capture_failed_count == 2, (
            f"expected exactly 2 CAPTURE_FAILED directives (one per "
            f"scanned agent) after 2 cycles, got {capture_failed_count}. "
            f"All directives: {subtypes}"
        )

    @pytest.mark.asyncio
    async def test_delivery_probe_calls_do_not_advance_debounce(
        self, real_watchdog, real_tmux, mock_nats,
    ):
        """Simulate the delivery protocol probing agents via
        ``is_agent_idle`` mid-cycle (as ``delivery._probe_neighbors``
        does via ``asyncio.to_thread``). Those probes must NOT
        advance the pane-state debounce counters — only the
        watchdog's explicit ``get_pane_state`` call from
        ``_check_unknown_agents`` owns the counter.
        """
        real_tmux.capture_pane = MagicMock(return_value=None)

        # Delivery probes fire between watchdog cycles — simulate
        # a dozen is_agent_idle calls from the probe thread pool.
        for _ in range(12):
            real_tmux.is_agent_idle("hub")
            real_tmux.is_agent_idle("qa")

        # Counters must still be at 0 — no get_pane_state calls yet
        for agent in ("hub", "qa"):
            assert real_tmux._consecutive_capture_failures.get(agent, 0) == 0
            assert real_tmux._consecutive_stale_cycles.get(agent, 0) == 0

        # Now run one watchdog cycle — counter advances to exactly 1
        await real_watchdog._check_unknown_agents()
        for agent in ("hub", "qa"):
            assert real_tmux._consecutive_capture_failures.get(agent, 0) == 1

        # No directive fired (debounce intact)
        for call in mock_nats.publish_to_inbox.call_args_list:
            assert call[0][1].get("subtype") != "agent_pane_capture_failed"
