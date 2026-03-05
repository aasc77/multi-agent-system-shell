Feature: Utility Scripts (stop.sh, setup-nats.sh, reset-tasks.sh, nats-monitor.sh)
  As a user of the Multi-Agent System Shell
  I want supporting shell scripts for managing the system lifecycle
  So that I can cleanly stop sessions, set up NATS, reset tasks, and monitor messages

  # ===== stop.sh <project> =====

  Scenario: stop.sh kills tmux session for the given project
    Given a tmux session named "demo" is running
    When I run "./scripts/stop.sh demo"
    Then the "demo" tmux session should be killed
    And exit code should be 0

  Scenario: stop.sh cleans up .mcp-configs directory
    Given a project "demo" has a .mcp-configs/ directory with generated files
    When I run "./scripts/stop.sh demo"
    Then the projects/demo/.mcp-configs/ directory should be cleaned up

  Scenario: stop.sh --kill-nats also stops nats-server
    Given nats-server is running
    When I run "./scripts/stop.sh demo --kill-nats"
    Then the "demo" tmux session should be killed
    And nats-server should be stopped

  Scenario: stop.sh without --kill-nats does NOT stop nats-server
    Given nats-server is running
    When I run "./scripts/stop.sh demo"
    Then nats-server should still be running

  Scenario: stop.sh is idempotent when session doesn't exist
    Given no tmux session named "demo" exists
    When I run "./scripts/stop.sh demo"
    Then the output should contain "already stopped"
    And exit code should be 0

  Scenario: stop.sh exits 0 even when session already stopped
    Given no tmux session named "demo" exists
    When I run "./scripts/stop.sh demo"
    Then exit code should be 0

  Scenario: stop.sh shows usage when no project argument given
    When I run "./scripts/stop.sh" without arguments
    Then a usage message should be displayed
    And exit code should be 1

  # ===== setup-nats.sh =====

  Scenario: setup-nats.sh installs nats-server via brew
    Given nats-server is not installed
    When I run "./scripts/setup-nats.sh"
    Then it should attempt to install nats-server via brew

  Scenario: setup-nats.sh installs nats CLI via brew
    Given nats CLI is not installed
    When I run "./scripts/setup-nats.sh"
    Then it should attempt to install nats CLI via brew

  Scenario: setup-nats.sh starts nats-server with JetStream enabled
    When I run "./scripts/setup-nats.sh"
    Then nats-server should be started with JetStream enabled

  # ===== reset-tasks.sh <project> =====

  Scenario: reset-tasks.sh resets all task statuses to pending
    Given a project "demo" has tasks with statuses "completed" and "stuck"
    When I run "./scripts/reset-tasks.sh demo"
    Then all tasks should have status "pending"

  Scenario: reset-tasks.sh resets all task attempts to 0
    Given a project "demo" has tasks with attempts > 0
    When I run "./scripts/reset-tasks.sh demo"
    Then all tasks should have attempts set to 0

  Scenario: reset-tasks.sh shows usage when no project argument
    When I run "./scripts/reset-tasks.sh" without arguments
    Then a usage message should be displayed
    And exit code should be 1

  # ===== nats-monitor.sh =====

  Scenario: nats-monitor.sh subscribes to agents.> by default
    When I run "./scripts/nats-monitor.sh"
    Then it should subscribe to "agents.>" using nats CLI

  Scenario: nats-monitor.sh accepts optional subject filter
    When I run "./scripts/nats-monitor.sh agents.writer.>"
    Then it should subscribe to "agents.writer.>" using nats CLI

  Scenario: nats-monitor.sh uses nats sub command
    When I run "./scripts/nats-monitor.sh"
    Then it should invoke "nats sub" command
