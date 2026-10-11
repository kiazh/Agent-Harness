"""Per-model context windows and the derived per-turn token budget."""

from ah.core.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    LEGACY_DEFAULT_BUDGET,
    context_window_for,
    turn_token_budget,
)


def test_legacy_default_matches_config_default():
    from ah.core.config import DEFAULTS

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
    assert turn_token_budget("some-future-model", 8000) == 8000
    assert turn_token_budget(None, 8000) == 8000


def test_longest_match_wins_for_ambiguous_ids():
    # Bare "mistral" is the local build; "mistral-small-…" is the API model.
    assert context_window_for("mistral") == 32_000
    assert context_window_for("mistral-small-latest") == 128_000


def test_auto_budget_scales_with_window_floored_at_legacy():
    assert turn_token_budget("deepseek-chat", 8000) == 16_000
    assert turn_token_budget("gpt-4o-mini", 8000) == 32_000
    assert turn_token_budget("gemini-2.0-flash", 8000) == 262_144
    # Small local windows never drop below the legacy floor.
    assert turn_token_budget("codellama", 8000) == 8000
    assert turn_token_budget("llama3.1", 8000) == 8000


def test_explicit_configuration_wins_unchanged():
    assert turn_token_budget("gemini-2.0-flash", 2000) == 2000
    assert turn_token_budget("deepseek-chat", 100_000) == 100_000
    assert turn_token_budget(None, 8000) == 8000


def test_effective_turn_budget_prefers_session_then_provider_then_config():
    import uuid

    from ah.core.agent import MAX_TOKEN_BUDGET, _effective_turn_budget, _turn_model
    from ah.core.models import Session

    session = Session(id=uuid.uuid4(), context_budget=8000, model="deepseek-chat")

    class StubProvider:
        model = "gpt-4o-mini"

    assert _turn_model(session, StubProvider()) == "deepseek-chat"
    assert (
        _turn_model(Session(id=uuid.uuid4(), context_budget=8000), StubProvider()) == "gpt-4o-mini"
    )
    from ah.core.config import config as _global_config

    assert _turn_model(Session(id=uuid.uuid4(), context_budget=8000), object()) == (
        _global_config.get("model") or None
    )

    # Derived budget is capped by the absolute ceiling, explicit config too.
    assert _effective_turn_budget(session, StubProvider()) == min(16_000, MAX_TOKEN_BUDGET)
    big = Session(id=uuid.uuid4(), context_budget=8000, model="gemini-2.0-flash")
    assert _effective_turn_budget(big, StubProvider()) == MAX_TOKEN_BUDGET
    explicit = Session(id=uuid.uuid4(), context_budget=2000, model="gemini-2.0-flash")
    assert _effective_turn_budget(explicit, StubProvider()) == 2000
