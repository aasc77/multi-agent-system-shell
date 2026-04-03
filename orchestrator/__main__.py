"""Entry point for `python3 -m orchestrator <project>`.

Wires together all orchestrator modules: config, state machine, task queue,
NATS client, tmux communicator, message router, lifecycle manager, console,
logging, and session report. Runs the async event loop.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import threading
import queue
from pathlib import Path

from orchestrator.config import load_config, ConfigError
from orchestrator.state_machine import StateMachine
from orchestrator.task_queue import TaskQueue
from orchestrator.nats_client import NatsClient
from orchestrator.tmux_comm import TmuxComm
from orchestrator.router import MessageRouter
from orchestrator.lifecycle import TaskLifecycleManager
from orchestrator.console import Console
from orchestrator.logging_setup import setup_logging
from orchestrator.session_report import SessionReport
from orchestrator.watchdog import IdleWatchdog

# --- CLI ---
parser = argparse.ArgumentParser(description="Multi-Agent System Shell Orchestrator")
parser.add_argument("project", help="Project name (matches projects/<name>/)")
args = parser.parse_args()

# --- Paths ---
root_dir = Path(__file__).parent.parent
projects_dir = root_dir / "projects"

# --- Config ---
try:
    config = load_config(root_dir=root_dir, project_name=args.project)
except ConfigError as e:
    print(f"Config error: {e}", file=sys.stderr)
    sys.exit(1)

# --- Logging ---
log_file = str(projects_dir / args.project / "orchestrator.log")
logger = setup_logging(log_file)

logger.info("=" * 50)
logger.info("MAS Orchestrator starting -- project: %s", args.project)
logger.info("=" * 50)

# --- Components ---
def to_dict(obj):
    """Recursively convert ConfigNode objects to plain dicts."""
    if hasattr(obj, "__dict__") and type(obj).__name__ == "ConfigNode":
        return {k: to_dict(v) for k, v in vars(obj).items()}
    if isinstance(obj, list):
        return [to_dict(item) for item in obj]
    return obj


# Build a plain dict config for components that expect dict (tmux_comm, lifecycle)
agents_dict = {name: to_dict(agent) for name, agent in config.agents.items()}
sm_config = to_dict(config.state_machine) if hasattr(config, "state_machine") else {}
nats_config = to_dict(config.nats) if hasattr(config, "nats") else {}
tmux_config = to_dict(config.tmux) if hasattr(config, "tmux") else {}

# Plain dict for components
component_config = {
    "agents": agents_dict,
    "tmux": tmux_config,
    "tasks": to_dict(config.tasks) if hasattr(config, "tasks") else {},
    "nats": nats_config,
    "watchdog": to_dict(config.watchdog) if hasattr(config, "watchdog") else {},
}

# Task queue
tasks_path = projects_dir / args.project / "tasks.json"
task_queue = TaskQueue(str(tasks_path))

# State machine
state_machine = StateMachine(config=sm_config, agents=agents_dict)

# NATS client
nats_client = NatsClient(config=nats_config, agents=agents_dict)

# tmux communicator
tmux_comm = TmuxComm(component_config)

# Lifecycle manager
lifecycle = TaskLifecycleManager(
    task_queue=task_queue,
    state_machine=state_machine,
    nats_client=nats_client,
    tmux_comm=tmux_comm,
    config=component_config,
)

# Session report
report = SessionReport(
    project_name=args.project,
    projects_dir=str(projects_dir),
)

# Console
console = Console(
    config=component_config,
    state_machine=state_machine,
    task_queue=task_queue,
    nats_client=nats_client,
    tmux_comm=tmux_comm,
    lifecycle_manager=lifecycle,
)

# Idle watchdog
watchdog = IdleWatchdog(
    lifecycle=lifecycle,
    state_machine=state_machine,
    nats_client=nats_client,
    tmux_comm=tmux_comm,
    config=component_config,
)

# Message router
router = MessageRouter(
    nats_client=nats_client,
    state_machine=state_machine,
    lifecycle_manager=lifecycle,
    agents=agents_dict,
    tmux_comm=tmux_comm,
    watchdog=watchdog,
)

# --- Interactive command reader ---
_cmd_queue: queue.Queue = queue.Queue()


def _stdin_reader():
    while True:
        try:
            line = input()
            if line.strip():
                _cmd_queue.put(line.strip())
        except EOFError:
            break


# --- Main async loop ---
async def main():
    # Connect to NATS
    try:
        await nats_client.connect()
        logger.info("NATS connected at %s", nats_config.get("url", "?"))
    except Exception as e:
        logger.error("Failed to connect to NATS: %s", e)
        logger.error("Run: ./scripts/setup-nats.sh")
        sys.exit(1)

    # Start router (subscribes to outbox subjects)
    await router.start()
    logger.info("Message router started -- subscribed to agent outboxes")

    # Wait for agents to initialize (Claude Code needs time to start + connect MCP)
    logger.info("Waiting for agents to initialize...")
    await asyncio.sleep(10)

    # Process first task
    result = await lifecycle.process_next_task()
    if result:
        task = lifecycle.current_task
        logger.info("Started first task: %s", task["title"] if task else "?")
    else:
        logger.info("No pending tasks -- waiting for commands")

    # Start stdin reader
    cmd_thread = threading.Thread(target=_stdin_reader, daemon=True)
    cmd_thread.start()

    logger.info("Type 'help' for commands")
    logger.info("")

    # Start idle watchdog
    watchdog_cfg = component_config.get("watchdog", {})
    if watchdog_cfg.get("enabled", False):
        asyncio.create_task(watchdog.run())
        logger.info("Idle watchdog enabled")
    else:
        logger.info("Idle watchdog disabled (set watchdog.enabled: true to enable)")

    # Inactivity announcer — speaks when no NATS activity for X seconds
    announcer_cfg = watchdog_cfg.get("inactivity_announcer", {})
    inactivity_threshold = announcer_cfg.get("threshold_seconds", 300)
    escalate_after = announcer_cfg.get("escalate_after", 3)
    _inactivity_count = 0
    _inactivity_escalated = False

    announce_on_speaker = announcer_cfg.get("announce_on_speaker", False)

    async def inactivity_announcer():
        nonlocal _inactivity_count, _inactivity_escalated
        speaker_subject = "agents.hassio.speaker"
        while True:
            await asyncio.sleep(30)  # check every 30s
            idle_seconds = time.time() - router.last_activity_time

            # Check if we've crossed another inactivity threshold
            if idle_seconds >= inactivity_threshold:
                expected_count = int(idle_seconds // inactivity_threshold)
                if expected_count > _inactivity_count:
                    _inactivity_count = expected_count
                    minutes = int(idle_seconds // 60)
                    logger.info("Inactivity alert #%d — no activity for %d minutes", _inactivity_count, minutes)

                    # Always notify manager via NATS
                    notify = {
                        "type": "agent_message",
                        "from": "orchestrator",
                        "message": f"Inactivity alert #{_inactivity_count}: no agent activity for {minutes} minutes. All agents appear idle.",
                        "priority": "normal",
                    }
                    try:
                        await nats_client.publish_to_inbox("manager", notify)
                        logger.info("Inactivity alert #%d sent to manager", _inactivity_count)
                    except Exception as e:
                        logger.warning("Failed to notify manager: %s", e)

                    # Optionally announce on speaker
                    if announce_on_speaker:
                        msg = json.dumps({
                            "text": f"Orchestrator here. No agent activity for {minutes} minutes. Alert number {_inactivity_count}.",
                            "from": "orchestrator",
                        })
                        try:
                            await nats_client.publish_raw(speaker_subject, msg.encode())
                        except Exception:
                            pass

                    # Escalate after N consecutive alerts
                    if _inactivity_count >= escalate_after and not _inactivity_escalated:
                        _inactivity_escalated = True
                        logger.warning("Inactivity escalation — %d alerts, notifying manager to investigate", escalate_after)
                        escalation = {
                            "type": "agent_message",
                            "from": "orchestrator",
                            "message": f"ESCALATION: No agent activity for {_inactivity_count} consecutive checks. Investigate why agents are idle. Ask hub (QA) to run health tests.",
                            "priority": "urgent",
                        }
                        try:
                            await nats_client.publish_to_inbox("manager", escalation)
                        except Exception as e:
                            logger.warning("Failed to escalate: %s", e)

            elif idle_seconds < inactivity_threshold:
                if _inactivity_count > 0:
                    logger.info("Activity resumed after %d inactivity alerts — flags cleared", _inactivity_count)
                _inactivity_count = 0
                _inactivity_escalated = False

    if announcer_cfg.get("enabled", False):
        asyncio.create_task(inactivity_announcer())
        logger.info("Inactivity announcer enabled (threshold=%ds, escalate after %d)", inactivity_threshold, escalate_after)
    else:
        logger.info("Inactivity announcer disabled (set watchdog.inactivity_announcer.enabled: true to enable)")

    # Poll loop
    try:
        while True:
            # Process commands
            while not _cmd_queue.empty():
                try:
                    cmd = _cmd_queue.get_nowait()
                    result = console.handle_command(cmd)
                    if result:
                        print(result)
                except queue.Empty:
                    break

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Orchestrator stopped by user")
    finally:
        await nats_client.close()
        logger.info("NATS disconnected")


if __name__ == "__main__":
    asyncio.run(main())
