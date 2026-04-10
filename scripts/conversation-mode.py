#!/usr/bin/env python3
"""Conversation Mode — hear agents talking to each other on home speakers.

Subscribes to all agent inbox messages on NATS and forwards agent-to-agent
messages to the hassio speaker service so they're spoken aloud via Piper TTS.

Usage:
    python3 scripts/conversation-mode.py [--nats-url nats://192.168.1.37:4222]

Each agent's message is spoken in their assigned voice (resolved by the
speaker service from the 'from' field).
"""

import argparse
import asyncio
import json
import logging
import signal
import time

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy, AckPolicy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CONVO] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STREAM_NAME = "AGENTS"
INBOX_SUBJECT = "agents.*.inbox"
SPEAKER_SUBJECT = "agents.hassio.speaker"
CONSUMER_NAME = "conversation-mode"

# Filter rules
MIN_MESSAGE_LENGTH = 20
MAX_SPEECH_LENGTH = 200
MIN_GAP_SECONDS = 3

NOISE_PHRASES = frozenset({
    "ok", "ack", "acknowledged", "got it", "roger", "understood",
    "standing by", "no new messages", "checking", "done",
})


# ---------------------------------------------------------------------------
# Conversation Mode
# ---------------------------------------------------------------------------

class ConversationMode:
    def __init__(self, nats_url: str):
        self._nats_url = nats_url
        self._nc = None
        self._js = None
        self._last_speak_time = 0.0
        self._running = True

    async def start(self):
        self._nc = await nats.connect(self._nats_url)
        self._js = self._nc.jetstream()
        log.info("Connected to NATS at %s", self._nats_url)

        # Create ephemeral consumer for all inboxes
        sub = await self._js.subscribe(
            INBOX_SUBJECT,
            stream=STREAM_NAME,
            config=ConsumerConfig(
                deliver_policy=DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                filter_subject=INBOX_SUBJECT,
            ),
        )

        log.info("Conversation mode ON — listening to agent messages")

        try:
            async for msg in sub.messages:
                if not self._running:
                    break
                await self._handle_message(msg)
        except asyncio.CancelledError:
            pass
        finally:
            await sub.unsubscribe()
            await self._nc.close()
            log.info("Conversation mode OFF")

    async def _handle_message(self, msg):
        await msg.ack()

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        # Only agent-to-agent messages
        if payload.get("type") != "agent_message":
            return

        sender = payload.get("from", "unknown")
        message = payload.get("message", "")

        # Filter noise
        if not self._should_speak(message):
            return

        # Rate limit
        now = time.time()
        gap = now - self._last_speak_time
        if gap < MIN_GAP_SECONDS:
            await asyncio.sleep(MIN_GAP_SECONDS - gap)

        # Truncate for speech
        speech_text = message[:MAX_SPEECH_LENGTH]
        if len(message) > MAX_SPEECH_LENGTH:
            speech_text += "..."

        # Extract target agent from subject (agents.<role>.inbox)
        target = msg.subject.split(".")[1] if "." in msg.subject else "unknown"

        # Prefix with who's talking to whom
        spoken = f"{sender} says to {target}: {speech_text}"

        await self._publish_to_speaker(sender, spoken)
        self._last_speak_time = time.time()
        log.info("%s → %s: %s", sender, target, speech_text[:80])

    def _should_speak(self, message: str) -> bool:
        if not message or len(message) < MIN_MESSAGE_LENGTH:
            return False
        normalized = message.strip().lower().rstrip(".")
        if normalized in NOISE_PHRASES:
            return False
        return True

    async def _publish_to_speaker(self, sender: str, text: str):
        envelope = json.dumps({"text": text, "from": sender})
        await self._js.publish(SPEAKER_SUBJECT, envelope.encode())

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Conversation Mode — hear agents talk")
    parser.add_argument(
        "--nats-url",
        default="nats://127.0.0.1:4222",
        help="NATS server URL (default: nats://127.0.0.1:4222)",
    )
    args = parser.parse_args()

    convo = ConversationMode(args.nats_url)

    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        log.info("Shutting down...")
        convo.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(convo.start())


if __name__ == "__main__":
    main()
