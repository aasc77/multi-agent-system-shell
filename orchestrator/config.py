"""Config Loader for Multi-Agent System Shell.

Loads global and project YAML configs, deep-merges them (two levels),
and returns a structured dot-access config object.

Merge strategy (PRD R9):
    Project config overrides global. Two levels deep -- e.g., project
    ``tmux.session_name`` overrides global ``tmux.session_name`` but
    inherits ``tmux.nudge_prompt`` if not specified by the project.

API::

    from orchestrator.config import load_config, ConfigError
    cfg = load_config(root_dir=path, project_name="demo")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = ["load_config", "ConfigError", "ConfigNode"]

# Sections that the global config may define and that MUST always exist
# on the returned config object (even if empty).
_DEFAULT_GLOBAL_SECTIONS: tuple[str, ...] = ("llm", "nats", "tasks", "tmux")


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


class ConfigNode:
    """Structured config object supporting dot-access on attributes.

    Wraps a flat ``dict`` so that keys become attributes accessible via
    ``node.key`` (dot notation) or ``node["key"]`` (bracket notation).
    Membership can be tested with ``"key" in node``.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        if data is None:
            data = {}
        for key, value in data.items():
            setattr(self, key, _to_config_node(value))

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __repr__(self) -> str:
        return f"ConfigNode({vars(self)})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_config_node(value: Any) -> Any:
    """Recursively convert raw Python values into :class:`ConfigNode` trees.

    * ``dict`` → ``ConfigNode``
    * ``list`` → list with each element converted recursively
    * everything else → returned as-is
    """
    if isinstance(value, dict):
        return ConfigNode(value)
    if isinstance(value, list):
        return [_to_config_node(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Two-level deep merge of *override* into *base*.

    For each key in *override*:

    * If both ``base[key]`` and ``override[key]`` are ``dict``s, their
      inner keys are merged (one level down).
    * Otherwise the value from *override* wins outright.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            merged_section = dict(result[key])
            merged_section.update(value)
            result[key] = merged_section
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict[str, Any] | None:
    """Load and parse a YAML file.

    Returns:
        The parsed mapping, or ``None`` if the file is empty.

    Raises:
        ConfigError: If the file contains invalid YAML.
    """
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}")


def _yaml_or_empty(path: Path) -> dict[str, Any]:
    """Load a YAML file, coalescing ``None`` (empty file) to ``{}``."""
    parsed = _load_yaml(path)
    return parsed if parsed is not None else {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(root_dir: str | Path, project_name: str) -> ConfigNode:
    """Load and merge global + project configs into a structured object.

    Args:
        root_dir: Path to workspace root (accepts ``str`` or ``Path``).
        project_name: Name of the project subdirectory under ``projects/``.

    Returns:
        A :class:`ConfigNode` with dot-access attributes.  The ``agents``
        attribute is a plain ``dict[str, ConfigNode]`` keyed by agent name.

    Raises:
        ConfigError: If the project config is missing or any YAML is invalid.
    """
    root_dir = Path(root_dir)

    # --- Global config (optional) ---
    global_path = root_dir / "config.yaml"
    global_data: dict[str, Any] = (
        _yaml_or_empty(global_path) if global_path.exists() else {}
    )

    # --- Project config (required) ---
    project_path = root_dir / "projects" / project_name / "config.yaml"
    if not project_path.exists():
        raise ConfigError(
            f"Project config not found: {project_path} "
            f"(project '{project_name}' does not exist)"
        )
    project_data = _yaml_or_empty(project_path)

    # --- Deep merge: project overrides global ---
    merged = _deep_merge(global_data, project_data)

    # Ensure project name is always set
    merged["project"] = project_name

    # --- Build agents as a plain dict of ConfigNodes ---
    agents_raw: dict[str, Any] = merged.pop("agents", {})
    agents = {
        name: _to_config_node(agent_data)
        for name, agent_data in agents_raw.items()
    }

    # --- Build the top-level config object ---
    cfg = ConfigNode(merged)
    cfg.agents = agents

    # Ensure standard global sections exist even if missing from both configs
    for section in _DEFAULT_GLOBAL_SECTIONS:
        if not hasattr(cfg, section):
            setattr(cfg, section, ConfigNode({}))

    return cfg
