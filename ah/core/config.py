"""Configuration system — YAML file + env var overrides + per-session overrides.

Config file: ~/.agent-harness/config.yaml
Environment variables: AGENT_HARNESS_<KEY> override file values.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ah.observability.diagnostics import record_failure

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".agent-harness" / "config.yaml"

# Secrets live in the environment (.env), never in config.yaml.
SECRET_KEYS = frozenset(
    {
        "openrouter_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "mistral_api_key",
        "groq_api_key",
        "together_api_key",
        "deepseek_api_key",
        "xai_api_key",
        "cohere_api_key",
        "database_url",
    }
)


# Load the nearest .env before the config singleton below is built. Uses the
# shared resolver contract (AH-028): $AH_ENV_FILE override, else nearest
# .env upward, else repo .env — the same path secret management writes to.
# Real environment variables always win over .env values.
def _startup_env_path() -> str | None:
    from ah.security.env_file import find_env_file

    try:
        return str(find_env_file())
    except (OSError, ValueError):
        logger.warning("environment file could not be resolved")
        return None


try:
    _env_path = _startup_env_path()
    if _env_path:
        load_dotenv(_env_path, override=False)
    else:
        load_dotenv(override=False)
except Exception:
    # Startup must never crash because an override path is unreadable;
    # secret lookup will surface a clear error later.
    try:
        load_dotenv(override=False)
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("config.module", _boundary_error)

__all__ = [
    "Config",
    "DEFAULT_CONFIG_PATH",
    "SECRET_KEYS",
    "LEGACY_ENV_VARS",
    "DEFAULTS",
    "get_config",
    "validate_value",
]

# Sensible defaults for all settings
DEFAULTS: dict[str, Any] = {
    "model": "openrouter/free",
    "provider": "openrouter",
    "context_budget": 8000,
    "max_iterations": 10,
    "turn_timeout": 300,
    "verbose": True,
    "agent_id": "harness",
    "temperature": 0.7,
    "max_tokens": 4096,
    # Reasoning depth for providers that support it: "" (provider default),
    # "low", "medium" or "high". Sent as reasoning_effort (OpenAI, DeepSeek,
    # xAI, Groq, Together), reasoning.effort (OpenRouter), a thinking budget
    # (Anthropic) or the think flag (Ollama); ignored where unsupported.
    "reasoning_effort": "",
    "rate_limit_calls_per_minute": 10,
    # Zero disables a budget. Limits include requests that fail after reaching
    # the provider; unknown token usage retains its pre-call reservation.
    "usage_session_token_limit": 0,
    "usage_session_request_limit": 0,
    "usage_agent_token_limit": 0,
    "usage_agent_request_limit": 0,
    "memory_enabled": True,
    "persona_memory_enabled": True,
    "persona_default_emotion": "trust",
    "learning_review_enabled": False,
    "learning_review_max_per_session": 3,
    "rag_enabled": True,
    "streaming": True,
    "theme": "default",
    "history_size": 100,
    # API keys and service URLs
    "openrouter_api_key": "",
    "openai_api_key": "",
    "anthropic_api_key": "",
    "google_api_key": "",
    "mistral_api_key": "",
    "groq_api_key": "",
    "together_api_key": "",
    "deepseek_api_key": "",
    "xai_api_key": "",
    "cohere_api_key": "",
    "database_url": "",
    "searxng_url": "http://localhost:8080",
    "agent_harness_home": "",
    "tmpdir": "",
    "temp": "",
    # Context compression settings
    "compression_enabled": True,
    "compression_threshold": 0.8,
    "compression_target_ratio": 0.5,
    "compression_preserve_recent": 3,
    "compression_llm_summarize": True,
    # Execution modes (Phase 5): ask | workspace | sandbox | full.
    "execution_mode": "ask",
    "workspace_root": "",
    "terminal_sandbox": "disabled",
    "terminal_image": "agent-harness-tool-sandbox:latest",
    "approval_timeout": 300,
    # Feature integration toggles with real enforcement.
    "memory_consolidation_enabled": True,
    "auto_compaction_enabled": True,
    "eviction_max_tokens": 0,
    "compaction_check_enabled": True,
}

# Map config keys to their legacy environment variable names
LEGACY_ENV_VARS: dict[str, str] = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
    "mistral_api_key": "MISTRAL_API_KEY",
    "groq_api_key": "GROQ_API_KEY",
    "together_api_key": "TOGETHER_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "xai_api_key": "XAI_API_KEY",
    "cohere_api_key": "COHERE_API_KEY",
    "database_url": "DATABASE_URL",
    "searxng_url": "SEARXNG_URL",
    "agent_harness_home": "AGENT_HARNESS_HOME",
    "tmpdir": "TMPDIR",
    "temp": "TEMP",
}


def validate_value(key: str, value: Any) -> Any:
    """Coerce and bounds-check *value* for *key* (AH-AUDIT-042).

    Returns the coerced value. Raises ValueError with an actionable message
    on failure — callers must not mutate state or files when this raises.
    Rejects NaN/infinity, out-of-range numbers, and unknown enum members
    across CLI/gateway/HTTP/config-file/env paths. Intentional
    zero/unlimited semantics (usage_*_limit == 0) are preserved.
    """
    if key == "execution_mode":
        normalized = str(value).strip().lower()
        if normalized not in ("ask", "workspace", "sandbox", "full"):
            raise ValueError("execution_mode must be ask|workspace|sandbox|full")
        return normalized
    if key == "terminal_sandbox":
        normalized = str(value).strip().lower()
        if normalized not in ("disabled", "local", "docker"):
            raise ValueError("terminal_sandbox must be disabled|local|docker")
        return normalized
    if key == "reasoning_effort":
        normalized = str(value).strip().lower()
        if normalized not in ("", "low", "medium", "high"):
            raise ValueError("reasoning_effort must be ''|low|medium|high")
        return normalized
    if key not in DEFAULTS:
        return value
    default_value = DEFAULTS[key]
    try:
        if isinstance(default_value, bool):
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes", "on"):
                    return True
                if lowered in ("false", "0", "no", "off"):
                    return False
                raise ValueError(f"{key} must be a boolean, got {value!r}")
            return bool(value)
        if isinstance(default_value, int) and not isinstance(default_value, bool):
            coerced = int(str(value).strip()) if isinstance(value, str) else int(value)
            _check_int_bounds(key, coerced)
            return coerced
        if isinstance(default_value, float):
            coerced_f = float(str(value).strip()) if isinstance(value, str) else float(value)
            import math as _math

            if not _math.isfinite(coerced_f):
                raise ValueError(f"{key} must be finite, got {value!r}")
            _check_float_bounds(key, coerced_f)
            return coerced_f
        return str(value)
    except ValueError as e:
        if str(e).startswith(key):
            raise
        raise ValueError(f"{key} must be {type(default_value).__name__}, got {value!r}") from e
    except (TypeError, AttributeError) as e:
        raise ValueError(f"{key} must be {type(default_value).__name__}, got {value!r}") from e


def _check_int_bounds(key: str, val: int) -> None:
    bounds: dict[str, tuple[int, int | None]] = {
        "turn_timeout": (1, 3600),
        "approval_timeout": (1, 3600),
        "context_budget": (100, None),
        "max_tokens": (1, None),
        "max_iterations": (1, 100),
        "rate_limit_calls_per_minute": (1, None),
        "usage_session_token_limit": (0, None),
        "usage_session_request_limit": (0, None),
        "usage_agent_token_limit": (0, None),
        "usage_agent_request_limit": (0, None),
        "history_size": (1, 10000),
        "compression_preserve_recent": (0, 100),
        "learning_review_max_per_session": (0, 100),
        "eviction_max_tokens": (0, None),
    }
    if key in bounds:
        lo, hi = bounds[key]
        if val < lo or (hi is not None and val > hi):
            hi_text = f"..{hi}" if hi is not None else "+"
            raise ValueError(f"{key} must be in range {lo}{hi_text}, got {val}")


def _check_float_bounds(key: str, val: float) -> None:
    if key == "temperature" and not 0.0 <= val <= 2.0:
        raise ValueError(f"temperature must be in range 0.0..2.0, got {val}")
    if key in ("compression_threshold", "compression_target_ratio") and not 0.0 < val <= 1.0:
        raise ValueError(f"{key} must be in range 0.0<..1.0, got {val}")


@dataclass
class Config:
    """AgentHarness configuration.

    Values are resolved in priority order:
    1. Per-session overrides (highest)
    2. Environment variables (AGENT_HARNESS_<KEY>)
    3. Config file (~/.agent-harness/config.yaml)
    4. Defaults (lowest)
    """

    model: str = "openrouter/free"
    provider: str = "openrouter"
    context_budget: int = 8000
    max_iterations: int = 10
    turn_timeout: int = 300
    verbose: bool = True
    agent_id: str = "harness"
    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str = ""
    rate_limit_calls_per_minute: int = 10
    usage_session_token_limit: int = 0
    usage_session_request_limit: int = 0
    usage_agent_token_limit: int = 0
    usage_agent_request_limit: int = 0
    memory_enabled: bool = True
    persona_memory_enabled: bool = True
    persona_default_emotion: str = "trust"
    learning_review_enabled: bool = False
    learning_review_max_per_session: int = 3
    rag_enabled: bool = True
    streaming: bool = True
    theme: str = "default"
    history_size: int = 100
    # API keys and service URLs
    openrouter_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    google_api_key: str = field(default="", repr=False)
    mistral_api_key: str = field(default="", repr=False)
    groq_api_key: str = field(default="", repr=False)
    together_api_key: str = field(default="", repr=False)
    deepseek_api_key: str = field(default="", repr=False)
    xai_api_key: str = field(default="", repr=False)
    cohere_api_key: str = field(default="", repr=False)
    database_url: str = field(default="", repr=False)
    searxng_url: str = "http://localhost:8080"
    agent_harness_home: str = ""
    tmpdir: str = ""
    temp: str = ""
    # Context compression settings
    compression_enabled: bool = True
    compression_threshold: float = 0.8
    compression_target_ratio: float = 0.5
    compression_preserve_recent: int = 3
    compression_llm_summarize: bool = True
    # Execution modes (Phase 5)
    execution_mode: str = "ask"
    workspace_root: str = ""
    terminal_sandbox: str = "disabled"
    terminal_image: str = "agent-harness-tool-sandbox:latest"
    approval_timeout: int = 300
    memory_consolidation_enabled: bool = True
    auto_compaction_enabled: bool = True
    eviction_max_tokens: int = 0
    compaction_check_enabled: bool = True

    # Per-session overrides (not persisted to file)
    _session_overrides: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str) -> Any:
        """Live precedence: override, explicit env, legacy env, backend, file, default."""
        if key in self._session_overrides:
            return validate_value(key, self._session_overrides[key])
        explicit = f"AGENT_HARNESS_{key.upper()}"
        legacy = LEGACY_ENV_VARS.get(key)
        names = (explicit, legacy) if legacy else (explicit,)
        if key in SECRET_KEYS:
            from ah.security.secrets import get_secret

            secret = get_secret(legacy or explicit, env_names=names)
            if secret is not None:
                return secret
        else:
            for name in names:
                value = os.environ.get(name)
                if value is not None:
                    return validate_value(key, value)
        value = getattr(self, key, None)
        return value if value is not None and value != "" else DEFAULTS.get(key)

    def set(self, key: str, value: Any, persist: bool = False) -> None:
        """Set a config value.

        Args:
            key: Config key to set.
            value: New value.
            persist: If True, write to config file. If False, only set in-memory.

        AH-AUDIT-042: validation is centralized in validate_value(): a
        failure raises with an actionable error and mutates nothing — no
        partial state, no partial file writes.
        """
        if key.startswith("_"):
            raise ValueError(f"Cannot set private key: {key}")
        coerced = validate_value(key, value)
        previous = getattr(self, key, None)
        setattr(self, key, coerced)
        if persist:
            try:
                self.save()
            except Exception:
                setattr(self, key, previous)
                raise

    def to_dict(self) -> dict[str, Any]:
        """Export config as a dict (excluding private fields)."""
        result = {}
        for key in DEFAULTS:
            result[key] = "" if key in SECRET_KEYS else self.get(key)
        return result

    def save(self, path: str | Path | None = None) -> None:
        """Save config to YAML file."""
        save_path = Path(path) if path else DEFAULT_CONFIG_PATH
        save_path.parent.mkdir(parents=True, exist_ok=True)

        data = {}
        for key in DEFAULTS:
            # Secrets belong in .env / the environment, not a plain-text file.
            data[key] = "" if key in SECRET_KEYS else getattr(self, key)

        with open(save_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=True)

        logger.debug("Config saved to %s", save_path)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load config from YAML file, merging with env vars and defaults."""
        config = Config()
        load_path = Path(path) if path else DEFAULT_CONFIG_PATH

        # Load from file if it exists. AH-AUDIT-042: invalid values are
        # rejected with a warning and the default kept — never activated.
        if load_path.exists():
            try:
                with open(load_path) as f:
                    file_data = yaml.safe_load(f) or {}
                for key, value in file_data.items():
                    if key in DEFAULTS and key not in SECRET_KEYS:
                        try:
                            setattr(config, key, validate_value(key, value))
                        except ValueError as e:
                            logger.warning(
                                "Ignoring invalid config %s in %s: %s", key, load_path, e
                            )
                logger.debug("Config loaded from %s", load_path)
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", load_path, e)

        # Environment and backends are resolved live by get(), so rotation
        # never leaves a stale credential copied into file configuration.
        return config

    @classmethod
    def reset(cls) -> None:
        """Reset the global config singleton."""
        global _config
        _config = None


# Lazy singleton: the config is only loaded on first access, not at import
# time.  This avoids side-effects (file I/O, env-var reads) when the module
# is merely imported (e.g. by tests or by tools that only need DEFAULTS).
_config: Config | None = None


def get_config() -> Config:
    """Return the global config singleton, loading it on first call."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def __getattr__(name: str) -> Any:
    """PEP 562 module-level __getattr__ for lazy config access.

    ``from ah.core.config import config`` continues to work, but the actual
    Config.load() call is deferred until the attribute is first accessed.
    """
    if name == "config":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
