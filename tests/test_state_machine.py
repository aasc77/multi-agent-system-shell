"""
Tests for orchestrator/state_machine.py -- Config-Driven State Machine Engine

TDD Contract (RED phase):
These tests define the expected behavior of the State Machine Engine.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R4: Config-Driven State Machine
  - Acceptance criteria from task rgr-2

Test categories:
  1. Initialization -- loading states and transitions from config
  2. Transition matching -- trigger + source_agent + status
  3. Transition result -- action + action_args returned
  4. No matching transition -- valid trigger but wrong state/agent/status
  5. Current state tracking
  6. reset() behavior
  7. Startup validation -- initial state reference
  8. Startup validation -- transition from/to references
  9. Startup validation -- action must be recognized built-in
  10. Startup validation -- source_agent must reference defined agent
  11. Startup validation -- target_agent in action_args must reference defined agent
  12. Startup validation -- at least one transition must exist
  13. Validation failure raises exception or exits with code 1
  14. Edge cases
"""

import pytest

# --- The import that MUST fail in RED phase ---
from orchestrator.state_machine import StateMachine, StateMachineError


# ---------------------------------------------------------------------------
# Fixtures -- Config dicts mirroring PRD R4 YAML structure
# ---------------------------------------------------------------------------

VALID_AGENTS = {
    "writer": {"runtime": "claude_code", "working_dir": "/tmp/demo"},
    "executor": {"runtime": "script", "command": "echo"},
}

VALID_CONFIG = {
    "initial": "idle",
    "states": {
        "idle": {"description": "No active task"},
        "waiting_writer": {"agent": "writer"},
        "waiting_executor": {"agent": "executor"},
        "blocked": {"description": "Human intervention needed"},
    },
    "transitions": [
        {
            "from": "idle",
            "to": "waiting_writer",
            "trigger": "task_assigned",
            "action": "assign_to_agent",
            "action_args": {"target_agent": "writer"},
        },
        {
            "from": "waiting_writer",
            "to": "waiting_executor",
            "trigger": "agent_complete",
            "source_agent": "writer",
            "status": "pass",
            "action": "assign_to_agent",
            "action_args": {"target_agent": "executor"},
        },
        {
            "from": "waiting_writer",
            "to": "idle",
            "trigger": "agent_complete",
            "source_agent": "writer",
            "status": "fail",
            "action": "flag_human",
        },
        {
            "from": "waiting_executor",
            "to": "idle",
            "trigger": "agent_complete",
            "source_agent": "executor",
            "status": "pass",
        },
        {
            "from": "waiting_executor",
            "to": "waiting_writer",
            "trigger": "agent_complete",
            "source_agent": "executor",
            "status": "fail",
            "action": "assign_to_agent",
            "action_args": {
                "target_agent": "writer",
                "message": "Tests failed. Fix and re-send.",
            },
        },
    ],
}


@pytest.fixture
def sm():
    """Create a valid state machine instance."""
    return StateMachine(config=VALID_CONFIG, agents=VALID_AGENTS)


@pytest.fixture
def valid_config():
    """Return a deep copy of the valid config dict."""
    import copy

    return copy.deepcopy(VALID_CONFIG)


@pytest.fixture
def valid_agents():
    """Return a deep copy of valid agents dict."""
    import copy

    return copy.deepcopy(VALID_AGENTS)


# ===========================================================================
# 1. INITIALIZATION -- Loading states and transitions from config
# ===========================================================================


class TestInitialization:
    """StateMachine must load states and transitions from a config dict."""

    def test_creates_instance_from_valid_config(self, sm):
        """StateMachine should be constructable with valid config + agents."""
        assert sm is not None

    def test_loads_all_states(self, sm):
        """All states defined in config must be accessible."""
        assert "idle" in sm.states
        assert "waiting_writer" in sm.states
        assert "waiting_executor" in sm.states
        assert "blocked" in sm.states

    def test_loads_all_transitions(self, sm):
        """All transitions defined in config must be loaded."""
        assert len(sm.transitions) == 5

    def test_initial_state_set_correctly(self, sm):
        """Initial state must match config's 'initial' value."""
        assert sm.current_state == "idle"

    def test_accepts_config_dict_and_agents_dict(self):
        """Constructor must accept config (dict) and agents (dict) params."""
        sm = StateMachine(config=VALID_CONFIG, agents=VALID_AGENTS)
        assert sm.current_state == "idle"


# ===========================================================================
# 2. TRANSITION MATCHING -- trigger + source_agent + status
# ===========================================================================


class TestTransitionMatching:
    """Transition matching by trigger type, source_agent, and status fields."""

    def test_match_by_trigger_only(self, sm):
        """task_assigned trigger from idle should match the first transition."""
        result = sm.handle_trigger(trigger="task_assigned")
        assert result is not None

    def test_match_by_trigger_and_source_agent_and_status(self, sm):
        """agent_complete with source_agent=writer, status=pass from waiting_writer."""
        # Move to waiting_writer first
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert result is not None
        assert sm.current_state == "waiting_executor"

    def test_match_fail_transition(self, sm):
        """agent_complete with source_agent=writer, status=fail from waiting_writer."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="fail"
        )
        assert result is not None
        assert sm.current_state == "idle"

    def test_match_executor_pass(self, sm):
        """agent_complete with source_agent=executor, status=pass from waiting_executor."""
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == "waiting_executor"

        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert result is not None
        assert sm.current_state == "idle"

    def test_match_executor_fail(self, sm):
        """agent_complete with source_agent=executor, status=fail loops to waiting_writer."""
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == "waiting_executor"

        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="fail"
        )
        assert result is not None
        assert sm.current_state == "waiting_writer"

    def test_source_agent_must_match_for_agent_complete(self, sm):
        """Trigger agent_complete with wrong source_agent should not match."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        # executor is the wrong source_agent when in waiting_writer
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert result is None or (hasattr(result, "matched") and not result.matched)

    def test_status_must_match(self, sm):
        """Trigger with correct source_agent but wrong status should not match."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        # There is no transition for writer + status "partial" from waiting_writer
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="partial"
        )
        assert result is None or (hasattr(result, "matched") and not result.matched)


# ===========================================================================
# 3. TRANSITION RESULT -- action + action_args returned
# ===========================================================================


class TestTransitionResult:
    """handle_trigger must return the matched transition with action and action_args."""

    def test_returns_action(self, sm):
        """Matched transition must include the action."""
        result = sm.handle_trigger(trigger="task_assigned")
        assert result.action == "assign_to_agent"

    def test_returns_action_args(self, sm):
        """Matched transition must include action_args."""
        result = sm.handle_trigger(trigger="task_assigned")
        assert result.action_args["target_agent"] == "writer"

    def test_returns_action_args_with_message(self, sm):
        """Transition with message in action_args must return it."""
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="fail"
        )
        assert result.action == "assign_to_agent"
        assert result.action_args["target_agent"] == "writer"
        assert result.action_args["message"] == "Tests failed. Fix and re-send."

    def test_returns_none_action_when_transition_has_no_action(self, sm):
        """Transition without action should return None or empty action."""
        # idle -> waiting_writer -> waiting_executor (pass) -> idle (executor pass, no action)
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert result.action is None or result.action == ""

    def test_result_includes_from_and_to_states(self, sm):
        """Result must expose the from and to states of the matched transition."""
        result = sm.handle_trigger(trigger="task_assigned")
        # 'from' is a reserved keyword, implementation may use from_state/to_state
        from_state = getattr(result, "from_state", None) or getattr(
            result, "from", None
        )
        to_state = getattr(result, "to_state", None) or getattr(result, "to", None)
        assert from_state == "idle"
        assert to_state == "waiting_writer"

    def test_result_includes_trigger(self, sm):
        """Result must include the trigger that was matched."""
        result = sm.handle_trigger(trigger="task_assigned")
        assert result.trigger == "task_assigned"


# ===========================================================================
# 4. NO MATCHING TRANSITION
# ===========================================================================


class TestNoMatchingTransition:
    """When trigger type is valid but no transition matches current state/source_agent/status."""

    def test_returns_none_for_no_match(self, sm):
        """No matching transition should return None."""
        # In idle state, agent_complete with writer/pass has no transition
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert result is None

    def test_state_unchanged_on_no_match(self, sm):
        """State must NOT change when no transition matches."""
        original_state = sm.current_state
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == original_state

    def test_unknown_trigger_returns_none(self, sm):
        """Completely unknown trigger type should return None."""
        result = sm.handle_trigger(trigger="some_random_trigger")
        assert result is None

    def test_state_unchanged_on_unknown_trigger(self, sm):
        """State must not change for unknown triggers."""
        original_state = sm.current_state
        sm.handle_trigger(trigger="some_random_trigger")
        assert sm.current_state == original_state

    def test_no_match_wrong_source_for_current_state(self, sm):
        """agent_complete from wrong agent for current state returns None."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        # executor is wrong agent when in waiting_writer
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert result is None


# ===========================================================================
# 5. CURRENT STATE TRACKING
# ===========================================================================


class TestCurrentStateTracking:
    """State machine must track and expose its current state."""

    def test_initial_state_is_idle(self, sm):
        """At creation, current_state must be the configured initial state."""
        assert sm.current_state == "idle"

    def test_state_updates_after_transition(self, sm):
        """current_state must update after a successful transition."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

    def test_state_chain_through_pipeline(self, sm):
        """Track state through a complete pipeline: idle -> writer -> executor -> idle."""
        assert sm.current_state == "idle"

        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == "waiting_executor"

        sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert sm.current_state == "idle"

    def test_state_after_fail_and_retry(self, sm):
        """Track state through fail path: idle -> writer -> fail -> idle."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="fail"
        )
        assert sm.current_state == "idle"

    def test_state_after_executor_fail_loops_to_writer(self, sm):
        """executor fail should loop back to waiting_writer."""
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="fail"
        )
        assert sm.current_state == "waiting_writer"


# ===========================================================================
# 6. RESET BEHAVIOR
# ===========================================================================


class TestReset:
    """reset() must return the state machine to its initial state."""

    def test_reset_returns_to_initial(self, sm):
        """reset() must set current_state back to the initial state."""
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state != "idle"

        sm.reset()
        assert sm.current_state == "idle"

    def test_reset_from_any_state(self, sm):
        """reset() should work from any state."""
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == "waiting_executor"

        sm.reset()
        assert sm.current_state == "idle"

    def test_reset_is_idempotent(self, sm):
        """Calling reset() when already in initial state should be fine."""
        assert sm.current_state == "idle"
        sm.reset()
        assert sm.current_state == "idle"

    def test_transitions_work_after_reset(self, sm):
        """After reset, transitions should function normally."""
        sm.handle_trigger(trigger="task_assigned")
        sm.reset()
        assert sm.current_state == "idle"

        # Should be able to transition again
        result = sm.handle_trigger(trigger="task_assigned")
        assert result is not None
        assert sm.current_state == "waiting_writer"


# ===========================================================================
# 7. VALIDATION -- initial must reference a defined state
# ===========================================================================


class TestValidationInitialState:
    """Startup validation: initial must reference a state defined in states."""

    def test_invalid_initial_state_raises_error(self, valid_config, valid_agents):
        """initial referencing an undefined state must raise StateMachineError."""
        valid_config["initial"] = "nonexistent_state"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "initial" in str(exc_info.value).lower() or "nonexistent_state" in str(
            exc_info.value
        )

    def test_missing_initial_key_raises_error(self, valid_config, valid_agents):
        """Missing 'initial' key in config must raise StateMachineError."""
        del valid_config["initial"]
        with pytest.raises((StateMachineError, KeyError)):
            StateMachine(config=valid_config, agents=valid_agents)

    def test_empty_initial_raises_error(self, valid_config, valid_agents):
        """Empty string for initial state must raise StateMachineError."""
        valid_config["initial"] = ""
        with pytest.raises(StateMachineError):
            StateMachine(config=valid_config, agents=valid_agents)


# ===========================================================================
# 8. VALIDATION -- transition from/to must reference defined states
# ===========================================================================


class TestValidationTransitionStates:
    """Startup validation: every transition from/to must reference a defined state."""

    def test_invalid_from_state_raises_error(self, valid_config, valid_agents):
        """Transition 'from' referencing undefined state must raise error."""
        valid_config["transitions"][0]["from"] = "fantasy_state"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "fantasy_state" in str(exc_info.value) or "from" in str(
            exc_info.value
        ).lower()

    def test_invalid_to_state_raises_error(self, valid_config, valid_agents):
        """Transition 'to' referencing undefined state must raise error."""
        valid_config["transitions"][0]["to"] = "dream_state"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "dream_state" in str(exc_info.value) or "to" in str(
            exc_info.value
        ).lower()

    def test_wildcard_from_is_allowed(self, valid_config, valid_agents):
        """from: '*' is a special wildcard and should NOT be treated as invalid."""
        valid_config["transitions"].append(
            {
                "from": "*",
                "to": "blocked",
                "trigger": "emergency_stop",
                "action": "flag_human",
            }
        )
        # Should NOT raise
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None

    def test_wildcard_to_is_not_allowed(self, valid_config, valid_agents):
        """to: '*' is NOT valid -- transitions must go to a specific state."""
        valid_config["transitions"].append(
            {
                "from": "idle",
                "to": "*",
                "trigger": "bad_transition",
            }
        )
        with pytest.raises(StateMachineError):
            StateMachine(config=valid_config, agents=valid_agents)

    def test_multiple_invalid_transitions_caught(self, valid_config, valid_agents):
        """Multiple invalid transitions should all be caught at startup."""
        valid_config["transitions"][0]["from"] = "bad1"
        valid_config["transitions"][1]["to"] = "bad2"
        with pytest.raises(StateMachineError):
            StateMachine(config=valid_config, agents=valid_agents)


# ===========================================================================
# 9. VALIDATION -- action must be a recognized built-in
# ===========================================================================


class TestValidationActions:
    """Startup validation: every action must be a recognized built-in action."""

    def test_unrecognized_action_raises_error(self, valid_config, valid_agents):
        """Unknown action name must raise StateMachineError."""
        valid_config["transitions"][0]["action"] = "launch_missiles"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "launch_missiles" in str(exc_info.value) or "action" in str(
            exc_info.value
        ).lower()

    def test_assign_to_agent_is_valid(self, valid_config, valid_agents):
        """assign_to_agent must be accepted as a valid action."""
        # Default config uses assign_to_agent, should not raise
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None

    def test_flag_human_is_valid(self, valid_config, valid_agents):
        """flag_human must be accepted as a valid action."""
        # Config already has flag_human in transition[2], should not raise
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None

    def test_transition_without_action_is_valid(self, valid_config, valid_agents):
        """A transition with no action key is valid (action is optional)."""
        # transition[3] has no action (executor pass -> idle)
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None

    def test_empty_string_action_raises_error(self, valid_config, valid_agents):
        """An empty string for action should be treated as invalid."""
        valid_config["transitions"][0]["action"] = ""
        with pytest.raises(StateMachineError):
            StateMachine(config=valid_config, agents=valid_agents)


# ===========================================================================
# 10. VALIDATION -- source_agent must reference a defined agent
# ===========================================================================


class TestValidationSourceAgent:
    """Startup validation: every source_agent must reference an agent defined in agents config."""

    def test_undefined_source_agent_raises_error(self, valid_config, valid_agents):
        """source_agent not in agents dict must raise error."""
        valid_config["transitions"][1]["source_agent"] = "ghost_agent"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "ghost_agent" in str(exc_info.value) or "source_agent" in str(
            exc_info.value
        ).lower()

    def test_valid_source_agent_passes(self, valid_config, valid_agents):
        """source_agent that exists in agents dict should pass validation."""
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None

    def test_transition_without_source_agent_is_valid(
        self, valid_config, valid_agents
    ):
        """Transition with no source_agent key is valid (e.g., task_assigned)."""
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None


# ===========================================================================
# 11. VALIDATION -- target_agent in action_args must reference defined agent
# ===========================================================================


class TestValidationTargetAgent:
    """Startup validation: every target_agent in action_args must reference a defined agent."""

    def test_undefined_target_agent_raises_error(self, valid_config, valid_agents):
        """target_agent in action_args not in agents dict must raise error."""
        valid_config["transitions"][0]["action_args"]["target_agent"] = "phantom_agent"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "phantom_agent" in str(exc_info.value) or "target_agent" in str(
            exc_info.value
        ).lower()

    def test_valid_target_agent_passes(self, valid_config, valid_agents):
        """target_agent that exists in agents dict should pass validation."""
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None

    def test_transition_without_action_args_is_valid(
        self, valid_config, valid_agents
    ):
        """Transition with action but no action_args is valid (e.g., flag_human)."""
        # transition[2] has action: flag_human with no action_args
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None


# ===========================================================================
# 12. VALIDATION -- at least one transition must exist
# ===========================================================================


class TestValidationMinimumTransitions:
    """Startup validation: at least one transition must exist."""

    def test_empty_transitions_raises_error(self, valid_config, valid_agents):
        """Empty transitions list must raise error."""
        valid_config["transitions"] = []
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        assert "transition" in str(exc_info.value).lower()

    def test_missing_transitions_key_raises_error(self, valid_config, valid_agents):
        """Missing 'transitions' key entirely must raise error."""
        del valid_config["transitions"]
        with pytest.raises((StateMachineError, KeyError)):
            StateMachine(config=valid_config, agents=valid_agents)

    def test_one_transition_is_sufficient(self, valid_agents):
        """A single valid transition should pass validation."""
        config = {
            "initial": "idle",
            "states": {
                "idle": {"description": "No active task"},
                "waiting_writer": {"agent": "writer"},
            },
            "transitions": [
                {
                    "from": "idle",
                    "to": "waiting_writer",
                    "trigger": "task_assigned",
                    "action": "assign_to_agent",
                    "action_args": {"target_agent": "writer"},
                }
            ],
        }
        sm = StateMachine(config=config, agents=valid_agents)
        assert sm is not None


# ===========================================================================
# 13. VALIDATION FAILURE -- raises exception or exits with code 1
# ===========================================================================


class TestValidationFailureBehavior:
    """Validation failure must raise StateMachineError (which callers can map to exit 1)."""

    def test_statemachienerror_is_exception(self):
        """StateMachineError must be a subclass of Exception."""
        assert issubclass(StateMachineError, Exception)

    def test_statemachienerror_has_message(self):
        """StateMachineError should accept and store a descriptive message."""
        err = StateMachineError("test validation failure")
        assert "test validation failure" in str(err)

    def test_validation_error_is_descriptive(self, valid_config, valid_agents):
        """Validation errors must include details about what failed."""
        valid_config["initial"] = "nonexistent"
        with pytest.raises(StateMachineError) as exc_info:
            StateMachine(config=valid_config, agents=valid_agents)
        error_msg = str(exc_info.value)
        # Must mention the problematic value or field
        assert "nonexistent" in error_msg or "initial" in error_msg.lower()

    def test_all_validations_run_at_construction(self, valid_config, valid_agents):
        """All validations must run during __init__, not lazily."""
        valid_config["initial"] = "bad_state"
        valid_config["transitions"][0]["action"] = "bad_action"
        # Should raise during construction, not during first handle_trigger
        with pytest.raises(StateMachineError):
            StateMachine(config=valid_config, agents=valid_agents)


# ===========================================================================
# 14. EDGE CASES
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions for the state machine."""

    def test_wildcard_from_matches_any_state(self):
        """from: '*' should match regardless of current state."""
        agents = {"writer": {"runtime": "claude_code"}}
        config = {
            "initial": "idle",
            "states": {
                "idle": {"description": "idle"},
                "working": {"description": "working"},
                "blocked": {"description": "blocked"},
            },
            "transitions": [
                {
                    "from": "idle",
                    "to": "working",
                    "trigger": "task_assigned",
                    "action": "assign_to_agent",
                    "action_args": {"target_agent": "writer"},
                },
                {
                    "from": "*",
                    "to": "blocked",
                    "trigger": "emergency_stop",
                    "action": "flag_human",
                },
            ],
        }
        sm = StateMachine(config=config, agents=agents)

        # From idle
        result = sm.handle_trigger(trigger="emergency_stop")
        assert result is not None
        assert sm.current_state == "blocked"

        # Reset and move to working
        sm.reset()
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "working"

        # From working -- wildcard should match
        result = sm.handle_trigger(trigger="emergency_stop")
        assert result is not None
        assert sm.current_state == "blocked"

    def test_specific_from_takes_precedence_over_wildcard(self):
        """A specific 'from' match should take precedence over from: '*'."""
        agents = {"writer": {"runtime": "claude_code"}}
        config = {
            "initial": "idle",
            "states": {
                "idle": {"description": "idle"},
                "working": {"description": "working"},
                "special": {"description": "special handling"},
                "blocked": {"description": "blocked"},
            },
            "transitions": [
                {
                    "from": "idle",
                    "to": "working",
                    "trigger": "task_assigned",
                    "action": "assign_to_agent",
                    "action_args": {"target_agent": "writer"},
                },
                # Specific match for 'idle'
                {
                    "from": "idle",
                    "to": "special",
                    "trigger": "emergency_stop",
                    "action": "flag_human",
                },
                # Wildcard match
                {
                    "from": "*",
                    "to": "blocked",
                    "trigger": "emergency_stop",
                    "action": "flag_human",
                },
            ],
        }
        sm = StateMachine(config=config, agents=agents)

        # From idle -- specific match should win
        result = sm.handle_trigger(trigger="emergency_stop")
        assert sm.current_state == "special"

    def test_no_states_raises_error(self):
        """Config with no states dict should raise error."""
        config = {
            "initial": "idle",
            "states": {},
            "transitions": [
                {"from": "idle", "to": "idle", "trigger": "noop"},
            ],
        }
        with pytest.raises(StateMachineError):
            StateMachine(config=config, agents=VALID_AGENTS)

    def test_missing_states_key_raises_error(self):
        """Config with missing 'states' key should raise error."""
        config = {
            "initial": "idle",
            "transitions": [
                {"from": "idle", "to": "idle", "trigger": "noop"},
            ],
        }
        with pytest.raises((StateMachineError, KeyError)):
            StateMachine(config=config, agents=VALID_AGENTS)

    def test_handle_trigger_with_no_optional_params(self, sm):
        """handle_trigger should work with just trigger (no source_agent, no status)."""
        result = sm.handle_trigger(trigger="task_assigned")
        assert result is not None
        assert sm.current_state == "waiting_writer"

    def test_multiple_transitions_same_trigger_different_states(self, sm):
        """Same trigger from different states should match different transitions."""
        # agent_complete from waiting_writer
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == "waiting_executor"

        # agent_complete from waiting_executor
        sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert sm.current_state == "idle"

    def test_full_pipeline_round_trip(self, sm):
        """Complete round trip: idle -> writer -> executor -> idle -> writer again."""
        # First pass
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert sm.current_state == "idle"

        # Second pass
        sm.handle_trigger(trigger="task_assigned")
        assert sm.current_state == "waiting_writer"

    def test_empty_agents_dict_still_validates_source_agents(self):
        """Even with empty agents dict, source_agent validation should catch errors."""
        config = {
            "initial": "idle",
            "states": {
                "idle": {"description": "idle"},
                "working": {"description": "working"},
            },
            "transitions": [
                {
                    "from": "idle",
                    "to": "working",
                    "trigger": "agent_complete",
                    "source_agent": "nonexistent",
                    "status": "pass",
                },
            ],
        }
        with pytest.raises(StateMachineError):
            StateMachine(config=config, agents={})

    def test_transition_with_action_args_but_no_target_agent(
        self, valid_config, valid_agents
    ):
        """action_args without target_agent is valid (e.g., message-only args)."""
        # Modify a transition to have action_args with message but no target_agent
        valid_config["transitions"][2]["action_args"] = {"reason": "writer failed"}
        # flag_human with action_args but no target_agent -- should be fine
        sm = StateMachine(config=valid_config, agents=valid_agents)
        assert sm is not None
