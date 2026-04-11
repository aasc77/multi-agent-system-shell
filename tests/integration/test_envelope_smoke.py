"""End-to-end integration smoke test for the NatsClient envelope
wrapping contract introduced in issue #34.

Publishes ONE ``task_assignment`` body through the real
``NatsClient.publish_to_inbox()`` to an isolated subject, then
subscribes to that subject via BOTH a core-NATS push subscription
AND a fresh durable JetStream pull consumer — the exact two paths
``mas-bridge/index.js handleCheckMessages`` drains and dedupes on
every ``check_messages`` tool call on the agent side.

Verifies:

1. **Single logical publish produces exactly one message on each
   drain path.** Not one on each, not two on one and zero on the
   other — one and one. If either path is missing the message,
   the bridge dedup path is fundamentally broken.
2. **Both paths see the same ``message_id`` bytewise.** This is
   the dedup key the bridge hashes on; identical ids on both
   sides is the proof that the bridge will collapse the merged
   set to a single delivered message.
3. **Envelope fields are present and correctly shaped.**
   ``message_id`` matches ``<type>-<epoch>-<short-uuid>``,
   ``timestamp`` parses as timezone-aware ISO8601, ``from`` is
   ``"orchestrator"``.
4. **Caller's body dict is not mutated by publish_to_inbox().**
   This is the fanout-safety guarantee — ``publish_all_done()``
   reuses the same body dict across every recipient, so mutating
   it would collapse every recipient's ``message_id`` to the
   first one and silently break the dedup contract.

Originally at ``/tmp/smoketest_34.py``; rolled into the gated
integration harness in issue #37 so the test is discoverable via
normal pytest and not vulnerable to ``rm -rf /tmp/*``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import nats

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.nats_client import NatsClient  # noqa: E402


_NATS_URL = "nats://127.0.0.1:4222"
_STREAM_NAME = "AGENTS"
_SUBJECTS_PREFIX = "agents"
# Isolated subject so this test does not wake a real agent or
# collide with production inboxes.
_TEST_ROLE = "envelope-smoke"
_TEST_SUBJECT = f"{_SUBJECTS_PREFIX}.{_TEST_ROLE}.inbox"


@pytest.mark.asyncio
async def test_envelope_wrap_end_to_end_against_live_nats():
    """Publish one body, observe it on both drain paths, assert
    byte-identical ``message_id`` between them.
    """
    # --- 1. Observer: subscribe via core NATS BEFORE we publish ---
    observer_nc = await nats.connect(_NATS_URL)
    push_received: list[dict] = []

    async def _push_cb(msg):
        push_received.append(json.loads(msg.data.decode()))

    push_sub = await observer_nc.subscribe(_TEST_SUBJECT, cb=_push_cb)

    try:
        # --- 2. Publish via the real NatsClient (enveloped) ---
        nats_config = {
            "url": _NATS_URL,
            "stream": _STREAM_NAME,
            "subjects_prefix": _SUBJECTS_PREFIX,
        }
        client = NatsClient(
            config=nats_config,
            agents={_TEST_ROLE: {"runtime": "claude_code"}},
        )
        await client.connect()

        body = {
            "type": "task_assignment",
            "task_id": f"envelope-smoke-{int(time.time())}",
            "title": "integration smoke: envelope dedup end-to-end",
            "description": "Verifies #34 NatsClient envelope wrapping.",
        }
        original_keys = set(body.keys())
        assert "message_id" not in body, (
            "precondition: body should start envelope-free"
        )

        await client.publish_to_inbox(_TEST_ROLE, body)

        # --- 3. Caller's body dict is not mutated (fanout safety) ---
        assert "message_id" not in body, (
            "publish_to_inbox mutated caller's dict — #34 envelope "
            "shallow-copy defense is broken"
        )
        assert "timestamp" not in body
        assert "from" not in body
        assert set(body.keys()) == original_keys

        # --- 4. Push path: wait briefly for delivery, assert one msg ---
        await asyncio.sleep(0.5)
        assert len(push_received) == 1, (
            f"push subscription should have received exactly 1 message, "
            f"got {len(push_received)}"
        )

        # --- 5. Pull path: fresh durable consumer matches bridge semantics ---
        js = observer_nc.jetstream()
        durable_name = f"envelope-smoke-{int(time.time())}-pull"
        jsm = observer_nc.jsm()
        await jsm.add_consumer(
            _STREAM_NAME,
            durable_name=durable_name,
            filter_subject=_TEST_SUBJECT,
            ack_policy="explicit",
        )
        try:
            psub = await js.pull_subscribe(_TEST_SUBJECT, durable=durable_name)
            pull_received: list[dict] = []
            try:
                msgs = await psub.fetch(10, timeout=2)
                for m in msgs:
                    pull_received.append(json.loads(m.data.decode()))
                    await m.ack()
            except TimeoutError:
                pass
        finally:
            try:
                await jsm.delete_consumer(_STREAM_NAME, durable_name)
            except Exception:
                pass

        assert len(pull_received) == 1, (
            f"durable pull consumer should have fetched exactly 1 "
            f"message, got {len(pull_received)}"
        )

        # --- 6. Byte-identical message_id on both paths (the dedup key) ---
        push_msg = push_received[0]
        pull_msg = pull_received[0]
        assert push_msg["message_id"] == pull_msg["message_id"], (
            f"dedup key mismatch: push={push_msg.get('message_id')!r} "
            f"pull={pull_msg.get('message_id')!r} — bridge dedup will "
            f"NOT collapse these to a single delivery"
        )

        # --- 7. Envelope shape on both paths ---
        for path_name, msg in [("push", push_msg), ("pull", pull_msg)]:
            assert "message_id" in msg, f"{path_name}: missing message_id"
            assert msg["message_id"], f"{path_name}: empty message_id"
            assert msg["message_id"].startswith("task_assignment-"), (
                f"{path_name}: message_id {msg['message_id']!r} "
                f"missing type prefix"
            )

            assert "timestamp" in msg, f"{path_name}: missing timestamp"
            parsed_ts = datetime.fromisoformat(msg["timestamp"])
            assert parsed_ts.tzinfo is not None, (
                f"{path_name}: envelope timestamp not tz-aware"
            )

            assert msg.get("from") == "orchestrator", (
                f"{path_name}: from field {msg.get('from')!r} "
                f"expected 'orchestrator'"
            )

            # Original body fields must still be present
            assert msg.get("type") == "task_assignment"
            assert "task_id" in msg
            assert "title" in msg
            assert "description" in msg

        await client.close()

    finally:
        await push_sub.unsubscribe()
        await observer_nc.close()
