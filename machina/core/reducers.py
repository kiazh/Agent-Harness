"""Deterministic format-specific result reducers (token-efficiency T3).

A reducer converts an oversized tool result into a bounded, information-dense
view: decisive lines, counts, schemas, and omissions — never a silent
truncation. Contract:

- **Deterministic.** No model calls, no randomness; identical input yields
  identical output.
- **Unchanged when unsafe.** Parse failure or no net size benefit returns
  ``changed=False`` and the caller keeps its existing behavior (blind cap);
  the head+tail fallback always produces a bounded view for oversized input,
  so callers with no durable handle must treat any ``changed=True`` result as
  lossy and require recovery before publishing it.
- **Recoverable or honest.** A lossy view names its omissions and points at
  the retained original (artifact handle); the caller is responsible for
  storing that original before publishing the view. Standalone callers
  without a handle get an honest re-run pointer instead.
- **Token-counted after wrappers.** The disclosure marker is part of the
  output; only a view that fits the budget with its marker counts as reduced.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from machina.core.assembler import get_token_count

__all__ = ["ReductionResult", "VIEW_MARKER", "reduce_tool_result"]

# Detection prefix for already-reduced views. ``prompt.condense_history``
# skips messages carrying it (re-condensing a curated view mutilates it) and
# the agent's persistence path never treats it as raw source.
VIEW_MARKER = "[machina view:"

_SUMMARY_COUNT_RE = re.compile(
    r"\b(\d+)\s+(passed|failed|error|errors|skipped|deselected|xfailed|xpassed|warnings?)\b"
)
_FAILED_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+\S")
_ASSERTION_RE = re.compile(r"^E\s+\S")
_HUNK_RE = re.compile(r"^@@ .*@@")
_LOGLINE_RE = re.compile(
    r"^\[?\d{4}-\d{2}-\d{2}|^\d{2}:\d{2}:\d{2}|"
    r"\b(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|TRACE|FATAL)\b"
)
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s*[:|]\s?")
_DEF_LINE_RE = re.compile(
    r"^\s*(?:\d+\s*[:|]\s?)?(?:async\s+def|def|class|function|export|import|from|"
    r"interface|struct|enum|impl|fn|pub\s+fn|type\s+\w+\s*=)\b"
)
_LEVEL_HIGH_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|WARN(?:ING)?)\b", re.IGNORECASE)
# Normalization for repeated-log grouping.
_LOG_NORMALIZE_RE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "#"),
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "#TS#",
    ),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "#HEX#"),
    (re.compile(r"\b\d+\b"), "#"),
]


@dataclass
class ReductionResult:
    """Outcome of a reduction attempt (unknown fields stay None)."""

    output: str
    changed: bool
    method: str
    lossy: bool = False
    recoverable: bool = False
    artifact_id: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    token_count_basis: str = "tiktoken"
    source_version: str | None = None
    omissions: list[str] = field(default_factory=list)


def _disclosure(
    method: str, total_chars: int, artifact_id: str | None, omitted: dict[str, int]
) -> str:
    """Stable model-visible tail stating the method, omissions, and recovery."""
    parts = [f"{VIEW_MARKER} reduced by {method}"]
    if omitted:
        detail = "; ".join(
            f"{value} {name} omitted" if value != 1 else f"1 {name[:-1]} omitted"
            for name, value in omitted.items()
            if value
        )
        if detail:
            parts.append(detail)
    if artifact_id:
        parts.append(
            f"original {total_chars} chars retained as artifact {artifact_id}; "
            f"artifact_read pages the exact original"
        )
    else:
        parts.append(f"original {total_chars} chars; re-run the tool for the full output")
    # ``parts[0]`` already opens with VIEW_MARKER's own bracket. Adding a
    # second opening bracket would break ``prompt._REDUCED_VIEW_TAIL_RE`` (it
    # anchors a single ``[machina view:``), and the next stale-round
    # condensation pass would then mutilate the curated view.
    return "\n" + "; ".join(parts) + "]"


def _truncate_line(line: str, cap: int = 400) -> str:
    return line if len(line) <= cap else line[:cap] + "…"


def _pack(lines: list[tuple[int, str]], budget: int) -> tuple[str, int]:
    """Pack ``(priority, line)`` candidates into *budget* chars.

    Candidates are stably sorted by priority (0 first) so decisive lines are
    attempted before details regardless of detector append order. Within a
    priority the input order is kept. Returns ``(text, dropped_count)``.
    """
    kept: list[str] = []
    used = 0
    dropped = 0
    for _priority, line in sorted(lines, key=lambda item: item[0]):
        cost = len(line) + 1
        if used + cost > budget:
            dropped += 1
            continue
        kept.append(line)
        used += cost
    return "\n".join(kept), dropped


def _reduce_tests(text: str, budget: int) -> list[tuple[int, str]] | None:
    lines = text.splitlines()
    summary: list[str] = []
    failures: list[str] = []
    assertions: list[str] = []
    zero_collected = False
    for line in lines:
        if _SUMMARY_COUNT_RE.search(line):
            summary.append(_truncate_line(line.strip(), 200))
        elif _FAILED_LINE_RE.match(line):
            failures.append(_truncate_line(line, 400))
        elif _ASSERTION_RE.match(line):
            assertions.append(_truncate_line(line, 300))
        elif "no tests ran" in line or "collected 0 items" in line:
            zero_collected = True
    if zero_collected:
        summary.insert(0, "0 tests collected — this is not a passing verification")
    if not summary and not failures:
        # A lone build-log exit line is not a test run; let logs/head_tail
        # handle it instead of mislabeling the view.
        return None
    out: list[tuple[int, str]] = [(0, "[test run]")]
    for line in dict.fromkeys(summary):  # dedupe, keep order
        out.append((0, _truncate_line(line, 200)))
    # Decisive failures before assertion details so the budget cuts detail,
    # never the names of what failed.
    for line in dict.fromkeys(failures):
        out.append((1, line))
    for line in dict.fromkeys(assertions):
        out.append((2, line))
    return out


def _reduce_json(text: str, budget: int) -> list[tuple[int, str]] | None:
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    out: list[tuple[int, str]] = []
    if isinstance(parsed, dict):
        keys = [str(key) for key in parsed]
        out.append((0, f"[json object] {len(keys)} keys, {len(text)} chars"))
        shown = ", ".join(keys[:40]) + (", …" if len(keys) > 40 else "")
        out.append((0, f"keys: {shown}"))
        for key, value in list(parsed.items())[:12]:
            if isinstance(value, list):
                out.append((1, f"  {key}: list[{len(value)}]"))
            elif isinstance(value, dict):
                out.append((1, f"  {key}: object{{{', '.join(str(k) for k in list(value)[:8])}}}"))
            else:
                rendered = repr(value)
                out.append(
                    (1, f"  {key}: {type(value).__name__} = {_truncate_line(rendered, 120)}")
                )
    else:
        out.append((0, f"[json array] {len(parsed)} items, {len(text)} chars"))
        kinds: dict[str, int] = {}
        for item in parsed[:2000]:
            kinds[type(item).__name__] = kinds.get(type(item).__name__, 0) + 1
        out.append((0, "item kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))))
        for index, item in enumerate(parsed[:5]):
            if isinstance(item, dict):
                out.append((1, f"  [{index}] {{{', '.join(str(k) for k in list(item)[:8])}}}"))
    # Lexical-preserving raw head so exact spellings (big numbers, ordering)
    # remain visible for the leading region; the artifact keeps the whole.
    head_budget = max(200, budget // 3)
    out.append((1, f"first chars (verbatim):\n{text[:head_budget]}"))
    return out


def _reduce_csv(text: str, budget: int) -> list[tuple[int, str]] | None:
    lines = text.splitlines()
    if len(lines) < 3 or "," not in lines[0]:
        return None
    header_commas = lines[0].count(",")
    if header_commas < 1:
        return None
    checked = lines[: min(len(lines), 200)]
    if any(line.count(",") != header_commas for line in checked):
        return None
    out: list[tuple[int, str]] = []
    header = lines[0]
    columns = next(csv.reader(io.StringIO(header)))
    data_lines = lines[1:]
    out.append((0, f"[csv] {len(data_lines)} rows, {len(columns)} columns, {len(text)} chars"))
    out.append((0, f"columns: {', '.join(columns[:30])}"))
    # Deterministic numeric summary over the first bounded sample.
    sample = data_lines[:5000]
    numbers: dict[int, list[float]] = {}
    nonempty = [0] * len(columns)
    for line in sample:
        try:
            row = next(csv.reader(io.StringIO(line)))
        except (csv.Error, StopIteration):
            continue
        for index, cell in enumerate(row[: len(columns)]):
            if cell != "":
                nonempty[index] += 1
            try:
                numbers.setdefault(index, []).append(float(cell))
            except ValueError:
                numbers.pop(index, None)
    for index, column in enumerate(columns[:30]):
        values = numbers.get(index)
        if values and len(values) == len(sample):
            out.append(
                (
                    1,
                    f"  {column}: numeric min={min(values):g} max={max(values):g} n={len(values)}",
                )
            )
        else:
            out.append((2, f"  {column}: non-empty {nonempty[index]}/{len(sample)}"))
    for line in lines[:4]:
        out.append((1, _truncate_line(line, 300)))
    return out


def _reduce_diff(text: str, budget: int) -> list[tuple[int, str]] | None:
    lines = text.splitlines()
    hunks = [line for line in lines if _HUNK_RE.match(line)]
    file_headers = [
        line for line in lines if line.startswith("diff --git") or line.startswith("+++ ")
    ]
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    if not hunks and not file_headers:
        return None
    out: list[tuple[int, str]] = [
        (0, f"[diff] {len(file_headers)} file markers, {len(hunks)} hunks, +{added} -{removed}")
    ]
    for line in file_headers[:20]:
        out.append((0, _truncate_line(line, 200)))
    for line in hunks[:20]:
        out.append((1, _truncate_line(line, 200)))
    # First changed lines give a concrete sample; full artifact keeps all.
    sampled = [line for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    for line in sampled[:20]:
        out.append((2, _truncate_line(line, 240)))
    return out


def _reduce_code(text: str, budget: int) -> list[tuple[int, str]] | None:
    lines = text.splitlines()
    if len(lines) < 40:
        return None
    numbered = sum(1 for line in lines[:200] if _NUMBERED_LINE_RE.match(line))
    structural = [line for line in lines if _DEF_LINE_RE.match(line)]
    if numbered < len(lines[:200]) * 0.5 and len(structural) < 15:
        return None
    out: list[tuple[int, str]] = [
        (0, f"[code outline] {len(lines)} lines, {len(structural)} structural lines")
    ]
    for line in structural[:60]:
        out.append((0, _truncate_line(line, 240)))
    for line in lines[:3]:
        out.append((2, _truncate_line(line, 200)))
    return out


def _reduce_logs(text: str, budget: int) -> list[tuple[int, str]] | None:
    lines = text.splitlines()
    if len(lines) < 30:
        return None
    structured = sum(1 for line in lines[:200] if _LOGLINE_RE.search(line))
    if structured < len(lines[:200]) * 0.4:
        return None
    groups: dict[str, dict[str, Any]] = {}
    high: list[str] = []
    for line in lines:
        if _LEVEL_HIGH_RE.search(line) and len(high) < 30:
            high.append(_truncate_line(line, 300))
        normalized = line
        for pattern, replacement in _LOG_NORMALIZE_RE:
            normalized = pattern.sub(replacement, normalized)
        key = normalized[:200]
        group = groups.setdefault(key, {"count": 0, "first": line, "last": line})
        group["count"] += 1
        group["last"] = line
    out: list[tuple[int, str]] = [
        (0, f"[logs] {len(lines)} lines, {len(groups)} distinct patterns")
    ]
    for line in high:
        out.append((0, line))
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["first"]))
    for group in ordered[:30]:
        if group["count"] == 1:
            continue
        out.append(
            (
                1,
                f"x{group['count']}: {_truncate_line(group['first'], 240)}",
            )
        )
    for line in lines[:3]:
        out.append((2, _truncate_line(line, 240)))
    for line in lines[-2:]:
        out.append((2, _truncate_line(line, 240)))
    return out


def _reduce_head_tail(text: str, budget: int) -> list[tuple[int, str]] | None:
    """Fallback: keep both ends, always, with a marked omission seam.

    ``budget`` is the effective detector budget (disclosure already
    subtracted). The marker and separator costs are accounted here so the
    packer never drops the tail — the verdicts it carries are the whole point
    of this fallback.
    """
    marker_reserve = 40  # worst case: "…[99999999 chars omitted]…"
    usable = budget - marker_reserve - 3  # two separators + safety
    if usable < 2 or len(text) <= usable + marker_reserve:
        return None
    head = max(1, int(usable * 0.6))
    tail = max(1, usable - head)
    omitted = len(text) - head - tail
    marker = f"…[{omitted} chars omitted]…"
    return [(0, text[:head]), (1, marker), (2, text[-tail:])]


_DETECTORS = (
    ("tests", _reduce_tests),
    ("diff", _reduce_diff),
    ("json", _reduce_json),
    ("csv", _reduce_csv),
    ("logs", _reduce_logs),
    ("code", _reduce_code),
    ("head_tail", _reduce_head_tail),
)


def reduce_tool_result(
    text: str,
    *,
    budget_chars: int,
    artifact_id: str | None = None,
    source_version: str | None = None,
) -> ReductionResult:
    """Reduce *text* to at most *budget_chars* including its disclosure.

    The first detector that recognizes the format and produces a view whose
    packed body plus disclosure fits the budget wins. Otherwise the result is
    unchanged (``changed=False``) and the caller keeps its existing behavior.
    """
    if not isinstance(text, str) or budget_chars <= 0 or len(text) <= budget_chars:
        return ReductionResult(
            output=text, changed=False, method="none", tokens_before=None, tokens_after=None
        )
    tokens_before = get_token_count(text)
    # Reserve covers the disclosure including worst-case omission detail
    # ("999 view entries omitted") so the trimmed fallback below is a pure
    # safety net that never fires for normal detectors.
    reserve = len(_disclosure("x", len(text), artifact_id, {})) + 48
    # Detectors size their output against the same effective budget the
    # packer uses, so internal sizing (e.g. head+tail split) matches reality.
    detector_budget = max(1, budget_chars - reserve)
    for method, detector in _DETECTORS:
        try:
            candidates = detector(text, detector_budget)
        except Exception:
            continue
        if not candidates:
            continue
        body, dropped = _pack(candidates, detector_budget)
        if not body or len(body) >= len(text):
            continue
        omitted = {"lines": dropped} if dropped else {}
        disclosure = _disclosure(method, len(text), artifact_id, omitted)
        output = body + disclosure
        if len(output) > budget_chars:
            # Trim the body to make room for the disclosure instead of
            # dropping the recovery pointer.
            body = body[: max(0, budget_chars - len(disclosure))]
            output = body + disclosure
        if len(output) >= len(text):
            continue
        tokens_after = get_token_count(output)
        if tokens_after >= tokens_before:
            continue
        omission_note = (
            [f"{dropped} view {'entry' if dropped == 1 else 'entries'} dropped to fit the budget"]
            if dropped
            else []
        )
        return ReductionResult(
            output=output,
            changed=True,
            method=method,
            lossy=True,
            recoverable=artifact_id is not None,
            artifact_id=artifact_id,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            source_version=source_version,
            omissions=omission_note,
        )
    return ReductionResult(
        output=text,
        changed=False,
        method="none",
        tokens_before=tokens_before,
        tokens_after=tokens_before,
    )
