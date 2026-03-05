"""
tmux Communication Module -- orchestrator/tmux_comm.py

Handles tmux send-keys communication between the orchestrator and agent panes.

Requirements traced to PRD:
  - R6: tmux Communication (Orchestrator -> Agents)
  - R7: Interactive Orchestrator Console (msg command safe-nudge check)
"""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

# Processes considered "busy" -- nudge/msg must be skipped
BUSY_PROCESSES = frozenset({
    "node", "python", "python3", "git", "npm", "pytest", "make",
})


class TmuxCommError(Exception):
    """Custom exception for tmux communication errors."""
    pass


class TmuxComm:
    """Manage tmux send-keys communication with agent panes.

    Args:
        config: Dict with 'tmux' and 'agents' keys.
    """

    def __init__(self, config: dict) -> None:
        if "tmux" not in config:
            raise TmuxCommError("Missing 'tmux' key in config")
        if "agents" not in config:
            raise TmuxCommError("Missing 'agents' key in config")
        if not config["agents"]:
            raise ValueError("'agents' config must not be empty")

        self._tmux_config = config["tmux"]
        self._session_name = self._tmux_config["session_name"]
        self._nudge_prompt = self._tmux_config["nudge_prompt"]
        self._cooldown_seconds = self._tmux_config["nudge_cooldown_seconds"]
        self._max_nudge_retries = self._tmux_config["max_nudge_retries"]

        # Build pane mapping: agent_name -> 0-based pane index (config order)
        self._pane_mapping: dict[str, int] = {
            name: idx for idx, name in enumerate(config["agents"])
        }

        # Per-agent tracking
        self._last_nudge_time: dict[str, float] = {}
        self._consecutive_skips: dict[str, int] = {}
        self._escalated: dict[str, bool] = {}

        # Escalation callback
        self._flag_human_callback = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_pane_mapping(self) -> dict[str, int]:
        """Return dict mapping agent name to 0-based pane index."""
        return dict(self._pane_mapping)

    def get_target(self, agent: str) -> str:
        """Return canonical tmux target: <session_name>:agents.<pane_index>."""
        if agent not in self._pane_mapping:
            raise TmuxCommError(f"Unknown agent: {agent}")
        return f"{self._session_name}:agents.{self._pane_mapping[agent]}"

    def send_keys(self, agent: str, text: str) -> None:
        """Send text to agent pane via tmux send-keys with Enter."""
        target = self.get_target(agent)
        subprocess.run(
            ["tmux", "send-keys", "-t", target, text, "Enter"],
            capture_output=True, text=True,
        )

    def nudge(self, agent: str) -> bool:
        """Nudge an agent pane with the configured nudge prompt.

        Returns True if the nudge was sent, False if skipped.
        """
        target = self.get_target(agent)

        # Check cooldown
        now = time.time()
        last = self._last_nudge_time.get(agent, 0)
        if last > 0 and (now - last) < self._cooldown_seconds:
            return False

        # Check foreground process (safe nudging)
        fg = self._get_foreground_process(target)
        if fg is None or fg in BUSY_PROCESSES:
            # Increment consecutive skip counter
            self._consecutive_skips[agent] = self._consecutive_skips.get(agent, 0) + 1
            skips = self._consecutive_skips[agent]

            # Escalation check
            if skips >= self._max_nudge_retries and not self._escalated.get(agent, False):
                self._escalated[agent] = True
                logger.warning(
                    "Agent %s appears stuck -- foreground process never returned to claude", agent
                )
                if self._flag_human_callback:
                    self._flag_human_callback(agent)

            return False

        # Safe to nudge
        self.send_keys(agent, self._nudge_prompt)
        self._last_nudge_time[agent] = time.time()
        self._consecutive_skips[agent] = 0
        self._escalated[agent] = False
        return True

    def send_msg(self, agent: str, text: str) -> bool:
        """Send a custom message to an agent pane (msg command).

        Performs the same safe-nudge check. Does NOT update nudge cooldown.
        Returns True if sent, False if agent is busy.
        """
        target = self.get_target(agent)

        fg = self._get_foreground_process(target)
        if fg is None or fg in BUSY_PROCESSES:
            logger.warning(
                "Agent %s is busy -- message not sent (foreground: %s)", agent, fg
            )
            return False

        subprocess.run(
            ["tmux", "send-keys", "-t", target, text, "Enter"],
            capture_output=True, text=True,
        )
        return True

    def get_consecutive_skips(self, agent: str) -> int:
        """Return the number of consecutive skipped nudges for an agent."""
        return self._consecutive_skips.get(agent, 0)

    def set_flag_human_callback(self, fn) -> None:
        """Register a callback for escalation when an agent appears stuck."""
        self._flag_human_callback = fn

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_foreground_process(self, target: str) -> str | None:
        """Query tmux for the foreground process in a pane.

        Returns the process name (stripped) or None on failure.
        """
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
