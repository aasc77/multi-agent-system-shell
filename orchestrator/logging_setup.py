"""Structured logging setup for the orchestrator.

Provides dual output (file + stdout), structured format, and domain-specific
helper functions for logging state transitions, task assignments, NATS events,
and nudge attempts.

Requirements
------------
- **R11** – Logging: dual output to file and stdout, structured format.
- **R4**  – State Machine: state transitions must be logged.
- **R5**  – Task Queue: task assignments must be logged.
- **R3**  – Communication Flow: NATS pub/sub events must be logged.
- **R6**  – tmux Communication: nudge attempts must be logged.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_LOGGER_NAME: str = "orchestrator"
"""Name used for the shared :class:`logging.Logger` instance."""

_LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(message)s"
"""Format string applied to every handler (file and stream)."""

_FILE_MODE: str = "a"
"""File open mode for the :class:`logging.FileHandler` (append)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _configure_handler(
    handler: logging.Handler,
    level: int,
    formatter: logging.Formatter,
) -> None:
    """Apply *level* and *formatter* to *handler*.

    Centralises the repetitive ``setLevel`` / ``setFormatter`` calls that
    every handler needs, keeping :func:`setup_logging` DRY.
    """
    handler.setLevel(level)
    handler.setFormatter(formatter)


# ---------------------------------------------------------------------------
# Public API – logger factory
# ---------------------------------------------------------------------------


def setup_logging(
    log_file: str,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure and return the ``orchestrator`` logger.

    Creates a logger named :data:`_LOGGER_NAME` with dual handlers:
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

    Returns
    -------
    logging.Logger
        The fully-configured ``orchestrator`` logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
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

    file_handler = logging.FileHandler(log_file, mode=_FILE_MODE)
    _configure_handler(file_handler, level, formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    _configure_handler(stream_handler, level, formatter)
    logger.addHandler(stream_handler)

    return logger


# ---------------------------------------------------------------------------
# Domain-specific logging helpers (PRD R4, R5, R3, R6)
# ---------------------------------------------------------------------------


def log_state_transition(
    logger: logging.Logger,
    *,
    from_state: str,
    to_state: str,
    task_id: Optional[str] = None,
) -> None:
    """Log a state-machine transition at INFO level.

    Format: ``State transition: <from> -> <to> [(task_id=<id>)]``

    Traced to PRD **R4** (State Machine).
    """
    msg = f"State transition: {from_state} -> {to_state}"
    if task_id is not None:
        msg += f" (task_id={task_id})"
    logger.info(msg)


def log_task_assignment(
    logger: logging.Logger,
    *,
    task_id: str,
    agent: str,
    title: Optional[str] = None,
) -> None:
    """Log a task assignment at INFO level.

    Format: ``Task assigned: <task_id> -> <agent> [<title>]``

    Traced to PRD **R5** (Task Queue).
    """
    msg = f"Task assigned: {task_id} -> {agent}"
    if title is not None:
        msg += f" [{title}]"
    logger.info(msg)


def log_nats_publish(
    logger: logging.Logger,
    *,
    subject: str,
    message_type: Optional[str] = None,
) -> None:
    """Log a NATS publish event at INFO level.

    Format: ``NATS publish: <subject> [(type=<message_type>)]``

    Traced to PRD **R3** (Communication Flow).
    """
    msg = f"NATS publish: {subject}"
    if message_type is not None:
        msg += f" (type={message_type})"
    logger.info(msg)


def log_nats_subscribe(
    logger: logging.Logger,
    *,
    subject: str,
    consumer: Optional[str] = None,
) -> None:
    """Log a NATS subscribe event at INFO level.

    Format: ``NATS subscribe: <subject> [(consumer=<consumer>)]``

    Traced to PRD **R3** (Communication Flow).
    """
    msg = f"NATS subscribe: {subject}"
    if consumer is not None:
        msg += f" (consumer={consumer})"
    logger.info(msg)


def log_nudge_sent(
    logger: logging.Logger,
    *,
    agent: str,
    target: Optional[str] = None,
) -> None:
    """Log that a nudge was sent to an agent at INFO level.

    Format: ``Nudge sent to <agent> [(target=<target>)]``

    Traced to PRD **R6** (tmux Communication).
    """
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
    """Log that a nudge was skipped at WARNING level.

    Format: ``Nudge skipped for <agent>: <reason>``

    Traced to PRD **R6** (tmux Communication).
    """
    msg = f"Nudge skipped for {agent}: {reason}"
    logger.warning(msg)


def log_nudge_escalated(
    logger: logging.Logger,
    *,
    agent: str,
    retries: int,
) -> None:
    """Log that nudge retries were exhausted at WARNING level.

    Format: ``Nudge escalated for <agent>: retries exhausted (<n>). Agent appears stuck.``

    Traced to PRD **R6** (tmux Communication).
    """
    msg = (
        f"Nudge escalated for {agent}: retries exhausted ({retries}). "
        f"Agent appears stuck."
    )
    logger.warning(msg)
