Feature: MCP Bridge (stdio server for Claude Code agents)
  As a Claude Code agent
  I need an MCP stdio bridge that connects me to NATS
  So that I can receive tasks and send results without knowing NATS internals

  Background:
    Given AGENT_ROLE is set to "writer"
    And NATS_URL is set to "nats://localhost:4222"
    And WORKSPACE_DIR is set to "/tmp/test"

  # --- Environment Configuration ---

  Scenario: Agent role is read from environment
    When I create an McpBridge
    Then the bridge agent role is "writer"

  Scenario: Missing AGENT_ROLE raises error
    Given AGENT_ROLE is not set
    When I try to create an McpBridge
    Then an error is thrown mentioning "AGENT_ROLE"

  Scenario: Missing NATS_URL raises error
    Given NATS_URL is not set
    When I try to create an McpBridge
    Then an error is thrown mentioning "NATS_URL"

  # --- Tool Registration ---

  Scenario: Bridge exposes check_messages and send_message tools
    When I create an McpBridge
    And I list available tools
    Then "check_messages" is in the tool list
    And "send_message" is in the tool list

  Scenario: check_messages does not require role parameter
    When I inspect the check_messages tool schema
    Then "role" is not in the required parameters

  Scenario: send_message accepts content parameter
    When I inspect the send_message tool schema
    Then "content" is in the input properties

  # --- check_messages ---

  Scenario: check_messages pulls from the correct inbox subject
    Given the bridge is connected to NATS
    When I call check_messages
    Then the bridge pulls from "agents.writer.inbox"

  # --- send_message ---

  Scenario: send_message publishes to the correct outbox subject
    Given the bridge is connected to NATS
    When I call send_message with status "pass" and summary "Done"
    Then the bridge publishes to "agents.writer.outbox"
    And the payload type is "agent_complete"
    And the payload status is "pass"

  # --- all_done ---

  Scenario: all_done message is returned without triggering outbox
    Given the bridge is connected to NATS
    When an all_done message arrives in the inbox
    Then check_messages returns the all_done message
    And no message is published to the outbox

  # --- Error Handling ---

  Scenario: Tool call before connect raises error
    Given the bridge is NOT connected
    When I call check_messages
    Then an error is thrown

  Scenario: NATS connection failure is handled
    Given NATS server is unreachable
    When I try to connect the bridge
    Then an error is thrown
