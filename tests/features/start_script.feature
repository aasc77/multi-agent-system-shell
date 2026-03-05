Feature: start.sh Launch Script
  As a user of the Multi-Agent System Shell
  I want a single launch script that sets up the entire environment
  So that I can run N agents with one command

  Background:
    Given a valid project config at projects/<name>/config.yaml
    And the script scripts/start.sh exists and is executable

  # --- Preflight Checks ---

  Scenario: Preflight passes when all tools are present
    Given tmux, python3, and nats-server are all installed
    When I run "./scripts/start.sh demo"
    Then the script should pass preflight checks
    And exit code should be 0

  Scenario: Preflight fails listing all missing tools
    Given tmux is missing
    And python3 is missing
    And nats-server is missing
    When I run "./scripts/start.sh demo"
    Then the error message should list "tmux", "python3", and "nats-server"
    And exit code should be 1

  Scenario: Preflight fails listing only missing tools
    Given tmux is installed
    And python3 is installed
    And nats-server is missing
    When I run "./scripts/start.sh demo"
    Then the error message should list "nats-server"
    And the error message should NOT list "tmux" or "python3"
    And exit code should be 1

  # --- NATS Auto-Start ---

  Scenario: Auto-starts NATS when not running
    Given nats-server is installed but not running
    When I run "./scripts/start.sh demo"
    Then setup-nats.sh should be invoked
    And NATS should be running after startup

  Scenario: Skips NATS start when already running
    Given nats-server is already running
    When I run "./scripts/start.sh demo"
    Then setup-nats.sh should NOT be invoked

  # --- Idempotent Session Creation ---

  Scenario: Kills existing tmux session before creating new one
    Given a tmux session named "demo" already exists
    When I run "./scripts/start.sh demo"
    Then the old "demo" session should be killed
    And a new "demo" session should be created

  Scenario: Creates session when none exists
    Given no tmux session named "demo" exists
    When I run "./scripts/start.sh demo"
    Then a new "demo" session should be created
    And exit code should be 0

  # --- Control Window Layout ---

  Scenario: Creates control window with orchestrator and nats-monitor panes
    When I run "./scripts/start.sh demo"
    Then the tmux session should have a "control" window
    And the control window should have 2 panes side-by-side
    And the first pane should run the orchestrator
    And the second pane should run nats-monitor

  # --- Agents Window Layout ---

  Scenario: Creates agents window with one pane per agent
    Given the project config defines 3 agents: qa, dev, refactor
    When I run "./scripts/start.sh demo"
    Then the tmux session should have an "agents" window
    And the agents window should have 3 panes in tiled layout

  Scenario: Creates agents window for 2 agents
    Given the project config defines 2 agents: writer, executor
    When I run "./scripts/start.sh demo"
    Then the agents window should have 2 panes in tiled layout

  # --- Pane Border Status ---

  Scenario: Enables pane-border-status top for pane titles
    When I run "./scripts/start.sh demo"
    Then pane-border-status should be set to "top"

  # --- MCP Config Generation ---

  Scenario: Generates MCP config for each claude_code agent
    Given the project config has agents: writer (claude_code), executor (script)
    When I run "./scripts/start.sh demo"
    Then projects/demo/.mcp-configs/writer.json should be created
    And projects/demo/.mcp-configs/executor.json should NOT be created

  Scenario: MCP config contains correct AGENT_ROLE
    Given the project config has agent "writer" with runtime "claude_code"
    When I run "./scripts/start.sh demo"
    Then the MCP config for writer should have AGENT_ROLE set to "writer"

  Scenario: MCP config contains correct NATS_URL
    Given the global config has nats.url set to "nats://localhost:4222"
    When I run "./scripts/start.sh demo"
    Then the MCP config for writer should have NATS_URL set to "nats://localhost:4222"

  Scenario: MCP config contains correct WORKSPACE_DIR
    Given the agent "writer" has working_dir set to "/path/to/workspace"
    When I run "./scripts/start.sh demo"
    Then the MCP config for writer should have WORKSPACE_DIR set to "/path/to/workspace"

  Scenario: MCP config uses project dir when no working_dir specified
    Given the agent "writer" has no working_dir configured
    When I run "./scripts/start.sh demo"
    Then the MCP config for writer should have WORKSPACE_DIR set to the project directory

  Scenario: MCP config has correct JSON structure
    When I run "./scripts/start.sh demo"
    Then the MCP config should have mcpServers.mas-bridge.command set to "node"
    And the MCP config should have mcpServers.mas-bridge.args containing the bridge path

  # --- Agent Launching ---

  Scenario: Launches Claude Code agent with --mcp-config
    Given the agent "writer" has runtime "claude_code"
    When I run "./scripts/start.sh demo"
    Then the writer pane should launch "claude --mcp-config" with the config path

  Scenario: Launches script agent with configured command
    Given the agent "executor" has runtime "script" and command "python3 agents/echo_agent.py"
    When I run "./scripts/start.sh demo"
    Then the executor pane should launch "python3 agents/echo_agent.py"

  # --- SSH Support ---

  Scenario: SSH into remote host before launching agent
    Given the agent "reviewer" has ssh_host "dgx1.local"
    When I run "./scripts/start.sh demo"
    Then the reviewer pane should SSH into "dgx1.local" before launching

  # --- Exit Codes ---

  Scenario: Exit 0 on success
    Given all prerequisites are met
    When I run "./scripts/start.sh demo"
    Then exit code should be 0

  Scenario: Exit 1 on failure
    Given the project config file does not exist
    When I run "./scripts/start.sh nonexistent"
    Then exit code should be 1

  Scenario: Missing project argument
    When I run "./scripts/start.sh" without arguments
    Then a usage message should be displayed
    And exit code should be 1
