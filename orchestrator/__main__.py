"""Entry point for `python3 -m orchestrator <project>`.

Wires together all orchestrator modules: config, state machine, task queue,
NATS client, tmux communicator, message router, lifecycle manager, console,
logging, and session report. Runs the async event loop.
"""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import subprocess
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
from orchestrator.watchdog import IdleWatchdog, InactivityAnnouncer
from orchestrator.delivery import DeliveryProtocol
from orchestrator.version import (
    capture_startup_info,
    make_version_request_handler,
)

# --- CLI ---
parser = argparse.ArgumentParser(description="Multi-Agent System Shell Orchestrator")
parser.add_argument("project", help="Project name (matches projects/<name>/)")
args = parser.parse_args()

# --- Singleton lock (prevents duplicate orchestrators per project) ---
# Must run BEFORE NATS connect, delivery protocol, or tmux comm init.
# The kernel auto-releases flock on process exit (including SIGKILL) because
# it tracks the underlying fd, so no stale-pidfile recovery is needed.
#
# IMPORTANT: open with O_CREAT|O_RDWR (NO O_TRUNC) so a losing contender
# doesn't wipe the holder's pid from the file. We truncate and write our
# pid only AFTER we successfully acquire the lock.
LOCK_PATH = f"/tmp/mas-orch-{args.project}.lock"
_lock_fd_raw = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
_lock_fd = os.fdopen(_lock_fd_raw, "r+")
try:
    fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    try:
        _lock_fd.seek(0)
        existing_pid = _lock_fd.read().strip() or "unknown"
    except OSError:
        existing_pid = "unknown"
    print(
        f"ERROR: another orchestrator is already running for project "
        f"'{args.project}' (pid {existing_pid})",
        file=sys.stderr,
    )
    print(f"Lock file: {LOCK_PATH}", file=sys.stderr)
    print(f"If stale, remove with: rm {LOCK_PATH}", file=sys.stderr)
    sys.exit(1)

# We own the lock. Truncate and write our pid so bounce-orchestrator.sh
# can read it. Keep _lock_fd alive for process lifetime — do NOT close
# it or the kernel releases the lock.
_lock_fd.seek(0)
_lock_fd.truncate()
_lock_fd.write(str(os.getpid()))
_lock_fd.flush()

# --- Paths ---
root_dir = Path(__file__).parent.parent
projects_dir = root_dir / "projects"

# --- Version probe (issue #31) ---
# Capture startup SHA, boot time, and pid once. The cached snapshot
# is handed to a NATS request handler registered in main() so
# external callers can detect stale-binary drift via
# `scripts/check-orchestrator-version.sh`.
_STARTUP_INFO = capture_startup_info(root_dir)

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


def _check_duplicate_orchestrator_panes(session: str) -> None:
    """Warn if more than one pane in the control window is labelled @label=orchestrator.

    This catches zombie-pane cases where a previous orchestrator process
    exited (releasing the flock) but its tmux pane was never cleaned up.
    Non-fatal: we log loudly but keep running, since tmux may not even be
    present (unit tests, CI).
    """
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{session}:control",
                "-F",
                "#{pane_id} #{@label}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # tmux not available or hung -- skip the check

    if result.returncode != 0:
        return  # session/window doesn't exist yet -- nothing to check

    orch_panes = [
        line
        for line in result.stdout.splitlines()
        if line.strip().endswith(" orchestrator")
    ]
    if len(orch_panes) > 1:
        logger.warning(
            "Detected %d panes labelled 'orchestrator' in %s:control -- "
            "zombie panes present: %s",
            len(orch_panes),
            session,
            orch_panes,
        )


_check_duplicate_orchestrator_panes(args.project)

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
    "routing": to_dict(config.routing) if hasattr(config, "routing") else {},
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

# Delivery protocol (reliable nudge with ACK + retransmit)
delivery = DeliveryProtocol(
    tmux_comm=tmux_comm,
    config=component_config,
)

# Lifecycle manager
lifecycle = TaskLifecycleManager(
    task_queue=task_queue,
    state_machine=state_machine,
    nats_client=nats_client,
    tmux_comm=tmux_comm,
    config=component_config,
    delivery=delivery,
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
    task_queue=task_queue,
)

# Message router
router = MessageRouter(
    nats_client=nats_client,
    state_machine=state_machine,
    lifecycle_manager=lifecycle,
    agents=agents_dict,
    tmux_comm=tmux_comm,
    watchdog=watchdog,
    delivery=delivery,
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

    # Register the version probe handler (issue #31). Core-NATS
    # request/reply on `system.orchestrator.version` — outside the
    # JetStream `agents.>` wildcard so requests are ephemeral and
    # responses fire back on the request-scoped inbox subject.
    version_handler = make_version_request_handler(_STARTUP_INFO)
    await nats_client.subscribe_core(
        "system.orchestrator.version", version_handler,
    )
    logger.info(
        "Version probe registered on system.orchestrator.version "
        "(startup_sha=%s pid=%d)",
        _STARTUP_INFO.startup_sha or "unknown",
        _STARTUP_INFO.pid,
    )

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

    # Start delivery protocol (reliable nudge with ACK + retransmit)
    await nats_client.subscribe_ack(delivery.handle_ack_message)
    asyncio.create_task(delivery.run())
    logger.info("Delivery protocol started -- ACK subscribed")

    # Start idle watchdog
    watchdog_cfg = component_config.get("watchdog", {})
    if watchdog_cfg.get("enabled", False):
        asyncio.create_task(watchdog.run())
        logger.info("Idle watchdog enabled")
    else:
        logger.info("Idle watchdog disabled (set watchdog.enabled: true to enable)")

    # Inactivity announcer
    announcer_cfg = watchdog_cfg.get("inactivity_announcer", {})
    if announcer_cfg.get("enabled", False):
        announcer = InactivityAnnouncer(nats_client=nats_client, router=router, config=announcer_cfg)
        asyncio.create_task(announcer.run())
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
