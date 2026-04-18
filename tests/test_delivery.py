"""Tests for the Delivery Protocol (orchestrator/delivery.py).

Covers: neighbor probing, mailbox flags, ACK processing, soft ACK,
retransmit/backoff, dead letters, grace period, push notification.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
