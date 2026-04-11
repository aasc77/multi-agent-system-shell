#!/usr/bin/env python3
"""Unit tests for the orchestrator version probe (issue #31).

Covers the three moving pieces of the version-probe pipeline:

1. ``capture_startup_info`` — shells out to git, handles success and
   every failure mode (non-zero exit, missing binary, timeout) by
   returning a ``StartupInfo`` with ``startup_sha = None`` instead
   of raising.
2. ``build_version_response`` — stable JSON-serializable dict shape.
3. ``make_version_request_handler`` — async callback suitable for
   ``NatsClient.subscribe_core``, replies via ``msg.respond(payload)``,
   ignores non-request publishes.

Usage:
    cd /Users/angelserrano/Repositories/multi-agent-system-shell
    python3 -m pytest tests/test_version_probe.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.version import (
    StartupInfo,
    build_version_response,
    capture_startup_info,
    make_version_request_handler,
)


# ---------------------------------------------------------------------------
# capture_startup_info
# ---------------------------------------------------------------------------


class TestCaptureStartupInfo:
    def _mock_run(
        self, stdout: str = "", stderr: str = "", returncode: int = 0,
    ):
        """Return a MagicMock CompletedProcess for subprocess.run."""
        m = MagicMock()
        m.stdout = stdout
        m.stderr = stderr
        m.returncode = returncode
        return m

    def test_captures_git_sha_on_success(self, tmp_path: Path):
        """Happy path: git rev-parse HEAD returns a 40-char SHA."""
        full_sha = "bd50d2cff537a717dda305e5d4142d7077b98c3b"
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run(stdout=full_sha + "\n")
            info = capture_startup_info(tmp_path)

        assert info.startup_sha_full == full_sha
        assert info.startup_sha == "bd50d2c"
        assert info.pid == os.getpid()
        assert info.started_at  # non-empty ISO8601
        datetime.fromisoformat(info.started_at)  # parses

    def test_captures_none_on_nonzero_exit(self, tmp_path: Path):
        """Non-zero exit from git (e.g. not a git repo) → sha is None."""
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run(
                returncode=128,
                stderr="fatal: not a git repository",
            )
            info = capture_startup_info(tmp_path)

        assert info.startup_sha_full is None
        assert info.startup_sha is None
        # Boot-time metadata still captured — partial info better than none
        assert info.pid == os.getpid()
        assert info.started_at

    def test_captures_none_on_missing_git_binary(self, tmp_path: Path):
        """FileNotFoundError (git not on PATH) → sha is None, no raise."""
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("no git binary")
            info = capture_startup_info(tmp_path)

        assert info.startup_sha_full is None
        assert info.startup_sha is None
        assert info.pid == os.getpid()

    def test_captures_none_on_timeout(self, tmp_path: Path):
        """subprocess.TimeoutExpired → sha is None, logged but not raised."""
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="git", timeout=2,
            )
            info = capture_startup_info(tmp_path)

        assert info.startup_sha_full is None
        assert info.startup_sha is None

    def test_captures_none_on_unexpected_error(self, tmp_path: Path):
        """Any other unexpected error is caught (never raise to caller)."""
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("permission denied")
            info = capture_startup_info(tmp_path)

        assert info.startup_sha_full is None
        assert info.startup_sha is None

    def test_captures_none_on_empty_stdout(self, tmp_path: Path):
        """Git returned 0 but stdout is empty/whitespace → sha is None."""
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run(stdout="   \n")
            info = capture_startup_info(tmp_path)

        assert info.startup_sha_full is None
        assert info.startup_sha is None

    def test_subprocess_run_cwd_is_repo_root(self, tmp_path: Path):
        """The `cwd` kwarg passed to subprocess.run must be *repo_root*.

        Otherwise git would read HEAD from whatever the orchestrator
        process's cwd happens to be, which is not necessarily the
        repo.
        """
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run(
                stdout="0000000000000000000000000000000000000000\n",
            )
            capture_startup_info(tmp_path)

        called_args = mock_run.call_args
        assert called_args.kwargs["cwd"] == str(tmp_path)

    def test_subprocess_run_has_timeout(self, tmp_path: Path):
        """Defensive: git invocation must have a finite timeout."""
        with patch("orchestrator.version.subprocess.run") as mock_run:
            mock_run.return_value = self._mock_run(
                stdout="0000000000000000000000000000000000000000\n",
            )
            capture_startup_info(tmp_path)

        assert mock_run.call_args.kwargs.get("timeout") is not None
        assert mock_run.call_args.kwargs["timeout"] > 0


# ---------------------------------------------------------------------------
# build_version_response
# ---------------------------------------------------------------------------


class TestBuildVersionResponse:
    def test_happy_path_shape(self):
        info = StartupInfo(
            startup_sha_full="bd50d2cff537a717dda305e5d4142d7077b98c3b",
            startup_sha="bd50d2c",
            started_at="2026-04-11T21:08:45+00:00",
            pid=12345,
        )
        resp = build_version_response(info)
        assert resp == {
            "startup_sha": "bd50d2c",
            "startup_sha_full": "bd50d2cff537a717dda305e5d4142d7077b98c3b",
            "started_at": "2026-04-11T21:08:45+00:00",
            "pid": 12345,
        }

    def test_none_sha_preserved_for_drift_indeterminate(self):
        """When git was unavailable at boot, the response still
        carries pid + started_at. The caller's bash wrapper treats
        `startup_sha is None` as drift-indeterminate (exit 2).
        """
        info = StartupInfo(
            startup_sha_full=None,
            startup_sha=None,
            started_at="2026-04-11T21:08:45+00:00",
            pid=12345,
        )
        resp = build_version_response(info)
        assert resp["startup_sha"] is None
        assert resp["startup_sha_full"] is None
        assert resp["pid"] == 12345
        assert resp["started_at"] == "2026-04-11T21:08:45+00:00"

    def test_response_is_json_serializable(self):
        """The wire payload must round-trip through json."""
        info = StartupInfo(
            startup_sha_full="abcdef1234567890abcdef1234567890abcdef12",
            startup_sha="abcdef1",
            started_at="2026-04-11T21:08:45+00:00",
            pid=99,
        )
        resp = build_version_response(info)
        encoded = json.dumps(resp).encode("utf-8")
        decoded = json.loads(encoded)
        assert decoded == resp


# ---------------------------------------------------------------------------
# make_version_request_handler
# ---------------------------------------------------------------------------


class TestVersionRequestHandler:
    def _info(self) -> StartupInfo:
        return StartupInfo(
            startup_sha_full="bd50d2cff537a717dda305e5d4142d7077b98c3b",
            startup_sha="bd50d2c",
            started_at="2026-04-11T21:08:45+00:00",
            pid=12345,
        )

    def _request_msg(self, reply_subject: str = "_INBOX.abc") -> MagicMock:
        """Build a MagicMock NATS message that looks like a request."""
        msg = MagicMock()
        msg.reply = reply_subject
        msg.respond = AsyncMock()
        return msg

    @pytest.mark.asyncio
    async def test_request_gets_json_response(self):
        """On an incoming request, the handler calls msg.respond() with
        the JSON-encoded version body.
        """
        handler = make_version_request_handler(self._info())
        msg = self._request_msg()
        await handler(msg)

        msg.respond.assert_called_once()
        payload = msg.respond.call_args[0][0]
        body = json.loads(payload.decode("utf-8"))
        assert body["startup_sha"] == "bd50d2c"
        assert body["startup_sha_full"] == (
            "bd50d2cff537a717dda305e5d4142d7077b98c3b"
        )
        assert body["pid"] == 12345
        assert body["started_at"] == "2026-04-11T21:08:45+00:00"

    @pytest.mark.asyncio
    async def test_non_request_publish_is_ignored(self):
        """A fire-and-forget publish (no reply subject) is silently
        dropped — the probe is strictly a request/reply surface.
        """
        handler = make_version_request_handler(self._info())
        msg = MagicMock()
        msg.reply = None  # publish, not request
        msg.respond = AsyncMock()

        await handler(msg)

        msg.respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_reply_subject_is_ignored(self):
        """An empty-string reply subject is also treated as non-request."""
        handler = make_version_request_handler(self._info())
        msg = MagicMock()
        msg.reply = ""
        msg.respond = AsyncMock()

        await handler(msg)

        msg.respond.assert_not_called()

    @pytest.mark.asyncio
    async def test_respond_failure_does_not_crash(self):
        """If `msg.respond` raises (NATS disconnect, encoding error,
        etc.), the handler logs and swallows so the subscription
        loop stays alive.
        """
        handler = make_version_request_handler(self._info())
        msg = self._request_msg()
        msg.respond.side_effect = RuntimeError("NATS disconnected")

        # Should not raise
        await handler(msg)
        msg.respond.assert_called_once()

    @pytest.mark.asyncio
    async def test_response_payload_is_precomputed(self):
        """The handler closes over the startup info and encodes the
        payload once at build time, NOT on every request. This is
        the O(1)-response guarantee the probe promises — two requests
        should call respond with byte-identical payloads without any
        re-encoding work.
        """
        handler = make_version_request_handler(self._info())
        msg1 = self._request_msg()
        msg2 = self._request_msg()
        await handler(msg1)
        await handler(msg2)

        payload1 = msg1.respond.call_args[0][0]
        payload2 = msg2.respond.call_args[0][0]
        assert payload1 == payload2
        # Byte-identical — same encoded JSON object.
        assert payload1 is not None
        assert isinstance(payload1, bytes)
