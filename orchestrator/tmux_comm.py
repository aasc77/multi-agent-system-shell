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

import enum
import hashlib
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _tmux_cmd() -> list[str]:
    """Return the ``tmux`` argv prefix, honoring ``MAS_TMUX_SOCKET``.

    When the env var is set, every ``subprocess.run`` dispatched from
    this module prepends ``-L <socket>``, routing ops to a dedicated
    tmux server. Used by pytest (see ``tests/conftest.py``) to isolate
    test tmux operations from default-socket user sessions. See #46.
    """
    socket = os.environ.get("MAS_TMUX_SOCKET")
    if socket:
        return ["tmux", "-L", socket]
    return ["tmux"]


# ---------------------------------------------------------------------------
# Agent pane state (issue #42)
# ---------------------------------------------------------------------------
#
# Replaces the pre-#42 negative-signal `is_agent_idle()` check (which
# inferred "working" from the absence of the `❯` prompt character) with
# a three-state positive-signal model derived from pane-content hashing.
#
# How the states are computed (see `TmuxComm.get_pane_state()`):
#   - WORKING: pane content hash changed since last cycle. Active work
#     continuously ticks time counters, token streams, and the `Running…`
#     animation — so a changing hash is direct positive proof of life.
#   - IDLE: pane unchanged since last cycle AND the `❯` idle prompt is
#     visible. The agent has parked at a prompt and is waiting for a
#     nudge. Preserves the existing idle-watchdog behavior.
#   - UNKNOWN: pane unchanged since last cycle AND no `❯` visible,
#     observed for at least `_STALE_DEBOUNCE_CYCLES` consecutive cycles.
#     New detection capability: catches crashed processes, bash prompts,
#     confirmation dialogs, and other failure modes that the pre-#42
#     check silently treated as "probably working". Debouncing avoids
#     one-shot false positives from a capture-timing race.
#
# Rejected alternatives (see #42 architectural discussion):
#   - `esc to interrupt` substring match: the string does not exist in
#     claude-code v2.1.x UI chrome. Empirically verified across 5 agent
#     panes — zero hits in live UI, all prose false positives. Also
#     vulnerable to the same self-reference trap as #28's `-32603`.
#   - Ranked-signal regex on gerund/Running patterns: needs line-start
#     anchor + hub exclusion + version parity check, all of which
#     pane-diff avoids by construction.
#
# See also:
#   - `_AGENT_STATE_CAPTURE_LINES` — hash-window line count
#   - `_STALE_DEBOUNCE_CYCLES` — min consecutive stale cycles to flag
#   - `is_agent_idle` — thin backwards-compat wrapper on `get_pane_state`


_AGENT_STATE_CAPTURE_LINES = 30
_STALE_DEBOUNCE_CYCLES = 2

# #66: default source tag when a caller doesn't supply one. Real
# orchestrator code paths always pass their own tag (delivery,
# watchdog, announcer, taskqueue, console); "unknown" is a
# backwards-compat fallback for tests and external integrations.
_NUDGE_SOURCE_UNKNOWN = "unknown"


def _format_nudge_prefix(source: str) -> str:
    """Return the ``[<ISO-8601 UTC> <source>] `` prefix prepended
    to every nudge / send-msg payload (#66).

    Format: ``[YYYY-MM-DDTHH:MM:SS.mmmZ <source>]`` with a trailing
    space so the caller's text appends cleanly. Millisecond
    resolution keeps correlation tight enough to distinguish
    retries that land back-to-back. UTC ``Z`` suffix so operators
    grepping logs don't have to think about local timezones.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return f"[{ts} {source}] "


class AgentPaneState(enum.Enum):
    """Positive-signal three-state model for agent pane liveness.

    See module-level comment for the full rationale. Values are
    string-valued so they serialize cleanly into directive payloads
    published by the watchdog.
    """

    WORKING = "working"
    IDLE = "idle"
    UNKNOWN = "unknown"
    #: Pane capture subprocess failed for `_STALE_DEBOUNCE_CYCLES`
    #: consecutive cycles. Operators troubleshoot this differently
    #: from UNKNOWN (tmux/session health vs agent health), so it
    #: gets its own enum value and directive subtype.
    CAPTURE_FAILED = "capture_failed"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Config top-level keys
_CFG_TMUX = "tmux"
_CFG_AGENTS = "agents"

# tmux config keys
_CFG_SESSION_NAME = "session_name"
_CFG_NUDGE_PROMPT = "nudge_prompt"
_CFG_MONITOR_NUDGE_PROMPT = "monitor_nudge_prompt"
_CFG_COOLDOWN_SECONDS = "nudge_cooldown_seconds"
_CFG_MAX_NUDGE_RETRIES = "max_nudge_retries"
_CFG_NUDGE_SEND_RETRIES = "nudge_send_retries"

# Default retry settings for send-keys delivery verification
_DEFAULT_SEND_RETRIES = 3
_SEND_KEYS_DELAY = 0.3       # seconds between text and Enter
_VERIFY_DELAY = 0.5           # seconds before verifying delivery
_RETRY_DELAY = 1.0            # seconds between retry attempts

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
        self._monitor_nudge_prompt: str = tmux_cfg.get(
            _CFG_MONITOR_NUDGE_PROMPT,
            "You have new messages. Use check_messages with your role.",
        )
        self._cooldown_seconds: int = tmux_cfg[_CFG_COOLDOWN_SECONDS]
        self._max_nudge_retries: int = tmux_cfg[_CFG_MAX_NUDGE_RETRIES]
        self._send_retries: int = tmux_cfg.get(
            _CFG_NUDGE_SEND_RETRIES, _DEFAULT_SEND_RETRIES,
        )

        # Build pane mapping: agent_name -> 0-based pane index.
        #
        # Skip agents with role=monitor -- they launch in the control
        # window, not the agents window (mirrors start.sh behaviour).
        #
        # Issue #51 / #68: we prefer @label-based resolution over
        # config-order enumeration so the pane mapping survives layout
        # rearrangement (continuum restore, manual splits, sibling
        # grouped sessions with different child pane ordering, …).
        # #52 only wired @label resolution for monitors — regular
        # agents kept using config-order, which silently misrouted
        # every NUDGE once the tmux layout drifted from config order.
        # This builds both a label map and the config-order fallback,
        # then picks per-agent with precedence:
        #   label_to_index[agent_label]   (preferred)
        #     > label_to_index[agent_name]  (name-as-label fallback)
        #     > config_order_index           (no live tmux / tests)
        pane_agents = [
            (name, cfg if isinstance(cfg, dict) else {})
            for name, cfg in config[_CFG_AGENTS].items()
            if not (isinstance(cfg, dict) and cfg.get("role") == "monitor")
        ]
        label_to_index = self._scan_agents_pane_labels()
        self._pane_mapping: dict[str, int] = {}
        for fallback_idx, (name, agent_cfg) in enumerate(pane_agents):
            configured_label = agent_cfg.get("label", name)
            idx = label_to_index.get(configured_label)
            if idx is None:
                idx = label_to_index.get(name)
            if idx is None:
                idx = fallback_idx
            self._pane_mapping[name] = idx

        # Monitor agents live in the control window.  Rather than hardcode
        # pane indices (which break if the user rearranges the layout),
        # scan the control window for panes whose @label matches the
        # agent's configured label or name.  Fall back to a legacy
        # positional layout (pane 1, pane 2, …) if the tmux query fails
        # (e.g. during unit tests with no live session).
        self._control_pane_mapping: dict[str, str] = {}
        monitors = [
            (name, cfg.get("label", name) if isinstance(cfg, dict) else name)
            for name, cfg in config[_CFG_AGENTS].items()
            if isinstance(cfg, dict) and cfg.get("role") == "monitor"
        ]
        if monitors:
            label_to_index = self._scan_control_pane_labels()
            for fallback_idx, (name, label) in enumerate(monitors, start=1):
                idx = label_to_index.get(label)
                if idx is None:
                    idx = label_to_index.get(name)
                if idx is None:
                    idx = fallback_idx
                self._control_pane_mapping[name] = (
                    f"{self._session_name}:control.{idx}"
                )

        # Track which agents are claude_code (skip busy-check for them)
        self._claude_code_agents: set[str] = {
            name for name, cfg in config[_CFG_AGENTS].items()
            if isinstance(cfg, dict) and cfg.get("runtime") == _RUNTIME_CLAUDE_CODE
        }

        # Per-agent tracking
        self._last_nudge_time: dict[str, float] = {}
        self._consecutive_skips: dict[str, int] = {}
        self._escalated: dict[str, bool] = {}

        # Pane state derivation state (issue #42 pane-diff staleness
        # detection). Keyed by agent name. The watchdog reads these
        # indirectly via `get_pane_state()`; they are write-once-per-
        # cycle from the tmux capture path.
        self._last_pane_hash: dict[str, str] = {}
        self._consecutive_stale_cycles: dict[str, int] = {}
        self._consecutive_capture_failures: dict[str, int] = {}

        # Escalation callback
        self._flag_human_callback: Optional[FlagHumanCallback] = None

    def _scan_control_pane_labels(self) -> dict[str, int]:
        """Scan the control window for pane @labels and return a
        ``{label: pane_index}`` mapping.

        Returns an empty dict on any failure (tmux not running, session
        missing, etc.) so the caller can fall back to a default layout.
        """
        return self._scan_window_pane_labels("control")

    def _scan_agents_pane_labels(self) -> dict[str, int]:
        """Scan the ``agents`` window for pane @labels and return a
        ``{label: pane_index}`` mapping.

        Used by ``__init__`` to resolve regular-agent pane indices by
        @label rather than config order (issue #51 / #68). Returns an
        empty dict on any failure (tmux not running, no session,
        agents window missing, unit-test mode, …) so the caller can
        fall back to config order.
        """
        return self._scan_window_pane_labels(_AGENTS_WINDOW)

    def _scan_window_pane_labels(self, window: str) -> dict[str, int]:
        """Shared helper for ``_scan_{control,agents}_pane_labels``.

        Runs ``tmux list-panes -t <session>:<window> -F '<idx>\\t<@label>'``
        and parses the tab-delimited output. Returns an empty dict on
        any failure so callers can fall back to a legacy positional
        layout.
        """
        try:
            result = subprocess.run(
                [
                    *_tmux_cmd(), "list-panes",
                    "-t", f"{self._session_name}:{window}",
                    "-F", "#{pane_index}\t#{@label}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return {}
        if result.returncode != 0:
            return {}
        mapping: dict[str, int] = {}
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            idx_str, label = line.split("\t", 1)
            label = label.strip()
            if not label:
                continue
            try:
                mapping[label] = int(idx_str)
            except ValueError:
                continue
        return mapping

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_pane_mapping(self) -> dict[str, int]:
        """Return a copy of the agent-name-to-pane-index mapping."""
        return dict(self._pane_mapping)

    def get_target(self, agent: str) -> str:
        """Return canonical tmux target for an agent.

        Regular agents return ``<session>:agents.<index>``.
        Monitor agents (e.g. manager) return their control window target.

        Raises:
            TmuxCommError: If *agent* is not in any pane mapping.
        """
        if agent in self._pane_mapping:
            return f"{self._session_name}:{_AGENTS_WINDOW}.{self._pane_mapping[agent]}"
        if agent in self._control_pane_mapping:
            return self._control_pane_mapping[agent]
        raise TmuxCommError(f"Unknown agent: {agent}")

    def send_keys(
        self,
        agent: str,
        text: str,
        source: str = _NUDGE_SOURCE_UNKNOWN,
    ) -> None:
        """Send *text* to an agent's tmux pane via ``tmux send-keys`` with Enter.

        #66: a trace prefix ``[<ISO-8601 UTC> <source>] `` is
        prepended to *text* before the pane receives it, so the
        rendered line in the pane carries its own origin and
        operators can ``grep`` ``orchestrator.log`` by the same
        timestamp. Each orchestrator call site passes its own
        ``source`` tag (``orch.delivery``, ``orch.watchdog``,
        ``orch.announcer``, ``orch.taskqueue``, ``orch.console``).
        Callers that omit the tag get ``"unknown"`` for
        backwards-compat; no production call site should hit that
        default.

        The composed message (prefix + text) is logged at INFO so
        ``grep <timestamp>`` in the orchestrator log finds the
        exact nudge that fired — the forensics payoff documented
        on #66.
        """
        target = self.get_target(agent)
        composed = _format_nudge_prefix(source) + text
        logger.info(
            "send_keys agent=%s source=%s text=%r",
            agent, source, composed,
        )
        # Claude Code agents: skip verify-retry (it causes false negatives
        # and Escape retries that sabotage delivery). The delivery protocol
        # ACK provides real confirmation.
        skip_verify = agent in self._claude_code_agents
        self._tmux_send_keys(target, composed, skip_verify=skip_verify)

    def capture_pane(self, agent: str, lines: int = 20) -> str | None:
        """Capture the last *lines* of an agent's tmux pane.

        Returns:
            The captured text, or ``None`` on failure.
        """
        target = self.get_target(agent)
        result = subprocess.run(
            [*_tmux_cmd(), "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def _has_idle_prompt(self, pane_text: str) -> bool:
        """Return ``True`` if *pane_text* contains Claude Code's
        ``❯`` idle-prompt marker.

        Pure helper. Shared between :meth:`get_pane_state` (which
        reads the prompt status as part of the 3-state derivation)
        and :meth:`is_agent_idle` (backwards-compat wrapper).
        """
        lines = [l for l in pane_text.strip().splitlines() if l.strip()]
        if not lines:
            return False
        # Match ❯ alone, at the end, or at the start (e.g.
        # "❯ Press up to edit queued messages") -- Claude Code
        # shows the prompt char as a line marker even when hint
        # text follows it.
        return any(
            l.strip() == "❯"
            or l.strip().endswith("❯")
            or l.strip().startswith("❯ ")
            or l.strip().startswith("❯")
            for l in lines
        )

    def get_pane_state(self, agent: str) -> AgentPaneState:
        """Return the current 3-state liveness signal for *agent*.

        Captures the agent's pane, hashes the content, and compares
        against the previous cycle's hash to decide between WORKING
        (hash changed = active work ticking counters), IDLE (unchanged
        + ``❯`` visible), UNKNOWN (unchanged + no ``❯`` for at least
        :data:`_STALE_DEBOUNCE_CYCLES` consecutive cycles), or
        CAPTURE_FAILED (pane capture returned ``None`` for at least
        :data:`_STALE_DEBOUNCE_CYCLES` consecutive cycles).

        Issue #42 positive-signal liveness. See module-level comment
        for the architectural rationale and rejected alternatives.

        Side effect: updates :attr:`_last_pane_hash`,
        :attr:`_consecutive_stale_cycles`, and
        :attr:`_consecutive_capture_failures` in place. The watchdog
        is expected to call this exactly once per check cycle per
        agent; repeat calls within a cycle would double-count the
        debounce counter.
        """
        pane_text = self.capture_pane(
            agent, lines=_AGENT_STATE_CAPTURE_LINES,
        )
        if pane_text is None:
            # Transient capture failure — debounce before flagging.
            # A single failed capture is a test-bench / tmux artifact,
            # not an agent-state signal. Only a sustained inability
            # to read the pane warrants a directive.
            failures = self._consecutive_capture_failures.get(agent, 0) + 1
            self._consecutive_capture_failures[agent] = failures
            if failures >= _STALE_DEBOUNCE_CYCLES:
                return AgentPaneState.CAPTURE_FAILED
            # Benefit of doubt on the first failure — return the
            # least-alarming state so existing idle alerts don't
            # misfire on a transient hiccup. WORKING is a safe
            # default because it suppresses any follow-on alerting
            # paths until the next cycle confirms.
            return AgentPaneState.WORKING

        # Capture succeeded — reset the failure counter.
        if agent in self._consecutive_capture_failures:
            del self._consecutive_capture_failures[agent]

        current_hash = hashlib.sha256(
            pane_text.encode("utf-8", errors="replace"),
        ).hexdigest()
        has_prompt = self._has_idle_prompt(pane_text)
        last_hash = self._last_pane_hash.get(agent)
        self._last_pane_hash[agent] = current_hash

        if last_hash is None:
            # First-ever capture for this agent. No prior hash to
            # compare against → we have no information to flag on.
            # Return WORKING as the least-alarming default; the next
            # cycle will have real signal. Do NOT increment the
            # stale counter on the first capture — debouncing starts
            # on the second cycle onward.
            return AgentPaneState.WORKING

        if current_hash != last_hash:
            # Hash changed → active work. Reset staleness counter.
            if agent in self._consecutive_stale_cycles:
                del self._consecutive_stale_cycles[agent]
            return AgentPaneState.WORKING

        # Unchanged hash.
        if has_prompt:
            # Idle at the prompt — reset staleness counter because
            # the agent is in a known-good state, just waiting for
            # a nudge.
            if agent in self._consecutive_stale_cycles:
                del self._consecutive_stale_cycles[agent]
            return AgentPaneState.IDLE

        # Unchanged + no prompt → starting to look stale. Debounce
        # by requiring N consecutive stale cycles before flagging.
        stale_count = self._consecutive_stale_cycles.get(agent, 0) + 1
        self._consecutive_stale_cycles[agent] = stale_count
        if stale_count >= _STALE_DEBOUNCE_CYCLES:
            return AgentPaneState.UNKNOWN
        return AgentPaneState.WORKING

    def get_stale_cycle_count(self, agent: str) -> int:
        """Return the number of consecutive stale cycles recorded for
        *agent* by :meth:`get_pane_state`.

        Used by the watchdog cycle log (#56) to show how long an
        agent has been stuck. Pure read — does not mutate the
        counter. Returns 0 for agents that have never gone stale or
        that were most recently WORKING / IDLE.
        """
        return self._consecutive_stale_cycles.get(agent, 0)

    def is_agent_idle(self, agent: str) -> bool:
        """Return ``True`` if the agent's pane appears idle (at prompt).

        Pure function — does NOT mutate the pane-state debounce
        machinery owned by :meth:`get_pane_state`. Callers from
        multiple paths and threads (watchdog idle check, delivery
        probe thread pool, legacy code) can invoke this at any
        cadence without polluting the 2-cycle debounce counters
        on UNKNOWN / CAPTURE_FAILED that :meth:`get_pane_state`
        relies on.

        Why this separation matters: an earlier #42 refactor made
        ``is_agent_idle`` a thin wrapper around
        ``get_pane_state``. That looked like a harmless
        simplification but leaked the side effects on
        ``_consecutive_stale_cycles`` and
        ``_consecutive_capture_failures`` into three independent
        call sites at three different cadences:

        1. watchdog ``_check_idle_agents`` → ``_check_single_agent``
           → ``is_agent_idle`` (once per watchdog cycle, 15s)
        2. watchdog ``_check_unknown_agents`` →
           ``_check_single_agent_pane_state`` → ``get_pane_state``
           directly (once per watchdog cycle, 15s — same cycle)
        3. delivery ``_probe_neighbors`` →
           ``asyncio.to_thread(is_agent_idle, name)`` (once per
           delivery probe interval, 10s — separate thread)

        Every call advanced the debounce counters. CAPTURE_FAILED
        fired on the FIRST capture failure instead of the second,
        because the combined advance rate was 2-3× the designed
        rate. A single transient tmux hiccup would produce a
        spurious directive. There was also a thread-safety hazard
        from ``asyncio.to_thread`` mutating the same dicts the
        watchdog asyncio loop was reading.

        The fix (macmini's #43 review finding): revert
        ``is_agent_idle`` to a pure 4-line function that reads a
        10-line capture and checks for the ``❯`` prompt via
        :meth:`_has_idle_prompt`, with NO state mutations.
        :meth:`get_pane_state` remains the single owner of the
        pane-state debounce machinery, invoked exactly once per
        watchdog cycle per agent from ``_check_unknown_agents``.

        Callers that need to distinguish WORKING, UNKNOWN, and
        CAPTURE_FAILED should use :meth:`get_pane_state` directly.
        """
        pane_text = self.capture_pane(agent, lines=10)
        if pane_text is None:
            return False
        return self._has_idle_prompt(pane_text)

    def nudge(
        self,
        agent: str,
        force: bool = False,
        source: str = _NUDGE_SOURCE_UNKNOWN,
    ) -> bool:
        """Nudge an agent pane with the configured nudge prompt.

        Respects per-agent cooldown and safe-nudge checks.  Tracks
        consecutive skips and escalates after ``max_nudge_retries``.
        Claude Code agents skip the busy-check (they queue input).

        #66: the outgoing pane text carries a ``[<ISO-8601 UTC>
        <source>]`` prefix so operators can correlate a pane event
        to the exact orchestrator caller. Each call site supplies
        its own tag; see ``send_keys`` for the taxonomy.

        Args:
            agent: Agent name.
            force: If ``True``, skip cooldown check (used for task assignments).
            source: Origin tag (``orch.delivery`` / ``orch.watchdog``
                / …). Defaults to ``"unknown"`` for backwards-compat;
                real call sites should always pass their own.

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

        # Monitor agents (e.g. manager) are mid-conversation in Claude Code,
        # so they need a conversational nudge, not a bare command.
        prompt = (
            self._monitor_nudge_prompt
            if agent in self._control_pane_mapping
            else self._nudge_prompt
        )
        self.send_keys(agent, prompt, source=source)
        self._last_nudge_time[agent] = time.time()
        self._reset_skip_tracking(agent)
        return True

    def send_msg(
        self,
        agent: str,
        text: str,
        source: str = _NUDGE_SOURCE_UNKNOWN,
    ) -> bool:
        """Send a custom message to an agent pane (``msg`` console command).

        Performs the same safe-nudge check as :meth:`nudge` but does
        **not** update the nudge cooldown timestamp.

        #66: see :meth:`send_keys` for the traceability-prefix
        semantics; ``source`` defaults to ``"unknown"`` for
        backwards-compat with tests / external callers.

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

        self.send_keys(agent, text, source=source)
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
                *_tmux_cmd(), "display-message", "-p", "-t", target,
                "#{pane_current_command}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def _tmux_send_keys_once(target: str, text: str) -> bool:
        """Send text + Enter to a tmux pane. Returns True if tmux commands succeeded."""
        result = subprocess.run(
            [*_tmux_cmd(), "send-keys", "-t", target, text],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("tmux send-keys text failed for %s: %s", target, result.stderr.strip())
            return False
        time.sleep(_SEND_KEYS_DELAY)
        result = subprocess.run(
            [*_tmux_cmd(), "send-keys", "-t", target, "Enter"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("tmux send-keys Enter failed for %s: %s", target, result.stderr.strip())
            return False
        return True

    @staticmethod
    def _tmux_clear_input(target: str) -> None:
        """Send Escape to clear any partial TUI input state."""
        subprocess.run(
            [*_tmux_cmd(), "send-keys", "-t", target, "Escape"],
            capture_output=True,
            text=True,
        )
        time.sleep(0.2)

    @staticmethod
    def _capture_pane_text(target: str, lines: int = 5) -> str:
        """Capture recent pane text for delivery verification."""
        result = subprocess.run(
            [*_tmux_cmd(), "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    def _tmux_send_keys(
        self, target: str, text: str, skip_verify: bool = False,
    ) -> None:
        """Send text + Enter to a tmux pane.

        When *skip_verify* is ``False`` (non-Claude-Code agents), captures the
        pane after each attempt to verify delivery and retries on failure.

        When *skip_verify* is ``True`` (Claude Code agents), sends once without
        verification.  Claude Code's TUI causes false-negative verification
        (text delivered but not visible in capture), and the Escape + retry
        cycle actively sabotages delivery.  The delivery protocol ACK provides
        real confirmation for these agents.
        """
        if skip_verify:
            if self._tmux_send_keys_once(target, text):
                logger.debug("Nudge sent to %s (skip-verify)", target)
            else:
                logger.warning("Nudge send-keys failed for %s", target)
            return

        for attempt in range(1, self._send_retries + 1):
            # On retry, clear TUI state first
            if attempt > 1:
                logger.info(
                    "Nudge retry %d/%d for %s — clearing TUI state",
                    attempt, self._send_retries, target,
                )
                self._tmux_clear_input(target)
                time.sleep(_RETRY_DELAY)

            if not self._tmux_send_keys_once(target, text):
                continue

            # Verify delivery: check if text appeared or agent started working
            time.sleep(_VERIFY_DELAY)
            pane_content = self._capture_pane_text(target, lines=8)
            if not pane_content:
                logger.warning("Could not capture pane %s for verification", target)
                continue

            # Success indicators: the sent text is visible, or the agent is
            # now processing (no idle prompt visible in the last few lines)
            last_lines = pane_content.strip().splitlines()[-3:] if pane_content.strip() else []
            last_text = " ".join(last_lines)

            # Check if our text landed or agent is actively working
            if text.lower() in last_text.lower() or "❯" not in last_text:
                logger.debug("Nudge delivered to %s on attempt %d", target, attempt)
                return

            logger.warning(
                "Nudge text not detected in pane %s after attempt %d",
                target, attempt,
            )

        logger.error(
            "Failed to deliver nudge to %s after %d attempts",
            target, self._send_retries,
        )
