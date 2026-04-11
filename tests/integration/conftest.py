"""Shared configuration for integration smoke tests.

Integration tests in this directory exercise real NATS JetStream +
real orchestrator components + real subprocess spawning. They are
gated on the ``INTEGRATION=1`` environment variable so the default
unit suite (``python3 -m pytest``) stays fast and does not require
a running local NATS server.

Usage::

    # Default — unit suite only, integration tests skipped
    python3 -m pytest

    # Opt in — run integration tests against a live NATS server
    INTEGRATION=1 python3 -m pytest tests/integration/

    # Run a single integration test
    INTEGRATION=1 python3 -m pytest tests/integration/test_envelope_smoke.py

Prerequisites for ``INTEGRATION=1`` runs:
- Local NATS JetStream server reachable at ``nats://127.0.0.1:4222``.
  Start it with ``bash scripts/setup-nats.sh``.
- The ``AGENTS`` stream created (orchestrator creates this on boot,
  or ``setup-nats.sh`` does it idempotently).

Contract for new integration tests added to this directory:
- Must use an isolated NATS subject (e.g. ``agents.<test-name>-smoke.inbox``)
  that will not collide with any real agent inbox
- Must clean up any resources created (durable consumers, subprocesses,
  tmp files) in a ``try/finally`` so failed runs don't leave orphans
- Should complete in under 30 seconds; aggressive thresholds are fine
- Should import from ``orchestrator.*`` using the real module paths, not
  mock the internals — the whole point of this harness is to catch
  regressions that unit tests with mocks miss
"""

from __future__ import annotations

import os

import pytest


# Gate every test in this directory on INTEGRATION=1. Applied via
# pytest's `collect_modifyitems` hook so new tests dropped into the
# directory inherit the gate with zero boilerplate and no decorator
# to forget.
def pytest_collection_modifyitems(config, items):
    if os.environ.get("INTEGRATION") == "1":
        return
    skip_reason = (
        "integration test skipped — set INTEGRATION=1 and run "
        "a local NATS server at nats://127.0.0.1:4222 to enable"
    )
    skip_marker = pytest.mark.skip(reason=skip_reason)
    for item in items:
        if "tests/integration/" in str(item.fspath):
            item.add_marker(skip_marker)
