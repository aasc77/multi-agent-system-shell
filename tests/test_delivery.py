"""Tests for the Delivery Protocol (orchestrator/delivery.py).

Covers: neighbor probing, mailbox flags, ACK processing, soft ACK,
retransmit/backoff, dead letters, grace period, push notification.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.delivery import (
    ACK_MESSAGE_TYPE,
    DeliveryProtocol,
    MailboxFlag,
    NeighborState,
    _STARTUP_GRACE_PERIOD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**routing_overrides):
    routing = {
        "probe_interval": 10,
        "process_interval": 5,
        "neighbor_timeout": 120,
        "max_attempts": 6,
        "table_log_interval": 60,
    }
    routing.update(routing_overrides)
    return {
        "agents": {
            "manager": {"role": "monitor", "runtime": "claude_code"},
            "hub": {"runtime": "claude_code"},
            "dgx2": {"runtime": "claude_code"},
            "macmini": {"runtime": "claude_code"},
        },
        "routing": routing,
    }


def _make_tmux(idle_agents=None, reachable_agents=None):
    idle = set(idle_agents or [])
    reachable = set(reachable_agents or [])
    mock = MagicMock()
    mock.is_agent_idle.side_effect = lambda a: a in idle
    mock.capture_pane.side_effect = lambda a, lines=1: "busy" if a in reachable else None
    mock.nudge.return_value = True
    return mock


def _age_neighbors(dp):
    """Set all neighbors past the startup grace period."""
    for n in dp._neighbors.values():
        n.last_state_change = time.time() - _STARTUP_GRACE_PERIOD - 1


def _make_ack_msg(agent, count=1):
    payload = {
        "type": ACK_MESSAGE_TYPE,
        "agent": agent,
        "count": count,
        "timestamp": "2026-04-06T00:00:00Z",
    }
    return SimpleNamespace(data=json.dumps(payload).encode())


# ---------------------------------------------------------------------------
# Neighbor table tests
# ---------------------------------------------------------------------------


class TestNeighborProbe:
    def test_builds_table_including_all_agents(self):
        """All agents including monitors should be in the neighbor table."""
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        assert "hub" in dp._neighbors
        assert "dgx2" in dp._neighbors
        assert "macmini" in dp._neighbors
        assert "manager" in dp._neighbors  # monitors included now

    @pytest.mark.asyncio
    async def test_probe_marks_idle_as_up(self):
        tmux = _make_tmux(idle_agents=["hub", "dgx2"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.UP
        assert dp._neighbors["dgx2"].state == NeighborState.UP

    @pytest.mark.asyncio
    async def test_probe_marks_busy_as_busy(self):
        tmux = _make_tmux(idle_agents=[], reachable_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.BUSY

    @pytest.mark.asyncio
    async def test_probe_marks_unreachable_as_down(self):
        tmux = _make_tmux(idle_agents=[], reachable_agents=[])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.DOWN

    @pytest.mark.asyncio
    async def test_probe_exception_marks_down(self):
        tmux = _make_tmux()
        tmux.is_agent_idle.side_effect = RuntimeError("pane gone")
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.DOWN


# ---------------------------------------------------------------------------
# Deliver + mailbox flag tests
# ---------------------------------------------------------------------------


class TestDeliver:
    @pytest.mark.asyncio
    async def test_deliver_sets_pending_and_nudges(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="task_assignment")

        assert dp._neighbors["hub"].mailbox.pending is True
        assert dp._neighbors["hub"].mailbox.attempt == 1
        tmux.nudge.assert_called_once_with(
            "hub", force=True, source="orch.delivery",
        )

    @pytest.mark.asyncio
    async def test_deliver_while_pending_still_nudges(self):
        """New message while already pending should nudge again."""
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="msg1")
        dp.deliver("hub", reason="msg2")

        assert dp._neighbors["hub"].mailbox.pending is True
        assert tmux.nudge.call_count == 2  # both nudge

    @pytest.mark.asyncio
    async def test_deliver_to_different_agents(self):
        tmux = _make_tmux(idle_agents=["hub", "dgx2"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="msg1")
        dp.deliver("dgx2", reason="msg2")

        assert dp._neighbors["hub"].mailbox.pending is True
        assert dp._neighbors["dgx2"].mailbox.pending is True

    @pytest.mark.asyncio
    async def test_deliver_skips_nudge_when_down(self):
        tmux = _make_tmux(idle_agents=[], reachable_agents=[])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()

        dp.deliver("hub", reason="test")

        assert dp._neighbors["hub"].mailbox.pending is True
        assert dp._neighbors["hub"].mailbox.attempt == 1
        tmux.nudge.assert_not_called()

    @pytest.mark.asyncio
    async def test_deliver_to_monitor_agent_one_shot_nudge(self):
        """Monitor agents get a one-shot nudge but no pending tracking."""
        tmux = _make_tmux(idle_agents=["manager"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("manager", reason="agent_message from hassio")

        # Nudge was sent exactly once
        tmux.nudge.assert_called_once_with(
            "manager", force=True, source="orch.delivery",
        )
        # But no pending state or attempt tracking
        assert dp._neighbors["manager"].mailbox.pending is False
        assert dp._neighbors["manager"].mailbox.attempt == 0
        assert dp._neighbors["manager"].mailbox.escalated is False

    @pytest.mark.asyncio
    async def test_process_mailboxes_skips_monitor_agents(self):
        """_process_mailboxes must never touch monitor agents, even if
        pending were somehow set on them."""
        tmux = _make_tmux(idle_agents=["manager", "hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        # Artificially force a pending flag on the monitor (shouldn't
        # happen in practice, but prove the guard holds).
        dp._neighbors["manager"].mailbox.pending = True
        dp._neighbors["manager"].mailbox.attempt = 1
        dp._neighbors["manager"].mailbox.last_nudge = time.time() - 9999

        dp._process_mailboxes()

        # Monitor was not re-nudged — attempt count stayed at 1
        assert dp._neighbors["manager"].mailbox.attempt == 1
        tmux.nudge.assert_not_called()

    @pytest.mark.asyncio
    @patch("orchestrator.delivery.subprocess")
    async def test_repeated_deliver_to_monitor_never_escalates(
        self, mock_subprocess,
    ):
        """Calling deliver() N times to a monitor produces N nudges,
        never flips pending, never triggers the Pushover escalation path."""
        tmux = _make_tmux(idle_agents=["manager"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        for i in range(10):
            dp.deliver("manager", reason=f"msg-{i}")

        assert tmux.nudge.call_count == 10
        assert dp._neighbors["manager"].mailbox.pending is False
        assert dp._neighbors["manager"].mailbox.attempt == 0
        assert dp._neighbors["manager"].mailbox.escalated is False
        # Pushover subprocess should never have been invoked
        mock_subprocess.run.assert_not_called()

    def test_deliver_to_unknown_agent_creates_entry(self):
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        dp.deliver("new_agent", reason="test")
        assert "new_agent" in dp._neighbors
        assert dp._neighbors["new_agent"].mailbox.pending is True


# ---------------------------------------------------------------------------
# ACK tests
# ---------------------------------------------------------------------------


class TestAck:
    @pytest.mark.asyncio
    async def test_ack_clears_pending(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        await dp.handle_ack_message(_make_ack_msg("hub"))

        assert dp._neighbors["hub"].mailbox.pending is False
        assert dp._neighbors["hub"].mailbox.attempt == 0

    @pytest.mark.asyncio
    async def test_ack_updates_rtt(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")
        dp._neighbors["hub"].mailbox.last_nudge = time.time() - 5.0

        await dp.handle_ack_message(_make_ack_msg("hub"))

        assert dp._neighbors["hub"].rtt_ema > 0

    @pytest.mark.asyncio
    async def test_ack_for_unknown_agent_no_crash(self):
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        await dp.handle_ack_message(_make_ack_msg("nonexistent"))

    @pytest.mark.asyncio
    async def test_ack_with_no_pending_no_crash(self):
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(idle_agents=["hub"]), config=_make_config(),
        )
        await dp._probe_neighbors()
        await dp.handle_ack_message(_make_ack_msg("hub"))
        assert dp._neighbors["hub"].last_ack > 0

    @pytest.mark.asyncio
    async def test_ack_invalid_json_no_crash(self):
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        await dp.handle_ack_message(SimpleNamespace(data=b"not json"))


# ---------------------------------------------------------------------------
# Soft ACK tests
# ---------------------------------------------------------------------------


class TestSoftAck:
    @pytest.mark.asyncio
    async def test_busy_to_up_clears_pending(self):
        """Agent goes BUSY -> UP while pending -> soft ACK clears flag."""
        tmux_busy = _make_tmux(idle_agents=[], reachable_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux_busy, config=_make_config())
        await dp._probe_neighbors()  # hub = BUSY

        dp._neighbors["hub"].mailbox.pending = True
        dp._neighbors["hub"].mailbox.attempt = 2

        # Now agent returns to idle
        tmux_idle = _make_tmux(idle_agents=["hub"])
        dp._tmux_comm = tmux_idle
        await dp._probe_neighbors()  # hub BUSY -> UP

        assert dp._neighbors["hub"].mailbox.pending is False
        assert dp._neighbors["hub"].mailbox.attempt == 0

    @pytest.mark.asyncio
    async def test_down_to_up_does_not_soft_ack(self):
        """DOWN -> UP should NOT clear pending (agent wasn't working)."""
        tmux_down = _make_tmux(idle_agents=[], reachable_agents=[])
        dp = DeliveryProtocol(tmux_comm=tmux_down, config=_make_config())
        await dp._probe_neighbors()  # hub = DOWN

        dp._neighbors["hub"].mailbox.pending = True

        tmux_idle = _make_tmux(idle_agents=["hub"])
        dp._tmux_comm = tmux_idle
        await dp._probe_neighbors()  # hub DOWN -> UP

        assert dp._neighbors["hub"].mailbox.pending is True  # NOT cleared

    @pytest.mark.asyncio
    async def test_busy_to_up_without_pending_is_noop(self):
        """BUSY -> UP with no pending mail should not crash."""
        tmux_busy = _make_tmux(idle_agents=[], reachable_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux_busy, config=_make_config())
        await dp._probe_neighbors()

        tmux_idle = _make_tmux(idle_agents=["hub"])
        dp._tmux_comm = tmux_idle
        await dp._probe_neighbors()  # no crash


# ---------------------------------------------------------------------------
# Retransmit / backoff tests
# ---------------------------------------------------------------------------


class TestRetransmit:
    @pytest.mark.asyncio
    async def test_process_renudges_after_backoff(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        # Force last_nudge into the past
        dp._neighbors["hub"].mailbox.last_nudge = time.time() - 60
        dp._process_mailboxes()

        assert dp._neighbors["hub"].mailbox.attempt == 2
        assert tmux.nudge.call_count == 2

    @pytest.mark.asyncio
    async def test_process_skips_during_backoff(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        # last_nudge is recent — backoff not elapsed
        dp._process_mailboxes()

        assert dp._neighbors["hub"].mailbox.attempt == 1  # no extra nudge

    @pytest.mark.asyncio
    async def test_dead_letter_after_max_attempts(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config(max_attempts=2))
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        dp._neighbors["hub"].mailbox.attempt = 2
        dp._neighbors["hub"].mailbox.last_nudge = time.time() - 9999
        dp._process_mailboxes()

        assert dp._neighbors["hub"].mailbox.pending is False  # cleared

    @pytest.mark.asyncio
    async def test_dead_letter_callback_fires(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config(max_attempts=2))
        await dp._probe_neighbors()
        _age_neighbors(dp)

        callback = MagicMock()
        dp.set_dead_letter_callback(callback)

        dp.deliver("hub", reason="test")
        dp._neighbors["hub"].mailbox.attempt = 2
        dp._neighbors["hub"].mailbox.last_nudge = time.time() - 9999
        dp._process_mailboxes()

        callback.assert_called_once_with("hub", 2)


# ---------------------------------------------------------------------------
# Push notification tests
# ---------------------------------------------------------------------------


class TestPushNotify:
    @pytest.mark.asyncio
    @patch("orchestrator.delivery.subprocess")
    async def test_push_fires_at_escalation(self, mock_subprocess):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        mb = dp._neighbors["hub"].mailbox
        mb.attempt = 4  # backoff = 3600s (escalation threshold)
        mb.last_nudge = time.time() - 9999

        mock_subprocess.run.return_value = MagicMock(returncode=0)
        dp._process_mailboxes()

        mock_subprocess.run.assert_called_once()
        assert mb.escalated is True

    @pytest.mark.asyncio
    @patch("orchestrator.delivery.subprocess")
    async def test_push_only_fires_once(self, mock_subprocess):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        mb = dp._neighbors["hub"].mailbox
        mb.attempt = 4
        mb.escalated = True  # already sent
        mb.last_nudge = time.time() - 9999

        mock_subprocess.run.return_value = MagicMock(returncode=0)
        dp._process_mailboxes()

        mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# Grace period tests
# ---------------------------------------------------------------------------


class TestGracePeriod:
    @pytest.mark.asyncio
    async def test_grace_defers_nudge(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()  # just came UP

        dp.deliver("hub", reason="test")

        tmux.nudge.assert_not_called()
        assert dp._neighbors["hub"].mailbox.pending is True

    @pytest.mark.asyncio
    async def test_grace_allows_after_period(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="test")

        tmux.nudge.assert_called_once()


# ---------------------------------------------------------------------------
# Status reporting tests
# ---------------------------------------------------------------------------


class TestStatusReporting:
    @pytest.mark.asyncio
    async def test_get_neighbor_table_includes_all(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()

        table = dp.get_neighbor_table()
        assert "hub" in table
        assert "manager" in table
        assert table["hub"]["state"] == "UP"

    def test_get_queue_status_empty(self):
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        assert dp.get_queue_status() == []

    @pytest.mark.asyncio
    async def test_get_queue_status_with_pending(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test_reason")

        status = dp.get_queue_status()
        assert len(status) == 1
        assert status[0]["target"] == "hub"
        assert status[0]["reason"] == "test_reason"

    @pytest.mark.asyncio
    async def test_log_route_table_no_crash(self):
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        await dp._probe_neighbors()
        dp._log_route_table()


# ---------------------------------------------------------------------------
# Re-delivery after ACK
# ---------------------------------------------------------------------------


class TestReDeliveryAfterAck:
    @pytest.mark.asyncio
    async def test_can_deliver_again_after_ack(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="first")
        await dp.handle_ack_message(_make_ack_msg("hub"))
        assert dp._neighbors["hub"].mailbox.pending is False

        dp.deliver("hub", reason="second")
        assert dp._neighbors["hub"].mailbox.pending is True
        assert tmux.nudge.call_count == 2


class TestPaneStateGate:
    """#9: ``DeliveryProtocol._attempt_nudge`` must consult the
    shared pane-state cache and DEFER (without incrementing the
    attempt counter) when the recipient is ``WORKING``. Other
    states fall through to the normal nudge path.

    These tests hit ``_attempt_nudge`` directly by calling
    ``deliver`` and ``_process_retransmits`` so we exercise both
    the first-send path and the retransmit path.
    """

    def _make_dp(self, cache, *, idle=None, reachable=None):
        from orchestrator.delivery import DeliveryProtocol
        tmux = _make_tmux(
            idle_agents=idle or ["hub"],
            reachable_agents=reachable or ["hub"],
        )
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config(),
            pane_state_cache=cache,
        )
        _age_neighbors(dp)
        return dp, tmux

    @pytest.mark.asyncio
    async def test_working_state_defers_without_incrementing_attempt(self):
        """WORKING state → the nudge is skipped AND the attempt
        counter stays put. This is the load-bearing invariant: we
        must not falsely escalate to dead-letter after 6 deferrals
        while the agent is just consuming a long turn."""
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        cache.set("hub", "working")
        dp, tmux = self._make_dp(cache)

        dp.deliver("hub", reason="first")

        assert tmux.nudge.call_count == 0, (
            "WORKING state must not produce a real tmux nudge"
        )
        assert dp._neighbors["hub"].mailbox.pending is True, (
            "mail stays pending — we didn't actually deliver"
        )
        assert dp._neighbors["hub"].mailbox.attempt == 0, (
            "WORKING-defer must not increment the attempt counter "
            "(otherwise repeated WORKING cycles would falsely escalate)"
        )

    @pytest.mark.asyncio
    async def test_working_deferral_across_many_cycles_does_not_escalate(self):
        """Regression guard for the attempt-counter invariant:
        even after 10 consecutive WORKING-deferrals, ``attempt``
        stays at 0 and no dead-letter escalation fires.
        """
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        cache.set("hub", "working")
        dp, tmux = self._make_dp(cache)

        # First deliver → pending=True, attempt stays 0 (deferred).
        dp.deliver("hub", reason="first")
        # Simulate 9 more retransmit cycles while still WORKING.
        for _ in range(9):
            dp._attempt_nudge(dp._neighbors["hub"])

        assert tmux.nudge.call_count == 0
        assert dp._neighbors["hub"].mailbox.attempt == 0
        assert dp._neighbors["hub"].mailbox.escalated is False

    @pytest.mark.asyncio
    async def test_idle_state_nudges_normally(self):
        """IDLE (❯ prompt visible) → nudge as usual."""
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        cache.set("hub", "idle")
        dp, tmux = self._make_dp(cache)

        dp.deliver("hub", reason="first")

        assert tmux.nudge.call_count == 1
        assert dp._neighbors["hub"].mailbox.attempt == 1

    @pytest.mark.asyncio
    async def test_unknown_state_still_nudges(self):
        """UNKNOWN (#42 stuck) → still nudge. The #42 directive
        already fires separately; delivery's job here is to keep
        trying in case the agent is a hung process that needs a
        wake-up."""
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        cache.set("hub", "unknown")
        dp, tmux = self._make_dp(cache)

        dp.deliver("hub", reason="first")

        assert tmux.nudge.call_count == 1
        assert dp._neighbors["hub"].mailbox.attempt == 1

    @pytest.mark.asyncio
    async def test_capture_failed_state_still_nudges(self):
        """CAPTURE_FAILED → fall back to existing behavior (can't
        tell the real state, so send)."""
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        cache.set("hub", "capture_failed")
        dp, tmux = self._make_dp(cache)

        dp.deliver("hub", reason="first")

        assert tmux.nudge.call_count == 1
        assert dp._neighbors["hub"].mailbox.attempt == 1

    @pytest.mark.asyncio
    async def test_none_cache_preserves_pre_pr_behavior(self):
        """``pane_state_cache=None`` → nudge unconditionally, same
        as pre-#9 behavior. Ensures we haven't silently broken
        orchestrator setups that don't wire the cache through."""
        from orchestrator.delivery import DeliveryProtocol
        tmux = _make_tmux(idle_agents=["hub"], reachable_agents=["hub"])
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config(),
            pane_state_cache=None,
        )
        _age_neighbors(dp)

        dp.deliver("hub", reason="first")

        assert tmux.nudge.call_count == 1
        assert dp._neighbors["hub"].mailbox.attempt == 1

    @pytest.mark.asyncio
    async def test_unseen_agent_in_cache_falls_through(self):
        """Cache wired but recipient never observed by watchdog
        (e.g. first cycle, no pane-state yet) → fall back to
        existing nudge behavior. Otherwise the first delivery
        after orchestrator boot would falsely defer indefinitely.
        """
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        # Cache is empty — no entry for hub.
        dp, tmux = self._make_dp(cache)

        dp.deliver("hub", reason="first")

        assert tmux.nudge.call_count == 1
        assert dp._neighbors["hub"].mailbox.attempt == 1

    @pytest.mark.asyncio
    async def test_working_then_idle_transition_delivers_on_cycle_two(self):
        """Integration-shaped: cycle 1 DEFER (WORKING), cycle 2
        SEND (IDLE). Simulates the real transition pattern the
        gate is designed for."""
        from orchestrator.pane_state_cache import PaneStateCache
        cache = PaneStateCache()
        cache.set("hub", "working")
        dp, tmux = self._make_dp(cache)

        # Cycle 1: WORKING → defer
        dp.deliver("hub", reason="first")
        assert tmux.nudge.call_count == 0
        assert dp._neighbors["hub"].mailbox.attempt == 0

        # Cycle 2: agent transitions to IDLE; retransmit fires.
        cache.set("hub", "idle")
        dp._attempt_nudge(dp._neighbors["hub"])
        assert tmux.nudge.call_count == 1
        assert dp._neighbors["hub"].mailbox.attempt == 1

    @pytest.mark.asyncio
    async def test_working_defer_does_not_prevent_escalation_when_agent_recovers(self):
        """Belt-and-suspenders: after a WORKING-defer burst, if
        the agent transitions to DOWN the normal DOWN-escalation
        path must still fire. Guards against \"we deferred so long
        the agent died silently\" regressions."""
        from orchestrator.pane_state_cache import PaneStateCache
        from orchestrator.delivery import NeighborState
        cache = PaneStateCache()
        cache.set("hub", "working")
        dp, tmux = self._make_dp(cache)

        dp.deliver("hub", reason="first")  # deferred, attempt=0
        # Agent goes DOWN — simulate probe result.
        dp._neighbors["hub"].state = NeighborState.DOWN
        dp._attempt_nudge(dp._neighbors["hub"])
        assert dp._neighbors["hub"].mailbox.attempt == 1, (
            "DOWN path must still increment attempt even if WORKING "
            "gate was previously exercised"
        )


class TestRouteTableLog:
    """#81: the ROUTE TABLE log line must label its pending counter
    as ``pending_ack`` (not bare ``pending``) so operators reading
    the log do not have to remember what the counter measures.
    """

    def test_route_table_log_says_pending_ack_not_pending(self, caplog):
        import logging
        from orchestrator.delivery import DeliveryProtocol
        dp = DeliveryProtocol(tmux_comm=_make_tmux(), config=_make_config())
        # Mark one mailbox pending so the counter is non-zero; locks
        # the format verbatim rather than counting on coincidences.
        dp._neighbors["hub"].mailbox.pending = True
        with caplog.at_level(logging.INFO, logger="orchestrator.delivery"):
            dp._log_route_table()
        messages = [r.getMessage() for r in caplog.records]
        assert any("ROUTE TABLE:" in m for m in messages), messages
        assert any("pending_ack=1" in m for m in messages), (
            f"expected `pending_ack=1` in the log; got {messages!r}"
        )
        # The old bare `pending=N` form must not appear (a `pending=1`
        # substring inside `pending_ack=1` would falsely match; we
        # check against the literal `| pending=` the pre-#81 format
        # emitted).
        assert not any("| pending=" in m for m in messages), (
            f"legacy `| pending=` label must be gone; got {messages!r}"
        )


# ---------------------------------------------------------------------------
# #80: heartbeat-gated probe + proactive UP→DOWN alerting
# ---------------------------------------------------------------------------


def _make_config_with_heartbeat(**hb_overrides):
    """_make_config + a `heartbeat:` section that defaults to 30s
    interval / 60s max-age / 60s startup grace (#80 defaults)."""
    cfg = _make_config()
    heartbeat = {
        "interval_seconds": 30,
        "max_age_seconds": 60,
        "startup_grace_seconds": 60,
        "alert_on_neighbor_down": True,
        "neighbor_down_alert_cooldown_seconds": 600,
    }
    heartbeat.update(hb_overrides)
    cfg["heartbeat"] = heartbeat
    return cfg


def _make_nats_mock():
    """Async mock with publish_to_inbox tracking."""
    mock = MagicMock()
    mock.publish_to_inbox = AsyncMock()
    return mock


class TestHeartbeatGatedProbe:
    """#80: ``_probe_neighbors`` must consult the ``HeartbeatTracker``
    and force DOWN when the agent's MCP bridge has not heartbeated
    within ``max_age_seconds``. This closes the ssh-reconnect
    stale-pane false-positive gap.
    """

    @pytest.mark.asyncio
    async def test_fresh_heartbeat_plus_idle_pane_stays_up(self):
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        tracker.touch("hub")  # fresh heartbeat
        tmux = _make_tmux(idle_agents=["hub"], reachable_agents=["hub"])
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        _age_neighbors(dp)
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.UP

    @pytest.mark.asyncio
    async def test_stale_heartbeat_forces_down_even_when_pane_idle(self):
        """The exact RTX5090 silent-logout scenario: pane looks
        fine (❯ prompt visible) but the bridge has stopped
        publishing. Heartbeat gate must override pane signal and
        demote to DOWN.
        """
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        # Heartbeat from 200s ago; max_age is 60s → stale.
        tracker.touch("hub", now=time.time() - 200)
        tmux = _make_tmux(idle_agents=["hub"], reachable_agents=["hub"])
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        _age_neighbors(dp)
        # Simulate orchestrator has been running past startup grace.
        dp._orchestrator_started_at = time.time() - 600
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.DOWN

    @pytest.mark.asyncio
    async def test_fresh_heartbeat_but_pane_capture_fails_is_down(self):
        """Pane-level failure still wins when heartbeat is fresh —
        we can't deliver to a pane we can't read. Covers the case
        where the bridge is alive but tmux is broken."""
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        tracker.touch("hub")
        tmux = _make_tmux(idle_agents=[], reachable_agents=[])
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        _age_neighbors(dp)
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.DOWN

    @pytest.mark.asyncio
    async def test_startup_grace_never_seen_heartbeat_does_not_demote(self):
        """During the orchestrator's startup-grace window, a
        never-observed-heartbeat agent must NOT be forced DOWN.
        Prevents alert-spam when bridges are still booting.
        """
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()  # empty
        tmux = _make_tmux(idle_agents=["hub"], reachable_agents=["hub"])
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config_with_heartbeat(startup_grace_seconds=300),
            heartbeat_tracker=tracker,
        )
        _age_neighbors(dp)
        # Orchestrator just started — inside the grace window.
        dp._orchestrator_started_at = time.time() - 5
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.UP

    @pytest.mark.asyncio
    async def test_monitors_exempt_from_heartbeat_gate(self):
        """Monitor agents don't have an MCP bridge that publishes
        on the heartbeat pattern; pane state is the only signal
        available for them. Must not be forced DOWN even when the
        tracker has never seen a heartbeat from them."""
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()  # empty — including for manager
        tmux = _make_tmux(
            idle_agents=["manager"], reachable_agents=["manager"],
        )
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        _age_neighbors(dp)
        dp._orchestrator_started_at = time.time() - 600  # past grace
        await dp._probe_neighbors()
        assert dp._neighbors["manager"].state == NeighborState.UP

    @pytest.mark.asyncio
    async def test_no_tracker_preserves_pre_pr_probe_behavior(self):
        """Without a heartbeat tracker wired up, ``_probe_neighbors``
        behaves exactly as it did pre-#80 — pane-only determination."""
        from orchestrator.delivery import DeliveryProtocol
        tmux = _make_tmux(idle_agents=["hub"], reachable_agents=["hub"])
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config(),
            heartbeat_tracker=None,
        )
        _age_neighbors(dp)
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.UP


class TestDownAlert:
    """#80: UP/BUSY → DOWN transitions must fire a `manager_directive`
    with subtype `agent_logout`, cooldown-suppressed and
    env-kill-switchable.
    """

    @pytest.mark.asyncio
    async def test_up_to_down_fires_directive_once(self):
        from orchestrator.delivery import DeliveryProtocol, NeighborState
        nats = _make_nats_mock()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(),
            nats_client=nats,
        )
        # Force "past startup grace" so the alert isn't suppressed.
        dp._orchestrator_started_at = time.time() - 600
        n = dp._neighbors["hub"]
        n.state = NeighborState.UP

        await dp._maybe_alert_neighbor_down(n, NeighborState.UP)

        nats.publish_to_inbox.assert_called_once()
        call = nats.publish_to_inbox.call_args[0]
        assert call[0] == "manager"
        directive = call[1]
        assert directive["type"] == "manager_directive"
        assert directive["subtype"] == "agent_logout"
        assert directive["agent"] == "hub"
        assert directive["priority"] == "high"

    @pytest.mark.asyncio
    async def test_repeated_down_within_cooldown_fires_once(self):
        from orchestrator.delivery import DeliveryProtocol, NeighborState
        nats = _make_nats_mock()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(
                neighbor_down_alert_cooldown_seconds=600,
            ),
            nats_client=nats,
        )
        dp._orchestrator_started_at = time.time() - 600
        n = dp._neighbors["hub"]

        await dp._maybe_alert_neighbor_down(n, NeighborState.UP)
        await dp._maybe_alert_neighbor_down(n, NeighborState.BUSY)
        await dp._maybe_alert_neighbor_down(n, NeighborState.UP)

        # Only the first fires; the rest are cooldown-suppressed.
        assert nats.publish_to_inbox.call_count == 1

    @pytest.mark.asyncio
    async def test_startup_grace_suppresses_alert(self):
        """Transitions observed during the startup-grace window
        must not produce alerts (bounce-noise suppression)."""
        from orchestrator.delivery import DeliveryProtocol, NeighborState
        nats = _make_nats_mock()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(
                startup_grace_seconds=300,
            ),
            nats_client=nats,
        )
        # Inside grace window.
        dp._orchestrator_started_at = time.time() - 5
        n = dp._neighbors["hub"]
        await dp._maybe_alert_neighbor_down(n, NeighborState.UP)
        nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_env_kill_switch_suppresses_alert(self, monkeypatch):
        from orchestrator.delivery import DeliveryProtocol, NeighborState
        nats = _make_nats_mock()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(),
            nats_client=nats,
        )
        dp._orchestrator_started_at = time.time() - 600
        monkeypatch.setenv("MAS_SUPPRESS_DOWN_ALERTS", "1")
        n = dp._neighbors["hub"]
        await dp._maybe_alert_neighbor_down(n, NeighborState.UP)
        nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_config_flag_disables_alert(self):
        """``alert_on_neighbor_down: false`` disables the path
        entirely regardless of env / cooldown."""
        from orchestrator.delivery import DeliveryProtocol, NeighborState
        nats = _make_nats_mock()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(alert_on_neighbor_down=False),
            nats_client=nats,
        )
        dp._orchestrator_started_at = time.time() - 600
        n = dp._neighbors["hub"]
        await dp._maybe_alert_neighbor_down(n, NeighborState.UP)
        nats.publish_to_inbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_transition_up_to_down_fires_alert(self):
        """End-to-end via the real probe loop: agent starts UP,
        heartbeat goes stale, next probe demotes to DOWN AND
        fires the alert."""
        from orchestrator.delivery import DeliveryProtocol, NeighborState
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        # Start with a fresh heartbeat so the first probe lands on UP.
        tracker.touch("hub")
        tmux = _make_tmux(idle_agents=["hub"], reachable_agents=["hub"])
        nats = _make_nats_mock()
        dp = DeliveryProtocol(
            tmux_comm=tmux,
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
            nats_client=nats,
        )
        _age_neighbors(dp)
        dp._orchestrator_started_at = time.time() - 600

        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.UP
        nats.publish_to_inbox.assert_not_called()

        # Now simulate bridge gone silent by overwriting the
        # tracker entry with an ancient timestamp.
        tracker.touch("hub", now=time.time() - 500)
        await dp._probe_neighbors()
        assert dp._neighbors["hub"].state == NeighborState.DOWN
        nats.publish_to_inbox.assert_called_once()
        directive = nats.publish_to_inbox.call_args[0][1]
        assert directive["subtype"] == "agent_logout"
        assert directive["agent"] == "hub"


class TestHeartbeatHandler:
    """``handle_heartbeat_message`` forwards the agent name from a
    heartbeat payload into the tracker."""

    @pytest.mark.asyncio
    async def test_handler_records_heartbeat(self):
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        msg = SimpleNamespace(data=json.dumps({
            "type": "heartbeat",
            "agent": "dgx",
            "timestamp": "2026-04-18T00:00:00Z",
            "interval_seconds": 30,
        }).encode())
        await dp.handle_heartbeat_message(msg)
        assert tracker.last_seen("dgx") is not None

    @pytest.mark.asyncio
    async def test_handler_ignores_wrong_type(self):
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        msg = SimpleNamespace(data=json.dumps({
            "type": "not_a_heartbeat", "agent": "dgx",
        }).encode())
        await dp.handle_heartbeat_message(msg)
        assert tracker.snapshot() == {}

    @pytest.mark.asyncio
    async def test_handler_ignores_malformed_json(self):
        from orchestrator.delivery import DeliveryProtocol
        from orchestrator.heartbeat_tracker import HeartbeatTracker
        tracker = HeartbeatTracker()
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config_with_heartbeat(),
            heartbeat_tracker=tracker,
        )
        msg = SimpleNamespace(data=b"not json")
        await dp.handle_heartbeat_message(msg)  # must not raise
        assert tracker.snapshot() == {}

    @pytest.mark.asyncio
    async def test_handler_noop_when_tracker_none(self):
        """If no tracker is wired, the handler must still be safe
        to invoke — the subscription may still be live in a
        downgraded config."""
        from orchestrator.delivery import DeliveryProtocol
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(),
            config=_make_config(),
            heartbeat_tracker=None,
        )
        msg = SimpleNamespace(data=json.dumps({
            "type": "heartbeat", "agent": "hub",
        }).encode())
        await dp.handle_heartbeat_message(msg)  # must not raise
