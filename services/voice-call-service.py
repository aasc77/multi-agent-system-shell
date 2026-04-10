#!/usr/bin/env python3
"""
Voice Call Service — NATS-triggered Twilio voice calls with TTS.

Subscribes to `agents.voicecall.inbox` on NATS. When a message arrives,
makes a Twilio voice call to the user's phone and speaks the text via TTS.

Any agent can trigger a call:
    send_to_agent(target_agent="voicecall", message="Move left and stop there")

Credentials are read from macOS Keychain via secure-credential.sh.

Usage:
    python3 services/voice-call-service.py
    python3 services/voice-call-service.py --once "Test message"  # single call, no NATS

Environment:
    NATS_URL     -- NATS server URL (default: nats://127.0.0.1:4222)
    NATS_STREAM  -- JetStream stream name (default: AGENTS)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [voicecall] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
STREAM_NAME = os.environ.get("NATS_STREAM", "AGENTS")
INBOX_SUBJECT = "agents.voicecall.inbox"
CONSUMER_NAME = "voice-call-service"
CREDENTIAL_SCRIPT = os.path.expanduser(
    "~/Repositories/operations/scripts/secure-credential.sh"
)


# ---------------------------------------------------------------------------
# Credential retrieval
# ---------------------------------------------------------------------------

def get_credential(service: str, account: str) -> str:
    """Retrieve a credential from macOS Keychain."""
    result = subprocess.run(
        ["bash", "-c", f"{CREDENTIAL_SCRIPT} get {service} {account}"],
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value or result.returncode != 0:
        logger.error("Could not retrieve %s/%s from Keychain", service, account)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Twilio voice call
# ---------------------------------------------------------------------------

def make_voice_call(text: str, to_number: str | None = None) -> str:
    """
    Make a Twilio voice call that speaks the given text via TTS.

    Returns the call SID on success.
    """
    from twilio.rest import Client

    account_sid = get_credential("twilio", "account_sid")
    auth_token = get_credential("twilio", "auth_token")
    from_number = get_credential("twilio", "from_number")
    if to_number is None:
        to_number = get_credential("twilio", "user_phone")

    client = Client(account_sid, auth_token)

    # Use TwiML to speak the text. Pause at start so user hears from the beginning.
    twiml = (
        '<Response>'
        '<Pause length="1"/>'
        f'<Say voice="Polly.Joanna" language="en-US">{_escape_xml(text)}</Say>'
        '<Pause length="1"/>'
        '</Response>'
    )

    call = client.calls.create(
        twiml=twiml,
        from_=from_number,
        to=to_number,
    )
    logger.info("Call initiated: SID=%s, to=%s", call.sid, to_number)
    return call.sid


def _escape_xml(text: str) -> str:
    """Escape XML special characters for TwiML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ---------------------------------------------------------------------------
# NATS listener
# ---------------------------------------------------------------------------

class VoiceCallService:
    def __init__(self, nats_url: str = NATS_URL):
        self._nats_url = nats_url
        self._running = True
        self._calls_made = 0

    async def start(self):
        import nats
        from nats.js.api import ConsumerConfig, DeliverPolicy, AckPolicy

        logger.info("Connecting to NATS at %s", self._nats_url)
        nc = await nats.connect(
            self._nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        js = nc.jetstream()

        sub = await js.subscribe(
            INBOX_SUBJECT,
            stream=STREAM_NAME,
            config=ConsumerConfig(
                durable_name=CONSUMER_NAME,
                deliver_policy=DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                filter_subject=INBOX_SUBJECT,
            ),
        )

        logger.info("Voice call service started — listening on %s", INBOX_SUBJECT)

        try:
            async for msg in sub.messages:
                if not self._running:
                    break
                await self._handle(msg)
        except asyncio.CancelledError:
            pass
        finally:
            await sub.unsubscribe()
            await nc.close()
            logger.info("Voice call service stopped (calls=%d)", self._calls_made)

    async def _handle(self, msg):
        await msg.ack()

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Dropped unparseable message")
            return

        # Extract text from MCP bridge format or direct
        text = payload.get("message", "") or payload.get("text", "")
        if not text:
            logger.warning("Dropped message with no text")
            return

        from_agent = payload.get("from", "unknown")
        logger.info("Call request from %s: %s", from_agent, text[:80])

        try:
            sid = make_voice_call(text)
            self._calls_made += 1
            logger.info("Call succeeded: SID=%s", sid)
        except Exception as e:
            logger.error("Call failed: %s", e)

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Twilio voice call via NATS or CLI")
    parser.add_argument(
        "--once",
        metavar="MESSAGE",
        help="Make a single call with this message and exit (no NATS)",
    )
    parser.add_argument(
        "--to",
        help="Override recipient phone number (E.164 format)",
    )
    args = parser.parse_args()

    if args.once:
        sid = make_voice_call(args.once, to_number=args.to)
        print(f"Call initiated: SID={sid}")
        return

    service = VoiceCallService()

    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        service.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(service.start())


if __name__ == "__main__":
    main()
