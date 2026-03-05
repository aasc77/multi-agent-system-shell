Feature: Config Loader
  As the orchestrator
  I need to load and merge YAML configurations
  So that projects can override global defaults while inheriting unset values

  Background:
    Given a global config.yaml with sections: llm, nats, tasks, tmux
    And a project config at projects/<name>/config.yaml with sections: project, agents, state_machine, tmux overrides

  Scenario: Load global config with all sections
    Given a valid global config.yaml exists
    When I load the config without a project
    Then the config object contains llm, nats, tasks, and tmux sections
    And each section has the expected default values

  Scenario: Load project config with deep merge
    Given a valid global config.yaml exists
    And a project config overrides tmux.session_name to "demo"
    When I load the config for project "demo"
    Then tmux.session_name equals "demo" (from project)
    And tmux.nudge_prompt equals the global default (inherited)
    And tmux.nudge_cooldown_seconds equals 30 (inherited)

  Scenario: Project keys fully override global keys at two levels deep
    Given a global config with tmux.nudge_cooldown_seconds = 30
    And a project config with tmux.nudge_cooldown_seconds = 60
    When I load the merged config
    Then tmux.nudge_cooldown_seconds equals 60

  Scenario: Unset project keys inherit from global
    Given a global config with all tmux settings
    And a project config with only tmux.session_name set
    When I load the merged config
    Then tmux.nudge_prompt is inherited from global
    And tmux.max_nudge_retries is inherited from global

  Scenario: Missing project config raises clear error
    Given a valid global config exists
    When I load config for project "nonexistent"
    Then a FileNotFoundError or ConfigError is raised
    And the error message includes the missing file path

  Scenario: Missing global config uses empty defaults
    Given no global config.yaml exists
    And a valid project config exists
    When I load the merged config
    Then the config object is valid
    And project values are present
    And global-only sections use empty/default values

  Scenario: Config returns structured object with dot-access
    When I load a valid merged config
    Then I can access config.tmux.session_name
    And I can access config.llm.provider
    And I can access config.nats.url
    And I can access config.agents as a dict of agent definitions
