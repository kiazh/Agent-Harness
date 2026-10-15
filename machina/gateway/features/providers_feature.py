"""Provider + model-catalog feature handlers (``models.*`` / ``providers.*``).

Surfaces the real catalogs to the UI:

* ``models.list`` — the request session's models. ChatGPT-account
  providers return the live per-account Codex catalog (slugs, per-model
  reasoning efforts, context windows; TTL-cached, with the Codex CLI cache
  or the bundled verified snapshot as honest fallbacks and a
  ``fetchError``/``source`` describing which one answered). User-added
  providers return their pulled OpenAI-compatible ``/models`` catalog.
  The provider is resolved from the request's session identity (explicit
  ``sessionId``, else the session the client last created/resumed), so a
  resumed session pinned to another route lists ITS provider; an explicit
  ``provider`` parameter still wins for cross-provider listings.
* ``models.setEffort`` — validate one reasoning effort against the request
  session's pinned model catalog entry (ChatGPT) or provider wire
  vocabulary (other providers) and apply it. Unset aliases (``off``,
  ``none``, ``default``, ``auto``) clear the setting instead of failing
  provider validation.
* ``providers.list/add/remove`` — manage user-added named providers. Keys
  are handled by the same mechanism ``/keys`` uses (git-ignored ``.env``,
  0600 writes); the JSON store keeps metadata only.

No secret value ever crosses this wire: key state is a boolean, and model
metadata contains ids/windows/efforts only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from machina.core.config import (
    DEFAULTS,
    REASONING_UNSET_EFFORTS,
    config,
    persist_provider_fallback,
)
from machina.core.exceptions import ValidationError
from machina.core.provider import catalog
from machina.core.provider.reasoning import supported_reasoning_efforts
from machina.core.provider.specs import PROVIDER_SPECS
from machina.core.session import session_manager
from machina.gateway.errors import INVALID_PARAMS, RpcError
from machina.gateway.features._common import _bool, _str
from machina.security import providers as store
from machina.security.env_file import set_env_values

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

logger = logging.getLogger(__name__)

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
    source = "cache"
    if refresh or not models or not fresh_enough:
        key = store.resolve_provider_key(entry)
        try:
            pulled = await asyncio.to_thread(store.fetch_models, base_url, api_key=key)
            models = await asyncio.to_thread(store.save_models, provider, pulled)
            entry = store.get_provider_entry(provider) or entry
            source = "live"
        except Exception as exc:  # ProviderStoreError and transport failures
            fetch_error = str(exc) or type(exc).__name__
    return _models_payload(
        provider,
        source=source,
        models=list(models),
        fetch_error=fetch_error,
        fetched_at=str(entry.get("models_fetched_at") or "") or None,
    )


async def _session_route(gw: Gateway, params: dict[str, Any]) -> tuple[str, str]:
    """The (provider, model) a model command acts on.

    Explicit session identity wins; otherwise the session the client most
    recently opened (``session.create`` / ``session.resume``) is consulted.
    A resumed session can pin a route the gateway defaults do not carry,
    and its route is what the next turn will use (round-3 review finding
    10). The gateway defaults are the final fallback.
    """
    session = None
    if params.get("sessionId") is not None:
        session = await gw.get_session(params)
    else:
        opened = gw.opened_session_id()
        if opened is not None:
            session = await session_manager.get(opened)
    if session is not None:
        provider = str(session.provider or "").strip().lower()
        if provider:
            return provider, str(session.model or "").strip()
    return str(gw.provider or "").strip().lower(), str(gw.model or "").strip()


async def models_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Models for a provider (default: the request session's pinned one)."""
    gw.require_db()
    explicit = str(params.get("provider") or "").strip().lower()
    if explicit:
        provider = explicit
    else:
        provider, _model = await _session_route(gw, params)
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


_REASONING_EFFORT_ENV = "MACHINA_REASONING_EFFORT"


def _sync_reasoning_effort_env(effort: str, *, persist: bool) -> None:
    """Keep the ``MACHINA_REASONING_EFFORT`` shadow in step with an /effort write.

    ``Config.get`` reads ``MACHINA_<KEY>`` (env / ``.env``) before the
    in-memory attribute, and for ``reasoning_effort`` even an explicitly empty
    value is an active "provider default" signal. Without this sync
    ``/effort high`` reported success while calls kept the environment's value
    (H23 holistic review). Mirror ``sync_execution_mode_env``: update the
    process environment always, and ``.env`` for the durable path; a failed
    ``.env`` write only warns (the in-process value already applies).
    """
    os.environ[_REASONING_EFFORT_ENV] = effort
    if not persist:
        return
    try:
        set_env_values({_REASONING_EFFORT_ENV: effort})
    except (OSError, ValueError) as error:
        logger.warning(
            "reasoning_effort: .env sync failed for %s (%s)",
            _REASONING_EFFORT_ENV,
            type(error).__name__,
        )


async def models_set_effort(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Validate + apply a reasoning effort for the request session's route."""
    gw.require_db()
    raw = params.get("effort")
    if raw is None:
        effort = ""
    elif isinstance(raw, str):
        effort = raw.strip().lower()
    else:
        raise RpcError(INVALID_PARAMS, "effort must be a string")
    # The UI advertises "off"; every unset alias means "provider default"
    # and clears the setting instead of failing provider validation
    # (round-3 review finding 11). The alias list is the canonical config
    # vocabulary (REASONING_UNSET_EFFORTS), so this surface and config.set
    # cannot disagree.
    if effort in REASONING_UNSET_EFFORTS:
        effort = ""
    persist = _bool(params, "persist")
    provider, model = await _session_route(gw, params)
    if provider == "chatgpt":
        try:
            normalized = catalog.validate_effort(model, effort)
        except ValidationError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from None
        supported = list(catalog.supported_efforts_for(model) or ())
    else:
        supported = list(supported_reasoning_efforts(provider))
        allowed = ("", *supported)
        if effort not in allowed:
            raise RpcError(
                INVALID_PARAMS,
                f"reasoning effort {effort!r} is not supported for "
                f"{provider or 'this provider'}; supported: {', '.join(supported)} "
                "(or unset)",
            )
        normalized = effort
    try:
        config.set("reasoning_effort", normalized, persist=persist)
    except ValueError as exc:
        # The catalog can never hand config a value it rejects (both read the
        # canonical vocabulary); if that ever drifts, report a typed invalid
        # parameter instead of escaping as INTERNAL_ERROR.
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    # H23: config.get() prefers MACHINA_REASONING_EFFORT (even an explicitly
    # empty value) over the in-memory write, so sync the shadow — the echo
    # below must report the effective effort, never an overridden request.
    _sync_reasoning_effort_env(normalized, persist=persist)
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
    """Remove a named provider (never a built-in) and every trace it left.

    The store call deletes the entry (with its cached model list), the
    derived ``.env`` key line, the live env var, and — because ``PROVIDERS``
    consults the store live — its dynamic-registry membership. When the
    removed provider was the gateway's active one, both provider and model
    fall back to the shipped defaults AND the fallback is persisted
    (config.yaml + shadowing env/``.env`` lines, the same mechanism the CLI
    uses) so a restart cannot resurrect the removed route; the response
    reports the cleanup and the fallback for a visible notice.
    """
    gw.require_db()
    name = _str(params, "name", max_len=64).lower()
    report = await asyncio.to_thread(store.remove_provider, name)
    if not report or not report.get("storeEntry"):
        raise RpcError(INVALID_PARAMS, f"unknown user-added provider: {name}")
    switched: str | None = None
    model_reset: str | None = None
    if str(gw.provider or "").strip().lower() == name:
        gw.provider = "openrouter"
        switched = str(gw.provider)
        gw.model = str(DEFAULTS["model"])
        model_reset = gw.model
        # Same durability contract as the CLI removal fallback: persist the
        # route (config.yaml + any shadowing MACHINA_PROVIDER/MACHINA_MODEL
        # env/.env lines) so a restart cannot resurrect the removed provider.
        try:
            persist_provider_fallback(gw.provider, gw.model)
        except (OSError, ValueError) as error:
            logger.warning(
                "providers.remove: fallback persistence failed (%s); a restart may still "
                "name the removed provider %s",
                type(error).__name__,
                name,
            )
    cleanup = {
        "storeEntry": bool(report.get("storeEntry")),
        "apiKeyEnv": report.get("apiKeyEnv"),
        "envLineRemoved": bool(report.get("envLineRemoved")),
        "envVarRemoved": bool(report.get("envVarRemoved")),
        "cachedModels": int(report.get("cachedModels") or 0),
        "registryEntry": bool(report.get("registryEntry")),
    }
    return {
        "provider": name,
        "removed": True,
        "selected": gw.provider,
        "switchedTo": switched,
        "modelResetTo": model_reset,
        "cleanup": cleanup,
    }
