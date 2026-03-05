"""Structured logging setup for the orchestrator.

Provides dual output (file + stdout), structured format, and helper
functions for logging state transitions, task assignments, NATS events,
and nudge attempts.

Requirements: PRD R11, R4, R5, R3, R6.
"""

from __future__ import annotations

import logging
import os
import sys


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def setup_logging(
    log_file: str,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure and return the ``orchestrator`` logger.

    Creates a logger named ``orchestrator`` with dual handlers:
    one writing to *log_file* and one writing to ``sys.stdout``.

    If the parent directory for *log_file* does not exist it is created
    automatically.

    Calling this function multiple times will **not** duplicate handlers;
    existing handlers are cleared first.

    Parameters
    ----------
    log_file:
        Absolute or relative path to the log file.
    level:
        Logging level (default ``logging.INFO``).
    """
    logger = logging.getLogger("orchestrator")
    logger.setLevel(level)

    # Prevent duplicate handlers on repeated calls.
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT)

    # Ensure the log directory exists.
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


# ------------------------------------------------------------------
# Domain-specific logging helpers
# ------------------------------------------------------------------


def log_state_transition(
    logger: logging.Logger,
    *,
    from_state: str,
    to_state: str,
    task_id: str | None = None,
) -> None:
    """Log a state machine transition at INFO level."""
    msg = f"State transition: {from_state} -> {to_state}"
    if task_id is not None:
        msg += f" (task_id={task_id})"
    logger.info(msg)


def log_task_assignment(
    logger: logging.Logger,
    *,
    task_id: str,
    agent: str,
    title: str | None = None,
) -> None:
    """Log a task assignment at INFO level."""
    msg = f"Task assigned: {task_id} -> {agent}"
    if title is not None:
        msg += f" [{title}]"
    logger.info(msg)


def log_nats_publish(
    logger: logging.Logger,
    *,
    subject: str,
    message_type: str | None = None,
) -> None:
    """Log a NATS publish event at INFO level."""
    msg = f"NATS publish: {subject}"
    if message_type is not None:
        msg += f" (type={message_type})"
    logger.info(msg)


def log_nats_subscribe(
    logger: logging.Logger,
    *,
    subject: str,
    consumer: str | None = None,
) -> None:
    """Log a NATS subscribe event at INFO level."""
    msg = f"NATS subscribe: {subject}"
    if consumer is not None:
        msg += f" (consumer={consumer})"
    logger.info(msg)


def log_nudge_sent(
    logger: logging.Logger,
    *,
    agent: str,
    target: str | None = None,
) -> None:
    """Log that a nudge was sent to an agent at INFO level."""
    msg = f"Nudge sent to {agent}"
    if target is not None:
        msg += f" (target={target})"
    logger.info(msg)


def log_nudge_skipped(
    logger: logging.Logger,
    *,
    agent: str,
    reason: str,
) -> None:
    """Log that a nudge was skipped at WARNING level."""
    msg = f"Nudge skipped for {agent}: {reason}"
    logger.warning(msg)


def log_nudge_escalated(
    logger: logging.Logger,
    *,
    agent: str,
    retries: int,
) -> None:
    """Log that nudge retries were exhausted at WARNING level."""
    msg = (
        f"Nudge escalated for {agent}: retries exhausted ({retries}). "
        f"Agent appears stuck."
    )
    logger.warning(msg)
