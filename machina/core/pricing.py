"""Versioned LLM price table and cost calculation.

Cost is an *estimate* derived from a dated price snapshot plus normalized
token categories. Missing price data yields ``None`` cost — never zero — and
the ledger records which price version produced a figure so old estimates
stay auditable.

Built-in entries exist only where this repository holds a verified snapshot
and are keyed ``provider/model``: rates differ per model, so a model without
its own entry stays unknown even when its provider has one — a sibling's
snapshot must never price it. Adding an entry or changing a rate requires a
new dated snapshot; do not copy headline discounts from vendor pages without
recording the verification date. Snapshot scope is the verified window (the
DeepSeek entry is peak-window list rates), so off-peak discounts make it a
conservative upper bound rather than an exact bill. Users can extend or
override the table with the ``pricing_table_json`` config value (same shape
as :data:`PRICE_TABLE` entries, keyed ``provider/model``; a provider-only
key applies to every model of that provider):

    {"anthropic/claude-3-5-sonnet": {"input_per_mtok": 3.0,
                   "output_per_mtok": 15.0, "cache_read_per_mtok": 0.3,
                   "cache_write_per_mtok": 3.75,
                   "verified_at": "2026-10-16", "source": "console pricing"}}

Unknown fields stay unknown. An override that omits ``verified_at`` or
``source`` is recorded as unverified/override — never as the base snapshot's
provenance. Rate units are USD per million tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PRICE_TABLE_VERSION",
    "PriceEntry",
    "lookup_price",
    "estimate_cost",
    "parse_overrides",
]

# Bump when the built-in table changes; stored on every priced row.
PRICE_TABLE_VERSION = "2026-10-16"


@dataclass(frozen=True)
class PriceEntry:
    """USD-per-million-token rates for one provider (or provider/model key).

    ``None`` on any rate means unknown — cost computation that needs that
    category returns None instead of guessing.
    """

    input_per_mtok: float | None
    output_per_mtok: float | None
    cache_read_per_mtok: float | None
    cache_write_per_mtok: float | None
    verified_at: str
    source: str


# Built-in snapshots, keyed ``provider/model``: model rates differ, so an
# unlisted model stays unknown instead of inheriting a sibling's entry.
# Only providers with an in-repo verified snapshot are listed; everything
# else stays unknown until configured or verified.
PRICE_TABLE: dict[str, PriceEntry] = {
    # Snapshot taken from tools/harness_evals_pilot.py (2026-10-14): peak
    # window rates. cache_miss is the ordinary input rate; DeepSeek charges
    # no separate cache-write premium (writes are ordinary misses), so the
    # write rate mirrors input rather than inventing a multiplier.
    "deepseek/deepseek-chat": PriceEntry(
        input_per_mtok=0.30,
        output_per_mtok=1.20,
        cache_read_per_mtok=0.006,
        cache_write_per_mtok=0.30,
        verified_at="2026-10-14",
        source="tools/harness_evals_pilot.py snapshot (peak window)",
    ),
}


def parse_overrides(raw: str | None) -> dict[str, dict[str, Any]]:
    """Parse the ``pricing_table_json`` config value into a key->fields map.

    Invalid JSON or a non-object shape returns {} (the setting is an optional
    enhancement; a malformed value must never break the ledger). Keys are
    matched exactly against provider identity or ``provider/model`` pairs.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, dict):
            overrides[key] = value
    return overrides


def _entry_from_override(value: dict[str, Any], base: PriceEntry | None) -> PriceEntry | None:
    """Merge an override dict over an optional base entry.

    Rates may be inherited from *base*, but provenance is not: an override
    that omits ``verified_at``/``source`` records "unverified"/"override" so a
    merged entry can never wear the base snapshot's dates for user-changed
    rates.
    """

    def rate(field: str) -> float | None:
        if field in value:
            candidate = value[field]
            if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
                return None
            if candidate < 0:
                return None
            return float(candidate)
        return getattr(base, field, None) if base is not None else None

    entry = PriceEntry(
        input_per_mtok=rate("input_per_mtok"),
        output_per_mtok=rate("output_per_mtok"),
        cache_read_per_mtok=rate("cache_read_per_mtok"),
        cache_write_per_mtok=rate("cache_write_per_mtok"),
        verified_at=str(value.get("verified_at") or "unverified"),
        source=str(value.get("source") or "override"),
    )
    if entry.input_per_mtok is None and entry.output_per_mtok is None:
        return None
    return entry


def _builtin_entry(provider_key: str, model_key: str) -> PriceEntry | None:
    """Built-in lookup on the exact ``provider/model`` key (case-normalized)."""
    if not model_key:
        return None
    return PRICE_TABLE.get(f"{provider_key}/{model_key}") or PRICE_TABLE.get(
        f"{provider_key}/{model_key.lower()}"
    )


def lookup_price(
    provider: str | None,
    model: str | None,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> PriceEntry | None:
    """Find the best price entry for *provider* (+ optional model).

    Resolution order: ``provider/model`` override, ``provider`` override,
    built-in ``provider/model`` entry. Built-ins are model-scoped, so an
    unknown model stays unknown (None) even when the provider has a verified
    entry — a sibling model's snapshot must never price it.
    """
    if not provider:
        return None
    provider_key = provider.strip().lower()
    model_key = (model or "").strip()
    overrides = overrides if overrides is not None else {}
    if model_key:
        for key in (f"{provider_key}/{model_key}", f"{provider_key}/{model_key.lower()}"):
            if key in overrides:
                base = _builtin_entry(provider_key, model_key)
                merged = _entry_from_override(overrides[key], base)
                if merged is not None:
                    return merged
    if provider_key in overrides:
        base = _builtin_entry(provider_key, model_key)
        merged = _entry_from_override(overrides[provider_key], base)
        if merged is not None:
            return merged
    return _builtin_entry(provider_key, model_key)


def estimate_cost(
    provider: str | None,
    model: str | None,
    *,
    uncached_input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> tuple[float | None, str | None]:
    """Return ``(cost_usd, price_version)`` or ``(None, None)`` when unknown.

    Categories with a zero token count do not require a rate: an entry may
    omit cache rates while ordinary input/output rates are known and the
    request genuinely used no cache. A missing rate for a *used* category
    makes the whole estimate unknown — the ledger must not under-report.
    """
    entry = lookup_price(provider, model, overrides=overrides)
    if entry is None:
        return None, None
    cost = 0.0
    for tokens, rate in (
        (uncached_input_tokens, entry.input_per_mtok),
        (cache_read_tokens, entry.cache_read_per_mtok),
        (cache_write_tokens, entry.cache_write_per_mtok),
        (output_tokens, entry.output_per_mtok),
    ):
        if tokens <= 0:
            continue
        if rate is None:
            return None, None
        cost += tokens * rate / 1_000_000
    version = f"{PRICE_TABLE_VERSION}:{entry.verified_at}"
    return cost, version
