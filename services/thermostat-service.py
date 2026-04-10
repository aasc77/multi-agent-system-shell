#!/usr/bin/env python3
"""
Thermostat Service — NATS-triggered Home Assistant climate control.

Subscribes to `agents.thermostat.inbox` on NATS. Parses natural language
commands and calls the HA REST API to control climate.thermostat.

Any agent can control the thermostat:
    send_to_agent(target_agent="thermostat", message="set to 72")
    send_to_agent(target_agent="thermostat", message="turn AC on")
    send_to_agent(target_agent="thermostat", message="status")

Runs locally on the orchestrator machine. HA API accessed over LAN.

Usage:
    python3 services/thermostat-service.py
    python3 services/thermostat-service.py --once "set to 72"

Environment:
    NATS_URL       -- NATS server URL (default: nats://127.0.0.1:4222)
    NATS_STREAM    -- JetStream stream name (default: AGENTS)
    HA_URL         -- Home Assistant API URL (default: http://homeassistant.local:8123)
    CLIMATE_ENTITY -- Climate entity ID (default: climate.thermostat)
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys

import httpx
import nats
from nats.js.api import ConsumerConfig, DeliverPolicy, AckPolicy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [thermostat] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
STREAM_NAME = os.environ.get("NATS_STREAM", "AGENTS")
INBOX_SUBJECT = "agents.thermostat.inbox"
CONSUMER_NAME = "thermostat-service"

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
CLIMATE_ENTITY = os.environ.get("CLIMATE_ENTITY", "climate.thermostat")

CREDENTIAL_SCRIPT = os.path.expanduser(
    "~/Repositories/operations/scripts/secure-credential.sh"
)


# ---------------------------------------------------------------------------
# HA API
# ---------------------------------------------------------------------------

def _get_ha_token() -> str:
    result = subprocess.run(
        ["bash", "-c", f"{CREDENTIAL_SCRIPT} get homeassistant long-lived-token"],
        capture_output=True, text=True,
    )
    token = result.stdout.strip()
    if not token:
        logger.error("Could not retrieve HA token from Keychain")
        sys.exit(1)
    return token


_ha_token = None


def _get_headers() -> dict:
    global _ha_token
    if _ha_token is None:
        _ha_token = _get_ha_token()
    return {"Authorization": f"Bearer {_ha_token}", "Content-Type": "application/json"}


async def ha_get_status() -> dict:
    """Get current thermostat status from HA."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{HA_URL}/api/states/{CLIMATE_ENTITY}",
            headers=_get_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        attrs = data.get("attributes", {})
        return {
            "mode": data.get("state", "unknown"),
            "current_temp": attrs.get("current_temperature"),
            "target_temp": attrs.get("temperature"),
            "humidity": attrs.get("current_humidity"),
            "fan_mode": attrs.get("fan_mode"),
        }


async def ha_call_service(domain: str, service: str, data: dict) -> bool:
    """Call an HA service."""
    payload = {"entity_id": CLIMATE_ENTITY, **data}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{HA_URL}/api/services/{domain}/{service}",
            headers=_get_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return True


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

def parse_command(text: str) -> dict | None:
    """
    Parse natural language thermostat command.

    Returns dict with 'action' and params, or None if unparseable.
    Actions: set_mode, set_temp, set_fan, status
    """
    text = text.strip().lower()

    # Status
    if text in ("status", "state", "current", "what's the temperature", "temp"):
        return {"action": "status"}

    # Turn off
    if text in ("turn off", "off", "stop", "shut off"):
        return {"action": "set_mode", "mode": "off"}

    # AC / cool
    if re.search(r"\b(ac|cool|cooling)\b", text):
        return {"action": "set_mode", "mode": "cool"}

    # Heat
    if re.search(r"\b(heat|heating|warm)\b", text):
        # "heat mode" or "turn heat on"
        return {"action": "set_mode", "mode": "heat"}

    # Auto / heat_cool
    if re.search(r"\b(auto mode|heat.?cool)\b", text):
        return {"action": "set_mode", "mode": "heat_cool"}

    # Fan
    fan_match = re.search(r"\bfan\s+(on|auto|off)\b", text)
    if fan_match:
        mode = fan_match.group(1)
        if mode == "off":
            mode = "auto"  # HA doesn't have "off" fan mode, use auto
        return {"action": "set_fan", "fan_mode": mode}

    # Set temperature
    temp_match = re.search(r"(?:set\s+(?:to\s+)?|temp\s+)(\d+)", text)
    if temp_match:
        temp = int(temp_match.group(1))
        if 45 <= temp <= 95:
            return {"action": "set_temp", "temperature": temp}
        else:
            return {"action": "error", "message": f"Temperature {temp} out of range (45-95°F)"}

    # Bare number
    bare_match = re.match(r"^(\d+)\s*(?:degrees?|°?f?)?$", text)
    if bare_match:
        temp = int(bare_match.group(1))
        if 45 <= temp <= 95:
            return {"action": "set_temp", "temperature": temp}

    return None


async def execute_command(cmd: dict) -> str:
    """Execute a parsed command and return a response string."""
    action = cmd["action"]

    if action == "status":
        status = await ha_get_status()
        return (
            f"Mode: {status['mode']} | "
            f"Current: {status['current_temp']}°F | "
            f"Target: {status['target_temp']}°F | "
            f"Humidity: {status['humidity']}% | "
            f"Fan: {status['fan_mode']}"
        )

    elif action == "set_mode":
        mode = cmd["mode"]
        await ha_call_service("climate", "set_hvac_mode", {"hvac_mode": mode})
        return f"HVAC mode set to: {mode}"

    elif action == "set_temp":
        temp = cmd["temperature"]
        await ha_call_service("climate", "set_temperature", {"temperature": temp})
        return f"Temperature set to: {temp}°F"

    elif action == "set_fan":
        fan_mode = cmd["fan_mode"]
        await ha_call_service("climate", "set_fan_mode", {"fan_mode": fan_mode})
        return f"Fan mode set to: {fan_mode}"

    elif action == "error":
        return f"Error: {cmd['message']}"

    return "Unknown action"


# ---------------------------------------------------------------------------
# NATS Service
# ---------------------------------------------------------------------------

class ThermostatService:
    def __init__(self, nats_url: str = NATS_URL):
        self._nats_url = nats_url
        self._nc = None
        self._running = True
        self._commands = 0

    async def start(self):
        logger.info("Connecting to NATS at %s", self._nats_url)
        self._nc = await nats.connect(
            self._nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        js = self._nc.jetstream()

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

        logger.info("Thermostat service started — listening on %s", INBOX_SUBJECT)

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
            logger.info("Thermostat service stopped (commands=%d)", self._commands)

    async def _handle(self, msg):
        await msg.ack()

        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        text = payload.get("message", "") or payload.get("text", "")
        if not text:
            return

        from_agent = payload.get("from", "unknown")
        logger.info("Command from %s: %s", from_agent, text)

        cmd = parse_command(text)
        if cmd is None:
            response = f"Could not parse thermostat command: '{text}'. Try: status, set to 72, turn AC on, heat mode, fan on, turn off"
            logger.warning("Unparseable: %s", text)
        else:
            try:
                response = await execute_command(cmd)
                self._commands += 1
                logger.info("Result: %s", response)
            except Exception as e:
                response = f"Error executing command: {e}"
                logger.error("Command failed: %s", e)

        # Reply to sender via NATS
        try:
            reply_subject = f"agents.{from_agent}.inbox"
            reply_payload = json.dumps({
                "type": "agent_message",
                "from": "thermostat",
                "message": response,
            })
            await self._nc.publish(reply_subject, reply_payload.encode())
            await self._nc.flush()
        except Exception as e:
            logger.error("Failed to reply to %s: %s", from_agent, e)

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Thermostat control via NATS")
    parser.add_argument("--once", metavar="CMD", help="Execute a single command and exit")
    args = parser.parse_args()

    if args.once:
        cmd = parse_command(args.once)
        if cmd is None:
            print(f"Could not parse: '{args.once}'")
            sys.exit(1)
        result = asyncio.run(execute_command(cmd))
        print(result)
        return

    service = ThermostatService()
    loop = asyncio.new_event_loop()

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        service.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(service.start())


if __name__ == "__main__":
    main()
