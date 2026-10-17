"""Check the canonical Machina token counter against tiktoken.

Prints one row per sample:

    sample  chars  tiktoken  fallback  delta  truncate

``tiktoken`` is the direct ``cl100k_base`` encoding length. ``fallback`` is
the deterministic offline heuristic (``assembler._fallback_token_count``)
used when tiktoken is unavailable or rejects literal special-token text.
``delta`` is the fallback's signed error; for dense scripts (CJK, kana,
hangul, emoji, mixed) the tool fails when ``|delta| > --tolerance`` (default
25%). Every sample must also satisfy ``fallback * 4 >= tiktoken`` (never 4x
low) and ``truncate`` verifies ``_fallback_prefix`` stays a true prefix whose
fallback count never exceeds the requested budget.

Exit status: 0 = pass, 1 = tolerance/invariant failure. Runs without a
database, provider, or configuration file; pass ``--tolerance 0.0001`` to
exercise the failure path.
"""

from __future__ import annotations

import argparse
import sys

from machina.core.assembler import (
    _fallback_prefix,
    _fallback_token_count,
    get_token_count,
)

# Kept in sync with tests/test_tok_token_counter.py (DevTools).
SAMPLES: dict[str, str] = {
    "ascii": "The quick brown fox jumps over the lazy dog. " * 4,
    "code": "def f(x):\n    return {'a': x + 1}\n" * 5,
    "json": '{"a": 1, "b": [true, false, null], "c": "hello"}' * 5,
    "punctuation": "!!!???...,,,;;;:::'\"'\"()[]{}<>---___" * 4,
    "emoji": "🎉🚀😀👍🏽❤️🔥" * 4,
    "cjk": "你好世界，这是一个测试。今天天气很好。" * 3,
    "kana": "こんにちは、これはテストです。カタカナも。" * 3,
    "hangul": "안녕하세요 이것은 테스트입니다." * 3,
    "mixed": "Hello 世界! 🎉 你好，这是 mixed 文本。" * 4,
}

# Samples whose fallback must track tiktoken within the documented tolerance.
DENSE = frozenset({"emoji", "cjk", "kana", "hangul", "mixed"})

TRUNCATE_BUDGETS = (1, 5, 20)


def _direct_tiktoken_count(text: str) -> int | None:
    """Direct ``cl100k_base`` length, or None when tiktoken is not installed."""
    try:
        import tiktoken
    except ImportError:
        return None
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def _truncate_fits(text: str, budget: int) -> bool:
    out = _fallback_prefix(text, budget)
    return text.startswith(out) and _fallback_token_count(out) <= budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.25,
        help="maximum relative fallback error for dense samples (default 0.25)",
    )
    args = parser.parse_args(argv)
    if args.tolerance < 0:
        parser.error("--tolerance must be non-negative")

    failures: list[str] = []
    rows: list[tuple[str, int, str, int, str, str]] = []
    have_tiktoken = True

    for name, text in SAMPLES.items():
        expected = _direct_tiktoken_count(text)
        fallback = _fallback_token_count(text)
        canonical = get_token_count(text)
        truncate_ok = all(_truncate_fits(text, budget) for budget in TRUNCATE_BUDGETS)

        if expected is None:
            have_tiktoken = False
            tiktoken_text = "n/a"
            delta_text = "n/a"
            if canonical != fallback:
                failures.append(f"{name}: get_token_count != fallback without tiktoken")
        else:
            tiktoken_text = str(expected)
            delta = (fallback - expected) / expected if expected else 0.0
            delta_text = f"{delta * 100:+.1f}%"
            if canonical != expected:
                failures.append(f"{name}: get_token_count={canonical} != tiktoken={expected}")
            if fallback * 4 < expected:
                failures.append(f"{name}: fallback is more than 4x low ({fallback} vs {expected})")
            if name in DENSE and abs(delta) > args.tolerance:
                failures.append(
                    f"{name}: fallback={fallback} tiktoken={expected} "
                    f"delta={delta_text} exceeds tolerance {args.tolerance:.0%}"
                )

        if not truncate_ok:
            failures.append(f"{name}: truncate exceeded budget or was not a prefix")
        rows.append(
            (
                name,
                len(text),
                tiktoken_text,
                fallback,
                delta_text,
                "ok" if truncate_ok else "FAIL",
            )
        )

    header = ("sample", "chars", "tiktoken", "fallback", "delta", "truncate")
    widths = [max(len(header[i]), *(len(str(row[i])) for row in rows)) for i in range(6)]
    print("  ".join(value.ljust(widths[i]) for i, value in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)))

    if not have_tiktoken:
        print("\ntiktoken is not installed: counts cannot be compared to cl100k_base,")
        print("so only fallback invariants (determinism, minimum 1, truncate budget) were checked.")

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nPASS ({len(rows)} samples, dense tolerance ±{args.tolerance:.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
