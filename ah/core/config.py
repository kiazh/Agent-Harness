"""Configuration system — YAML file + env var overrides + per-session overrides.

Config file: ~/.agent-harness/config.yaml
Environment variables: AGENT_HARNESS_<KEY> override file values.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".agent-harness" / "config.yaml"

# Sensible defaults for all settings
DEFAULTS: dict[str, Any] = {
    "model": "anthropic/claude-3.5-sonnet",
    "provider": "openrouter",
    "context_budget": 8000,
    "max_iterations": 10,
    "verbose": True,
    "agent_id": "harness",
    "temperature": 0.7,
    "max_tokens": 4096,
    "rate_limit_calls_per_minute": 10,
    "memory_enabled": True,
    "rag_enabled": True,
    "streaming": True,
    "theme": "default",
    "history_size": 100,
    # API keys and service URLs
    "openrouter_api_key": "",
    "openai_api_key": "",
    "cohere_api_key": "",
    "database_url": "",
    "searxng_url": "http://localhost:8080",
    "agent_harness_home": "",
    "tmpdir": "",
    "temp": "",
}

# Map config keys to their legacy environment variable names
LEGACY_ENV_VARS: dict[str, str] = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "cohere_api_key": "COHERE_API_KEY",
    "database_url": "DATABASE_URL",
    "searxng_url": "SEARXNG_URL",
    "agent_harness_home": "AGENT_HARNESS_HOME",
    "tmpdir": "TMPDIR",
    "temp": "TEMP",
}


@dataclass
class Config:
    """AgentHarness configuration.

    Values are resolved in priority order:
    1. Per-session overrides (highest)
    2. Environment variables (AGENT_HARNESS_<KEY>)
    3. Config file (~/.agent-harness/config.yaml)
    4. Defaults (lowest)
    """

    model: str = "anthropic/claude-3.5-sonnet"
    provider: str = "openrouter"
    context_budget: int = 8000
    max_iterations: int = 10
    verbose: bool = True
    agent_id: str = "harness"
    temperature: float = 0.7
    max_tokens: int = 4096
    rate_limit_calls_per_minute: int = 10
    memory_enabled: bool = True
    rag_enabled: bool = True
    streaming: bool = True
    theme: str = "default"
    history_size: int = 100
    # API keys and service URLs
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    cohere_api_key: str = ""
    database_url: str = ""
    searxng_url: str = "http://localhost:8080"
    agent_harness_home: str = ""
    tmpdir: str = ""
    temp: str = ""

    # Per-session overrides (not persisted to file)
    _session_overrides: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str) -> Any:
        """Get a config value, checking session overrides first, then legacy env vars."""
        if key in self._session_overrides:
            return self._session_overrides[key]
        value = getattr(self, key, None)
        if value is not None and value != "":
            return value
        # Check legacy environment variable
        if key in LEGACY_ENV_VARS:
            env_val = os.environ.get(LEGACY_ENV_VARS[key])
            if env_val is not None:
                return env_val
        return DEFAULTS.get(key)

    def set(self, key: str, value: Any, persist: bool = False) -> None:
        """Set a config value.

        Args:
            key: Config key to set.
            value: New value.
            persist: If True, write to config file. If False, only set in-memory.
        """
        if key.startswith("_"):
            raise ValueError(f"Cannot set private key: {key}")

        # Type coercion based on defaults
        if key in DEFAULTS:
            default_value = DEFAULTS[key]
            try:
                if isinstance(default_value, bool):
                    if isinstance(value, str):
                        value = value.lower() in ("true", "1", "yes", "on")
                    else:
                        value = bool(value)
                elif isinstance(default_value, int):
                    value = int(value)
                elif isinstance(default_value, float):
                    value = float(value)
                else:
                    value = str(value)
            except (ValueError, TypeError):
                pass  # Keep original value if coercion fails

        setattr(self, key, value)

        if persist:
            self.save()

    def set_session_override(self, key: str, value: Any) -> None:
        """Set a per-session override (not persisted to file)."""
        self._session_overrides[key] = value

    def clear_session_override(self, key: str) -> None:
        """Clear a per-session override."""
        self._session_overrides.pop(key, None)

    def clear_all_session_overrides(self) -> None:
        """Clear all per-session overrides."""
        self._session_overrides.clear()

    def to_dict(self) -> dict[str, Any]:
        """Export config as a dict (excluding private fields)."""
        result = {}
        for key in DEFAULTS:
            result[key] = self.get(key)
        return result

    def save(self, path: str | Path | None = None) -> None:
        """Save config to YAML file."""
        save_path = Path(path) if path else DEFAULT_CONFIG_PATH
        save_path.parent.mkdir(parents=True, exist_ok=True)

        data = {}
        for key in DEFAULTS:
            data[key] = getattr(self, key)

        with open(save_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=True)

        logger.debug("Config saved to %s", save_path)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load config from YAML file, merging with env vars and defaults."""
        config = Config()
        load_path = Path(path) if path else DEFAULT_CONFIG_PATH

        # Load from file if it exists
        if load_path.exists():
            try:
                with open(load_path) as f:
                    file_data = yaml.safe_load(f) or {}
                for key, value in file_data.items():
                    if key in DEFAULTS:
                        setattr(config, key, value)
                logger.debug("Config loaded from %s", load_path)
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", load_path, e)

        # Override with environment variables (AGENT_HARNESS_<KEY>)
        for key in DEFAULTS:
            env_key = f"AGENT_HARNESS_{key.upper()}"
            env_value = os.environ.get(env_key)
            if env_value is not None:
                # Coerce type from default
                default_value = DEFAULTS[key]
                try:
                    if isinstance(default_value, bool):
                        env_value = env_value.lower() in ("true", "1", "yes", "on")
                    elif isinstance(default_value, int):
                        env_value = int(env_value)
                    elif isinstance(default_value, float):
                        env_value = float(env_value)
                except (ValueError, TypeError):
                    pass
                setattr(config, key, env_value)

        # Override with legacy environment variables (OPENROUTER_API_KEY, etc.)
        for key, env_name in LEGACY_ENV_VARS.items():
            env_value = os.environ.get(env_name)
            if env_value is not None:
                setattr(config, key, env_value)

        return config

    @classmethod
    def reset(cls) -> None:
        """Reset the global config singleton."""
        global config
        cls._instance = None


# Global singleton
config = Config.load()
