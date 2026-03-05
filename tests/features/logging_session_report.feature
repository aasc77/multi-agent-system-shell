Feature: Logging and Session Reports
  As an orchestrator operator
  I need structured logging to file and stdout
  And session reports for project-level audit trails
  So that I can debug agent communication and review task progress

  Background:
    Given the orchestrator is configured with a project "demo"

  # --- R11: Orchestrator Logs ---

  Scenario: Dual output logging to file and stdout
    When the logging system is initialized
    Then logs are written to "orchestrator/orchestrator.log"
    And logs are also written to stdout
    And both outputs receive the same log messages

  Scenario: Log format matches specification
    When a log message is emitted
    Then the format is "%(asctime)s [%(levelname)s] %(message)s"
    And the asctime uses the standard datetime format

  Scenario: State transition events are logged
    When a state transition occurs from "idle" to "waiting_writer"
    Then a log entry is created with level INFO
    And the message contains the from_state "idle"
    And the message contains the to_state "waiting_writer"

  Scenario: Task assignment events are logged
    When task "task-1" is assigned to agent "writer"
    Then a log entry is created with level INFO
    And the message contains the task_id "task-1"
    And the message contains the agent name "writer"

  Scenario: NATS publish events are logged
    When a NATS publish event occurs on subject "agents.writer.inbox"
    Then a log entry is created with level INFO
    And the message contains the subject "agents.writer.inbox"

  Scenario: NATS subscribe events are logged
    When a NATS subscribe event occurs on subject "agents.writer.outbox"
    Then a log entry is created with level INFO
    And the message contains the subject "agents.writer.outbox"

  Scenario: Nudge sent events are logged
    When a nudge is sent to agent "writer"
    Then a log entry is created with level INFO
    And the message indicates nudge was "sent" to "writer"

  Scenario: Nudge skipped events are logged
    When a nudge is skipped for agent "writer" because process is "node"
    Then a log entry is created with level WARNING
    And the message indicates nudge was "skipped" for "writer"

  Scenario: Nudge escalated events are logged
    When nudge retries are exhausted for agent "writer"
    Then a log entry is created with level WARNING
    And the message indicates nudge was "escalated" for "writer"

  # --- R11: Session Report ---

  Scenario: Session report is created at the correct path
    When the session report module is initialized for project "demo"
    Then the report path is "projects/demo/session-report.md"

  Scenario: Session report entries are timestamped markdown
    When a session report entry is added
    Then the entry contains a timestamp
    And the entry is valid markdown

  Scenario: Session report records task assignments
    When task "task-1" is assigned to agent "writer"
    And the event is recorded in the session report
    Then the session report contains a task assignment entry for "task-1" to "writer"

  Scenario: Session report records task completions
    When task "task-1" is completed
    And the event is recorded in the session report
    Then the session report contains a task completion entry for "task-1"

  Scenario: Session report records blockers
    When task "task-2" is flagged as blocked
    And the event is recorded in the session report
    Then the session report contains a blocker entry for "task-2"
