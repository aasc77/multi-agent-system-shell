"""Config-driven state machine engine for the orchestrator.

Requirements traced to PRD:
  - R4: Config-Driven State Machine

Provides StateMachine and StateMachineError for loading states/transitions
from a config dict, validating at construction time, and handling triggers
to drive state transitions.

Example usage::

    config = {
        "initial": "idle",
        "states": {"idle": {}, "working": {}},
        "transitions": [
            {"from": "idle", "to": "working", "trigger": "start"}
        ],
    }
    sm = StateMachine(config=config, agents={"writer": {}})
    result = sm.handle_trigger(trigger="start")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Wildcard token for ``from`` field — matches any current state (PRD R4).
WILDCARD_STATE = "*"

#: Recognised built-in actions (PRD R4 — "Built-in actions").
VALID_ACTIONS: frozenset[str] = frozenset({"assign_to_agent", "flag_human"})

# --- Config key names (avoids scattered magic strings) ---
_KEY_INITIAL = "initial"
_KEY_STATES = "states"
_KEY_TRANSITIONS = "transitions"
_KEY_FROM = "from"
_KEY_TO = "to"
_KEY_TRIGGER = "trigger"
_KEY_ACTION = "action"
_KEY_ACTION_ARGS = "action_args"
_KEY_SOURCE_AGENT = "source_agent"
_KEY_TARGET_AGENT = "target_agent"
_KEY_STATUS = "status"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StateMachineError(Exception):
    """Raised when state machine configuration is invalid."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Immutable result of a matched transition.

    Attributes:
        trigger: The trigger name that caused the transition.
        from_state: State the machine was in before the transition.
        to_state: State the machine moved to after the transition.
        action: Built-in action to execute (``None`` if no action).
        action_args: Additional arguments for the action.
    """

    trigger: str
    from_state: str
    to_state: str
    action: str | None
    action_args: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: A single transition definition dict from the config.
TransitionDict = dict[str, Any]


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------


class StateMachine:
    """Config-driven state machine engine (PRD R4).

    Loads states and transitions from a configuration dict, validates all
    references at construction time, and processes triggers to drive state
    transitions.

    Args:
        config: Dict with keys ``initial``, ``states``, ``transitions``.
        agents: Dict of agent definitions (name -> agent config).

    Raises:
        StateMachineError: If the configuration fails any validation check.
    """

    def __init__(self, config: dict[str, Any], agents: dict[str, Any]) -> None:
        self._agents = agents
        self._config = config

        self._validate(config, agents)

        self.states: dict[str, Any] = config[_KEY_STATES]
        self.transitions: list[TransitionDict] = config[_KEY_TRANSITIONS]
        self._initial: str = config[_KEY_INITIAL]
        self.current_state: str = self._initial

    # ------------------------------------------------------------------
    # Validation (PRD R4 — "Startup validation")
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(config: dict[str, Any], agents: dict[str, Any]) -> None:
        """Run all startup validations.

        Raises:
            StateMachineError: With a detailed description of every problem
                found (errors are collected, not raised one-at-a-time).
        """
        state_names = _validate_states(config)
        _validate_initial(config, state_names)
        transitions = _validate_transitions_present(config)
        _validate_transition_details(transitions, state_names, agents)

    @property
    def initial_state(self) -> str:
        """Return the initial state name."""
        return self._initial

    # ------------------------------------------------------------------
    # Trigger handling
    # ------------------------------------------------------------------

    def handle_trigger(
        self,
        trigger: str,
        source_agent: str | None = None,
        status: str | None = None,
    ) -> TransitionResult | None:
        """Attempt to match a trigger against transitions from the current state.

        Matching rules (PRD R4):
        - ``trigger`` must match the transition's ``trigger`` field.
        - ``source_agent`` must match if the transition specifies one.
        - ``status`` must match if the transition specifies one.
        - ``from`` must equal the current state or be the wildcard ``*``.
        - A specific ``from`` match takes precedence over a wildcard match.

        Returns:
            A :class:`TransitionResult` on match, or ``None`` if no
            transition matches the trigger from the current state.
        """
        matched = self._find_matching_transition(trigger, source_agent, status)
        if matched is None:
            return None

        from_state = self.current_state
        to_state: str = matched[_KEY_TO]
        action: str | None = matched.get(_KEY_ACTION) or None
        action_args: dict[str, Any] = matched.get(_KEY_ACTION_ARGS, {}) or {}

        self.current_state = to_state

        return TransitionResult(
            trigger=trigger,
            from_state=from_state,
            to_state=to_state,
            action=action,
            action_args=action_args,
        )

    def _find_matching_transition(
        self,
        trigger: str,
        source_agent: str | None,
        status: str | None,
    ) -> TransitionDict | None:
        """Search transitions for one matching the trigger and current state.

        A specific ``from`` match takes precedence over a wildcard (``*``)
        match. The first wildcard match is remembered as a fallback.
        """
        wildcard_match: TransitionDict | None = None

        for t in self.transitions:
            if not self._transition_matches(t, trigger, source_agent, status):
                continue

            from_state = t.get(_KEY_FROM)
            if from_state == self.current_state:
                return t  # specific match — highest precedence
            if from_state == WILDCARD_STATE and wildcard_match is None:
                wildcard_match = t

        return wildcard_match

    @staticmethod
    def _transition_matches(
        transition: TransitionDict,
        trigger: str,
        source_agent: str | None,
        status: str | None,
    ) -> bool:
        """Check whether a transition matches the given trigger criteria.

        Validates ``trigger``, ``source_agent``, and ``status`` fields.
        Does **not** check the ``from`` state — that is handled by the caller.
        """
        if transition.get(_KEY_TRIGGER) != trigger:
            return False

        t_source = transition.get(_KEY_SOURCE_AGENT)
        if t_source is not None and t_source != source_agent:
            return False

        t_status = transition.get(_KEY_STATUS)
        if t_status is not None and t_status != status:
            return False

        return True

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset the state machine to its initial state."""
        self.current_state = self._initial


# ---------------------------------------------------------------------------
# Validation helpers (module-private)
# ---------------------------------------------------------------------------


def _validate_states(config: dict[str, Any]) -> set[str]:
    """Validate that ``states`` is present and non-empty.

    Returns:
        The set of defined state names.

    Raises:
        StateMachineError: If ``states`` is missing or empty.
    """
    states = config.get(_KEY_STATES)
    if not states:
        raise StateMachineError(
            "No states defined: 'states' must be a non-empty mapping"
        )
    return set(states.keys())


def _validate_initial(config: dict[str, Any], state_names: set[str]) -> None:
    """Validate that ``initial`` is present and references a defined state.

    Raises:
        StateMachineError: If ``initial`` is missing, empty, or undefined.
    """
    initial = config.get(_KEY_INITIAL)
    if not initial:
        raise StateMachineError("Missing or empty 'initial' state in config")
    if initial not in state_names:
        raise StateMachineError(
            f"Initial state '{initial}' is not defined in states"
        )


def _validate_transitions_present(
    config: dict[str, Any],
) -> list[TransitionDict]:
    """Validate that ``transitions`` is present with at least one entry.

    Returns:
        The transitions list from the config.

    Raises:
        StateMachineError: If ``transitions`` is missing or empty.
    """
    transitions = config.get(_KEY_TRANSITIONS)
    if transitions is None:
        raise StateMachineError("Missing 'transitions' key in config")
    if len(transitions) == 0:
        raise StateMachineError("At least one transition must be defined")
    return transitions


def _validate_transition_details(
    transitions: list[TransitionDict],
    state_names: set[str],
    agents: dict[str, Any],
) -> None:
    """Validate every transition's fields against defined states and agents.

    Checks performed per transition:
    - ``from`` must reference a defined state or be the wildcard ``*``.
    - ``to`` must reference a defined state (wildcard not allowed).
    - ``action`` (if present) must be non-empty and in :data:`VALID_ACTIONS`.
    - ``source_agent`` (if present) must reference a defined agent.
    - ``target_agent`` in ``action_args`` (if present) must reference a
      defined agent.

    Raises:
        StateMachineError: With all collected errors if any check fails.
    """
    errors: list[str] = []

    for i, t in enumerate(transitions):
        _check_from_state(i, t, state_names, errors)
        _check_to_state(i, t, state_names, errors)
        _check_action(i, t, errors)
        _check_source_agent(i, t, agents, errors)
        _check_target_agent(i, t, agents, errors)

    if errors:
        raise StateMachineError(
            "State machine validation failed:\n" + "\n".join(errors)
        )


# --- Individual field checks ------------------------------------------------


def _check_from_state(
    index: int,
    transition: TransitionDict,
    state_names: set[str],
    errors: list[str],
) -> None:
    """``from`` must be a defined state or the wildcard ``*``."""
    from_state = transition.get(_KEY_FROM, "")
    if from_state != WILDCARD_STATE and from_state not in state_names:
        errors.append(
            f"Transition [{index}]: 'from' state '{from_state}' "
            f"is not defined in states"
        )


def _check_to_state(
    index: int,
    transition: TransitionDict,
    state_names: set[str],
    errors: list[str],
) -> None:
    """``to`` must be a defined state (wildcard NOT allowed)."""
    to_state = transition.get(_KEY_TO, "")
    if to_state == WILDCARD_STATE or to_state not in state_names:
        errors.append(
            f"Transition [{index}]: 'to' state '{to_state}' "
            f"is not defined in states"
        )


def _check_action(
    index: int,
    transition: TransitionDict,
    errors: list[str],
) -> None:
    """``action`` (if present) must be non-empty and recognised."""
    action = transition.get(_KEY_ACTION)
    if action is None:
        return
    if action == "":
        errors.append(
            f"Transition [{index}]: empty string action is not valid"
        )
    elif action not in VALID_ACTIONS:
        errors.append(
            f"Transition [{index}]: unrecognized action '{action}'"
        )


def _check_source_agent(
    index: int,
    transition: TransitionDict,
    agents: dict[str, Any],
    errors: list[str],
) -> None:
    """``source_agent`` (if present) must reference a defined agent."""
    source_agent = transition.get(_KEY_SOURCE_AGENT)
    if source_agent is not None and source_agent not in agents:
        errors.append(
            f"Transition [{index}]: source_agent '{source_agent}' "
            f"is not a defined agent"
        )


def _check_target_agent(
    index: int,
    transition: TransitionDict,
    agents: dict[str, Any],
    errors: list[str],
) -> None:
    """``target_agent`` in ``action_args`` (if present) must reference a defined agent."""
    action_args = transition.get(_KEY_ACTION_ARGS)
    if not action_args:
        return
    target_agent = action_args.get(_KEY_TARGET_AGENT)
    if target_agent is not None and target_agent not in agents:
        errors.append(
            f"Transition [{index}]: target_agent '{target_agent}' "
            f"in action_args is not a defined agent"
        )
