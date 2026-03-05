Feature: Echo Agent and Demo Project
  As a system operator
  I want an example echo_agent.py script agent and a demo project config
  So that I can validate end-to-end message flow without Claude Code

  Background:
    Given NATS server is running on localhost:4222
    And the demo project config exists at projects/demo/config.yaml

  # ── echo_agent.py: NATS subscription ──────────────────────────────

  Scenario: Echo agent subscribes to its NATS inbox
    Given echo_agent.py is started with --role executor
    When the agent connects to NATS
    Then it subscribes to "agents.executor.inbox"

  Scenario: Echo agent accepts --role argument
    Given echo_agent.py is started with --role reviewer
    When the agent connects to NATS
    Then it subscribes to "agents.reviewer.inbox"

  # ── echo_agent.py: outbox response ────────────────────────────────

  Scenario: Echo agent responds with valid agent_complete message
    Given echo_agent.py is running with --role executor
    When a task_assignment message arrives on agents.executor.inbox:
      | type             | task_id | title      | description       |
      | task_assignment  | demo-1  | Echo test  | Test the echo     |
    Then the agent publishes to "agents.executor.outbox" a JSON message with:
      | field   | value           |
      | type    | agent_complete  |
      | status  | pass            |
    And the summary field echoes the task title or description

  # ── echo_agent.py: clean exit on all_done ─────────────────────────

  Scenario: Echo agent exits cleanly on all_done
    Given echo_agent.py is running with --role executor
    When an all_done message arrives on agents.executor.inbox:
      | type      | summary                              |
      | all_done  | All tasks processed: 1 completed     |
    Then echo_agent.py exits with code 0

  # ── Demo project: config structure ────────────────────────────────

  Scenario: Demo config defines writer and executor agents
    Given the demo project config is loaded
    Then it defines an agent "writer" with runtime "claude_code"
    And it defines an agent "executor" with runtime "script"
    And the executor command references echo_agent.py

  Scenario: Demo state machine has required states
    Given the demo project config is loaded
    Then the state machine has initial state "idle"
    And the state machine has state "waiting_writer"
    And the state machine has state "waiting_executor"

  Scenario: Demo state machine has valid transitions
    Given the demo project config is loaded
    Then there is a transition from "idle" to "waiting_writer" on trigger "task_assigned"
    And there is a transition from "waiting_writer" to "waiting_executor" on agent_complete from writer with status pass
    And there is a transition from "waiting_executor" to "idle" on agent_complete from executor with status pass

  Scenario: Demo config has sample tasks
    Given the demo project has a tasks.json file
    Then it contains at least one task
    And each task has id, title, description, status, and attempts fields

  Scenario: Demo config passes startup validation
    Given the demo project config is loaded
    When the state machine engine validates the config
    Then no validation errors are raised

  # ── End-to-end: task round-trip ───────────────────────────────────

  Scenario: End-to-end task completion with echo agent
    Given the demo project is started via start.sh
    And NATS is running
    When the orchestrator picks up a pending task
    Then it assigns the task to the writer agent
    When the writer agent completes with status pass
    Then the orchestrator assigns to the executor (echo_agent.py)
    When echo_agent.py responds with agent_complete status pass
    Then the task is marked as completed
    And the state machine returns to idle
