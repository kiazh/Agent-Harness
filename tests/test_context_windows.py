"""Per-model context windows and the derived per-turn token budget."""

from machina.core.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    LEGACY_DEFAULT_BUDGET,
    context_window_for,
    turn_token_budget,
)


def test_legacy_default_matches_config_default():
    from machina.core.config import DEFAULTS

    assert LEGACY_DEFAULT_BUDGET == DEFAULTS["context_budget"]


def test_known_models_resolve_to_documented_windows():
    assert context_window_for("deepseek-chat") == 64_000
    assert context_window_for("deepseek-reasoner") == 64_000
    assert context_window_for("gpt-4o-mini") == 128_000
    assert context_window_for("gemini-2.0-flash") == 1_048_576
    assert context_window_for("claude-3-5-sonnet-20241022") == 200_000
    assert context_window_for("grok-4") == 256_000
    assert context_window_for("llama-3.3-70b-versatile") == 128_000
    assert context_window_for("meta-llama/Llama-3.3-70B-Instruct-Turbo") == 128_000
    assert context_window_for("mistral-small-latest") == 128_000


def test_unknown_and_empty_models_keep_legacy_behavior():
    assert context_window_for("some-future-model") == DEFAULT_CONTEXT_WINDOW
    assert context_window_for(None) == DEFAULT_CONTEXT_WINDOW
    assert context_window_for("") == DEFAULT_CONTEXT_WINDOW
    assert turn_token_budget("some-future-model", 8000) == 32_000
    assert turn_token_budget(None, 8000) == 32_000


def test_longest_match_wins_for_ambiguous_ids():
    # Bare "mistral" is the local build; "mistral-small-…" is the API model.
    assert context_window_for("mistral") == 32_000
    assert context_window_for("mistral-small-latest") == 128_000


def test_capacity_is_the_full_model_window():
    assert turn_token_budget("deepseek-chat", 8000) == 64_000
    assert turn_token_budget("gpt-4o-mini", 8000) == 128_000
    assert turn_token_budget("gemini-2.0-flash", 8000) == 1_048_576
    assert turn_token_budget("codellama", 8000) == 16_384
    assert turn_token_budget("llama3.1", 8000) == 32_000


def test_legacy_spend_configuration_does_not_cap_model_capacity():
    assert turn_token_budget("gemini-2.0-flash", 2000) == 1_048_576
    assert turn_token_budget("deepseek-chat", 100_000) == 64_000
    assert turn_token_budget(None, 8000) == 32_000


# ─── chatgpt catalog windows (provider-scoped, like OpenRouter) ─────────────


def _seed_chatgpt_catalog(monkeypatch, tmp_path):
    """Seed the live catalog from the REAL 2026-10-14 fixture, hermetically."""
    import json
    from pathlib import Path

    from machina.core.provider import catalog

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "codex-home").mkdir()
    catalog.clear_catalog_cache()
    payload = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "model_catalog_chatgpt.json").read_text(
            encoding="utf-8"
        )
    )
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda *args, **kwargs: models)
    catalog.refresh_live_catalog("token-1", "acct-1", force=True)


def test_chatgpt_catalog_windows_apply_only_to_chatgpt(monkeypatch, tmp_path):
    _seed_chatgpt_catalog(monkeypatch, tmp_path)
    # 500K for the 6.1-sol entry, 272K for the others — from the catalog.
    assert context_window_for("gpt-6.1-sol", "chatgpt") == 500_000
    assert context_window_for("gpt-6-luna", "chatgpt") == 272_000
    assert context_window_for("chatgpt/gpt-6.1-sol", "chatgpt") == 500_000
    # Not provider-scoped: a same-named model on another provider keeps the
    # curated/default behavior.
    assert context_window_for("gpt-6.1-sol", "custom") == DEFAULT_CONTEXT_WINDOW
    assert context_window_for("gpt-6.1-sol") == DEFAULT_CONTEXT_WINDOW


def test_chatgpt_unknown_slug_keeps_default(monkeypatch, tmp_path):
    _seed_chatgpt_catalog(monkeypatch, tmp_path)
    assert context_window_for("gpt-9-future", "chatgpt") == DEFAULT_CONTEXT_WINDOW


def test_effective_turn_budget_prefers_session_then_provider_then_config():
    import uuid

    from machina.core.agent import _effective_turn_budget, _turn_model
    from machina.core.models import Session

    session = Session(id=uuid.uuid4(), context_budget=8000, model="deepseek-chat")

    class StubProvider:
        model = "gpt-4o-mini"

    assert _turn_model(session, StubProvider()) == "deepseek-chat"
    assert (
        _turn_model(Session(id=uuid.uuid4(), context_budget=8000), StubProvider()) == "gpt-4o-mini"
    )
    from machina.core.config import config as _global_config

    assert _turn_model(Session(id=uuid.uuid4(), context_budget=8000), object()) == (
        _global_config.get("model") or None
    )

    assert _effective_turn_budget(session, StubProvider()) == 64_000
    big = Session(id=uuid.uuid4(), context_budget=8000, model="gemini-2.0-flash")
    assert _effective_turn_budget(big, StubProvider()) == 1_048_576
    explicit = Session(id=uuid.uuid4(), context_budget=2000, model="gemini-2.0-flash")
    assert _effective_turn_budget(explicit, StubProvider()) == 1_048_576
