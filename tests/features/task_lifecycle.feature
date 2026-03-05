Feature: Task Lifecycle Manager
  The orchestration logic that drives task progression through the pipeline.
  Module location: orchestrator/lifecycle.py

  Requirements traced to PRD:
    - R5: Task Queue (task completion, retry, stuck)
    - R4: Config-Driven State Machine (trigger firing, transition results)
    - R3: Communication Flow (NATS messaging, all_done)
    - R6: tmux Communication (nudging agents)

  Background:
    Given a valid project config with agents, state machine, and tasks
    And the NATS client is connected
    And the tmux session is running
    And the task queue has at least one pending task

  # --- Picking next task ---

  Scenario: Pick next pending task and mark in_progress
    Given the task queue has tasks "task-1" (pending), "task-2" (pending)
    When the lifecycle manager processes the next task
    Then "task-1" status must be "in_progress"
    And the current task must be "task-1"

  Scenario: Skip completed and stuck tasks when picking next
    Given the task queue has tasks "done-1" (completed), "stuck-1" (stuck), "task-1" (pending)
    When the lifecycle manager processes the next task
    Then the current task must be "task-1"

  # --- Firing task_assigned trigger ---

  Scenario: Fire task_assigned trigger into state machine
    Given the state machine is in "idle" state
    And the next pending task is "task-1"
    When the lifecycle manager processes the next task
    Then a "task_assigned" trigger must be fired into the state machine

  Scenario: task_assigned trigger results in assign_to_agent
    Given the state machine transitions idle -> waiting_writer on task_assigned
    And the transition has action "assign_to_agent" with target_agent "writer"
    When the lifecycle manager fires task_assigned
    Then the task must be published to the "writer" agent's NATS inbox
    And the "writer" agent must be nudged via tmux

  # --- Completion rule ---

  Scenario: Mark task completed when state returns to initial without assign_to_agent
    Given the state machine is in "waiting_executor" state
    And "task-1" is the current in_progress task
    When the executor sends agent_complete with status "pass"
    And the state machine transitions to the initial state "idle"
    And the transition has no "assign_to_agent" action
    Then "task-1" must be marked "completed"

  Scenario: Do NOT mark task completed when transition has assign_to_agent action
    Given the state machine is in "waiting_writer" state
    And "task-1" is the current in_progress task
    When the writer sends agent_complete with status "pass"
    And the state machine transitions to "waiting_executor" with action "assign_to_agent"
    Then "task-1" must remain "in_progress"

  Scenario: Do NOT mark task completed when state does not return to initial
    Given the state machine is in "waiting_writer" state
    And "task-1" is the current in_progress task
    When the writer sends agent_complete with status "pass"
    And the state machine transitions to "waiting_executor" (not initial)
    Then "task-1" must remain "in_progress"

  # --- Move to next task after completion ---

  Scenario: Move to next pending task after completing current
    Given "task-1" has just been completed
    And "task-2" is pending
    When the lifecycle manager processes the next task
    Then the current task must be "task-2"
    And "task-2" status must be "in_progress"

  # --- Fail handling with no matching transition ---

  Scenario: Increment attempts when fail has no matching transition
    Given the state machine is in "waiting_writer" state
    And "task-1" is the current in_progress task with attempts=0
    When the writer sends agent_complete with status "fail"
    And no transition matches from the current state
    Then "task-1" attempts must be 1

  # --- Retry logic ---

  Scenario: Retry when attempts below max_attempts
    Given max_attempts_per_task is 5
    And "task-1" has attempts=1 (below max)
    When the lifecycle manager processes a fail with no matching transition
    Then the state machine must be reset to initial state
    And the "task_assigned" trigger must be fired again
    And "task-1" must remain "in_progress"

  Scenario: Mark stuck when attempts reach max_attempts
    Given max_attempts_per_task is 3
    And "task-1" has attempts=3 (at max)
    When the lifecycle manager processes a fail with no matching transition
    Then "task-1" must be marked "stuck"
    And the lifecycle manager must move to the next pending task

  # --- all_done message ---

  Scenario: Send all_done when all tasks completed or stuck
    Given all tasks are either "completed" or "stuck"
    When the lifecycle manager checks if all tasks are done
    Then an "all_done" message must be published to every agent's NATS inbox
    And the summary must say "All tasks processed: X completed, Y stuck"

  Scenario: Log summary when all tasks processed
    Given 2 tasks are "completed" and 1 task is "stuck"
    When all_done is triggered
    Then the log must contain "All tasks processed: 2 completed, 1 stuck"

  # --- Orchestrator stays alive ---

  Scenario: Orchestrator stays alive after all_done
    Given all tasks have been processed
    And all_done has been sent
    Then the orchestrator must NOT exit
    And the orchestrator must remain in a running state

  # --- Fail handling with matching fail transition ---

  Scenario: Fire matched fail transition (no retry) when fail transition exists
    Given the state machine is in "waiting_executor" state
    And "task-1" is the current in_progress task
    When the executor sends agent_complete with status "fail"
    And a fail transition exists that matches from the current state
    Then the matched transition must fire (e.g., go to waiting_writer)
    And the task attempts counter must NOT be incremented
    And the task must NOT be marked stuck

  # --- Move to next task after stuck ---

  Scenario: Move to next pending task after marking stuck
    Given "task-1" has been marked "stuck"
    And "task-2" is pending
    When the lifecycle manager processes the next task
    Then the current task must be "task-2"
