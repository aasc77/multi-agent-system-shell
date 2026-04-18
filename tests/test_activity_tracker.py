"""Unit tests for orchestrator.activity_tracker (#55).

Covers:
- touch/snapshot CRUD
- orchestrator/empty sender is filtered out (no noise in the map)
- active_within honors the threshold and sorts by recency
- aging-out: an agent older than the window is excluded from
  active_within but still present in snapshot (consumers can age
  out by recomputing — we deliberately DON'T garbage-collect in the
  background because it would race with the single-lock design)
- thread safety against concurrent touches and a live reader
"""

from __future__ import annotations

import threading
import time

import pytest

from orchestrator.activity_tracker import ActivityTracker


class TestTouchAndSnapshot:
    def test_touch_records_timestamp(self):
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)
        assert tracker.snapshot() == {"dev": 1000.0}

    def test_touch_overwrites_previous_timestamp(self):
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)
        tracker.touch("dev", now=1005.0)
        assert tracker.snapshot()["dev"] == 1005.0

    def test_touch_ignores_empty_name(self):
        tracker = ActivityTracker()
        tracker.touch("", now=1000.0)
        tracker.touch(None, now=1000.0)  # type: ignore[arg-type]
        assert tracker.snapshot() == {}

    def test_touch_ignores_orchestrator_sender(self):
        """#55: orchestrator's own housekeeping publishes must not
        register as agent activity — otherwise every inactivity
        alert would reset the counter for no real-world reason."""
        tracker = ActivityTracker()
        tracker.touch("orchestrator", now=1000.0)
        assert tracker.snapshot() == {}

    def test_snapshot_returns_detached_copy(self):
        """Mutating the snapshot must not affect the tracker's
        internal map."""
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)
        snap = tracker.snapshot()
        snap["dev"] = 9999.0
        assert tracker.snapshot()["dev"] == 1000.0


class TestActiveWithin:
    def test_empty_map_returns_empty_list(self):
        tracker = ActivityTracker()
        assert tracker.active_within(60, now=1000.0) == []

    def test_single_agent_within_window_included(self):
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)
        result = tracker.active_within(60, now=1003.0)
        assert result == [("dev", 3.0)]

    def test_agent_older_than_window_excluded(self):
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)
        # 61s > 60s window → dev is not active.
        result = tracker.active_within(60, now=1061.0)
        assert result == []
        # But still present in snapshot (aging-out is read-only).
        assert tracker.snapshot() == {"dev": 1000.0}

    def test_multi_agent_sorted_by_recency(self):
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)       # age 10
        tracker.touch("RTX5090", now=1005.0)    # age 5
        tracker.touch("macmini", now=995.0)    # age 15
        result = tracker.active_within(60, now=1010.0)
        # Sorted freshest first (lowest age).
        assert result == [("RTX5090", 5.0), ("dev", 10.0), ("macmini", 15.0)]

    def test_mixed_inside_and_outside_window(self):
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)           # age 10 → in
        tracker.touch("stale_agent", now=900.0)    # age 110 → out
        tracker.touch("RTX5090", now=1005.0)        # age 5 → in
        result = tracker.active_within(60, now=1010.0)
        names = [name for name, _ in result]
        assert "stale_agent" not in names
        assert "dev" in names
        assert "RTX5090" in names

    def test_age_boundary_inclusive(self):
        """An agent whose age equals the window exactly is INCLUDED.

        Pins the ``<=`` bound so a tracker consumer won't flake when
        the age computation lands right on the boundary.
        """
        tracker = ActivityTracker()
        tracker.touch("dev", now=1000.0)
        result = tracker.active_within(60, now=1060.0)  # age == 60
        assert len(result) == 1
        assert result[0][0] == "dev"


class TestThreadSafety:
    """Concurrent touches from many threads must not corrupt the
    map; concurrent snapshot/active_within calls must never observe
    a partially-written entry.

    These tests are timing-sensitive but we keep the thread count
    modest so they pass reliably on CI. The real guarantee comes
    from the ``threading.Lock`` in ``ActivityTracker``; these tests
    are smoke coverage that the lock is actually wired.
    """

    def test_concurrent_touches_do_not_lose_entries(self):
        tracker = ActivityTracker()
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
                f"concurrent touches lost agent {a!r}; "
                f"snapshot contained {list(snap.keys())}"
            )

    def test_reader_does_not_observe_partial_state(self):
        tracker = ActivityTracker()
        stop = threading.Event()

        def _writer():
            i = 0
            while not stop.is_set():
                tracker.touch(f"agent-{i % 5}")
                i += 1

        w = threading.Thread(target=_writer, daemon=True)
        w.start()
        try:
            # Snapshot many times; each must parse as a real dict
            # of name->float, never raise, never mix partial state.
            for _ in range(500):
                snap = tracker.snapshot()
                for name, ts in snap.items():
                    assert isinstance(name, str)
                    assert isinstance(ts, float)
                    assert ts > 0
        finally:
            stop.set()
            w.join(timeout=5)


class TestDefaultTimeHook:
    """``now`` defaults to ``time.time()``. Verify the wall-clock
    path without pinning: the returned age must be non-negative and
    approximately the elapsed real time."""

    def test_real_time_touch_is_recent(self):
        tracker = ActivityTracker()
        tracker.touch("dev")
        result = tracker.active_within(60)
        assert len(result) == 1
        agent, age = result[0]
        assert agent == "dev"
        assert 0.0 <= age < 5.0
