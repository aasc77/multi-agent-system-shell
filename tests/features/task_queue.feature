Feature: Task Queue Manager
  As an orchestrator
  I need to load, track, and persist task states
  So that tasks are processed sequentially with proper status tracking

  Background:
    Given a tasks.json file with 3 pending tasks

  # --- Loading ---

  Scenario: Load tasks from JSON file
    When I create a TaskQueue from the file
    Then all 3 tasks are loaded
    And each task has id, title, description, status, and attempts fields

  # --- Status Transitions ---

  Scenario: Valid status transition pending to in_progress
    When I mark task "task-1" as in_progress
    Then task "task-1" status is "in_progress"

  Scenario: Valid status transition in_progress to completed
    Given task "task-1" is in_progress
    When I mark task "task-1" as completed
    Then task "task-1" status is "completed"

  Scenario: Valid status transition in_progress to stuck
    Given task "task-1" is in_progress
    When I mark task "task-1" as stuck
    Then task "task-1" status is "stuck"

  Scenario: Invalid transition pending to completed
    When I try to mark task "task-1" as completed
    Then a TaskQueueError is raised

  Scenario: Invalid transition pending to stuck
    When I try to mark task "task-1" as stuck
    Then a TaskQueueError is raised

  Scenario: Invalid transition completed to in_progress
    Given task "task-1" is completed
    When I try to mark task "task-1" as in_progress
    Then a TaskQueueError is raised

  # --- get_next_pending ---

  Scenario: Get first pending task
    When I call get_next_pending
    Then the returned task has id "task-1"

  Scenario: Get next pending after first is completed
    Given task "task-1" is completed
    When I call get_next_pending
    Then the returned task has id "task-2"

  Scenario: No pending tasks returns None
    Given all tasks are completed
    When I call get_next_pending
    Then None is returned

  # --- Attempts ---

  Scenario: Increment attempts counter
    Given task "task-1" is in_progress
    When I increment attempts for "task-1" 3 times
    Then task "task-1" has 3 attempts

  Scenario: Task is stuck when attempts reach max
    Given task "task-1" is in_progress with 5 attempts
    When I check is_stuck for "task-1" with max_attempts 5
    Then the result is True

  Scenario: Task is not stuck when below max attempts
    Given task "task-1" is in_progress with 3 attempts
    When I check is_stuck for "task-1" with max_attempts 5
    Then the result is False

  # --- all_done ---

  Scenario: all_done is False with pending tasks
    Then all_done returns False

  Scenario: all_done is True when all completed
    Given all tasks are completed
    Then all_done returns True

  Scenario: all_done is True with mix of completed and stuck
    Given task "task-1" is completed
    And task "task-2" is stuck
    And task "task-3" is completed
    Then all_done returns True

  # --- Persistence ---

  Scenario: Save and reload preserves state
    Given task "task-1" is in_progress with 2 attempts
    When I save the task queue
    And I reload the task queue from the same file
    Then task "task-1" status is "in_progress"
    And task "task-1" has 2 attempts
