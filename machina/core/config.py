"""Configuration system — YAML file + env var overrides + per-session overrides.

Config file: ~/.machina/config.yaml
Environment variables: MACHINA_<KEY> override file values.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from machina.observability.diagnostics import record_failure
from machina.security.env_file import find_env_file, read_env_values, set_env_values

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".machina" / "config.yaml"

# Canonical reasoning-effort vocabulary — the single source of truth shared by
# config validation and provider normalization (machina.core.provider.reasoning
# derives its exported set from these; config never imports the provider
# package, so the dependency stays acyclic).
#  * REASONING_EFFORTS: tiers every provider transport can map directly
#    (low|medium|high: OpenAI-style top-level param, OpenRouter
#    reasoning.effort, Anthropic thinking budget, Ollama think flag).
#  * REASONING_CATALOG_EFFORTS: wider tiers the ChatGPT catalog validates per
#    model; stored only after that validation. Transports without a mapping
#    ignore them (provider default), exactly as before.
#  * REASONING_UNSET_EFFORTS: aliases meaning "provider default" -> "".
REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
REASONING_CATALOG_EFFORTS: tuple[str, ...] = ("minimal", "xhigh", "max")
REASONING_UNSET_EFFORTS: tuple[str, ...] = ("", "none", "off", "default", "auto")

# Single output-token ceiling shared with the provider transports. Every
# provider validates ``max_tokens`` against it at request time
# (machina.core.provider.transport._validate_params), so configuration accepts
# exactly the same executable range — a saved value above it could never reach
# the provider (H10 holistic review). Defined here because transport already
# imports config; config never imports the provider stack.
MAX_TOKENS = 32768

# Upper bound (UTF-8 bytes) for the versioned workspace UI state JSON string
# (plan §21). The state holds layout/URIs only and is small by construction;
# the bound keeps a runaway client from growing config.yaml without limit.
WORKSPACE_UI_STATE_MAX_BYTES = 64 * 1024

# Upper bound (UTF-8 bytes) for the global UI preferences JSON document
# (editor prefs such as syntax highlighting / soft wrap / tab width). Small by
# construction; the bound keeps a runaway client from growing config.yaml.
UI_PREFERENCES_MAX_BYTES = 16 * 1024

# Upper bound (UTF-8 bytes) for pricing_table_json (optional per-provider
# price overrides used by cost estimates). Must parse as a JSON object when
# set; the bound keeps a runaway client from growing config.yaml.
PRICING_TABLE_MAX_BYTES = 32 * 1024

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
        "opencode_api_key",
        "kimi_api_key",
        "custom_api_key",
        "cohere_api_key",
        "database_url",
    }
)


# Load the nearest .env before the config singleton below is built. Uses the
# shared resolver contract (AH-028): $MACHINA_ENV_FILE override, else nearest
# .env upward, else repo .env — the same path secret management writes to.
# Real environment variables always win over .env values.
def _startup_env_path() -> str | None:
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
    "MAX_TOKENS",
    "WORKSPACE_UI_STATE_MAX_BYTES",
    "UI_PREFERENCES_MAX_BYTES",
    "PRICING_TABLE_MAX_BYTES",
    "REASONING_EFFORTS",
    "REASONING_CATALOG_EFFORTS",
    "REASONING_UNSET_EFFORTS",
    "get_config",
    "persist_provider_fallback",
    "validate_value",
]

# Sensible defaults for all settings
DEFAULTS: dict[str, Any] = {
    "model": "openrouter/free",
    "provider": "openrouter",
    # Legacy storage/config compatibility only; runtime uses model context capacity.
    "context_budget": 8000,
    "turn_timeout": 300,
    "agent_id": "harness",
    "temperature": 0.7,
    "max_tokens": 4096,
    # Reasoning depth for providers that support it: "" (provider default),
    # "low"/"medium"/"high" wire-wide, and "xhigh"/"max" for the ChatGPT
    # catalog tiers (per-model validated by the chatgpt provider). Sent as
    # reasoning_effort (OpenAI, DeepSeek, xAI, Groq, Together),
    # reasoning.effort (OpenRouter), a thinking budget (Anthropic) or the
    # think flag (Ollama); ignored where unsupported.
    "reasoning_effort": "",
    "rate_limit_calls_per_minute": 10,
    # Zero disables a budget. Limits include requests that fail after reaching
    # the provider; unknown token usage retains its pre-call reservation.
    "usage_session_token_limit": 0,
    "usage_session_request_limit": 0,
    "usage_agent_token_limit": 0,
    "usage_agent_request_limit": 0,
    "memory_enabled": True,
    "personality_growth_enabled": True,
    "rsi_enabled": True,
    "persona_description": "",
    "persona_memory_enabled": True,
    "persona_default_emotion": "trust",
    "learning_review_enabled": False,
    "learning_review_max_per_session": 3,
    # Post-turn learning publishes its own bounded, source-tagged skills:
    # auto-applied skills are enabled on creation and participate in normal
    # trigger injection; there is no standing human approval gate. Post-hoc
    # review + revert (machina/skills/learning.py) is the correction path, and
    # a precision auto-disable flips the reviewer back to stage-only when
    # auto-applied proposals turn bad.
    "learning_review_auto_apply": True,
    # Rolling 24h auto-apply cap per agent; counts only auto-applied reviews.
    # 0 disables auto-apply (proposals are only staged for manual approval).
    "learning_review_auto_apply_per_day": 5,
    # Stage-aware instruction disclosure planner (spec §10/A3/A4). ON: the
    # disclosure path serves current-stage sections + prerequisites, keyed by
    # the §11 revision identities; OFF restores the pre-A3 whole-body excerpt
    # path (one-key rollback; read lazily by runtime._disclosure_planner_enabled).
    "skills_disclosure_planner": True,
    "rag_enabled": True,
    # Embedding backend for dense retrieval (RAG documents + memory).
    # Default "off": the LoCoMo archive replay showed keyword-only (recall@5
    # 0.425/MRR 0.345) beating static-model hybrids (potion 0.422/MRR 0.316)
    # and the bge-small ONNX hybrid (0.415/MRR 0.312) at k=5 — dense stays
    # opt-in until a model clears the go/no-go bars.
    # "auto": explicit embedding_base_url → onnx → model2vec → OpenAI/
    # OpenRouter key → keyword-only (local-first when enabled).
    # "onnx" forces fastembed, "local" forces model2vec, "openai" forces the
    # API adapter.
    "embedding_backend": "off",
    # Model id for the selected backend (empty = backend default:
    # text-embedding-3-small for OpenAI, BAAI/bge-small-en-v1.5 for onnx,
    # minishlab/potion-base-8M for local).
    "embedding_model": "",
    # Explicit OpenAI-compatible endpoint (llama.cpp, Ollama, LM Studio, ...).
    "embedding_base_url": "",
    # Cross-encoder reranker for retrieved candidates (local-first, zero-API).
    # Default "none": the local ONNX MiniLM-L12 reranker improved every
    # LoCoMo metric but landed below the default-on bars (keyword-only
    # Δrecall@5 +0.015, ΔMRR +0.022, rerank p50 ~250 ms) — enable with
    # "local" (or "cohere"/"http" for a llama.cpp /v1/rerank server).
    # "auto" = local ONNX when installed, then a Cohere key, else passthrough.
    # Local reranking keeps query/candidate text on-host.
    "reranker_backend": "none",
    # Local ONNX reranker model (empty = Xenova/ms-marco-MiniLM-L-12-v2).
    # Multilingual option: BAAI/bge-reranker-v2-m3-int8 (~0.6 GB).
    "reranker_local_model": "",
    # Local llama.cpp /v1/rerank endpoint used by the "http" backend
    # (e.g. http://127.0.0.1:8080 with `llama-server --reranking`).
    "reranker_http_url": "",
    "reranker_http_model": "",
    "reranker_timeout": 10.0,
    # RRF fusion weights for hybrid search (dense vs keyword branches).
    # Measured on LoCoMo with bge-small-ONNX: at 1.0 the dense branch drags
    # hybrid MRR −0.033 below keyword-only; 0.25 keeps recall +0.013 while
    # limiting the MRR trail to −0.011. Weights apply only when dense is
    # enabled (embedding_backend is "off" by default).
    "rrf_keyword_weight": 1.0,
    "rrf_dense_weight": 0.25,
    # PILOT (default off): attach Anthropic-style ephemeral prompt-cache
    # breakpoints to the two stable-prefix ends — the final system content
    # block and the last tool schema entry. Provider-dependent: honored by
    # AnthropicProvider and by OpenRouter requests routed to Anthropic
    # ("anthropic" in the model id); Direct providers and Ollama ignore it.
    # Expected saving is unmeasured and bounded by each model's minimum
    # cacheable prompt length plus real-turn prefix stability.
    "prompt_cache_enabled": False,
    "theme": "codex",
    # Transparent canvas: ON renders the UI on the terminal's own default
    # background (no background SGR anywhere; foreground styling is unchanged
    # and selection rows switch to reverse video). OFF (default) keeps the
    # opaque theme canvas. The UI reads it at boot (env override
    # MACHINA_TRANSPARENT_BACKGROUND applied before the first frame) and via
    # the /settings Appearance row.
    "transparent_background": False,
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
    "opencode_api_key": "",
    "kimi_api_key": "",
    # Generic OpenAI-compatible provider ("custom"): any base URL (LM Studio,
    # vLLM, llama.cpp, the Hermes loopback proxy, ...). The key is optional —
    # local endpoints commonly run keyless.
    "custom_base_url": "",
    "custom_api_key": "",
    # Optional override for the OpenCode Zen preset base URL (default is the
    # probed-live https://opencode.ai/inference/v1).
    "opencode_base_url": "",
    "cohere_api_key": "",
    "database_url": "",
    "searxng_url": "",
    # Firecrawl/SearXNG stack lifecycle for the interactive UI (see
    # core/firecrawl_lifecycle.py): opt-in autostart, stop-on-exit on by
    # default so an enabled autostart cannot outlive the last live session.
    # ``firecrawl_home`` holds the user's up.sh launcher for the Lima
    # docker-VM stack; failures are log-only and never block the UI.
    "firecrawl_autostart": False,
    "firecrawl_stop_on_exit": True,
    "firecrawl_home": "~/firecrawl",
    "machina_home": "",
    "tmpdir": "",
    "temp": "",
    # Context compression settings
    "compression_enabled": True,
    "compression_threshold": 0.8,
    "compression_target_ratio": 0.5,
    "compression_preserve_recent": 3,
    "compression_llm_summarize": True,
    # ── Token-efficiency policy (2026-10-16) ─────────────────────────────
    # Durable tool artifacts: oversized tool results are retained verbatim
    # (session-scoped, re-authorized reads via artifact_read/search/describe)
    # so bounded views stay recoverable. Store failure, oversize refusal, or
    # quota falls back to the legacy append cap — a lossy view is never
    # published without a stored original.
    "artifacts_enabled": True,
    # 0 disables expiry (artifacts live until the session is deleted).
    "artifact_retention_days": 30,
    # 0 disables the per-session quota (quotas refuse new stores; they never
    # evict live evidence).
    "artifact_max_per_session": 500,
    # Hard per-artifact size cap; larger results keep legacy truncation.
    "artifact_max_chars": 2000000,
    # Deterministic format reducers (tests/logs/JSON/CSV/diff/code/head+tail)
    # for oversized results. Disabled returns to blind truncation only.
    "reducers_enabled": True,
    # B2: wire the shared budgeted request projector into both live loops.
    # ON since the Track B rollout gate: a real deepseek/deepseek-chat turn
    # through run() and run_stream() accepted the projected payload (pins-first
    # system messages plus verified ``[machina view: …]`` units); see
    # docs/verification/DETERMINISTIC_SKILLS_B3_EVIDENCE.md §10. Off reverts to
    # shadow-only (bounded comparison log, byte-identical outgoing payloads).
    "request_projection_enabled": True,
    # Evidence projection: exact-duplicate removal plus token-budgeted
    # coverage selection across memory/RAG/archive excerpts; omissions are
    # disclosed in the prompt section. 0 tokens disables the budget.
    "retrieval_selection_enabled": True,
    "retrieval_token_budget": 6000,
    # Output policy: "legacy" keeps the existing brevity sentence; "concise"
    # appends an explicit answer-first policy that preserves requirements,
    # caveats, negations, numbers, code, commands, and paths.
    "output_policy": "legacy",
    # Optional JSON price overrides for cost estimates (USD per million
    # tokens), keyed by provider or "provider/model", e.g.
    # {"anthropic": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}.
    # Unknown models keep NULL cost — estimates are never presented as bills.
    "pricing_table_json": "",
    # ask is risk-based: routine project tools proceed; risky actions ask.
    # Execution modes: ask | workspace | sandbox | full.
    "execution_mode": "ask",
    "workspace_root": "",
    "terminal_sandbox": "disabled",
    "terminal_image": "machina-tool-sandbox:latest",
    "approval_timeout": 300,
    # Feature integration toggles with real enforcement.
    # Human-in-the-loop approval gate for memory writes (R4-089): when on,
    # MemoryStore.add routes writes to pending_memories for approve/reject.
    # Default OFF preserves direct durable writes.
    "memory_approval_enabled": False,
    "memory_consolidation_enabled": True,
    "auto_compaction_enabled": True,
    "eviction_max_tokens": 0,
    # Versioned workspace UI state (plan §21): one JSON string document
    # ({"version": 1, "byRoot": {resolvedRoot: state}}) holding layout mode,
    # split ratio, explorer state, open non-sensitive file URIs and cursor
    # offsets only — never document text or secrets. Bounded to 64 KiB by
    # validate_value (WORKSPACE_UI_STATE_MAX_BYTES).
    "workspace_ui_state": "",
    # Versioned global UI preferences (JSON string): editor prefs that are not
    # workspace-scoped — syntax highlighting, soft wrap, tab width. Bounded to
    # 16 KiB by validate_value (UI_PREFERENCES_MAX_BYTES); never holds source
    # text or secrets.
    "ui_preferences": "",
    # Pinned now-playing bar (TUI): shows what is playing on this computer —
    # any app (browser video, Spotify, Music, …) — via the gateway's
    # nowplaying.get detection. Local-only: metadata never leaves the
    # machine, and the feature degrades to one honest text line when the OS
    # provides nothing. The bar hides only when this is false.
    "nowplaying_enabled": True,
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
    "opencode_api_key": "OPENCODE_API_KEY",
    "kimi_api_key": "KIMI_API_KEY",
    "custom_base_url": "CUSTOM_BASE_URL",
    "custom_api_key": "CUSTOM_API_KEY",
    "opencode_base_url": "OPENCODE_BASE_URL",
    "cohere_api_key": "COHERE_API_KEY",
    "database_url": "DATABASE_URL",
    "searxng_url": "SEARXNG_URL",
    "machina_home": "MACHINA_HOME",
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
    if key == "persona_description":
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("persona_description must be a string of at most 2000 characters")
        from machina.memory.redaction import redact_secrets

        return redact_secrets(value.strip()).text
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
        # low/medium/high are the wire-wide transport tiers; minimal/xhigh/max
        # are live-verified wider tiers (ChatGPT Codex catalog) stored only
        # after the chatgpt provider validates them per model. The unset
        # aliases mean "provider default" and normalize to "". All three sets
        # come from the canonical vocabulary above so both surfaces agree.
        normalized = str(value).strip().lower()
        if normalized in REASONING_UNSET_EFFORTS:
            return ""
        if normalized not in REASONING_EFFORTS + REASONING_CATALOG_EFFORTS:
            tiers = "|".join(REASONING_EFFORTS + REASONING_CATALOG_EFFORTS)
            raise ValueError(f"reasoning_effort must be ''|{tiers} (or none|off|default|auto)")
        return normalized
    if key == "embedding_backend":
        normalized = str(value).strip().lower()
        if normalized not in ("auto", "local", "onnx", "openai", "off"):
            raise ValueError("embedding_backend must be auto|local|onnx|openai|off")
        return normalized
    if key == "reranker_backend":
        normalized = str(value).strip().lower()
        if normalized not in ("auto", "cohere", "local", "http", "none"):
            raise ValueError("reranker_backend must be auto|cohere|local|http|none")
        return normalized
    if key == "output_policy":
        normalized = str(value).strip().lower()
        if normalized not in ("legacy", "concise"):
            raise ValueError("output_policy must be legacy|concise")
        return normalized
    if key == "pricing_table_json":
        if not isinstance(value, str):
            raise ValueError("pricing_table_json must be a string")
        if len(value.encode("utf-8")) > PRICING_TABLE_MAX_BYTES:
            raise ValueError(
                f"pricing_table_json is too long (max {PRICING_TABLE_MAX_BYTES} bytes)"
            )
        if value.strip():
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise ValueError("pricing_table_json must be valid JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("pricing_table_json must be a JSON object")
        return value
    if key == "workspace_ui_state":
        # Versioned UI layout JSON (plan §21): a plain string, but typed and
        # byte-bounded here so config.set and every transport coercion path
        # share one refusal (AH-AUDIT-042 single-source contract).
        if not isinstance(value, str):
            raise ValueError("workspace_ui_state must be a string")
        if len(value.encode("utf-8")) > WORKSPACE_UI_STATE_MAX_BYTES:
            raise ValueError(
                f"workspace_ui_state is too long (max {WORKSPACE_UI_STATE_MAX_BYTES} bytes)"
            )
        return value
    if key == "ui_preferences":
        # Versioned global UI preferences JSON: same single-refusal contract
        # and bound as workspace_ui_state, one order of magnitude smaller.
        if not isinstance(value, str):
            raise ValueError("ui_preferences must be a string")
        if len(value.encode("utf-8")) > UI_PREFERENCES_MAX_BYTES:
            raise ValueError(f"ui_preferences is too long (max {UI_PREFERENCES_MAX_BYTES} bytes)")
        return value
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
        "max_tokens": (1, MAX_TOKENS),
        # 0 disables throttling (AsyncTokenBucket.acquire short-circuits);
        # documented in rate_limit.py and pinned by test_f5_zero_rate.
        "rate_limit_calls_per_minute": (0, None),
        "usage_session_token_limit": (0, None),
        "usage_session_request_limit": (0, None),
        "usage_agent_token_limit": (0, None),
        "usage_agent_request_limit": (0, None),
        "compression_preserve_recent": (0, 100),
        "learning_review_max_per_session": (0, 100),
        "learning_review_auto_apply_per_day": (0, 100),
        "eviction_max_tokens": (0, None),
        "artifact_retention_days": (0, 3650),
        "artifact_max_per_session": (0, 100_000),
        "artifact_max_chars": (1000, 100_000_000),
        "retrieval_token_budget": (0, 1_000_000),
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
    if key == "reranker_timeout" and not 1.0 <= val <= 120.0:
        raise ValueError(f"reranker_timeout must be in range 1..120, got {val}")
    if key in ("rrf_keyword_weight", "rrf_dense_weight") and not 0.0 <= val <= 10.0:
        raise ValueError(f"{key} must be in range 0..10, got {val}")


@dataclass
class Config:
    """Machina configuration.

    Values are resolved in priority order:
    1. Per-session overrides (highest)
    2. Environment variables (MACHINA_<KEY>)
    3. Config file (~/.machina/config.yaml)
    4. Defaults (lowest)
    """

    model: str = "openrouter/free"
    provider: str = "openrouter"
    context_budget: int = 8000
    turn_timeout: int = 300
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
    personality_growth_enabled: bool = True
    rsi_enabled: bool = True
    persona_description: str = ""
    persona_memory_enabled: bool = True
    persona_default_emotion: str = "trust"
    learning_review_enabled: bool = False
    learning_review_max_per_session: int = 3
    # Auto-apply is the default: proposals publish themselves (no standing
    # approval gate) within the documented caps; stage-only is the fallback.
    learning_review_auto_apply: bool = True
    # Rolling 24h auto-apply cap per agent; counts only auto-applied reviews.
    learning_review_auto_apply_per_day: int = 5
    # Stage-aware instruction disclosure planner (spec §10; default ON,
    # one-key rollback); contract documented in DEFAULTS.
    skills_disclosure_planner: bool = True
    rag_enabled: bool = True
    # Embedding backend selection for dense retrieval (RAG + memory).
    # Default "off": LoCoMo measurement kept dense models opt-in (see DEFAULTS).
    embedding_backend: str = "off"
    embedding_model: str = ""
    embedding_base_url: str = ""
    # Cross-encoder reranker selection (local-first, zero-API; opt-in).
    reranker_backend: str = "none"
    reranker_local_model: str = ""
    reranker_http_url: str = ""
    reranker_http_model: str = ""
    reranker_timeout: float = 10.0
    # RRF fusion weights for hybrid search (dense vs keyword branches).
    rrf_keyword_weight: float = 1.0
    rrf_dense_weight: float = 0.25
    prompt_cache_enabled: bool = False
    theme: str = "codex"
    # Transparent canvas (default OFF = opaque theme background); contract in
    # DEFAULTS. Config.save() reads getattr(self, key) once per DEFAULTS key, so
    # this typed field is mandatory — without it every save() raises.
    transparent_background: bool = False
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
    opencode_api_key: str = field(default="", repr=False)
    kimi_api_key: str = field(default="", repr=False)
    # "custom" provider: any OpenAI-compatible base URL; key optional.
    custom_base_url: str = ""
    custom_api_key: str = field(default="", repr=False)
    # Optional base-url override for the opencode preset.
    opencode_base_url: str = ""
    cohere_api_key: str = field(default="", repr=False)
    database_url: str = field(default="", repr=False)
    searxng_url: str = ""
    # Firecrawl/SearXNG stack lifecycle for the interactive UI (opt-in; see
    # DEFAULTS for the contract).
    firecrawl_autostart: bool = False
    firecrawl_stop_on_exit: bool = True
    firecrawl_home: str = "~/firecrawl"
    machina_home: str = ""
    tmpdir: str = ""
    temp: str = ""
    # Context compression settings
    compression_enabled: bool = True
    compression_threshold: float = 0.8
    compression_target_ratio: float = 0.5
    compression_preserve_recent: int = 3
    compression_llm_summarize: bool = True
    # ── Token-efficiency policy (2026-10-16); see DEFAULTS for contracts ──
    artifacts_enabled: bool = True
    artifact_retention_days: int = 30
    artifact_max_per_session: int = 500
    artifact_max_chars: int = 2000000
    reducers_enabled: bool = True
    # B2: wire the shared request projector (both loops). Default ON since the
    # Track B live-provider rollout gate succeeded; see DEFAULTS for evidence.
    request_projection_enabled: bool = True
    retrieval_selection_enabled: bool = True
    retrieval_token_budget: int = 6000
    output_policy: str = "legacy"
    pricing_table_json: str = ""
    # Execution modes (Phase 5)
    execution_mode: str = "ask"
    workspace_root: str = ""
    terminal_sandbox: str = "disabled"
    terminal_image: str = "machina-tool-sandbox:latest"
    approval_timeout: int = 300
    # Human-in-the-loop memory writes: ON routes MemoryStore.add to
    # pending_memories (approve/reject via CLI/gateway); OFF writes directly.
    memory_approval_enabled: bool = False
    memory_consolidation_enabled: bool = True
    auto_compaction_enabled: bool = True
    eviction_max_tokens: int = 0
    # Pinned now-playing bar: show what is playing on this computer.
    nowplaying_enabled: bool = True
    # Versioned, non-sensitive workspace UI state (plan §21); see DEFAULTS for
    # the document contract and WORKSPACE_UI_STATE_MAX_BYTES for the bound.
    workspace_ui_state: str = ""
    # Versioned global UI preferences (editor prefs); see DEFAULTS and
    # UI_PREFERENCES_MAX_BYTES for the bound.
    ui_preferences: str = ""
    # n4: compaction_check_enabled was dead config surface (no production
    # reader); it was removed rather than kept as a misleading toggle.

    # Per-session overrides (not persisted to file).
    # n4 note: no production code writes this dict — tests exercise the
    # precedence path by setting it directly. Wiring a per-session setter
    # would need a real caller; until then it stays as documented surface.
    _session_overrides: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str) -> Any:
        """Live precedence: override, explicit env, legacy env, backend, file, default."""
        if key in self._session_overrides:
            return validate_value(key, self._session_overrides[key])
        explicit = f"MACHINA_{key.upper()}"
        legacy = LEGACY_ENV_VARS.get(key)
        names = (explicit, legacy) if legacy else (explicit,)
        if key in SECRET_KEYS:
            from machina.security.secrets import get_secret

            secret = get_secret(legacy or explicit, env_names=names)
            if secret is not None:
                return secret
        else:
            for name in names:
                value = os.environ.get(name)
                if value is None:
                    continue
                # Empty env values are absent for normal keys; only
                # reasoning_effort="" is an explicit provider-default signal.
                if key != "reasoning_effort" and str(value).strip() == "":
                    continue
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
        if key not in DEFAULTS:
            valid = ", ".join(sorted(DEFAULTS.keys()))
            raise ValueError(f"Unknown config key: {key!r}. Valid keys: {valid}")
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

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=save_path.parent,
                prefix=".config-",
                suffix=".tmp",
                delete=False,
            ) as f:
                temporary_path = Path(f.name)
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, save_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        logger.debug("Config saved to %s", save_path)

    @classmethod
    def load(cls, path: str | Path | None = None, *, _target: Config | None = None) -> Config:
        """Load config from YAML file, merging with env vars and defaults."""
        config = _target if _target is not None else cls()
        load_path = Path(path) if path else DEFAULT_CONFIG_PATH

        # Load from file if it exists. AH-AUDIT-042: invalid values are
        # rejected with a warning and the default kept — never activated.
        # Per-key isolation: one unreadable key (e.g. a startup import-cycle
        # failure) must never discard the entire file.
        if load_path.exists():
            file_data: dict = {}
            try:
                with open(load_path) as f:
                    file_data = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", load_path, e, exc_info=True)
            else:
                logger.debug("Config loaded from %s", load_path)
            # A YAML root that is not a mapping (list/scalar) must never crash
            # the caller: keep the defaults and say exactly what was ignored.
            if not isinstance(file_data, dict):
                logger.warning(
                    "Ignoring config file %s: expected a YAML mapping at the top level, got %s",
                    load_path,
                    type(file_data).__name__,
                )
                file_data = {}
            for key, value in file_data.items():
                if key in DEFAULTS and key not in SECRET_KEYS:
                    try:
                        setattr(config, key, validate_value(key, value))
                    except ValueError as e:
                        logger.warning("Ignoring invalid config %s in %s: %s", key, load_path, e)
                    except Exception as e:
                        logger.warning(
                            "Ignoring unreadable config %s in %s: %s",
                            key,
                            load_path,
                            type(e).__name__,
                            exc_info=True,
                        )

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
        # Persona validation imports the memory package, which imports
        # providers and config again. Publish the object before loading so
        # those imports retain the same live configuration, not a second copy.
        _config = Config()
        try:
            Config.load(_target=_config)
        except BaseException:
            _config = None
            raise
    return _config


def persist_provider_fallback(provider: str = "openrouter", model: str | None = None) -> None:
    """Persist the provider/model safe defaults and sync shadowing env entries.

    Shared removal fallback (see machina/cli/providers.py and the gateway's
    ``providers.remove``): config.yaml keeps the durable default, and
    MACHINA_PROVIDER/MACHINA_MODEL are updated wherever they already exist in
    the process environment or ``.env`` — ``Config.get`` checks the
    environment before config.yaml, so a stale line would otherwise resurrect
    a removed provider across restarts. ``.env`` lines are only updated in
    place, never appended (an absent line cannot shadow the config file).
    """
    resolved_model = model or DEFAULTS["model"]
    current = get_config()
    current.set("provider", provider, persist=True)
    current.set("model", resolved_model, persist=True)
    os.environ["MACHINA_PROVIDER"] = provider
    os.environ["MACHINA_MODEL"] = resolved_model
    try:
        present = read_env_values()
        updates = {
            key: value
            for key, value in (
                ("MACHINA_PROVIDER", provider),
                ("MACHINA_MODEL", resolved_model),
            )
            if key in present
        }
        if updates:
            set_env_values(updates)
    except (OSError, ValueError) as error:
        # Optional fallback preserves the primary outcome (the config file
        # already carries the default); report the class, never payloads.
        logger.warning(
            "provider fallback: .env sync failed (%s); edit MACHINA_PROVIDER/"
            "MACHINA_MODEL manually",
            type(error).__name__,
        )


def __getattr__(name: str) -> Any:
    """PEP 562 module-level __getattr__ for lazy config access.

    ``from machina.core.config import config`` continues to work, but the actual
    Config.load() call is deferred until the attribute is first accessed.
    """
    if name == "config":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
