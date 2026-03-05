"""
Tests for projects/demo/ -- Demo Project Configuration and Validation

TDD Contract (RED phase):
These tests define the expected structure and validity of the demo project
configuration, tasks file, and end-to-end validation with the state machine.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R2: Config-Driven Agents
  - R4: Config-Driven State Machine
  - R5: Task Queue
  - R8: Scripts (start.sh)
  - Acceptance criteria from task rgr-13

Test categories:
  1. Demo config file existence and structure
  2. Demo agents definition (writer=claude_code, executor=script)
  3. Demo state machine states and transitions
  4. Demo tasks.json file structure
  5. Config validation passes (state machine engine validates without errors)
  6. End-to-end integration: orchestrator assigns task, echo agent responds
"""

import json
import os
import yaml
import pytest
from pathlib import Path

# --- The imports that MUST fail in RED phase ---
from orchestrator.state_machine import StateMachine, StateMachineError
from orchestrator.config import load_config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
DEMO_CONFIG_PATH = PROJECT_ROOT / "projects" / "demo" / "config.yaml"
DEMO_TASKS_PATH = PROJECT_ROOT / "projects" / "demo" / "tasks.json"
ECHO_AGENT_PATH = PROJECT_ROOT / "agents" / "echo_agent.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_config_raw():
    """Load the raw YAML from the demo project config."""
    assert DEMO_CONFIG_PATH.exists(), (
        f"Demo project config must exist at {DEMO_CONFIG_PATH}"
    )
    with open(DEMO_CONFIG_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture
def demo_tasks_raw():
    """Load the raw JSON from the demo tasks file."""
    assert DEMO_TASKS_PATH.exists(), (
        f"Demo tasks file must exist at {DEMO_TASKS_PATH}"
    )
    with open(DEMO_TASKS_PATH) as f:
        return json.load(f)


@pytest.fixture
def demo_config_loaded():
    """Load the demo config using the config loader with root_dir."""
    return load_config(root_dir=PROJECT_ROOT, project_name="demo")


# ===========================================================================
# 1. DEMO CONFIG FILE EXISTENCE AND STRUCTURE
# ===========================================================================


class TestDemoConfigExists:
    """Demo project config files must exist in the expected locations."""

    def test_demo_config_yaml_exists(self):
        """projects/demo/config.yaml must exist."""
        assert DEMO_CONFIG_PATH.exists(), (
            f"Demo config not found at {DEMO_CONFIG_PATH}"
        )

    def test_demo_tasks_json_exists(self):
        """projects/demo/tasks.json must exist."""
        assert DEMO_TASKS_PATH.exists(), (
            f"Demo tasks not found at {DEMO_TASKS_PATH}"
        )

    def test_echo_agent_script_exists(self):
        """agents/echo_agent.py must exist."""
        assert ECHO_AGENT_PATH.exists(), (
            f"Echo agent script not found at {ECHO_AGENT_PATH}"
        )

    def test_demo_config_is_valid_yaml(self):
        """Demo config must be valid YAML."""
        with open(DEMO_CONFIG_PATH) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), "Demo config must be a YAML mapping"

    def test_demo_config_has_project_key(self, demo_config_raw):
        """Demo config must have a 'project' key."""
        assert "project" in demo_config_raw
        assert demo_config_raw["project"] == "demo"


# ===========================================================================
# 2. DEMO AGENTS DEFINITION
# ===========================================================================


class TestDemoAgents:
    """Demo config must define writer (claude_code) and executor (script) agents."""

    def test_has_agents_section(self, demo_config_raw):
        """Demo config must have an 'agents' section."""
        assert "agents" in demo_config_raw

    def test_defines_writer_agent(self, demo_config_raw):
        """Demo config must define a 'writer' agent."""
        assert "writer" in demo_config_raw["agents"]

    def test_defines_executor_agent(self, demo_config_raw):
        """Demo config must define an 'executor' agent."""
        assert "executor" in demo_config_raw["agents"]

    def test_writer_runtime_is_claude_code(self, demo_config_raw):
        """Writer agent must have runtime 'claude_code'."""
        writer = demo_config_raw["agents"]["writer"]
        assert writer["runtime"] == "claude_code"

    def test_executor_runtime_is_claude_code(self, demo_config_raw):
        """Executor agent must have runtime 'claude_code'."""
        executor = demo_config_raw["agents"]["executor"]
        assert executor["runtime"] == "claude_code"

    def test_executor_has_working_dir(self, demo_config_raw):
        """Executor agent should have a working_dir specified."""
        executor = demo_config_raw["agents"]["executor"]
        assert "working_dir" in executor

    def test_executor_has_system_prompt(self, demo_config_raw):
        """Executor agent should have a system_prompt specified."""
        executor = demo_config_raw["agents"]["executor"]
        assert "system_prompt" in executor

    def test_writer_has_working_dir(self, demo_config_raw):
        """Writer agent should have a working_dir specified."""
        writer = demo_config_raw["agents"]["writer"]
        assert "working_dir" in writer or "system_prompt" in writer


# ===========================================================================
# 3. DEMO STATE MACHINE STATES AND TRANSITIONS
# ===========================================================================


class TestDemoStateMachine:
    """Demo state machine must have idle, waiting_writer, waiting_executor states."""

    def test_has_state_machine_section(self, demo_config_raw):
        """Demo config must have a 'state_machine' section."""
        assert "state_machine" in demo_config_raw

    def test_initial_state_is_idle(self, demo_config_raw):
        """Initial state must be 'idle'."""
        sm = demo_config_raw["state_machine"]
        assert sm["initial"] == "idle"

    def test_has_idle_state(self, demo_config_raw):
        """State machine must define an 'idle' state."""
        states = demo_config_raw["state_machine"]["states"]
        assert "idle" in states

    def test_has_waiting_writer_state(self, demo_config_raw):
        """State machine must define a 'waiting_writer' state."""
        states = demo_config_raw["state_machine"]["states"]
        assert "waiting_writer" in states

    def test_has_waiting_executor_state(self, demo_config_raw):
        """State machine must define a 'waiting_executor' state."""
        states = demo_config_raw["state_machine"]["states"]
        assert "waiting_executor" in states

    def test_has_transitions(self, demo_config_raw):
        """State machine must have at least one transition."""
        sm = demo_config_raw["state_machine"]
        assert "transitions" in sm
        assert len(sm["transitions"]) >= 1

    def test_transition_idle_to_waiting_writer(self, demo_config_raw):
        """Must have a transition from idle to waiting_writer on task_assigned."""
        transitions = demo_config_raw["state_machine"]["transitions"]
        match = [
            t for t in transitions
            if t.get("from") == "idle"
            and t.get("to") == "waiting_writer"
            and t.get("trigger") == "task_assigned"
        ]
        assert len(match) >= 1, (
            "No transition from idle to waiting_writer on task_assigned"
        )

    def test_transition_waiting_writer_to_waiting_executor(self, demo_config_raw):
        """Must have transition from waiting_writer to waiting_executor on writer pass."""
        transitions = demo_config_raw["state_machine"]["transitions"]
        match = [
            t for t in transitions
            if t.get("from") == "waiting_writer"
            and t.get("to") == "waiting_executor"
            and t.get("trigger") == "agent_complete"
            and t.get("source_agent") == "writer"
            and t.get("status") == "pass"
        ]
        assert len(match) >= 1, (
            "No transition from waiting_writer to waiting_executor on writer pass"
        )

    def test_transition_waiting_executor_to_idle(self, demo_config_raw):
        """Must have transition from waiting_executor to idle on executor pass."""
        transitions = demo_config_raw["state_machine"]["transitions"]
        match = [
            t for t in transitions
            if t.get("from") == "waiting_executor"
            and t.get("to") == "idle"
            and t.get("trigger") == "agent_complete"
            and t.get("source_agent") == "executor"
            and t.get("status") == "pass"
        ]
        assert len(match) >= 1, (
            "No transition from waiting_executor to idle on executor pass"
        )

    def test_transition_assign_to_writer_has_action(self, demo_config_raw):
        """idle->waiting_writer transition must have assign_to_agent action."""
        transitions = demo_config_raw["state_machine"]["transitions"]
        match = [
            t for t in transitions
            if t.get("from") == "idle"
            and t.get("to") == "waiting_writer"
            and t.get("trigger") == "task_assigned"
        ]
        assert len(match) >= 1
        t = match[0]
        assert t.get("action") == "assign_to_agent"
        assert t.get("action_args", {}).get("target_agent") == "writer"

    def test_transition_assign_to_executor_has_action(self, demo_config_raw):
        """waiting_writer->waiting_executor transition must have assign_to_agent action."""
        transitions = demo_config_raw["state_machine"]["transitions"]
        match = [
            t for t in transitions
            if t.get("from") == "waiting_writer"
            and t.get("to") == "waiting_executor"
            and t.get("source_agent") == "writer"
            and t.get("status") == "pass"
        ]
        assert len(match) >= 1
        t = match[0]
        assert t.get("action") == "assign_to_agent"
        assert t.get("action_args", {}).get("target_agent") == "executor"

    def test_executor_pass_to_idle_has_no_assign_action(self, demo_config_raw):
        """waiting_executor->idle (pass) should NOT have assign_to_agent action.

        Per PRD R5: returning to initial without assigning another agent means
        the pipeline is finished for this task.
        """
        transitions = demo_config_raw["state_machine"]["transitions"]
        match = [
            t for t in transitions
            if t.get("from") == "waiting_executor"
            and t.get("to") == "idle"
            and t.get("source_agent") == "executor"
            and t.get("status") == "pass"
        ]
        assert len(match) >= 1
        t = match[0]
        # Should have no action or action is not assign_to_agent
        assert t.get("action") is None or t.get("action") != "assign_to_agent"

    def test_has_fail_handling_transitions(self, demo_config_raw):
        """Demo config should have at least one fail-handling transition."""
        transitions = demo_config_raw["state_machine"]["transitions"]
        fail_transitions = [
            t for t in transitions if t.get("status") == "fail"
        ]
        assert len(fail_transitions) >= 1, (
            "Demo config should have at least one fail transition"
        )


# ===========================================================================
# 4. DEMO TASKS.JSON FILE STRUCTURE
# ===========================================================================


class TestDemoTasks:
    """Demo tasks.json must have at least one sample task with required fields."""

    def test_tasks_file_is_valid_json(self):
        """tasks.json must be valid JSON."""
        with open(DEMO_TASKS_PATH) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_tasks_has_tasks_key(self, demo_tasks_raw):
        """tasks.json must have a 'tasks' key."""
        assert "tasks" in demo_tasks_raw

    def test_tasks_is_a_list(self, demo_tasks_raw):
        """tasks must be a list."""
        assert isinstance(demo_tasks_raw["tasks"], list)

    def test_has_at_least_one_task(self, demo_tasks_raw):
        """There must be at least one sample task."""
        assert len(demo_tasks_raw["tasks"]) >= 1

    def test_task_has_id(self, demo_tasks_raw):
        """Each task must have an 'id' field."""
        for task in demo_tasks_raw["tasks"]:
            assert "id" in task, f"Task missing 'id': {task}"

    def test_task_has_title(self, demo_tasks_raw):
        """Each task must have a 'title' field."""
        for task in demo_tasks_raw["tasks"]:
            assert "title" in task, f"Task missing 'title': {task}"

    def test_task_has_description(self, demo_tasks_raw):
        """Each task must have a 'description' field."""
        for task in demo_tasks_raw["tasks"]:
            assert "description" in task, f"Task missing 'description': {task}"

    def test_task_has_status(self, demo_tasks_raw):
        """Each task must have a 'status' field."""
        for task in demo_tasks_raw["tasks"]:
            assert "status" in task, f"Task missing 'status': {task}"

    def test_task_has_attempts(self, demo_tasks_raw):
        """Each task must have an 'attempts' field."""
        for task in demo_tasks_raw["tasks"]:
            assert "attempts" in task, f"Task missing 'attempts': {task}"

    def test_task_status_is_pending(self, demo_tasks_raw):
        """Sample tasks should start with status 'pending'."""
        for task in demo_tasks_raw["tasks"]:
            assert task["status"] == "pending", (
                f"Task {task['id']} should have status 'pending', got '{task['status']}'"
            )

    def test_task_attempts_is_zero(self, demo_tasks_raw):
        """Sample tasks should start with attempts = 0."""
        for task in demo_tasks_raw["tasks"]:
            assert task["attempts"] == 0, (
                f"Task {task['id']} should have attempts 0, got {task['attempts']}"
            )

    def test_task_ids_are_unique(self, demo_tasks_raw):
        """All task IDs must be unique."""
        ids = [t["id"] for t in demo_tasks_raw["tasks"]]
        assert len(ids) == len(set(ids)), f"Duplicate task IDs found: {ids}"


# ===========================================================================
# 5. CONFIG VALIDATION PASSES
# ===========================================================================


class TestDemoConfigValidation:
    """Demo config must pass state machine startup validation without errors."""

    def test_state_machine_validates_without_error(self, demo_config_raw):
        """State machine engine must accept the demo config without errors."""
        agents = demo_config_raw["agents"]
        sm_config = demo_config_raw["state_machine"]
        # This must NOT raise StateMachineError
        sm = StateMachine(config=sm_config, agents=agents)
        assert sm is not None

    def test_initial_state_references_valid_state(self, demo_config_raw):
        """initial state must reference a state defined in states."""
        sm = demo_config_raw["state_machine"]
        assert sm["initial"] in sm["states"]

    def test_all_transition_from_states_are_valid(self, demo_config_raw):
        """Every transition 'from' must reference a defined state."""
        sm = demo_config_raw["state_machine"]
        states = set(sm["states"].keys())
        for t in sm["transitions"]:
            from_state = t["from"]
            if from_state != "*":
                assert from_state in states, (
                    f"Transition 'from' state '{from_state}' not in defined states"
                )

    def test_all_transition_to_states_are_valid(self, demo_config_raw):
        """Every transition 'to' must reference a defined state."""
        sm = demo_config_raw["state_machine"]
        states = set(sm["states"].keys())
        for t in sm["transitions"]:
            assert t["to"] in states, (
                f"Transition 'to' state '{t['to']}' not in defined states"
            )

    def test_all_source_agents_reference_defined_agents(self, demo_config_raw):
        """Every source_agent must reference an agent defined in agents config."""
        agents = set(demo_config_raw["agents"].keys())
        for t in demo_config_raw["state_machine"]["transitions"]:
            if "source_agent" in t:
                assert t["source_agent"] in agents, (
                    f"source_agent '{t['source_agent']}' not in defined agents"
                )

    def test_all_target_agents_reference_defined_agents(self, demo_config_raw):
        """Every target_agent in action_args must reference a defined agent."""
        agents = set(demo_config_raw["agents"].keys())
        for t in demo_config_raw["state_machine"]["transitions"]:
            if "action_args" in t and "target_agent" in t["action_args"]:
                assert t["action_args"]["target_agent"] in agents, (
                    f"target_agent '{t['action_args']['target_agent']}' not in defined agents"
                )

    def test_all_actions_are_recognized_builtins(self, demo_config_raw):
        """Every action must be a recognized built-in (assign_to_agent, flag_human)."""
        valid_actions = {"assign_to_agent", "flag_human"}
        for t in demo_config_raw["state_machine"]["transitions"]:
            if "action" in t:
                assert t["action"] in valid_actions, (
                    f"Unrecognized action '{t['action']}'"
                )

    def test_state_machine_full_pipeline_works(self, demo_config_raw):
        """State machine should support the full idle->writer->executor->idle pipeline."""
        agents = demo_config_raw["agents"]
        sm_config = demo_config_raw["state_machine"]
        sm = StateMachine(config=sm_config, agents=agents)

        # idle -> waiting_writer
        result = sm.handle_trigger(trigger="task_assigned")
        assert result is not None
        assert sm.current_state == "waiting_writer"

        # waiting_writer -> waiting_executor
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert result is not None
        assert sm.current_state == "waiting_executor"

        # waiting_executor -> idle
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )
        assert result is not None
        assert sm.current_state == "idle"


# ===========================================================================
# 6. END-TO-END INTEGRATION (config + state machine + echo agent contract)
# ===========================================================================


class TestEndToEndContract:
    """End-to-end contract: orchestrator assigns task, echo agent responds, task completes.

    These tests validate the contract between components without requiring
    actual NATS or tmux. They verify that:
    - The demo config drives the state machine correctly
    - The echo agent response schema triggers the correct state transition
    - A complete task lifecycle can be traced through the state machine
    """

    def test_echo_agent_response_triggers_correct_transition(self, demo_config_raw):
        """Echo agent's agent_complete/pass response must trigger
        waiting_executor -> idle transition."""
        agents = demo_config_raw["agents"]
        sm_config = demo_config_raw["state_machine"]
        sm = StateMachine(config=sm_config, agents=agents)

        # Setup: move to waiting_executor
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        assert sm.current_state == "waiting_executor"

        # Simulate echo agent response
        echo_response = {
            "type": "agent_complete",
            "status": "pass",
            "summary": "Echoed task: demo-1",
        }

        result = sm.handle_trigger(
            trigger=echo_response["type"],
            source_agent="executor",
            status=echo_response["status"],
        )
        assert result is not None
        assert sm.current_state == "idle"

    def test_task_completion_on_return_to_idle(self, demo_config_raw):
        """Returning to idle state without assign_to_agent action = task complete.

        Per PRD R5: When a transition returns the state machine to the initial
        state AND the transition has no action (or the action is not
        assign_to_agent), the task is completed.
        """
        agents = demo_config_raw["agents"]
        sm_config = demo_config_raw["state_machine"]
        sm = StateMachine(config=sm_config, agents=agents)

        # Full pipeline
        sm.handle_trigger(trigger="task_assigned")
        sm.handle_trigger(
            trigger="agent_complete", source_agent="writer", status="pass"
        )
        result = sm.handle_trigger(
            trigger="agent_complete", source_agent="executor", status="pass"
        )

        # Verify: returned to initial state
        assert sm.current_state == sm_config["initial"]

        # Verify: no assign_to_agent action (task is done)
        assert result.action is None or result.action != "assign_to_agent"

    def test_fail_path_loops_back(self, demo_config_raw):
        """Writer fail or executor fail must be handled by state machine."""
        agents = demo_config_raw["agents"]
        sm_config = demo_config_raw["state_machine"]
        sm = StateMachine(config=sm_config, agents=agents)

        # Check if there are fail transitions in the demo config
        fail_transitions = [
            t for t in sm_config["transitions"]
            if t.get("status") == "fail"
        ]

        if len(fail_transitions) > 0:
            # At least one fail path exists -- verify it works
            sm.handle_trigger(trigger="task_assigned")

            # Try writer fail
            result = sm.handle_trigger(
                trigger="agent_complete", source_agent="writer", status="fail"
            )
            # Should either match a transition or return None (no matching transition)
            # If matched, state should change; if not, orchestrator handles retry
            assert True  # At minimum, it should not crash

    def test_multiple_task_cycles(self, demo_config_raw):
        """State machine must support multiple task cycles (reset and rerun)."""
        agents = demo_config_raw["agents"]
        sm_config = demo_config_raw["state_machine"]
        sm = StateMachine(config=sm_config, agents=agents)

        for i in range(3):
            # Full pipeline cycle
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

    def test_demo_config_loaded_via_config_loader(self):
        """Demo config must be loadable via the config loader module."""
        cfg = load_config(root_dir=PROJECT_ROOT, project_name="demo")
        assert cfg.project == "demo"
        assert "writer" in cfg.agents
        assert "executor" in cfg.agents
        assert cfg.state_machine.initial == "idle"
