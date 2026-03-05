"""
Tests for orchestrator/config.py -- Config Loader

TDD Contract (RED phase):
These tests define the expected behavior of the Config Loader module.
They MUST fail until the implementation is written.

Requirements traced to PRD:
  - R9: Configuration (Two-level config with deep merge)
  - Acceptance criteria from task rgr-1

Test categories:
  1. Global config loading
  2. Project config loading
  3. Deep merge behavior (two levels deep)
  4. Structured config object (dot-access)
  5. Error handling (missing files, invalid YAML)
  6. Edge cases
"""

import os
import pytest
import tempfile
import shutil
from pathlib import Path

# --- The import that MUST fail in RED phase ---
from orchestrator.config import load_config, ConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace with global and project configs."""
    # Copy global config
    global_cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURES_DIR / "global_config.yaml", global_cfg)

    # Create project directory structure
    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / "project_config.yaml", project_dir / "config.yaml")

    return tmp_path


@pytest.fixture
def workspace_override_tmux(tmp_path):
    """Workspace where project only overrides tmux.session_name."""
    global_cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURES_DIR / "global_config.yaml", global_cfg)

    project_dir = tmp_path / "projects" / "override-test"
    project_dir.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / "project_override_tmux.yaml", project_dir / "config.yaml")

    return tmp_path


@pytest.fixture
def workspace_minimal_project(tmp_path):
    """Workspace where project has no tmux overrides at all."""
    global_cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURES_DIR / "global_config.yaml", global_cfg)

    project_dir = tmp_path / "projects" / "minimal"
    project_dir.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / "minimal_project_config.yaml", project_dir / "config.yaml")

    return tmp_path


@pytest.fixture
def workspace_no_global(tmp_path):
    """Workspace with project config but NO global config.yaml."""
    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / "project_config.yaml", project_dir / "config.yaml")

    return tmp_path


@pytest.fixture
def workspace_empty_global(tmp_path):
    """Workspace with an empty global config.yaml."""
    global_cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURES_DIR / "empty_global_config.yaml", global_cfg)

    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / "project_config.yaml", project_dir / "config.yaml")

    return tmp_path


# ===========================================================================
# 1. GLOBAL CONFIG LOADING
# ===========================================================================


class TestGlobalConfigLoading:
    """Tests for loading the global config.yaml with all standard sections."""

    def test_loads_llm_section(self, workspace):
        """Global config must include the llm section with all keys."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.llm.provider == "ollama"
        assert cfg.llm.model == "qwen3:8b"
        assert cfg.llm.base_url == "http://localhost:11434"
        assert cfg.llm.temperature == 0.3
        assert cfg.llm.disable_thinking is True

    def test_loads_nats_section(self, workspace):
        """Global config must include the nats section."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.nats.url == "nats://localhost:4222"
        assert cfg.nats.stream == "AGENTS"
        assert cfg.nats.subjects_prefix == "agents"

    def test_loads_tasks_section(self, workspace):
        """Global config must include the tasks section."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.tasks.max_attempts_per_task == 5

    def test_loads_tmux_section(self, workspace):
        """Global config must include the tmux section with all defaults."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert "check_messages" in cfg.tmux.nudge_prompt
        assert cfg.tmux.nudge_cooldown_seconds == 30
        assert cfg.tmux.max_nudge_retries == 20


# ===========================================================================
# 2. PROJECT CONFIG LOADING
# ===========================================================================


class TestProjectConfigLoading:
    """Tests for loading project-specific config sections."""

    def test_loads_project_name(self, workspace):
        """Project config must expose the project name."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.project == "demo"

    def test_loads_agents_section(self, workspace):
        """Project config must include agents with their definitions."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert "writer" in cfg.agents
        assert "executor" in cfg.agents

    def test_agent_writer_properties(self, workspace):
        """Writer agent must have runtime, working_dir, and system_prompt."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        writer = cfg.agents["writer"]
        assert writer.runtime == "claude_code"
        assert writer.working_dir == "/tmp/demo"
        assert writer.system_prompt == "You are a writer agent."

    def test_agent_executor_properties(self, workspace):
        """Executor agent must have runtime and command."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        executor = cfg.agents["executor"]
        assert executor.runtime == "script"
        assert executor.command == "python3 agents/echo_agent.py --role executor"

    def test_loads_state_machine_section(self, workspace):
        """Project config must include the state_machine section."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.state_machine.initial == "idle"
        assert "idle" in cfg.state_machine.states
        assert "waiting_writer" in cfg.state_machine.states
        assert len(cfg.state_machine.transitions) >= 2


# ===========================================================================
# 3. DEEP MERGE BEHAVIOR (TWO LEVELS DEEP)
# ===========================================================================


class TestDeepMerge:
    """Tests for the two-level deep merge strategy.

    From PRD R9:
    > Project config overrides global. Two levels deep -- e.g., project
    > tmux.session_name overrides global tmux.session_name but inherits
    > tmux.nudge_prompt if not specified.
    """

    def test_project_tmux_session_name_overrides_global(self, workspace):
        """Project tmux.session_name MUST override global value."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.tmux.session_name == "demo"

    def test_inherits_tmux_nudge_prompt_from_global(self, workspace_override_tmux):
        """When project only sets tmux.session_name, nudge_prompt inherits from global."""
        cfg = load_config(root_dir=workspace_override_tmux, project_name="override-test")
        assert cfg.tmux.session_name == "my-custom-session"
        assert "check_messages" in cfg.tmux.nudge_prompt  # inherited from global

    def test_inherits_tmux_cooldown_from_global(self, workspace_override_tmux):
        """When project only sets tmux.session_name, nudge_cooldown_seconds inherits from global."""
        cfg = load_config(root_dir=workspace_override_tmux, project_name="override-test")
        assert cfg.tmux.nudge_cooldown_seconds == 30  # inherited from global

    def test_inherits_tmux_max_nudge_retries_from_global(self, workspace_override_tmux):
        """When project only sets tmux.session_name, max_nudge_retries inherits from global."""
        cfg = load_config(root_dir=workspace_override_tmux, project_name="override-test")
        assert cfg.tmux.max_nudge_retries == 20  # inherited from global

    def test_all_tmux_inherited_when_project_has_no_tmux(self, workspace_minimal_project):
        """When project has no tmux section at all, ALL tmux settings come from global."""
        cfg = load_config(root_dir=workspace_minimal_project, project_name="minimal")
        assert cfg.tmux.nudge_cooldown_seconds == 30
        assert cfg.tmux.max_nudge_retries == 20
        assert "check_messages" in cfg.tmux.nudge_prompt

    def test_project_only_keys_do_not_leak_to_global_sections(self, workspace):
        """Project-specific keys (agents, state_machine) should not interfere with global sections."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        # Global sections should remain intact
        assert cfg.llm.provider == "ollama"
        assert cfg.nats.url == "nats://localhost:4222"
        assert cfg.tasks.max_attempts_per_task == 5

    def test_deep_merge_does_not_replace_entire_section(self, workspace_override_tmux):
        """Deep merge must merge keys WITHIN a section, not replace the whole section."""
        cfg = load_config(root_dir=workspace_override_tmux, project_name="override-test")
        # Project sets session_name, global sets nudge_prompt, cooldown, max_retries
        # All must be present in the merged config
        assert hasattr(cfg.tmux, "session_name")
        assert hasattr(cfg.tmux, "nudge_prompt")
        assert hasattr(cfg.tmux, "nudge_cooldown_seconds")
        assert hasattr(cfg.tmux, "max_nudge_retries")

    def test_explicit_project_override_of_global_value(self, tmp_path):
        """When project explicitly overrides a global value, project wins."""
        # Global: nudge_cooldown_seconds = 30
        global_yaml = tmp_path / "config.yaml"
        global_yaml.write_text(
            "tmux:\n"
            "  nudge_cooldown_seconds: 30\n"
            "  nudge_prompt: global prompt\n"
        )

        # Project: nudge_cooldown_seconds = 60
        project_dir = tmp_path / "projects" / "custom"
        project_dir.mkdir(parents=True)
        (project_dir / "config.yaml").write_text(
            "project: custom\n"
            "tmux:\n"
            "  nudge_cooldown_seconds: 60\n"
            "agents:\n"
            "  worker:\n"
            "    runtime: script\n"
            "    command: echo\n"
            "state_machine:\n"
            "  initial: idle\n"
            "  states:\n"
            "    idle:\n"
            "      description: noop\n"
            "  transitions:\n"
            "    - from: idle\n"
            "      to: idle\n"
            "      trigger: task_assigned\n"
        )

        cfg = load_config(root_dir=tmp_path, project_name="custom")
        assert cfg.tmux.nudge_cooldown_seconds == 60  # project overrides
        assert cfg.tmux.nudge_prompt == "global prompt"  # inherited from global


# ===========================================================================
# 4. STRUCTURED CONFIG OBJECT (DOT-ACCESS)
# ===========================================================================


class TestStructuredConfigObject:
    """Config must be returned as a structured object, not a raw dict.

    From acceptance criteria:
    > Returns a structured config object (not raw dict) with dot-access
    > or clear accessors.
    """

    def test_config_is_not_a_raw_dict(self, workspace):
        """load_config must NOT return a plain dict."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert not isinstance(cfg, dict), "Config must be a structured object, not a raw dict"

    def test_dot_access_top_level(self, workspace):
        """Top-level sections should be accessible via dot notation."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        # These should work without KeyError
        _ = cfg.llm
        _ = cfg.nats
        _ = cfg.tasks
        _ = cfg.tmux

    def test_dot_access_nested(self, workspace):
        """Nested keys should be accessible via chained dot notation."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert cfg.llm.provider == "ollama"
        assert cfg.nats.stream == "AGENTS"
        assert cfg.tmux.nudge_cooldown_seconds == 30

    def test_agents_accessible_as_dict_like(self, workspace):
        """Agents should be accessible by name (dict-like access)."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        writer = cfg.agents["writer"]
        assert writer.runtime == "claude_code"

    def test_state_machine_transitions_is_list(self, workspace):
        """state_machine.transitions must be a list-like iterable."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        transitions = cfg.state_machine.transitions
        assert hasattr(transitions, "__iter__")
        assert len(transitions) >= 1

    def test_transition_properties_dot_access(self, workspace):
        """Individual transition objects should support dot-access."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        t = cfg.state_machine.transitions[0]
        assert t.trigger == "task_assigned"
        assert hasattr(t, "from") or hasattr(t, "from_state")  # 'from' is reserved, impl may rename


# ===========================================================================
# 5. ERROR HANDLING
# ===========================================================================


class TestErrorHandling:
    """Tests for error conditions and edge cases."""

    def test_missing_project_config_raises_error(self, workspace):
        """Must raise a clear error when project config file is missing."""
        with pytest.raises((FileNotFoundError, ConfigError)) as exc_info:
            load_config(root_dir=workspace, project_name="nonexistent")

        error_msg = str(exc_info.value)
        assert "nonexistent" in error_msg.lower() or "config" in error_msg.lower()

    def test_missing_project_config_error_includes_path(self, workspace):
        """Error message must include the expected file path."""
        with pytest.raises((FileNotFoundError, ConfigError)) as exc_info:
            load_config(root_dir=workspace, project_name="nonexistent")

        error_msg = str(exc_info.value)
        assert "projects" in error_msg or "nonexistent" in error_msg

    def test_missing_global_config_uses_empty_defaults(self, workspace_no_global):
        """When global config.yaml is missing, use empty defaults (don't crash)."""
        cfg = load_config(root_dir=workspace_no_global, project_name="demo")
        # Should still be a valid config object
        assert cfg.project == "demo"
        assert "writer" in cfg.agents

    def test_missing_global_config_global_sections_have_defaults(self, workspace_no_global):
        """When global config is missing, global-only sections should exist with safe defaults."""
        cfg = load_config(root_dir=workspace_no_global, project_name="demo")
        # These should not raise AttributeError -- they may be empty/default but must exist
        _ = cfg.llm
        _ = cfg.nats
        _ = cfg.tasks
        _ = cfg.tmux

    def test_empty_global_config_works(self, workspace_empty_global):
        """An empty global config.yaml (None after parse) should be handled gracefully."""
        cfg = load_config(root_dir=workspace_empty_global, project_name="demo")
        assert cfg.project == "demo"
        assert "writer" in cfg.agents

    def test_invalid_yaml_raises_error(self, tmp_path):
        """Invalid YAML in config should raise a clear error."""
        global_cfg = tmp_path / "config.yaml"
        global_cfg.write_text("this: is: not: valid: yaml: [[[")

        project_dir = tmp_path / "projects" / "bad"
        project_dir.mkdir(parents=True)
        (project_dir / "config.yaml").write_text(
            "project: bad\n"
            "agents:\n"
            "  worker:\n"
            "    runtime: script\n"
            "    command: echo\n"
            "state_machine:\n"
            "  initial: idle\n"
            "  states:\n"
            "    idle:\n"
            "      description: noop\n"
            "  transitions:\n"
            "    - from: idle\n"
            "      to: idle\n"
            "      trigger: task_assigned\n"
        )

        with pytest.raises(Exception):  # Could be yaml.YAMLError or ConfigError
            load_config(root_dir=tmp_path, project_name="bad")


# ===========================================================================
# 6. EDGE CASES
# ===========================================================================


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_project_with_no_overlapping_keys(self, workspace_minimal_project):
        """Project that defines no overlapping keys should get all global values intact."""
        cfg = load_config(root_dir=workspace_minimal_project, project_name="minimal")
        assert cfg.llm.provider == "ollama"
        assert cfg.nats.url == "nats://localhost:4222"
        assert cfg.tasks.max_attempts_per_task == 5
        assert cfg.tmux.nudge_cooldown_seconds == 30

    def test_merge_preserves_all_global_sections(self, workspace):
        """After merge, all global sections should still be present."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        # Verify all global sections
        assert hasattr(cfg, "llm")
        assert hasattr(cfg, "nats")
        assert hasattr(cfg, "tasks")
        assert hasattr(cfg, "tmux")

    def test_merge_preserves_all_project_sections(self, workspace):
        """After merge, all project sections should be present."""
        cfg = load_config(root_dir=workspace, project_name="demo")
        assert hasattr(cfg, "project")
        assert hasattr(cfg, "agents")
        assert hasattr(cfg, "state_machine")

    def test_load_config_returns_consistent_object(self, workspace):
        """Two calls with the same arguments should return equivalent configs."""
        cfg1 = load_config(root_dir=workspace, project_name="demo")
        cfg2 = load_config(root_dir=workspace, project_name="demo")
        assert cfg1.llm.provider == cfg2.llm.provider
        assert cfg1.tmux.session_name == cfg2.tmux.session_name
        assert cfg1.project == cfg2.project

    def test_root_dir_accepts_path_object(self, workspace):
        """load_config should accept both str and Path for root_dir."""
        cfg = load_config(root_dir=Path(workspace), project_name="demo")
        assert cfg.project == "demo"

    def test_root_dir_accepts_string(self, workspace):
        """load_config should accept string path for root_dir."""
        cfg = load_config(root_dir=str(workspace), project_name="demo")
        assert cfg.project == "demo"

    def test_deeply_nested_agent_config_preserved(self, tmp_path):
        """Agent configs with nested dicts should be preserved in merge."""
        global_cfg = tmp_path / "config.yaml"
        global_cfg.write_text("tmux:\n  nudge_prompt: default\n")

        project_dir = tmp_path / "projects" / "nested"
        project_dir.mkdir(parents=True)
        (project_dir / "config.yaml").write_text(
            "project: nested\n"
            "agents:\n"
            "  writer:\n"
            "    runtime: claude_code\n"
            "    working_dir: /tmp/test\n"
            "    env:\n"
            "      FOO: bar\n"
            "      BAZ: qux\n"
            "state_machine:\n"
            "  initial: idle\n"
            "  states:\n"
            "    idle:\n"
            "      description: noop\n"
            "  transitions:\n"
            "    - from: idle\n"
            "      to: idle\n"
            "      trigger: task_assigned\n"
        )

        cfg = load_config(root_dir=tmp_path, project_name="nested")
        writer = cfg.agents["writer"]
        assert writer.runtime == "claude_code"


# ===========================================================================
# 7. ConfigError CUSTOM EXCEPTION
# ===========================================================================


class TestConfigError:
    """ConfigError should be a proper exception class importable from orchestrator.config."""

    def test_config_error_is_exception(self):
        """ConfigError must be a subclass of Exception."""
        assert issubclass(ConfigError, Exception)

    def test_config_error_has_message(self):
        """ConfigError should accept and store a message."""
        err = ConfigError("test message")
        assert "test message" in str(err)
