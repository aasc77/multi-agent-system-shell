"""Unit tests for orchestrator.pane_state_cache (#9)."""

from __future__ import annotations

import threading

import pytest

from orchestrator.pane_state_cache import PaneStateCache


class TestSetAndGet:
    def test_get_returns_none_for_unknown_agent(self):
        cache = PaneStateCache()
        assert cache.get("nobody") is None

    def test_set_then_get_roundtrip(self):
        cache = PaneStateCache()
        cache.set("dev", "working")
        assert cache.get("dev") == "working"

    def test_set_overwrites_previous_state(self):
        cache = PaneStateCache()
        cache.set("dev", "working")
        cache.set("dev", "idle")
        assert cache.get("dev") == "idle"

    def test_set_empty_or_none_agent_is_noop(self):
        cache = PaneStateCache()
        cache.set("", "working")
        cache.set(None, "working")  # type: ignore[arg-type]
        assert cache.snapshot() == {}

    def test_set_empty_or_none_state_is_noop(self):
        """Callers that pass the result of
        ``getattr(state, 'value', state)`` may hand us ``None`` or an
        empty string for edge cases. The cache should ignore both so
        downstream readers never see a half-recorded entry."""
        cache = PaneStateCache()
        cache.set("dev", None)  # type: ignore[arg-type]
        cache.set("macmini", "")
        assert cache.snapshot() == {}


class TestSnapshot:
    def test_snapshot_returns_detached_copy(self):
        cache = PaneStateCache()
        cache.set("dev", "working")
        snap = cache.snapshot()
        snap["dev"] = "hacked"
        assert cache.get("dev") == "working"

    def test_snapshot_is_empty_for_fresh_cache(self):
        cache = PaneStateCache()
        assert cache.snapshot() == {}


class TestThreadSafety:
    def test_concurrent_writers_do_not_lose_entries(self):
        """Same pattern as ActivityTracker's thread-safety test:
        many threads hammer the cache, final snapshot must contain
        every agent that was touched."""
        cache = PaneStateCache()
        agents = [f"agent-{i}" for i in range(20)]

        def _hammer(name: str) -> None:
            for _ in range(200):
                cache.set(name, "working")

        threads = [
            threading.Thread(target=_hammer, args=(a,), daemon=True)
            for a in agents
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        snap = cache.snapshot()
        for a in agents:
            assert snap.get(a) == "working", (
                f"concurrent writes lost agent {a!r}: {snap}"
            )

    def test_reader_does_not_observe_partial_state(self):
        cache = PaneStateCache()
        stop = threading.Event()

        def _writer():
            i = 0
            while not stop.is_set():
                cache.set(f"agent-{i % 5}", "working" if i % 2 else "idle")
                i += 1

        w = threading.Thread(target=_writer, daemon=True)
        w.start()
        try:
            for _ in range(500):
                snap = cache.snapshot()
                for name, state in snap.items():
                    assert isinstance(name, str)
                    assert state in ("working", "idle", "unknown", "capture_failed")
        finally:
            stop.set()
            w.join(timeout=5)
