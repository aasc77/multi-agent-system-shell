"""Config-driven state machine engine for the orchestrator.

Requirements traced to PRD:
  - R4: Config-Driven State Machine

Provides StateMachine and StateMachineError for loading states/transitions
from a config dict, validating at construction time, and handling triggers
to drive state transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class StateMachineError(Exception):
    """Raised when state machine configuration is invalid."""


_VALID_ACTIONS = {"assign_to_agent", "flag_human"}


@dataclass
class TransitionResult:
    """Result of a matched transition."""

    trigger: str
    from_state: str
    to_state: str
    action: str | None
    action_args: dict[str, Any] = field(default_factory=dict)


class StateMachine:
    """Config-driven state machine engine.

    Args:
        config: Dict with keys ``initial``, ``states``, ``transitions``.
        agents: Dict of agent definitions (name -> agent config).
    """

    def __init__(self, config: dict[str, Any], agents: dict[str, Any]) -> None:
        self._agents = agents
        self._config = config

        # --- extract and validate ---
        self._validate(config, agents)

        self.states: dict[str, Any] = config["states"]
        self.transitions: list[dict[str, Any]] = config["transitions"]
        self._initial: str = config["initial"]
        self.current_state: str = self._initial

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(config: dict[str, Any], agents: dict[str, Any]) -> None:
        """Run all startup validations. Raises StateMachineError on failure."""
        errors: list[str] = []

        # --- states ---
        states = config.get("states")
        if not states:
            raise StateMachineError(
                "No states defined: 'states' must be a non-empty mapping"
            )

        state_names = set(states.keys())

        # --- initial ---
        initial = config.get("initial")
        if not initial:
            raise StateMachineError(
                "Missing or empty 'initial' state in config"
            )
        if initial not in state_names:
            raise StateMachineError(
                f"Initial state '{initial}' is not defined in states"
            )

        # --- transitions ---
        transitions = config.get("transitions")
        if transitions is None:
            raise StateMachineError(
                "Missing 'transitions' key in config"
            )
        if len(transitions) == 0:
            raise StateMachineError(
                "At least one transition must be defined"
            )

        for i, t in enumerate(transitions):
            from_state = t.get("from", "")
            to_state = t.get("to", "")

            # from must be defined state or wildcard '*'
            if from_state != "*" and from_state not in state_names:
                errors.append(
                    f"Transition [{i}]: 'from' state '{from_state}' is not defined in states"
                )

            # to must be a defined state (wildcard NOT allowed)
            if to_state == "*" or to_state not in state_names:
                errors.append(
                    f"Transition [{i}]: 'to' state '{to_state}' is not defined in states"
                )

            # action validation
            action = t.get("action")
            if action is not None:
                if action == "":
                    errors.append(
                        f"Transition [{i}]: empty string action is not valid"
                    )
                elif action not in _VALID_ACTIONS:
                    errors.append(
                        f"Transition [{i}]: unrecognized action '{action}'"
                    )

            # source_agent validation
            source_agent = t.get("source_agent")
            if source_agent is not None and source_agent not in agents:
                errors.append(
                    f"Transition [{i}]: source_agent '{source_agent}' is not a defined agent"
                )

            # target_agent in action_args validation
            action_args = t.get("action_args")
            if action_args:
                target_agent = action_args.get("target_agent")
                if target_agent is not None and target_agent not in agents:
                    errors.append(
                        f"Transition [{i}]: target_agent '{target_agent}' in action_args is not a defined agent"
                    )

        if errors:
            raise StateMachineError(
                "State machine validation failed:\n" + "\n".join(errors)
            )

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

        Returns a TransitionResult on match, or None if no transition matches.
        """
        specific_match: dict[str, Any] | None = None
        wildcard_match: dict[str, Any] | None = None

        for t in self.transitions:
            # trigger must match
            if t.get("trigger") != trigger:
                continue

            from_state = t.get("from")

            # source_agent must match if defined on transition
            t_source = t.get("source_agent")
            if t_source is not None and t_source != source_agent:
                continue

            # status must match if defined on transition
            t_status = t.get("status")
            if t_status is not None and t_status != status:
                continue

            # from must match current state or be wildcard
            if from_state == self.current_state:
                specific_match = t
                break  # specific match takes precedence, stop searching
            elif from_state == "*":
                if wildcard_match is None:
                    wildcard_match = t

        matched = specific_match or wildcard_match

        if matched is None:
            return None

        from_state = self.current_state
        to_state = matched["to"]
        action = matched.get("action") or None
        action_args = matched.get("action_args", {}) or {}

        self.current_state = to_state

        return TransitionResult(
            trigger=trigger,
            from_state=from_state,
            to_state=to_state,
            action=action,
            action_args=action_args,
        )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset the state machine to its initial state."""
        self.current_state = self._initial
