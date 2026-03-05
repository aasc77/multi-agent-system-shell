"""Entry point for `python3 -m orchestrator <project>`.

Wires together all orchestrator modules: config, state machine, task queue,
NATS client, tmux communicator, message router, lifecycle manager, console,
logging, and session report. Runs the async event loop.
"""

import argparse
import asyncio
import logging
import sys
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

# Message router
router = MessageRouter(
    nats_client=nats_client,
    state_machine=state_machine,
    lifecycle_manager=lifecycle,
    agents=agents_dict,
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

    # Wait for agents to connect before publishing first task
    logger.info("Waiting for agents to connect...")
    await asyncio.sleep(3)

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
