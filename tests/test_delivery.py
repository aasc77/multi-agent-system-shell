"""Tests for the Delivery Protocol (orchestrator/delivery.py).

Covers: neighbor probing, delivery lifecycle, ACK processing,
coalescing, backoff, dead letters, grace period, push notification.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.delivery import (
    ACK_MESSAGE_TYPE,
    DeliveryProtocol,
    DeliveryState,
    NeighborState,
    _STARTUP_GRACE_PERIOD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(**routing_overrides):
    """Build a minimal config dict for DeliveryProtocol."""
    routing = {
        "probe_interval": 10,
        "process_interval": 5,
        "neighbor_timeout": 120,
        "max_attempts": 4,
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
    """Build a mock TmuxComm."""
    idle = set(idle_agents or [])
    reachable = set(reachable_agents or [])

    mock = MagicMock()
    mock.is_agent_idle.side_effect = lambda a: a in idle
    mock.capture_pane.side_effect = lambda a, lines=1: "busy" if a in reachable else None
    mock.nudge.return_value = True
    return mock


def _age_neighbors(dp):
    """Set all neighbors' last_state_change past the startup grace period."""
    for n in dp._neighbors.values():
        n.last_state_change = time.time() - _STARTUP_GRACE_PERIOD - 1


def _make_ack_msg(agent, count=1):
    """Build a fake NATS message with delivery_ack payload."""
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
    def test_builds_table_excluding_monitor(self):
        config = _make_config()
        tmux = _make_tmux()
        dp = DeliveryProtocol(tmux_comm=tmux, config=config)

        assert "hub" in dp._neighbors
        assert "dgx2" in dp._neighbors
        assert "macmini" in dp._neighbors
        assert "manager" not in dp._neighbors

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
        assert dp._neighbors["dgx2"].state == NeighborState.DOWN

    @pytest.mark.asyncio
    async def test_probe_exception_marks_down(self):
        tmux = _make_tmux()
        tmux.is_agent_idle.side_effect = RuntimeError("pane gone")
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())

        await dp._probe_neighbors()

        assert dp._neighbors["hub"].state == NeighborState.DOWN


# ---------------------------------------------------------------------------
# Delivery lifecycle tests
# ---------------------------------------------------------------------------


class TestDelivery:
    @pytest.mark.asyncio
    async def test_deliver_creates_record_and_nudges(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        seq = dp.deliver("hub", reason="task_assignment")

        assert seq == 1
        assert len(dp._queue) == 1
        assert dp._queue[0].state == DeliveryState.SENT
        assert dp._queue[0].attempts == 1
        tmux.nudge.assert_called_once_with("hub", force=True)

    @pytest.mark.asyncio
    async def test_deliver_coalesces_duplicate(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        seq1 = dp.deliver("hub", reason="msg1")
        seq2 = dp.deliver("hub", reason="msg2")

        assert seq1 == 1
        assert seq2 == 0  # coalesced
        assert len(dp._queue) == 1
        assert tmux.nudge.call_count == 1  # only one nudge

    @pytest.mark.asyncio
    async def test_deliver_different_agents_no_coalesce(self):
        tmux = _make_tmux(idle_agents=["hub", "dgx2"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        seq1 = dp.deliver("hub", reason="msg1")
        seq2 = dp.deliver("dgx2", reason="msg2")

        assert seq1 == 1
        assert seq2 == 2
        assert len(dp._queue) == 2

    @pytest.mark.asyncio
    async def test_deliver_skips_nudge_when_down(self):
        tmux = _make_tmux(idle_agents=[], reachable_agents=[])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()  # all DOWN

        seq = dp.deliver("hub", reason="test")

        assert seq == 1
        assert dp._queue[0].attempts == 1
        tmux.nudge.assert_not_called()  # skipped because DOWN

    @pytest.mark.asyncio
    async def test_pending_count_increments(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="test")

        assert dp._neighbors["hub"].pending == 1


# ---------------------------------------------------------------------------
# ACK processing tests
# ---------------------------------------------------------------------------


class TestAck:
    @pytest.mark.asyncio
    async def test_ack_clears_delivery(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        assert len(dp._queue) == 1

        await dp.handle_ack_message(_make_ack_msg("hub", count=3))

        assert len(dp._queue) == 0
        assert dp._neighbors["hub"].pending == 0
        assert dp._neighbors["hub"].state == NeighborState.UP

    @pytest.mark.asyncio
    async def test_ack_updates_rtt(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        # Simulate time passing
        dp._queue[0].last_sent = time.time() - 5.0

        await dp.handle_ack_message(_make_ack_msg("hub"))

        assert dp._neighbors["hub"].rtt_ema > 0

    @pytest.mark.asyncio
    async def test_ack_for_unknown_agent_no_crash(self):
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(), config=_make_config(),
        )
        # Should not raise
        await dp.handle_ack_message(_make_ack_msg("nonexistent"))

    @pytest.mark.asyncio
    async def test_ack_with_no_pending_delivery(self):
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(idle_agents=["hub"]),
            config=_make_config(),
        )
        await dp._probe_neighbors()

        # ACK without prior delivery
        await dp.handle_ack_message(_make_ack_msg("hub"))

        # Should not crash, neighbor updated
        assert dp._neighbors["hub"].last_ack > 0

    @pytest.mark.asyncio
    async def test_ack_invalid_json_no_crash(self):
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(), config=_make_config(),
        )
        msg = SimpleNamespace(data=b"not json")
        await dp.handle_ack_message(msg)  # should not raise


# ---------------------------------------------------------------------------
# Retransmit / backoff tests
# ---------------------------------------------------------------------------


class TestRetransmit:
    @pytest.mark.asyncio
    async def test_process_queue_retries_after_backoff(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        # Force next_retry to be in the past
        dp._queue[0].next_retry = time.time() - 1

        dp._process_queue()

        assert dp._queue[0].attempts == 2
        assert tmux.nudge.call_count == 2  # initial + retry

    @pytest.mark.asyncio
    async def test_process_queue_skips_if_not_due(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        # next_retry is in the future (just set by deliver)
        initial_attempts = dp._queue[0].attempts

        dp._process_queue()

        assert dp._queue[0].attempts == initial_attempts  # no retry yet

    @pytest.mark.asyncio
    async def test_dead_letter_after_max_attempts(self):
        tmux = _make_tmux(idle_agents=["hub"])
        config = _make_config(max_attempts=2)
        dp = DeliveryProtocol(tmux_comm=tmux, config=config)
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        # Exhaust attempts
        dp._queue[0].attempts = 2
        dp._queue[0].next_retry = time.time() - 1

        dp._process_queue()

        # Record should be cleaned up (dead letter removed from queue)
        assert len(dp._queue) == 0
        assert dp._neighbors["hub"].pending == 0

    @pytest.mark.asyncio
    async def test_dead_letter_callback_fires(self):
        tmux = _make_tmux(idle_agents=["hub"])
        config = _make_config(max_attempts=2)
        dp = DeliveryProtocol(tmux_comm=tmux, config=config)
        await dp._probe_neighbors()
        _age_neighbors(dp)

        callback = MagicMock()
        dp.set_dead_letter_callback(callback)

        dp.deliver("hub", reason="test")
        dp._queue[0].attempts = 2
        dp._queue[0].next_retry = time.time() - 1

        dp._process_queue()

        callback.assert_called_once_with("hub", 1)


# ---------------------------------------------------------------------------
# Push notification tests
# ---------------------------------------------------------------------------


class TestPushNotify:
    @pytest.mark.asyncio
    @patch("orchestrator.delivery.subprocess")
    async def test_push_fires_at_escalation_backoff(self, mock_subprocess):
        """Push notification fires when backoff reaches 1hr threshold."""
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config(max_attempts=6))
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        record = dp._queue[0]
        record.attempts = 4  # next backoff = 3600s (escalation threshold)
        record.next_retry = time.time() - 1

        mock_subprocess.run.return_value = MagicMock(returncode=0)
        dp._process_queue()

        mock_subprocess.run.assert_called_once()
        assert record.escalated is True

    @pytest.mark.asyncio
    @patch("orchestrator.delivery.subprocess")
    async def test_push_only_fires_once(self, mock_subprocess):
        """Push notification should not fire twice for the same delivery."""
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)
        dp.deliver("hub", reason="test")

        record = dp._queue[0]
        record.attempts = 4
        record.escalated = True  # already sent
        record.next_retry = time.time() - 1

        mock_subprocess.run.return_value = MagicMock(returncode=0)
        dp._process_queue()

        mock_subprocess.run.assert_not_called()


# ---------------------------------------------------------------------------
# Status reporting tests
# ---------------------------------------------------------------------------


class TestStatusReporting:
    @pytest.mark.asyncio
    async def test_get_neighbor_table(self):
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()

        table = dp.get_neighbor_table()

        assert "hub" in table
        assert table["hub"]["state"] == "UP"
        assert "manager" not in table

    def test_get_queue_status_empty(self):
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(), config=_make_config(),
        )
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
        dp = DeliveryProtocol(
            tmux_comm=_make_tmux(), config=_make_config(),
        )
        await dp._probe_neighbors()
        dp._log_route_table()  # should not raise


# ---------------------------------------------------------------------------
# Startup grace period tests
# ---------------------------------------------------------------------------


class TestGracePeriod:
    @pytest.mark.asyncio
    async def test_grace_defers_nudge(self):
        """Agent that just came UP should defer the nudge."""
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()  # just came UP — grace active

        dp.deliver("hub", reason="test")

        # Nudge should NOT have been sent (grace period)
        tmux.nudge.assert_not_called()
        assert dp._queue[0].state == DeliveryState.QUEUED

    @pytest.mark.asyncio
    async def test_grace_allows_after_period(self):
        """After grace period, nudge should proceed normally."""
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)  # past grace

        dp.deliver("hub", reason="test")

        tmux.nudge.assert_called_once()
        assert dp._queue[0].state == DeliveryState.SENT


# ---------------------------------------------------------------------------
# After-ACK re-delivery test
# ---------------------------------------------------------------------------


class TestReDeliveryAfterAck:
    @pytest.mark.asyncio
    async def test_can_deliver_again_after_ack(self):
        """Once ACKed and cleaned, a new delivery to the same agent works."""
        tmux = _make_tmux(idle_agents=["hub"])
        dp = DeliveryProtocol(tmux_comm=tmux, config=_make_config())
        await dp._probe_neighbors()
        _age_neighbors(dp)

        dp.deliver("hub", reason="first")
        await dp.handle_ack_message(_make_ack_msg("hub"))
        assert len(dp._queue) == 0

        seq = dp.deliver("hub", reason="second")
        assert seq > 0
        assert len(dp._queue) == 1
