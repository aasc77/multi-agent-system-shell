#!/usr/bin/env python3
"""
NATS Message Indexer — subscribes to agents.> and indexes into ChromaDB.

Runs as a background daemon alongside the orchestrator. Uses a durable
NATS consumer so it can resume from where it left off after restart.

Usage:
    NATS_URL=nats://192.168.1.37:4222 \
    CHROMADB_PATH=/path/to/data/chromadb \
    python3 indexer.py
"""

import asyncio
import json
import logging
import os
import sys

# Add parent directory for store import
sys.path.insert(0, os.path.dirname(__file__))
import store

import nats
from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [indexer] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
STREAM_NAME = os.environ.get("NATS_STREAM", "AGENTS")
DURABLE_NAME = "knowledge-indexer"
SUBJECT = "agents.>"

# Message types to skip indexing (internal orchestrator messages)
SKIP_TYPES = {"all_done"}

# Stats
_indexed_count = 0
_skipped_count = 0


async def main():
    global _indexed_count, _skipped_count

    # Pre-flight: check Ollama
    ok, msg = await store.check_ollama_health()
    if not ok:
        logger.error("Ollama health check failed: %s", msg)
        sys.exit(1)

    logger.info("Connecting to NATS at %s", NATS_URL)
    nc = await nats.connect(
        NATS_URL,
        max_reconnect_attempts=-1,
        reconnect_time_wait=2,
    )
    js = nc.jetstream()

    # Create durable pull consumer
    try:
        sub = await js.pull_subscribe(
            SUBJECT,
            durable=DURABLE_NAME,
            stream=STREAM_NAME,
        )
    except Exception as e:
        logger.error("Failed to subscribe: %s", e)
        await nc.close()
        sys.exit(1)

    logger.info("Indexer started — subscribed to '%s' on stream '%s'", SUBJECT, STREAM_NAME)

    while True:
        try:
            msgs = await sub.fetch(batch=10, timeout=5)
        except nats.errors.TimeoutError:
            continue
        except Exception as e:
            logger.warning("Fetch error: %s", e)
            await asyncio.sleep(2)
            continue

        for msg in msgs:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
                msg_type = payload.get("type", "")

                # Skip non-indexable messages
                if msg_type in SKIP_TYPES:
                    await msg.ack()
                    _skipped_count += 1
                    continue

                if not payload.get("message_id"):
                    await msg.ack()
                    _skipped_count += 1
                    continue

                indexed = await store.index_message(payload, msg.subject)
                if indexed:
                    _indexed_count += 1
                    logger.debug(
                        "Indexed %s from %s [total: %d]",
                        payload.get("message_id"),
                        msg.subject,
                        _indexed_count,
                    )
                else:
                    _skipped_count += 1

            except Exception as e:
                logger.warning(
                    "Failed to index message on %s: %s",
                    msg.subject, e,
                )
                _skipped_count += 1

            # Always ACK to avoid redelivery loops
            await msg.ack()

        # Log stats periodically
        if _indexed_count > 0 and _indexed_count % 50 == 0:
            logger.info("Stats: indexed=%d skipped=%d", _indexed_count, _skipped_count)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Indexer stopped (indexed=%d skipped=%d)", _indexed_count, _skipped_count)
