"""Orchestrator version probe (issue #31).

Captures the git SHA of the running process at boot time and exposes
it over a NATS request/reply subject so external callers can detect
stale-binary drift — i.e. the case where the orchestrator process is
still running code from an older commit while the on-disk repo has
newer code, and nothing surfaces the mismatch.

Motivation: the #28 QA smoke test arc wasted two probe runs because
the orchestrator in the tmux pane was launched from a pre-commit
revision, but neither the pane banner nor the logs surfaced that fact.
Operators debugging an \"idle agent\" spent ~15 minutes chasing
ghosts before noticing the orchestrator was a stale binary.

Usage from the orchestrator side::

    from orchestrator.version import (
        capture_startup_info, make_version_request_handler,
    )
    info = capture_startup_info(root_dir)
    handler = make_version_request_handler(info)
    await nats_client.subscribe_core("system.orchestrator.version", handler)

Usage from the caller side::

    # From a shell operator tool (see scripts/check-orchestrator-version.sh)
    nats req system.orchestrator.version '' --timeout 2s

    # Response is a JSON dict with startup_sha, startup_sha_full,
    # started_at, pid. The operator tool compares startup_sha against
    # `git rev-parse --short HEAD` in the repo root and exits:
    #   0 = in sync
    #   1 = drift detected
    #   2 = indeterminate (unknown SHA, nats timeout, git missing)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# Subprocess timeout for `git rev-parse HEAD`. Git should return
# essentially instantly on a healthy checkout; two seconds is a
# defensive upper bound for pathological filesystem conditions.
_GIT_REV_PARSE_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class StartupInfo:
    """Snapshot of the orchestrator process at boot.

    All fields are captured once at boot and cached for the process
    lifetime. Responses from the version-probe handler are O(1) — no
    subprocess invocations, no filesystem reads per request.

    Attributes:
        startup_sha_full: 40-char git SHA of HEAD in the repo root at
            boot time, or ``None`` if git was unavailable (detached
            HEAD refusing, shallow clone, non-git checkout, git binary
            missing). A ``None`` value signals drift-indeterminate to
            the caller, not drift-confirmed.
        startup_sha: 7-char short form of ``startup_sha_full``, or
            ``None`` if the full SHA is unavailable.
        started_at: Boot time as ISO8601 UTC with timezone offset.
        pid: OS process id of the running orchestrator.
    """

    startup_sha_full: Optional[str]
    startup_sha: Optional[str]
    started_at: str
    pid: int


def capture_startup_info(repo_root: Path | str) -> StartupInfo:
    """Capture the git SHA, boot time, and pid at orchestrator startup.

    Shells out to ``git rev-parse HEAD`` against *repo_root* exactly
    once. Callers should invoke this at the module or function scope
    where the orchestrator process begins and cache the result for
    the process lifetime — the returned :class:`StartupInfo` is
    immutable.

    If the git invocation fails for any reason (non-git checkout,
    missing binary, permission error, timeout), ``startup_sha_full``
    and ``startup_sha`` are set to ``None`` rather than raising. The
    orchestrator still exposes the version probe, and the caller
    interprets ``None`` as drift-indeterminate.
    """
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pid = os.getpid()

    sha_full: Optional[str] = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_REV_PARSE_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                sha_full = sha
        else:
            logger.debug(
                "git rev-parse HEAD returned %d: %s",
                result.returncode, result.stderr.strip(),
            )
    except FileNotFoundError:
        logger.debug("git binary not found — startup_sha will be None")
    except subprocess.TimeoutExpired:
        logger.warning(
            "git rev-parse HEAD timed out after %ds — "
            "startup_sha will be None",
            _GIT_REV_PARSE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception(
            "unexpected error capturing git SHA — startup_sha will be None",
        )

    sha_short = sha_full[:7] if sha_full else None

    return StartupInfo(
        startup_sha_full=sha_full,
        startup_sha=sha_short,
        started_at=started_at,
        pid=pid,
    )


def build_version_response(info: StartupInfo) -> dict[str, Any]:
    """Return the JSON-serializable response body for a version request.

    Shape is stable and documented so the bash check tool
    (``scripts/check-orchestrator-version.sh``) can parse it with
    ``jq``. Future fields can be added without breaking callers, but
    existing fields should not be renamed or removed without a
    deprecation cycle.

    Schema stability contract (DO NOT RENAME OR REMOVE without a
    deprecation cycle): the four fields below are consumed by the
    bash wrapper with ``jq -r '.<field>'``. Adding a new field is
    safe. Renaming or removing any of ``startup_sha``,
    ``startup_sha_full``, ``started_at``, ``pid`` will break
    operator tooling silently.
    """
    return {
        "startup_sha": info.startup_sha,
        "startup_sha_full": info.startup_sha_full,
        "started_at": info.started_at,
        "pid": info.pid,
    }


# Callback type — same signature the mas-bridge and NatsClient
# expect for subscribe_core callbacks.
VersionRequestHandler = Callable[[Any], Coroutine[Any, Any, None]]


def make_version_request_handler(info: StartupInfo) -> VersionRequestHandler:
    """Build an async callback that responds to a version request.

    The returned handler is suitable for
    :meth:`NatsClient.subscribe_core`. On each incoming message, it
    encodes :func:`build_version_response` as JSON and calls
    ``msg.respond(payload)`` to reply on the request-scoped inbox.
    Messages that arrive without a reply subject (a publish, not a
    request) are silently dropped — the probe is strictly a
    request/reply surface, never a pub/sub broadcast.

    The handler closes over the boot-time :class:`StartupInfo`, so
    responses are O(1) with no subprocess or filesystem I/O.
    """
    body = build_version_response(info)
    payload = json.dumps(body).encode("utf-8")

    async def _handle(msg: Any) -> None:
        # Drop non-request publishes. Core-NATS messages carry a
        # `reply` attribute set to the inbox subject when the sender
        # used `nc.request(...)`. When it's empty/None the message
        # was a fire-and-forget publish and there is no one to
        # respond to.
        reply_to = getattr(msg, "reply", None)
        if not reply_to:
            logger.debug(
                "version probe: ignoring non-request publish on "
                "system.orchestrator.version",
            )
            return
        try:
            await msg.respond(payload)
        except Exception:
            logger.exception(
                "version probe: failed to respond to request",
            )

    return _handle
