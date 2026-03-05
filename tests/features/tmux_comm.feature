Feature: tmux Communication Module
  As an orchestrator
  I need to communicate with agent panes via tmux send-keys
  So that I can nudge agents safely without breaking running subprocesses

  # --- Pane Index Mapping ---

  Scenario: Build agent-to-pane-index mapping from config order
    Given agents defined in config order: qa, dev, refactor
    When TmuxComm is initialized
    Then the pane mapping is qa=0, dev=1, refactor=2

  Scenario: Single agent maps to pane index 0
    Given agents defined in config order: writer
    When TmuxComm is initialized
    Then the pane mapping is writer=0

  # --- Canonical Target Format ---

  Scenario: Target format uses session:agents.pane_index
    Given a session name "demo" and agent "dev" at pane index 1
    When I get the tmux target for "dev"
    Then the target is "demo:agents.1"

  # --- Sending Text (send-keys) ---

  Scenario: Send nudge text to agent pane
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "claude"
    When I nudge agent "writer"
    Then tmux send-keys is called with target "demo:agents.0" and text ending with Enter

  # --- Safe Nudging: pane_current_command Check ---

  Scenario: Nudge succeeds when foreground is claude
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "claude"
    When I nudge agent "writer"
    Then the nudge is sent successfully

  Scenario: Nudge skipped when foreground is node
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "node"
    When I nudge agent "writer"
    Then the nudge is skipped

  Scenario: Nudge skipped when foreground is python
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "python"
    When I nudge agent "writer"
    Then the nudge is skipped

  Scenario: Nudge skipped when foreground is git
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "git"
    When I nudge agent "writer"
    Then the nudge is skipped

  Scenario: Nudge skipped when foreground is npm
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "npm"
    When I nudge agent "writer"
    Then the nudge is skipped

  # --- Cooldown ---

  Scenario: Nudge respects cooldown period
    Given a TmuxComm with nudge_cooldown_seconds=30
    And the foreground process for "writer" is "claude"
    And agent "writer" was nudged 10 seconds ago
    When I nudge agent "writer"
    Then the nudge is skipped due to cooldown

  Scenario: Nudge sent after cooldown expires
    Given a TmuxComm with nudge_cooldown_seconds=30
    And the foreground process for "writer" is "claude"
    And agent "writer" was nudged 31 seconds ago
    When I nudge agent "writer"
    Then the nudge is sent successfully

  # --- Consecutive Skip Tracking ---

  Scenario: Track consecutive skipped nudges
    Given a TmuxComm with max_nudge_retries=3
    And the foreground process for "writer" is "python"
    When I nudge agent "writer" 2 times
    Then the consecutive skip count for "writer" is 2

  Scenario: Consecutive skip counter resets on successful nudge
    Given a TmuxComm with max_nudge_retries=20
    And the foreground process for "writer" is "python"
    When I nudge agent "writer" 3 times
    And the foreground process changes to "claude"
    And I nudge agent "writer" 1 time
    Then the consecutive skip count for "writer" is 0

  # --- Escalation ---

  Scenario: Escalate to flag_human after max_nudge_retries consecutive skips
    Given a TmuxComm with max_nudge_retries=3
    And the foreground process for "writer" is "node"
    When I nudge agent "writer" 3 times
    Then flag_human is called for agent "writer"
    And a warning log is emitted about agent "writer" being stuck

  Scenario: No escalation below max_nudge_retries
    Given a TmuxComm with max_nudge_retries=5
    And the foreground process for "writer" is "node"
    When I nudge agent "writer" 4 times
    Then flag_human is not called

  # --- msg Command ---

  Scenario: msg command sends text when agent is idle (claude foreground)
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "claude"
    When I send msg to "writer" with text "fix the tests"
    Then tmux send-keys is called with "fix the tests" and Enter

  Scenario: msg command refuses when agent is busy
    Given a TmuxComm with session "demo" and agent "writer" at pane 0
    And the foreground process for "writer" is "python"
    When I send msg to "writer" with text "fix the tests"
    Then the msg is refused with a warning
    And tmux send-keys is NOT called
