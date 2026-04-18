"""Unit tests for orchestrator.heartbeat_tracker (#80)."""

from __future__ import annotations

import threading

import pytest

from orchestrator.heartbeat_tracker import HeartbeatTracker


class TestTouchAndLastSeen:
    def test_last_seen_returns_none_for_unknown_agent(self):
        tracker = HeartbeatTracker()
        assert tracker.last_seen("nobody") is None

    def test_touch_records_timestamp(self):
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        assert tracker.last_seen("dev") == 1000.0

    def test_touch_overwrites_previous_timestamp(self):
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        tracker.touch("dev", now=1050.0)
        assert tracker.last_seen("dev") == 1050.0

    def test_touch_ignores_empty_agent(self):
        tracker = HeartbeatTracker()
        tracker.touch("", now=1000.0)
        tracker.touch(None, now=1000.0)  # type: ignore[arg-type]
        assert tracker.snapshot() == {}

    def test_touch_ignores_orchestrator_sender(self):
        """The orchestrator itself never heartbeats — ignore the
        sentinel if it ever shows up in a payload (defense in
        depth against loopback weirdness)."""
        tracker = HeartbeatTracker()
        tracker.touch("orchestrator", now=1000.0)
        assert tracker.snapshot() == {}


class TestAgeSeconds:
    def test_age_seconds_none_for_unknown_agent(self):
        """``age_seconds`` returns ``None`` (not zero) for a
        never-observed agent. Delivery's startup-grace decision
        depends on distinguishing \"never seen\" from \"seen recently\"."""
        tracker = HeartbeatTracker()
        assert tracker.age_seconds("nobody") is None

    def test_age_seconds_is_reference_minus_last(self):
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        assert tracker.age_seconds("dev", now=1003.0) == 3.0


class TestIsAlive:
    def test_is_alive_true_within_window(self):
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        assert tracker.is_alive("dev", max_age_seconds=60, now=1003.0)

    def test_is_alive_false_past_window(self):
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        assert not tracker.is_alive("dev", max_age_seconds=60, now=1061.0)

    def test_is_alive_boundary_inclusive(self):
        """Age == window exactly → alive. Pins the ``<=`` bound so
        callers never see a flaky boundary on a wall-clock tick."""
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        assert tracker.is_alive("dev", max_age_seconds=60, now=1060.0)

    def test_is_alive_false_for_never_observed(self):
        """A never-seen agent is not alive. Callers that need the
        startup-grace distinction should use ``age_seconds() is None``."""
        tracker = HeartbeatTracker()
        assert not tracker.is_alive("nobody", max_age_seconds=60)


class TestSnapshot:
    def test_snapshot_is_detached_copy(self):
        tracker = HeartbeatTracker()
        tracker.touch("dev", now=1000.0)
        snap = tracker.snapshot()
        snap["dev"] = 9999.0
        assert tracker.last_seen("dev") == 1000.0


class TestThreadSafety:
    def test_concurrent_touches_do_not_lose_entries(self):
        tracker = HeartbeatTracker()
        agents = [f"agent-{i}" for i in range(20)]

        def _hammer(name: str) -> None:
            for _ in range(200):
                tracker.touch(name)

        threads = [
            threading.Thread(target=_hammer, args=(a,), daemon=True)
            for a in agents
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        snap = tracker.snapshot()
        for a in agents:
            assert a in snap, (
                f"concurrent touches lost agent {a!r}; snapshot={snap}"
            )

    def test_reader_does_not_observe_partial_state(self):
        tracker = HeartbeatTracker()
        stop = threading.Event()

        def _writer():
            i = 0
            while not stop.is_set():
                tracker.touch(f"agent-{i % 5}")
                i += 1

        w = threading.Thread(target=_writer, daemon=True)
        w.start()
        try:
            for _ in range(500):
                snap = tracker.snapshot()
                for name, ts in snap.items():
                    assert isinstance(name, str)
                    assert isinstance(ts, float)
                    assert ts > 0
        finally:
            stop.set()
            w.join(timeout=5)
