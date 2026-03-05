"""Config Loader for Multi-Agent System Shell.

Loads global and project YAML configs, deep-merges them (two levels),
and returns a structured dot-access config object.

API:
    from orchestrator.config import load_config, ConfigError
    cfg = load_config(root_dir=path, project_name="demo")
"""

import yaml
from pathlib import Path


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""
    pass


class ConfigNode:
    """Structured config object supporting dot-access on attributes."""

    def __init__(self, data=None):
        if data is None:
            data = {}
        for key, value in data.items():
            setattr(self, key, _wrap(value))

    def __contains__(self, key):
        return hasattr(self, key)

    def __getitem__(self, key):
        return getattr(self, key)

    def __repr__(self):
        return f"ConfigNode({vars(self)})"


def _wrap(value):
    """Recursively wrap dicts into ConfigNodes and lists of dicts into lists of ConfigNodes."""
    if isinstance(value, dict):
        return ConfigNode(value)
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


def _deep_merge(base, override):
    """Deep merge two dicts, two levels deep.

    For each key in override:
    - If both base[key] and override[key] are dicts, merge their inner keys
    - Otherwise, override[key] wins
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


def _load_yaml(path):
    """Load a YAML file, raising ConfigError on parse failure."""
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}")


def load_config(root_dir, project_name):
    """Load and merge global + project configs into a structured object.

    Args:
        root_dir: Path to workspace root (str or Path).
        project_name: Name of the project subdirectory under projects/.

    Returns:
        A structured config object (not a raw dict) with dot-access.

    Raises:
        ConfigError: If project config is missing or YAML is invalid.
    """
    root_dir = Path(root_dir)

    # --- Global config (optional) ---
    global_path = root_dir / "config.yaml"
    global_data = {}
    if global_path.exists():
        parsed = _load_yaml(global_path)
        if parsed is not None:
            global_data = parsed

    # --- Project config (required) ---
    project_path = root_dir / "projects" / project_name / "config.yaml"
    if not project_path.exists():
        raise ConfigError(
            f"Project config not found: {project_path} "
            f"(project '{project_name}' does not exist)"
        )

    parsed = _load_yaml(project_path)
    project_data = parsed if parsed is not None else {}

    # --- Deep merge: project overrides global ---
    merged = _deep_merge(global_data, project_data)

    # Ensure project name is always set
    merged["project"] = project_name

    # --- Build agents as a plain dict of ConfigNodes ---
    agents_raw = merged.pop("agents", {})
    agents = {name: _wrap(agent_data) for name, agent_data in agents_raw.items()}

    # --- Build the top-level config object ---
    cfg = ConfigNode(merged)

    # Attach agents as a plain dict (supports `in` and `[]`)
    cfg.agents = agents

    # Ensure standard global sections exist even if missing from both configs
    for section in ("llm", "nats", "tasks", "tmux"):
        if not hasattr(cfg, section):
            setattr(cfg, section, ConfigNode({}))

    return cfg
