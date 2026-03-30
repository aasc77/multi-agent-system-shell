"""
tmux Communication Module -- orchestrator/tmux_comm.py

Handles tmux send-keys communication between the orchestrator and agent panes.
Provides safe nudging (checks foreground process before sending), per-agent
cooldown tracking, consecutive-skip escalation, and the ``msg`` command.

Usage::

    comm = TmuxComm(config)
    comm.set_flag_human_callback(my_escalation_handler)

    # Nudge an agent (respects cooldown + safe-nudge check)
    sent = comm.nudge("qa")

    # Send a custom message (same safe-nudge check, no cooldown update)
    sent = comm.send_msg("dev", "please fix the tests")

Requirements traced to PRD:
  - R6: tmux Communication (Orchestrator -> Agents)
  - R7: Interactive Orchestrator Console (msg command safe-nudge check)
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Config top-level keys
_CFG_TMUX = "tmux"
_CFG_AGENTS = "agents"

# tmux config keys
_CFG_SESSION_NAME = "session_name"
_CFG_NUDGE_PROMPT = "nudge_prompt"
_CFG_COOLDOWN_SECONDS = "nudge_cooldown_seconds"
_CFG_MAX_NUDGE_RETRIES = "max_nudge_retries"

# tmux window name for agent panes (matches start.sh convention)
_AGENTS_WINDOW = "agents"

# Processes considered "busy" -- nudge/msg must be skipped when these are
# in the foreground of an agent's tmux pane.
BUSY_PROCESSES: frozenset[str] = frozenset({
    "node", "python", "python3", "git", "npm", "pytest", "make",
})

# Runtime type for Claude Code agents (skip busy-check for these)
_RUNTIME_CLAUDE_CODE = "claude_code"

# Type alias for the escalation callback: receives the stuck agent's name.
FlagHumanCallback = Callable[[str], None]


class TmuxCommError(Exception):
    """Custom exception for tmux communication errors."""


class TmuxComm:
    """Manage tmux send-keys communication with agent panes.

    Builds a pane-index mapping from config order and provides methods to
    send text, nudge agents safely, and escalate when agents appear stuck.

    Args:
        config: Dict with ``'tmux'`` and ``'agents'`` keys.  The ``tmux``
            section must include ``session_name``, ``nudge_prompt``,
            ``nudge_cooldown_seconds``, and ``max_nudge_retries``.  The
            ``agents`` section is an ordered dict of agent definitions.
    """

    def __init__(self, config: dict) -> None:
        self._validate_config(config)

        tmux_cfg = config[_CFG_TMUX]
        self._session_name: str = tmux_cfg[_CFG_SESSION_NAME]
        self._nudge_prompt: str = tmux_cfg[_CFG_NUDGE_PROMPT]
        self._cooldown_seconds: int = tmux_cfg[_CFG_COOLDOWN_SECONDS]
        self._max_nudge_retries: int = tmux_cfg[_CFG_MAX_NUDGE_RETRIES]

        # Build pane mapping: agent_name -> 0-based pane index (config order).
        # Skip agents with role=monitor -- they launch in the control window,
        # not the agents window (mirrors start.sh behaviour).
        pane_agents = [
            name for name, cfg in config[_CFG_AGENTS].items()
            if not (isinstance(cfg, dict) and cfg.get("role") == "monitor")
        ]
        self._pane_mapping: dict[str, int] = {
            name: idx for idx, name in enumerate(pane_agents)
        }

        # Track which agents are claude_code (skip busy-check for them)
        self._claude_code_agents: set[str] = {
            name for name, cfg in config[_CFG_AGENTS].items()
            if isinstance(cfg, dict) and cfg.get("runtime") == _RUNTIME_CLAUDE_CODE
        }

        # Per-agent tracking
        self._last_nudge_time: dict[str, float] = {}
        self._consecutive_skips: dict[str, int] = {}
        self._escalated: dict[str, bool] = {}

        # Escalation callback
        self._flag_human_callback: Optional[FlagHumanCallback] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_pane_mapping(self) -> dict[str, int]:
        """Return a copy of the agent-name-to-pane-index mapping."""
        return dict(self._pane_mapping)

    def get_target(self, agent: str) -> str:
        """Return canonical tmux target ``<session>:agents.<index>``.

        Raises:
            TmuxCommError: If *agent* is not in the pane mapping.
        """
        if agent not in self._pane_mapping:
            raise TmuxCommError(f"Unknown agent: {agent}")
        return f"{self._session_name}:{_AGENTS_WINDOW}.{self._pane_mapping[agent]}"

    def send_keys(self, agent: str, text: str) -> None:
        """Send *text* to an agent's tmux pane via ``tmux send-keys`` with Enter."""
        target = self.get_target(agent)
        self._tmux_send_keys(target, text)

    def capture_pane(self, agent: str, lines: int = 20) -> str | None:
        """Capture the last *lines* of an agent's tmux pane.

        Returns:
            The captured text, or ``None`` on failure.
        """
        target = self.get_target(agent)
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def is_agent_idle(self, agent: str) -> bool:
        """Return ``True`` if the agent's pane appears idle (at prompt).

        Claude Code agents show a ``❯`` prompt when idle.
        """
        pane_text = self.capture_pane(agent, lines=5)
        if pane_text is None:
            return False
        # Check if the last non-empty line ends with the Claude Code prompt
        lines = [l for l in pane_text.strip().splitlines() if l.strip()]
        if not lines:
            return False
        last = lines[-1].strip()
        return last == "❯" or last.endswith("❯")

    def nudge(self, agent: str, force: bool = False) -> bool:
        """Nudge an agent pane with the configured nudge prompt.

        Respects per-agent cooldown and safe-nudge checks.  Tracks
        consecutive skips and escalates after ``max_nudge_retries``.
        Claude Code agents skip the busy-check (they queue input).

        Args:
            agent: Agent name.
            force: If ``True``, skip cooldown check (used for task assignments).

        Returns:
            ``True`` if the nudge was sent, ``False`` if skipped.
        """
        target = self.get_target(agent)

        if not force and self._is_within_cooldown(agent):
            return False

        # Claude Code agents handle input queuing -- skip busy check
        if agent not in self._claude_code_agents:
            if self._is_agent_busy(target):
                self._record_skip(agent)
                return False

        # Safe to nudge
        self.send_keys(agent, self._nudge_prompt)
        self._last_nudge_time[agent] = time.time()
        self._reset_skip_tracking(agent)
        return True

    def send_msg(self, agent: str, text: str) -> bool:
        """Send a custom message to an agent pane (``msg`` console command).

        Performs the same safe-nudge check as :meth:`nudge` but does
        **not** update the nudge cooldown timestamp.

        Returns:
            ``True`` if the message was sent, ``False`` if the agent is busy.
        """
        target = self.get_target(agent)

        if self._is_agent_busy(target):
            fg = self._get_foreground_process(target)
            logger.warning(
                "Agent %s is busy -- message not sent (foreground: %s)", agent, fg
            )
            return False

        self.send_keys(agent, text)
        return True

    def get_consecutive_skips(self, agent: str) -> int:
        """Return the number of consecutive skipped nudges for *agent*."""
        return self._consecutive_skips.get(agent, 0)

    def set_flag_human_callback(self, fn: FlagHumanCallback) -> None:
        """Register a callback invoked when an agent appears stuck.

        The callback receives the stuck agent's name as its sole argument.
        """
        self._flag_human_callback = fn

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_config(config: dict) -> None:
        """Validate required top-level config keys on construction."""
        if _CFG_TMUX not in config:
            raise TmuxCommError(f"Missing '{_CFG_TMUX}' key in config")
        if _CFG_AGENTS not in config:
            raise TmuxCommError(f"Missing '{_CFG_AGENTS}' key in config")
        if not config[_CFG_AGENTS]:
            raise ValueError(f"'{_CFG_AGENTS}' config must not be empty")

    def _is_within_cooldown(self, agent: str) -> bool:
        """Return ``True`` if the agent's nudge cooldown has not yet elapsed."""
        last = self._last_nudge_time.get(agent, 0)
        return last > 0 and (time.time() - last) < self._cooldown_seconds

    def _is_agent_busy(self, target: str) -> bool:
        """Return ``True`` if the agent's foreground process is busy or unknown."""
        fg = self._get_foreground_process(target)
        return fg is None or fg in BUSY_PROCESSES

    def _record_skip(self, agent: str) -> None:
        """Increment the consecutive-skip counter and escalate if needed."""
        self._consecutive_skips[agent] = self._consecutive_skips.get(agent, 0) + 1
        skips = self._consecutive_skips[agent]

        if skips >= self._max_nudge_retries and not self._escalated.get(agent, False):
            self._escalated[agent] = True
            logger.warning(
                "Agent %s appears stuck -- foreground process never returned to claude",
                agent,
            )
            if self._flag_human_callback:
                self._flag_human_callback(agent)

    def _reset_skip_tracking(self, agent: str) -> None:
        """Reset consecutive-skip counter and escalation flag for *agent*."""
        self._consecutive_skips[agent] = 0
        self._escalated[agent] = False

    def _get_foreground_process(self, target: str) -> str | None:
        """Query tmux for the foreground process in a pane.

        Returns:
            The process name (stripped) or ``None`` on failure.
        """
        result = subprocess.run(
            [
                "tmux", "display-message", "-p", "-t", target,
                "#{pane_current_command}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def _tmux_send_keys(target: str, text: str) -> None:
        """Low-level wrapper: execute ``tmux send-keys`` with Enter.

        Sends text and Enter separately to work with Claude Code's TUI
        input handler, which may not process them correctly when sent
        atomically in a single send-keys invocation.
        """
        # Send text first
        subprocess.run(
            ["tmux", "send-keys", "-t", target, text],
            capture_output=True,
            text=True,
        )
        # Brief delay for the TUI to register the text
        time.sleep(0.3)
        # Send Enter separately
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            capture_output=True,
            text=True,
        )
