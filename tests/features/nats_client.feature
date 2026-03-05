Feature: NATS Client Wrapper
  As an orchestrator
  I need to communicate with agents via NATS JetStream
  So that messages are reliably delivered and persisted

  # --- Connection ---

  Scenario: Connect to NATS server
    Given a NATS config with URL "nats://localhost:4222"
    When I create a NatsClient and connect
    Then the client is connected

  Scenario: Close connection
    Given a connected NatsClient
    When I close the connection
    Then the client is disconnected

  # --- Publishing ---

  Scenario: Publish task to agent inbox
    Given a connected NatsClient with agents "writer" and "executor"
    When I publish a task_assignment to "writer"
    Then the message is published to "agents.writer.inbox"
    And the payload is valid JSON with type "task_assignment"

  # --- Subscribing ---

  Scenario: Subscribe to agent outbox
    Given a connected NatsClient with agents "writer" and "executor"
    When I subscribe to "writer" outbox with a callback
    Then a subscription is created on "agents.writer.outbox"
    And the subscription uses a durable consumer

  # --- JetStream Setup ---

  Scenario: Create AGENTS stream on connect
    Given a NATS config with stream "AGENTS" covering "agents.>"
    When I create a NatsClient and connect
    Then the AGENTS stream is created or updated
    And the stream has limits retention policy
    And the stream has max 10000 messages
    And the stream has max age of 1 hour

  # --- Health Check ---

  Scenario: NATS server unreachable on startup
    Given a NATS config with URL "nats://unreachable:4222"
    When I try to connect
    Then a NatsClientError is raised
    And the error message contains "scripts/setup-nats.sh"

  # --- all_done ---

  Scenario: Publish all_done to all agents
    Given a connected NatsClient with agents "writer" and "executor"
    When I publish all_done with summary "3 completed, 0 stuck"
    Then an all_done message is published to "agents.writer.inbox"
    And an all_done message is published to "agents.executor.inbox"
    And each message has type "all_done" and the summary text

  # --- Error Handling ---

  Scenario: Publish before connect raises error
    Given an unconnected NatsClient
    When I try to publish a message
    Then a NatsClientError or RuntimeError is raised

  Scenario: Subscribe before connect raises error
    Given an unconnected NatsClient
    When I try to subscribe to an outbox
    Then a NatsClientError or RuntimeError is raised
