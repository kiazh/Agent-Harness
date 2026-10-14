"""Provider + model-catalog feature handlers (``models.*`` / ``providers.*``).

Surfaces the real catalogs to the UI:

* ``models.list`` — the selected provider's models. ChatGPT-account
  providers return the live per-account Codex catalog (slugs, per-model
  reasoning efforts, context windows; TTL-cached, with the Codex CLI cache
  or the bundled verified snapshot as honest fallbacks and a
  ``fetchError``/``source`` describing which one answered). User-added
  providers return their pulled OpenAI-compatible ``/models`` catalog.
* ``models.setEffort`` — validate one reasoning effort against the
  selected model's catalog entry (ChatGPT) or the shared wire vocabulary
  (other providers) and apply it.
* ``providers.list/add/remove`` — manage user-added named providers. Keys
  are handled by the same mechanism ``/keys`` uses (git-ignored ``.env``,
  0600 writes); the JSON store keeps metadata only.

No secret value ever crosses this wire: key state is a boolean, and model
metadata contains ids/windows/efforts only.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from machina.core.config import config
from machina.core.exceptions import ValidationError
from machina.core.provider import catalog
from machina.core.provider.specs import PROVIDER_SPECS
from machina.gateway.errors import INVALID_PARAMS, RpcError
from machina.gateway.features._common import _bool, _str
from machina.security import providers as store

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

#: Bound-in-family names treated as built-in providers by ``models.list``
#: (no discovery endpoint of their own; the UI falls back to its curated list).
_BUILTIN_WITHOUT_CATALOG = ("openrouter", "anthropic", "custom", "ollama")


def _catalog_model_view(models: list[Any]) -> list[dict[str, Any]]:
    return [model.to_dict() for model in models]


def _models_payload(
    provider: str,
    *,
    source: str,
    models: list[dict[str, Any]],
    fetch_error: str | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "source": source,
        "models": models,
        "fetchError": fetch_error,
        "fetchedAt": fetched_at,
    }


async def _chatgpt_models(provider: str, refresh: bool) -> dict[str, Any]:
    fetch_error: str | None = None
    source = "cache"
    models = []
    try:
        live = await asyncio.to_thread(catalog.fetch_catalog_for_account, force=refresh)
        models = catalog.visible_models(live)
        source = "live"
    except Exception as exc:  # CatalogFetchError and friends: stay honest, keep serving
        fetch_error = str(exc) or type(exc).__name__
        cached = catalog.cached_catalog()
        models = catalog.visible_models(cached)
        source = cached[0].source if cached else "none"
    return _models_payload(
        provider,
        source=source,
        models=_catalog_model_view(models),
        fetch_error=fetch_error,
    )


async def _named_models(provider: str, refresh: bool) -> dict[str, Any]:
    entry = store.get_provider_entry(provider)
    if entry is None:
        raise RpcError(INVALID_PARAMS, f"unknown provider: {provider}")
    base_url = str(entry.get("base_url") or "")
    models = entry.get("models") if isinstance(entry.get("models"), list) else []
    age = store.pulled_at_age_seconds(entry)
    fresh_enough = age is not None and age < 3600
    fetch_error: str | None = None
    if refresh or not models or not fresh_enough:
        key = store.resolve_provider_key(entry)
        try:
            pulled = await asyncio.to_thread(store.fetch_models, base_url, api_key=key)
            if pulled:
                models = await asyncio.to_thread(store.save_models, provider, pulled)
                entry = store.get_provider_entry(provider) or entry
        except Exception as exc:  # ProviderStoreError and transport failures
            fetch_error = str(exc) or type(exc).__name__
    return _models_payload(
        provider,
        source="live" if fetch_error is None and models else "cache",
        models=list(models),
        fetch_error=fetch_error,
        fetched_at=str(entry.get("models_fetched_at") or "") or None,
    )


async def models_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Models for a provider (default: the gateway's selected one)."""
    gw.require_db()
    provider = str(params.get("provider") or gw.provider or "").strip().lower()
    if not provider:
        raise RpcError(INVALID_PARAMS, "provider is required")
    refresh = _bool(params, "refresh")
    if provider == "chatgpt":
        return await _chatgpt_models(provider, refresh)
    if store.is_named_provider(provider):
        return await _named_models(provider, refresh)
    # Built-in direct families have no discovery endpoint this surface owns;
    # the UI falls back to its curated list for them.
    if provider in PROVIDER_SPECS or provider in _BUILTIN_WITHOUT_CATALOG:
        return _models_payload(provider, source="none", models=[])
    raise RpcError(INVALID_PARAMS, f"unknown provider: {provider}")


async def models_set_effort(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Validate + apply a reasoning effort for the selected provider/model."""
    gw.require_db()
    raw = params.get("effort")
    if raw is None:
        effort = ""
    elif isinstance(raw, str):
        effort = raw.strip().lower()
    else:
        raise RpcError(INVALID_PARAMS, "effort must be a string")
    persist = _bool(params, "persist")
    provider = str(gw.provider or "").strip().lower()
    model = str(gw.model or "").strip()
    if provider == "chatgpt":
        try:
            normalized = catalog.validate_effort(model, effort)
        except ValidationError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from None
        supported = list(catalog.supported_efforts_for(model) or ())
    else:
        allowed = ("", "low", "medium", "high")
        if effort not in allowed:
            raise RpcError(
                INVALID_PARAMS,
                f"reasoning effort {effort!r} is not supported for "
                f"{provider or 'this provider'}; supported: low, medium, high (or unset)",
            )
        normalized = effort
        supported = ["low", "medium", "high"]
    config.set("reasoning_effort", normalized, persist=persist)
    return {
        "provider": provider,
        "model": model,
        "effort": normalized or None,
        "supported": supported,
    }


async def providers_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Built-in + user-added providers, with honest per-entry state."""
    gw.require_db()
    selected = str(gw.provider or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for name in ("openrouter", *tuple(PROVIDER_SPECS), "anthropic", "chatgpt", "custom", "ollama"):
        rows.append({"name": name, "kind": "builtin", "selected": name == selected})
    for name in store.provider_names():
        entry = store.get_provider_entry(name) or {}
        key = store.resolve_provider_key(entry)
        rows.append(
            {
                "name": name,
                "kind": "named",
                "selected": name == selected,
                "baseUrl": entry.get("base_url"),
                "keySet": bool(key),
                "keylessLocal": bool(entry.get("base_url"))
                and store.is_loopback_base_url(str(entry.get("base_url"))),
                "apiKeyEnv": entry.get("api_key_env"),
                "modelsCount": len(entry.get("models") or []),
                "modelsFetchedAt": entry.get("models_fetched_at"),
            }
        )
    return {"selected": selected, "providers": rows}


async def providers_add(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Add (or update) a named provider, then pull its model catalog.

    The provider entry is saved even when the pull fails — the store is
    durable user intent and the fetch error is reported in the response
    (``fetchError``) so the UI can say exactly what happened.
    """
    gw.require_db()
    name = _str(params, "name", max_len=64).lower()
    base_url = _str(params, "baseUrl", max_len=500)
    raw_key = params.get("apiKey")
    if raw_key is not None and not isinstance(raw_key, str):
        raise RpcError(INVALID_PARAMS, "apiKey must be a string")
    api_key = (raw_key or "").strip()
    persist_key = _bool(params, "persistKey", default=True)
    try:
        entry = await asyncio.to_thread(
            store.add_provider, name, base_url, api_key=api_key or None, persist_key=persist_key
        )
    except store.ProviderStoreError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    except (OSError, ValueError) as exc:
        raise RpcError(INVALID_PARAMS, f"could not save provider ({type(exc).__name__})") from None
    key = store.resolve_provider_key(store.get_provider_entry(name) or entry)
    fetch_error: str | None = None
    models: list[dict[str, Any]] = []
    try:
        pulled = await asyncio.to_thread(store.fetch_models, entry["base_url"], api_key=key)
        if pulled:
            models = await asyncio.to_thread(store.save_models, name, pulled)
    except Exception as exc:
        fetch_error = str(exc) or type(exc).__name__
    return {
        "provider": name,
        "baseUrl": entry["base_url"],
        "apiKeyEnv": entry["api_key_env"],
        "keySet": bool(key),
        "keySaved": bool(api_key) and persist_key,
        "models": models,
        "fetchError": fetch_error,
    }


async def providers_remove(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Remove a named provider (never a built-in) and deselect it if active."""
    gw.require_db()
    name = _str(params, "name", max_len=64).lower()
    if name not in store.provider_names():
        raise RpcError(INVALID_PARAMS, f"unknown user-added provider: {name}")
    removed = await asyncio.to_thread(store.remove_provider, name)
    if not removed:
        raise RpcError(INVALID_PARAMS, f"unknown user-added provider: {name}")
    switched: str | None = None
    if str(gw.provider or "").strip().lower() == name:
        gw.provider = "openrouter"
        switched = "openrouter"
    return {"provider": name, "removed": True, "selected": gw.provider, "switchedTo": switched}
