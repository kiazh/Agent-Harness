"""Provider usage normalization for the durable ledger.

Providers report overlapping but differently shaped usage figures:

- Anthropic separates cache reads/writes from ordinary input tokens
  (``input_tokens`` excludes both cache categories).
- OpenAI-compatible endpoints include cached input as a *subset* of
  ``prompt_tokens`` (``prompt_tokens_details.cached_tokens``).
- DeepSeek reports ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
  which partition the prompt.
- ChatGPT (Responses API) reports ``input_tokens_details.cached_tokens``.

The ledger needs one mutually exclusive split:

    prompt_tokens (= logical input) = uncached + cache_read + cache_write

so cost can be computed per category. Providers call these helpers instead of
rebuilding three-key dicts, and every result keeps the legacy keys
(``prompt_tokens``/``completion_tokens``/``total_tokens``) working.

Missing is not zero: when a provider family that reports cache details does
not include them, the cache split stays unknown (``cache_reported`` False)
rather than being recorded as a zero-cache reading. ``usage_basis`` labels
provider-reported versus locally estimated figures; the ledger must never
present an estimate as a bill.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "parse_anthropic_usage",
    "parse_openai_usage",
    "parse_chatgpt_usage",
    "parse_ollama_usage",
]


def _nonneg_int(value: Any) -> int | None:
    """Coerce *value* to a non-negative int, else None (missing is not zero)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _finalize(
    uncached: int | None,
    cache_read: int | None,
    cache_write: int | None,
    output: int | None,
    *,
    total: int | None = None,
    cache_reported: bool,
    estimated: bool = False,
) -> dict[str, Any]:
    """Assemble the normalized usage dict from mutually exclusive categories.

    Every input may be None (unknown). The dict keeps the legacy three keys
    for every response so every existing consumer works unchanged. The
    mutually-exclusive split (``uncached_input_tokens``/``cache_read_tokens``/
    ``cache_write_tokens`` + ``usage_basis``) is added only when the provider
    actually reported cache categories — cache-absent responses stay exactly
    the legacy shape, and cache-unknown never becomes a fabricated zero.
    All-None input returns the legacy zeros (matching the old provider
    behavior); the ledger's numeric guard keeps that a conservative unknown.
    """
    uncached_i = uncached or 0
    read_i = cache_read or 0
    write_i = cache_write or 0
    output_i = output or 0
    logical_input = uncached_i + read_i + write_i
    total_i = total if total is not None else logical_input + output_i
    result: dict[str, Any] = {
        "prompt_tokens": logical_input,
        "completion_tokens": output_i,
        "total_tokens": max(total_i, logical_input + output_i),
    }
    if estimated:
        result["estimated"] = True
    if cache_reported:
        # Reported categories are copied verbatim; unreported ones stay
        # absent so the ledger stores NULL (missing is not zero). The sum
        # above only counts the known parts and the basis discloses when
        # that sum is partial.
        if uncached is not None:
            result["uncached_input_tokens"] = uncached_i
        if cache_read is not None:
            result["cache_read_tokens"] = read_i
        if cache_write is not None:
            result["cache_write_tokens"] = write_i
        result["cache_reported"] = True
        # Fully known = the input partition and the output count arrived.
        fully_known = uncached is not None and output is not None
        if estimated:
            # An estimate must never label itself as provider-reported usage.
            basis = "local_estimate"
        elif fully_known:
            basis = "provider"
        else:
            basis = "provider_partial"
        result["usage_basis"] = basis
    return result


def parse_anthropic_usage(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize an Anthropic ``usage`` block (streaming or non-streaming).

    ``input_tokens`` counts uncached input only; cache reads and cache
    creations are separate additive categories.
    """
    usage_in = raw or {}
    cache_read = _nonneg_int(usage_in.get("cache_read_input_tokens"))
    cache_write = _nonneg_int(usage_in.get("cache_creation_input_tokens"))
    return _finalize(
        _nonneg_int(usage_in.get("input_tokens")),
        cache_read,
        cache_write,
        _nonneg_int(usage_in.get("output_tokens")),
        # Value presence, not key presence: an explicit null is missing, and
        # a reported zero is a real reading.
        cache_reported=cache_read is not None or cache_write is not None,
    )


def parse_openai_usage(raw: dict[str, Any] | None, *, estimated: bool = False) -> dict[str, Any]:
    """Normalize an OpenAI-compatible ``usage`` block.

    ``prompt_tokens`` includes cached input, so the cache split is derived
    (``uncached = prompt - cached``). DeepSeek-style hit/miss fields take
    precedence when present. Endpoints that never report cache details keep
    ``cache_reported`` False; their prompt counts as uncached input, which is
    the conservative upper bound for cost.
    """
    usage_in = raw or {}
    prompt = _nonneg_int(usage_in.get("prompt_tokens"))
    output = _nonneg_int(usage_in.get("completion_tokens"))
    total = _nonneg_int(usage_in.get("total_tokens"))
    details = usage_in.get("prompt_tokens_details")
    cached = _nonneg_int(details.get("cached_tokens")) if isinstance(details, dict) else None
    hit = _nonneg_int(usage_in.get("prompt_cache_hit_tokens"))
    miss = _nonneg_int(usage_in.get("prompt_cache_miss_tokens"))
    if hit is not None or miss is not None:
        # DeepSeek partitions the prompt (hit + miss == prompt). These fields
        # are checked before ``prompt_tokens_details`` so a payload carrying
        # both shapes keeps the provider's explicit split instead of recording
        # the details' (often zero) cached count as the cache read.
        if hit is not None and miss is not None:
            uncached = miss
            cache_read = hit
        elif hit is not None and prompt is not None:
            uncached = max(prompt - hit, 0)
            cache_read = hit
        elif hit is not None:
            uncached = None
            cache_read = hit
        else:
            uncached = miss
            cache_read = None
    elif cached is not None and prompt is not None:
        uncached = max(prompt - cached, 0)
        cache_read = cached
    elif cached is not None:
        # Reported cache without a prompt total: keep the reading, leave the
        # uncached side unknown (partial basis) instead of fabricating zero.
        uncached = None
        cache_read = cached
    else:
        uncached = prompt
        cache_read = None
    cache_reported = cached is not None or hit is not None or miss is not None
    return _finalize(
        uncached,
        cache_read,
        None,
        output,
        total=total,
        cache_reported=cache_reported,
        estimated=estimated,
    )


def parse_chatgpt_usage(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a ChatGPT Responses-API usage block.

    ``input_tokens`` includes cached input (``input_tokens_details``).
    """
    usage_in = raw or {}
    input_tokens = _nonneg_int(usage_in.get("input_tokens"))
    output = _nonneg_int(usage_in.get("output_tokens"))
    total = _nonneg_int(usage_in.get("total_tokens"))
    details = usage_in.get("input_tokens_details")
    cached = _nonneg_int(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is not None and input_tokens is not None:
        uncached = max(input_tokens - cached, 0)
        cache_read = cached
    elif cached is not None:
        uncached = None  # reported cache, unknown input total -> partial basis
        cache_read = cached
    else:
        uncached = input_tokens
        cache_read = None
    return _finalize(
        uncached,
        cache_read,
        None,
        output,
        total=total,
        cache_reported=cached is not None,
    )


def parse_ollama_usage(prompt_eval_count: Any, eval_count: Any) -> dict[str, Any]:
    """Normalize Ollama's ``prompt_eval_count`` / ``eval_count`` pair.

    Local models report no cache categories and no billing; the ledger keeps
    the figures with a provider basis so "provider versus estimate" stays
    distinguishable (cost remains unknown without a price entry).
    """
    return _finalize(
        _nonneg_int(prompt_eval_count),
        None,
        None,
        _nonneg_int(eval_count),
        cache_reported=False,
    )
