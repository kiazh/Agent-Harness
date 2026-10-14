"""Model catalog: parsing, TTL cache, effort/slug validation, windows.

Fixture provenance: ``tests/fixtures/model_catalog_chatgpt.json`` is a
trimmed REAL copy of ``chatgpt.com/backend-api/codex/models?client_version=
99.0.0`` fetched on 2026-10-14 with this machine's ChatGPT auth (full proof:
``docs/verification/proofs/model_catalog_2026-10-14/``). The parser tests
therefore pin against the actual backend shape, not a hand-made one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from machina.core.exceptions import ValidationError
from machina.core.provider import catalog

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "model_catalog_chatgpt.json"


def _fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def hermetic_catalog(tmp_path, monkeypatch):
    """No developer Codex cache, no prior in-process catalog state."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    catalog.clear_catalog_cache()
    yield codex_home
    catalog.clear_catalog_cache()


def _seed_live(monkeypatch, payload: dict | None = None, *, account: str = "acct-1") -> None:
    """Populate the in-process live cache through the public refresh path."""
    models = catalog.parse_catalog(payload or _fixture_payload(), source="live")
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda *a, **k: models)
    catalog.refresh_live_catalog("token-1", account, force=True)


# ─── parsing the real payload ───────────────────────────────────────────────


def test_parse_real_catalog_sorts_and_filters_efforts(hermetic_catalog):
    models = catalog.parse_catalog(_fixture_payload())
    assert [m.slug for m in models] == [
        "gpt-6.1-sol",
        "gpt-6-luna",
        "gpt-reserve",
        "codex-auto-review",
    ]  # priority order from the real payload
    sol = models[0]
    assert sol.context_window == 500_000
    assert sol.max_context_window == 872_000
    # The catalog advertises ultra; the wire rejects it, so it is filtered.
    assert "ultra" in sol.advertised_efforts
    assert sol.supported_efforts == ("low", "medium", "high", "xhigh", "max")
    assert sol.default_effort == "low"
    assert sol.source == "live"
    assert models[1].slug == "gpt-6-luna"


def test_hidden_entries_are_kept_but_not_displayed(hermetic_catalog):
    models = catalog.parse_catalog(_fixture_payload())
    hidden = [m.slug for m in models if m.visibility == "hide"]
    assert hidden == ["gpt-reserve", "codex-auto-review"]
    assert [m.slug for m in catalog.visible_models(models)] == [
        "gpt-6.1-sol",
        "gpt-6-luna",
    ]


def test_malformed_rows_are_skipped(hermetic_catalog):
    payload = {"models": [None, {"no_slug": 1}, {"slug": "x"}, "nope", {"slug": "x"}]}
    models = catalog.parse_catalog(payload)
    assert [m.slug for m in models] == ["x"]
    assert models[0].supported_efforts == ()


# ─── live fetch + TTL cache ─────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, answers: dict[str, _FakeResponse]) -> None:
        self.answers = answers
        self.urls: list[str] = []

    def get(self, url, params=None, headers=None):  # noqa: ANN001 - test double
        version = (params or {}).get("client_version", "")
        self.urls.append(f"{url}?client_version={version}")
        return self.answers[version]


def test_fetch_prefers_newest_client_version(hermetic_catalog):
    client = _FakeClient({"99.0.0": _FakeResponse(200, _fixture_payload())})
    models = catalog.fetch_live_catalog("tok", "acct", client=client)
    assert [m.slug for m in models][:2] == ["gpt-6.1-sol", "gpt-6-luna"]
    assert client.urls == ["https://chatgpt.com/backend-api/codex/models?client_version=99.0.0"]


def test_fetch_falls_back_to_legacy_sentinel_on_empty(hermetic_catalog):
    client = _FakeClient(
        {
            "99.0.0": _FakeResponse(200, {"models": []}),
            "0.0.0": _FakeResponse(200, _fixture_payload()),
        }
    )
    models = catalog.fetch_live_catalog("tok", "acct", client=client)
    assert models and client.urls[-1].endswith("client_version=0.0.0")


def test_fetch_raises_when_no_version_answers(hermetic_catalog):
    client = _FakeClient(
        {
            "99.0.0": _FakeResponse(200, {"models": []}),
            "0.0.0": _FakeResponse(503, {}),
        }
    )
    with pytest.raises(catalog.CatalogFetchError) as excinfo:
        catalog.fetch_live_catalog("tok", "acct", client=client)
    assert "503" in str(excinfo.value)


def test_refresh_is_ttl_cached_and_force_bypasses(hermetic_catalog, monkeypatch):
    calls = {"count": 0}

    def fake_fetch(*_args, **_kwargs):
        calls["count"] += 1
        return catalog.parse_catalog(_fixture_payload(), source="live")

    monkeypatch.setattr(catalog, "fetch_live_catalog", fake_fetch)
    catalog.refresh_live_catalog("tok", "acct")
    catalog.refresh_live_catalog("tok", "acct")
    assert calls["count"] == 1
    catalog.refresh_live_catalog("tok", "acct", force=True)
    assert calls["count"] == 2


def test_failures_are_negative_cached(hermetic_catalog, monkeypatch):
    calls = {"count": 0}

    def failing_fetch(*_args, **_kwargs):
        calls["count"] += 1
        raise catalog.CatalogFetchError("boom")

    monkeypatch.setattr(catalog, "fetch_live_catalog", failing_fetch)
    with pytest.raises(catalog.CatalogFetchError):
        catalog.refresh_live_catalog("tok", "acct")
    with pytest.raises(catalog.CatalogFetchError):
        catalog.refresh_live_catalog("tok", "acct")
    assert calls["count"] == 1  # second call served by the negative TTL


# ─── per-model effort + slug validation ─────────────────────────────────────


def test_effort_validation_comes_from_that_models_entry(hermetic_catalog, monkeypatch):
    _seed_live(monkeypatch)
    assert catalog.validate_effort("gpt-6.1-sol", "max") == "max"
    assert catalog.validate_effort("gpt-6.1-sol", "off") == ""
    with pytest.raises(ValidationError) as excinfo:
        catalog.validate_effort("gpt-6.1-sol", "ultra")
    message = str(excinfo.value)
    assert "gpt-6.1-sol" in message
    assert "supported: low, medium, high, xhigh, max" in message
    assert "ultra" in message and "transport" in message


def test_unknown_model_effort_falls_back_to_wire_vocabulary(hermetic_catalog, monkeypatch):
    _seed_live(monkeypatch)
    assert catalog.validate_effort("future-model", "xhigh") == "xhigh"
    with pytest.raises(ValidationError) as excinfo:
        catalog.validate_effort("future-model", "bogus")
    assert "supported: none, minimal, low, medium, high, xhigh, max" in str(excinfo.value)


def test_slug_strictness_requires_fresh_live_data(hermetic_catalog, monkeypatch):
    # No live data yet: the dated fallback/codex cache must not block a slug.
    assert catalog.validate_model_slug("gpt-9-future") == "gpt-9-future"
    _seed_live(monkeypatch)
    assert catalog.validate_model_slug("gpt-6.1-sol") == "gpt-6.1-sol"
    with pytest.raises(ValidationError) as excinfo:
        catalog.validate_model_slug("gpt-9-future")
    assert "available: gpt-6.1-sol, gpt-6-luna" in str(excinfo.value)


# ─── cached fallback chain + windows ────────────────────────────────────────


def test_cached_catalog_reads_codex_cli_cache(hermetic_catalog, monkeypatch):
    (hermetic_catalog / "models_cache.json").write_text(
        json.dumps(_fixture_payload()), encoding="utf-8"
    )
    models = catalog.cached_catalog()
    assert models[0].slug == "gpt-6.1-sol"
    assert models[0].source == "codex-cache"


def test_bundled_fallback_only_carries_verified_rows(hermetic_catalog):
    models = catalog.cached_catalog()
    assert models[0].source == "fallback"
    slugs = [m.slug for m in models]
    assert "gpt-6.1-sol" in slugs and "gpt-6-astra" in slugs
    assert "gpt-reserve" not in slugs  # hidden in the live payload: not bundled
    assert all(m.supported_efforts for m in models)
    assert all(m.context_window for m in models)


def test_window_lookup_uses_entry_context_window(hermetic_catalog, monkeypatch):
    _seed_live(monkeypatch)
    assert catalog.window_for("gpt-6.1-sol") == 500_000
    assert catalog.window_for("gpt-6-luna") == 272_000
    assert catalog.window_for("gpt-9-future") is None
