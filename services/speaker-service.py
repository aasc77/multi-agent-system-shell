#!/usr/bin/env python3
"""
Speaker Service — routes speak requests from any agent to hassio speakers.

Subscribes to `agents.speaker.inbox` on NATS JetStream. When a message arrives,
resolves the sender's voice from the voice map and re-publishes to
`agents.hassio.speaker` for Piper TTS playback.

Any agent anywhere can trigger speech by calling:
    send_to_agent(target_agent="speaker", message="Hello world")

The speaker service runs locally on the orchestrator machine as a background
daemon (started by start.sh alongside the knowledge indexer).

Usage:
    NATS_URL=nats://127.0.0.1:4222 python3 services/speaker-service.py

Environment:
    NATS_URL        -- NATS server URL (default: nats://127.0.0.1:4222)
    NATS_STREAM     -- JetStream stream name (default: AGENTS)
"""

import asyncio
import json
import logging
import os
import signal
import sys

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy, AckPolicy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [speaker] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
STREAM_NAME = os.environ.get("NATS_STREAM", "AGENTS")
INBOX_SUBJECT = "agents.speaker.inbox"
SPEAKER_SUBJECT = "agents.hassio.speaker"
CONSUMER_NAME = "speaker-service"

# Voice map: agent name -> Piper voice
VOICE_MAP = {
    "hub": "lessac",
    "hassio": "amy",
    "dgx": "ryan",
    "dgx2": "ryan",
    "macmini": "kusal",
    "manager": "alba",
    "orchestrator": "amy",
    "system": "amy",
}
DEFAULT_VOICE = "amy"


# ---------------------------------------------------------------------------
# Voice resolution
# ---------------------------------------------------------------------------

def resolve_voice(from_agent: str) -> str:
    """Resolve the Piper voice for a given agent name."""
    return VOICE_MAP.get(from_agent, DEFAULT_VOICE)


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def parse_speak_request(data: bytes) -> dict | None:
    """
    Parse a speak request from NATS message data.

    Accepts two formats:
    1. MCP bridge format: {"type": "agent_message", "from": "hub", "message": "text to speak"}
    2. Direct format: {"text": "text to speak", "from": "hub"}

    Returns dict with 'text' and 'from' keys, or None if unparseable.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    from_agent = payload.get("from", "unknown")

    # MCP bridge format (send_to_agent puts content in "message")
    text = payload.get("message", "")
    if not text:
        # Direct format
        text = payload.get("text", "")

    if not text or not isinstance(text, str):
        return None

    return {"text": text.strip(), "from": from_agent}


# ---------------------------------------------------------------------------
# Speaker Service
# ---------------------------------------------------------------------------

class SpeakerService:
    def __init__(self, nats_url: str = NATS_URL):
        self._nats_url = nats_url
        self._nc = None
        self._js = None
        self._running = True
        self._forwarded = 0
        self._dropped = 0

    async def start(self):
        logger.info("Connecting to NATS at %s", self._nats_url)
        self._nc = await nats.connect(
            self._nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        self._js = self._nc.jetstream()

        # Durable consumer so we don't miss messages across restarts
        sub = await self._js.subscribe(
            INBOX_SUBJECT,
            stream=STREAM_NAME,
            config=ConsumerConfig(
                durable_name=CONSUMER_NAME,
                deliver_policy=DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                filter_subject=INBOX_SUBJECT,
            ),
        )

        logger.info("Speaker service started — listening on %s", INBOX_SUBJECT)

        try:
            async for msg in sub.messages:
                if not self._running:
                    break
                await self._handle(msg)
        except asyncio.CancelledError:
            pass
        finally:
            await sub.unsubscribe()
            await self._nc.close()
            logger.info(
                "Speaker service stopped (forwarded=%d, dropped=%d)",
                self._forwarded, self._dropped,
            )

    async def _handle(self, msg):
        await msg.ack()

        request = parse_speak_request(msg.data)
        if not request:
            self._dropped += 1
            logger.debug("Dropped unparseable message")
            return

        from_agent = request["from"]
        text = request["text"]
        voice = resolve_voice(from_agent)

        # Build the hassio speaker envelope
        envelope = json.dumps({
            "text": text,
            "from": from_agent,
            "voice": voice,
        })

        try:
            # Use plain NATS publish (not JetStream) for the outbound speaker
            # message — the hassio speaker service subscribes via plain nats sub,
            # and the original /speak skill also uses nc.publish.
            await self._nc.publish(SPEAKER_SUBJECT, envelope.encode())
            await self._nc.flush()
            self._forwarded += 1
            logger.info(
                "Spoke for %s (voice=%s): %s",
                from_agent, voice, text[:80],
            )
        except Exception as e:
            logger.error("Failed to publish to %s: %s", SPEAKER_SUBJECT, e)
            self._dropped += 1

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    service = SpeakerService()

    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        logger.info("Shutting down (signal %s)...", sig)
        service.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(service.start())


if __name__ == "__main__":
    main()
