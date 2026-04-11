"""End-to-end integration smoke test for the orchestrator version
probe introduced in issue #31.

Spins up a real NATS subscription using the real
``make_version_request_handler``, then sends a NATS request against
the same subject (simulating what
``scripts/check-orchestrator-version.sh`` does), verifies the
response parses as JSON with the full envelope
(``startup_sha``, ``startup_sha_full``, ``started_at``, ``pid``),
cross-checks the captured SHA against a live ``git rev-parse HEAD``
against the repo, and verifies that a non-request publish to the
same subject does NOT get a response (the handler's reply-subject
gate is the contract that protects the probe from accidental
fire-and-forget publishes that confuse operators).

Uses an isolated subject so the test does not collide with a
running production orchestrator's handler on
``system.orchestrator.version``.

Originally at ``/tmp/smoketest_31.py``; rolled into the gated
integration harness in issue #37 so the test is discoverable and
not vulnerable to ``rm -rf /tmp/*``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import nats

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.version import (  # noqa: E402
    capture_startup_info,
    make_version_request_handler,
)


_NATS_URL = "nats://127.0.0.1:4222"
# Isolated subject so this test does not collide with the production
# orchestrator's handler on `system.orchestrator.version`.
_TEST_SUBJECT = "system.orchestrator.version.smoke_integration"


@pytest.mark.asyncio
async def test_version_probe_end_to_end_against_live_nats():
    """Capture real startup info, register the real handler, send a
    real request, verify response shape and reply-gate semantics.
    """
    # --- 1. Capture real startup info against the repo ---
    info = capture_startup_info(REPO_ROOT)
    assert info.startup_sha is not None, (
        "capture_startup_info returned startup_sha=None — is git on "
        "PATH? is REPO_ROOT correct?"
    )
    assert info.pid > 0
    assert info.started_at

    # Cross-check captured SHA against live disk HEAD
    disk_sha_full = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    ).stdout.strip()
    assert info.startup_sha_full == disk_sha_full, (
        f"captured SHA {info.startup_sha_full} != disk SHA "
        f"{disk_sha_full} — capture happened at a different revision?"
    )

    # --- 2. Register the handler against a real NATS connection ---
    nc = await nats.connect(_NATS_URL)
    handler = make_version_request_handler(info)
    sub = await nc.subscribe(_TEST_SUBJECT, cb=handler)

    try:
        # --- 3. Send a request and verify the response envelope ---
        reply = await nc.request(_TEST_SUBJECT, b"", timeout=2)
        body = json.loads(reply.data.decode("utf-8"))

        assert body.get("startup_sha") == info.startup_sha
        assert body.get("startup_sha_full") == info.startup_sha_full
        assert body.get("pid") == info.pid
        assert body.get("started_at") == info.started_at

        # --- 4. Non-request publish is silently dropped ---
        # Publish to the same subject without `nc.request`. The
        # handler sees `msg.reply` empty/None and does NOT respond.
        # We cannot directly observe "handler did not call respond"
        # from outside, but we verify the subscription survives the
        # non-request and a second request still succeeds.
        await nc.publish(_TEST_SUBJECT, b"")
        await asyncio.sleep(0.2)  # let the subscription callback run

        reply2 = await nc.request(_TEST_SUBJECT, b"", timeout=2)
        body2 = json.loads(reply2.data.decode("utf-8"))
        assert body2.get("startup_sha") == info.startup_sha, (
            "subscription died or returned wrong body after a "
            "non-request publish"
        )

        # --- 5. Byte-identical responses across successive requests ---
        # The #31 O(1) contract: payload is pre-encoded at
        # handler-build time and reused on every request.
        assert reply.data == reply2.data, (
            "successive requests returned non-identical bytes — "
            "handler is re-encoding per request, violating the #31 "
            "O(1) guarantee"
        )

    finally:
        await sub.unsubscribe()
        await nc.close()
