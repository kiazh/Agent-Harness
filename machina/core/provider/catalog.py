"""ChatGPT/Codex model catalog — live per-account discovery, TTL-cached.

The catalog is the *real* source of model metadata for the ``chatgpt``
provider: slugs, per-model reasoning efforts, and context windows. Nothing
here hardcodes a model list for normal operation; the bundled snapshot is a
last-resort fallback for offline first runs and is refreshed by live
discovery whenever the account is reachable.

Wire facts, verified live on 2026-10-14 (see
``docs/verification/proofs/model_catalog_2026-10-14/``):

* ``GET {base}/models?client_version=<v>`` with ``Authorization: Bearer
  <access_token>`` and ``ChatGPT-Account-ID`` returns
  ``{"models": [{slug, display_name, description, visibility, priority,
  context_window, max_context_window, default_reasoning_level,
  supported_reasoning_levels: [{effort, description}], ...}]}``.
  Without the account header the endpoint answers ``{"models": []}``.
* ``client_version`` matters: ``99.0.0`` returned the full entitlement (9
  entries, 7 visible) while the legacy ``0.0.0`` sentinel returned a frozen
  6-entry subset. Discovery asks newest-first and accepts the first non-empty
  200.
* The Responses endpoint accepts ``reasoning.effort`` in
  ``none|minimal|low|medium|high|xhigh|max``. The catalog additionally
  advertises ``ultra`` ("automatic task delegation", a Codex-client tier) for
  some models, but the Responses API rejects it with HTTP 400 ("Invalid
  value: 'ultra'") — verified live. The per-model supported set exposed here
  is therefore the catalog entry INTERSECTED with the wire vocabulary, so a
  level the transport cannot send is never offered or accepted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from machina.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

CATALOG_BASE_URL = "https://chatgpt.com/backend-api/codex"
CATALOG_PATH = "/models"
# Newest-first; "0.0.0" is the historical ungated sentinel (now a frozen
# legacy list on this backend — kept only for the day newest is rejected).
CATALOG_CLIENT_VERSIONS: tuple[str, ...] = ("99.0.0", "0.0.0")

#: Reasoning efforts the Responses wire accepts (probed live 2026-10-14).
WIRE_REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: Machina's "unset" idiom: never sent to any provider (provider default).
UNSET_EFFORT_SENTINELS = ("", "none", "off", "default", "auto")

_CATALOG_TTL_SECONDS = 3600.0
_CATALOG_NEGATIVE_TTL_SECONDS = 300.0
_FETCH_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

#: In-process live catalog cache, keyed by account identity. Account-scoped
#: by design: a second ChatGPT principal must not inherit the first's list.
_live_cache: dict[str, tuple[list[CatalogModel], float]] = {}
_negative_until: dict[str, float] = {}

#: Codex CLI cache read-back (read-only, like the auth import): the CLI
#: writes the same payload shape to ``~/.codex/models_cache.json``.
_codex_cache: tuple[float, list[CatalogModel]] | None = None
_CODEX_CACHE_TTL_SECONDS = 60.0

#: Bundled fallback — a dated, account-scoped snapshot of the models this
#: machine verified live on 2026-10-14 (7 visible entries; hidden ones such
#: as gpt-reserve/codex-auto-review are excluded, as are plan-gated slugs
#: that were not returned). It exists for offline first runs only; every
#: listing surface prefers live discovery and the effort/context data below
#: is exactly what the live response reported that day. Refresh by design:
#: delete nothing — live data simply wins.
VERIFIED_FALLBACK_MODELS: tuple[dict[str, Any], ...] = (
    {
        "slug": "gpt-6.1-sol",
        "display_name": "GPT-6.1-Sol",
        "context_window": 500_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
    {
        "slug": "gpt-6-astra",
        "display_name": "GPT-6-Astra",
        "context_window": 272_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
    {
        "slug": "gpt-6-sol",
        "display_name": "GPT-6-Sol",
        "context_window": 272_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
    {
        "slug": "gpt-6-luna",
        "display_name": "GPT-6-Luna",
        "context_window": 272_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max"),
    },
    {
        "slug": "gpt-5.6-sol",
        "display_name": "GPT-5.6-Sol",
        "context_window": 272_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
    {
        "slug": "gpt-5.6-terra",
        "display_name": "GPT-5.6-Terra",
        "context_window": 272_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max", "ultra"),
    },
    {
        "slug": "gpt-5.6-luna",
        "display_name": "GPT-5.6-Luna",
        "context_window": 272_000,
        "max_context_window": 872_000,
        "efforts": ("low", "medium", "high", "xhigh", "max"),
    },
)


@dataclass(frozen=True)
class CatalogModel:
    """One catalog entry, normalized to the fields Machina consumes."""

    slug: str
    display_name: str
    description: str
    visibility: str
    priority: int
    context_window: int | None
    max_context_window: int | None
    default_effort: str
    advertised_efforts: tuple[str, ...]
    supported_efforts: tuple[str, ...]
    source: str  # "live" | "codex-cache" | "fallback"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for the gateway (no account/secret material)."""
        return {
            "id": self.slug,
            "displayName": self.display_name,
            "description": self.description,
            "efforts": list(self.supported_efforts),
            "advertisedEfforts": list(self.advertised_efforts),
            "defaultEffort": self.default_effort or None,
            "contextWindow": self.context_window,
            "maxContextWindow": self.max_context_window,
            "source": self.source,
        }


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _normalize_efforts(raw: Any) -> tuple[str, ...]:
    """Catalog ``supported_reasoning_levels`` -> ordered, wire-filtered set."""
    efforts: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                effort = str(item.get("effort") or "").strip().lower()
            else:
                effort = str(item or "").strip().lower()
            if effort and effort not in efforts:
                efforts.append(effort)
    return tuple(effort for effort in efforts if effort in WIRE_REASONING_EFFORTS)


def parse_catalog(payload: Any, source: str = "live") -> list[CatalogModel]:
    """Parse a catalog response body into entries; malformed rows are skipped.

    Hidden entries (``visibility`` hide/hidden) are returned too — callers
    filter for display — but they carry their visibility so nothing is
    silently lost.
    """
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    models: list[CatalogModel] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        raw_efforts = item.get("supported_reasoning_levels")
        advertised: list[str] = []
        if isinstance(raw_efforts, list):
            for entry in raw_efforts:
                effort = (
                    str(entry.get("effort") or "").strip().lower()
                    if isinstance(entry, dict)
                    else str(entry or "").strip().lower()
                )
                if effort and effort not in advertised:
                    advertised.append(effort)
        models.append(
            CatalogModel(
                slug=slug,
                display_name=str(item.get("display_name") or slug),
                description=str(item.get("description") or ""),
                visibility=str(item.get("visibility") or "list").strip().lower(),
                priority=rank,
                context_window=_int_or_none(item.get("context_window")),
                max_context_window=_int_or_none(item.get("max_context_window")),
                default_effort=str(item.get("default_reasoning_level") or "").strip().lower(),
                advertised_efforts=tuple(advertised),
                supported_efforts=_normalize_efforts(raw_efforts),
                source=source,
            )
        )
    models.sort(key=lambda model: (model.priority, model.slug))
    return models


def visible_models(models: list[CatalogModel]) -> list[CatalogModel]:
    """Entries a picker/list may show (``visibility`` not hide/hidden)."""
    return [model for model in models if model.visibility not in ("hide", "hidden")]


def _fallback_models() -> list[CatalogModel]:
    return [
        CatalogModel(
            slug=row["slug"],
            display_name=row["display_name"],
            description="",
            visibility="list",
            priority=index,
            context_window=row["context_window"],
            max_context_window=row["max_context_window"],
            default_effort="",
            advertised_efforts=tuple(row["efforts"]),
            supported_efforts=_normalize_efforts(row["efforts"]),
            source="fallback",
        )
        for index, row in enumerate(VERIFIED_FALLBACK_MODELS)
    ]


def fetch_live_catalog(
    access_token: str,
    account_id: str = "",
    *,
    base_url: str = "",
    client: httpx.Client | None = None,
) -> list[CatalogModel]:
    """One live discovery pass; raises on transport/status failure.

    Empty-but-200 answers try the next ``client_version`` and end in an
    empty list, which the callers treat as "no catalog", never as success.
    """
    base = (base_url or CATALOG_BASE_URL).strip().rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    owned = client is None
    http = client or httpx.Client(timeout=_FETCH_TIMEOUT)
    try:
        last_status = 0
        for version in CATALOG_CLIENT_VERSIONS:
            response = http.get(
                f"{base}{CATALOG_PATH}", params={"client_version": version}, headers=headers
            )
            last_status = response.status_code
            if response.status_code != 200:
                continue
            models = parse_catalog(response.json(), source="live")
            if models:
                return models
        raise CatalogFetchError(f"catalog fetch returned HTTP {last_status or 'no response'}")
    finally:
        if owned:
            http.close()


class CatalogFetchError(RuntimeError):
    """Live discovery failed (transport error or non-200/no models)."""


def _cache_key(account_id: str, base_url: str) -> str:
    return f"{(account_id or '').strip()}\n{(base_url or CATALOG_BASE_URL).strip().rstrip('/')}"


def _remember_live(key: str, models: list[CatalogModel]) -> None:
    _live_cache[key] = (models, time.monotonic())
    _negative_until.pop(key, None)


def refresh_live_catalog(
    access_token: str,
    account_id: str = "",
    *,
    base_url: str = "",
    force: bool = False,
) -> list[CatalogModel]:
    """TTL-cached live discovery; failures are negative-cached and re-raised.

    The failure is raised so listing surfaces can be honest about *why* they
    are showing a cached/fallback list; the provider path catches it and
    keeps serving with whatever metadata it has.
    """
    key = _cache_key(account_id, base_url)
    now = time.monotonic()
    cached = _live_cache.get(key)
    if not force and cached is not None and now - cached[1] < _CATALOG_TTL_SECONDS:
        return cached[0]
    if not force and now < _negative_until.get(key, 0.0):
        raise CatalogFetchError("catalog fetch is in a negative-TTL window after a recent failure")
    try:
        models = fetch_live_catalog(access_token, account_id, base_url=base_url)
    except CatalogFetchError:
        _negative_until[key] = now + _CATALOG_NEGATIVE_TTL_SECONDS
        raise
    except Exception as exc:  # httpx transport errors
        _negative_until[key] = now + _CATALOG_NEGATIVE_TTL_SECONDS
        raise CatalogFetchError(f"catalog fetch failed ({type(exc).__name__})") from exc
    _remember_live(key, models)
    return models


def fetch_catalog_for_account(*, force: bool = False, base_url: str = "") -> list[CatalogModel]:
    """Resolve Machina's ChatGPT credentials and run TTL-cached discovery."""
    from machina.security import chatgpt_auth

    tokens = chatgpt_auth.resolve_tokens()
    access = str(tokens.get("access_token") or "")
    account = str(tokens.get("account_id") or "") or (chatgpt_auth.extract_account_id(access) or "")
    if not access:
        raise CatalogFetchError("no ChatGPT access token available")
    return refresh_live_catalog(access, account, base_url=base_url, force=force)


def _read_codex_cache() -> list[CatalogModel]:
    """Read the Codex CLI's ``models_cache.json`` (read-only local hint)."""
    global _codex_cache
    codex_home = Path(
        os.environ.get("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    ).expanduser()
    path = codex_home / "models_cache.json"
    try:
        stat = path.stat()
    except OSError:
        return []
    now = time.monotonic()
    if _codex_cache is not None and now - _codex_cache[0] < _CODEX_CACHE_TTL_SECONDS:
        return _codex_cache[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        models = parse_catalog(payload, source="codex-cache") if stat.st_size else []
    except Exception as exc:
        logger.debug("Codex models cache unreadable (%s)", type(exc).__name__)
        models = []
    _codex_cache = (now, models)
    return models


def _most_recent_live(*, fresh_only: bool = False) -> list[CatalogModel] | None:
    """Live entries of the most recently fetched account.

    The unknown-account fallback for display surfaces without a resolved
    auth context: the freshest data available wins; account-scoped callers
    never go through here (review round 6, R6-1).
    """
    now = time.monotonic()
    best_models: list[CatalogModel] | None = None
    best_fetched = -1.0
    for models, fetched in _live_cache.values():
        if not models or (fresh_only and now - fetched >= _CATALOG_TTL_SECONDS):
            continue
        if fetched > best_fetched:
            best_models, best_fetched = models, fetched
    return best_models


def cached_catalog(account_id: str = "", base_url: str = "") -> list[CatalogModel]:
    """Best available catalog without network I/O.

    Precedence: live data for the requested account (or, when the account is
    unknown, the most recently fetched live entry — real data beats
    snapshots) -> Codex CLI models cache -> bundled verified snapshot.
    Account-scoped callers must pass their account so one account's live
    list is never served for another (review round 6, R6-1); with an account
    present and no live entry for it, the non-live sources answer instead of
    another account's list.
    """
    if account_id or base_url:
        entry = _live_cache.get(_cache_key(account_id, base_url))
        if entry and entry[0]:
            return entry[0]
    else:
        live = _most_recent_live()
        if live:
            return live
    models = _read_codex_cache()
    if models:
        return models
    return _fallback_models()


def catalog_source(account_id: str = "", base_url: str = "") -> str:
    """Source tag for :func:`cached_catalog` without materializing entries."""
    models = cached_catalog(account_id, base_url)
    return models[0].source if models else "none"


def find_model(slug: str | None, models: list[CatalogModel] | None = None) -> CatalogModel | None:
    """Exact slug lookup, case-insensitive, over *models* (default cached).

    Catalog slugs never contain ``/``, so a vendor-prefixed id like
    ``chatgpt/gpt-6.1-sol`` resolves via its last path segment (the provider
    normalizes prefixes the same way).
    """
    name = str(slug or "").strip()
    if not name:
        return None
    entries = cached_catalog() if models is None else models
    candidates = (name, name.rsplit("/", 1)[-1] if "/" in name else name)
    for candidate in candidates:
        for model in entries:
            if model.slug == candidate:
                return model
    lowered = [candidate.lower() for candidate in candidates]
    return next(
        (model for model in entries if model.slug.lower() in lowered),
        None,
    )


def live_models_or_none(account_id: str = "", base_url: str = "") -> list[CatalogModel] | None:
    """Fresh in-process live entries, or None when none were fetched yet.

    Callers that know their account get exact-key isolation (R6-1); the
    unknown-account form keeps the documented most-recent-entry fallback.
    """
    if account_id or base_url:
        entry = _live_cache.get(_cache_key(account_id, base_url))
        if entry and entry[0] and time.monotonic() - entry[1] < _CATALOG_TTL_SECONDS:
            return entry[0]
        return None
    return _most_recent_live(fresh_only=True)


def window_for(slug: str | None, *, account_id: str = "", base_url: str = "") -> int | None:
    """Context window for a ChatGPT slug from live/cache data only (no net).

    Falls back to the bundled snapshot last. Returns None when the slug is
    unknown to every source so the caller keeps its own default.
    """
    entries = cached_catalog(account_id, base_url) if (account_id or base_url) else None
    model = find_model(slug, entries)
    return model.context_window if model is not None else None


def supported_efforts_for(
    slug: str | None, *, account_id: str = "", base_url: str = ""
) -> tuple[str, ...] | None:
    """Per-model supported efforts from the entry for THAT slug, or None."""
    entries = cached_catalog(account_id, base_url) if (account_id or base_url) else None
    model = find_model(slug, entries)
    return model.supported_efforts if model is not None else None


def validate_model_slug(slug: str, *, account_id: str = "", base_url: str = "") -> str:
    """Reject a slug the *live* catalog definitively lacks.

    Strictness is deliberately scoped: only fresh live discovery can reject
    (a dated snapshot or the CLI cache must never block a model the account
    gained since). With no live data the slug is accepted — the backend is
    the final authority — and the call proceeds normally.
    """
    name = str(slug or "").strip()
    live = live_models_or_none(account_id, base_url)
    if live and find_model(name, live) is None:
        available = ", ".join(model.slug for model in visible_models(live)) or "none"
        raise ValidationError(
            f"unknown ChatGPT model {name!r} (not in the live catalog); available: {available}"
        )
    return name


def validate_effort(
    slug: str | None,
    effort: str | None,
    *,
    account_id: str = "",
    base_url: str = "",
) -> str:
    """Validate one effort against the catalog entry for *slug*.

    Returns the normalized effort or "" for the unset idiom. Raises
    :class:`ValidationError` naming the model's supported set when the
    effort is not in it; a model unknown to every source falls back to the
    wire vocabulary only (never another model's set).
    """
    requested = str(effort or "").strip().lower()
    if requested in UNSET_EFFORT_SENTINELS:
        return ""
    supported = supported_efforts_for(slug, account_id=account_id, base_url=base_url)
    if supported is None:
        if requested not in WIRE_REASONING_EFFORTS:
            raise ValidationError(
                f"reasoning effort {requested!r} is not a wire effort; "
                f"supported: {', '.join(WIRE_REASONING_EFFORTS)}"
            )
        return requested
    if requested not in supported:
        entries = cached_catalog(account_id, base_url) if (account_id or base_url) else None
        model = find_model(slug, entries)
        advertised = model.advertised_efforts if model is not None else ()
        extra = ""
        if requested in advertised:
            extra = (
                " (advertised by the Codex catalog but rejected by the Responses "
                "transport: HTTP 400, live-verified 2026-10-14)"
            )
        raise ValidationError(
            f"reasoning effort {requested!r} is not supported for {slug}{extra}; "
            f"supported: {', '.join(supported)}"
        )
    return requested


def clear_catalog_cache() -> None:
    """Test seam: drop in-process live and Codex-cache state."""
    global _codex_cache
    _live_cache.clear()
    _negative_until.clear()
    _codex_cache = None
