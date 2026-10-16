"""Bounded intelligence payloads and the diagnostics store (plan §16/§17).

Language-server replies are untrusted data. Everything that leaves this module
is shape-normalized, bounds-checked and length-capped; nothing is fabricated
to fill a gap (a malformed range is dropped, never guessed, and an unavailable
state is never turned into a "clean" empty result).

The diagnostics store keeps one set per ``(uri, producer)`` with LSP replace
semantics: a publish replaces that producer's set, an empty list clears only
that producer, older-version publishes are rejected, and entries carry the
publishing server's generation so a result from a dead generation can never
become current (plan §16).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from machina.workspace.models import MAX_POSITION_VALUE, MAX_URI_CHARS

# ─── bounds (disclosed, never silently exceeded) ─────────────────────────────
MAX_COMPLETION_ITEMS = 100
MAX_ADDITIONAL_EDITS = 64
MAX_LABEL_CHARS = 512
MAX_DETAIL_CHARS = 512
MAX_DOCUMENTATION_CHARS = 512
MAX_EDIT_TEXT_CHARS = 4096
MAX_HOVER_CHARS = 4096
MAX_LOCATION_RESULTS = 500
MAX_SYMBOL_RESULTS = 500
MAX_SYMBOL_NAME_CHARS = 512
MAX_DIAGNOSTICS_PER_FILE = 1000
MAX_DIAGNOSTIC_MESSAGE_CHARS = 8192
MAX_SOURCE_CHARS = 200
MAX_CODE_CHARS = 200
MAX_LOG_DETAIL_CHARS = 500

#: Bounded per-operation deadlines for language-server requests (plan §19).
#: Configurable through ``WorkspaceService(intelligence_timeouts=...)``.
DEFAULT_INTELLIGENCE_TIMEOUTS: dict[str, float] = {
    "completion": 10.0,
    "hover": 10.0,
    "definition": 10.0,
    "references": 15.0,
    "symbols": 15.0,
}

#: Profile ids and language names accepted by ``workspace.lspControl``.
_PROFILE_LANGUAGE = {
    "python": "python",
    "pyright": "python",
    "typescript": "typescript",
    "javascript": "typescript",
    "typescriptreact": "typescript",
    "javascriptreact": "typescript",
    "typescript-language-server": "typescript",
}

#: Operations that need a cursor position (documentSymbol does not).
POSITION_OPERATIONS = ("completion", "hover", "definition", "references")


def language_for_profile(profile: str) -> str | None:
    """Canonical language for a control profile id/name; ``None`` if unknown."""
    return _PROFILE_LANGUAGE.get(profile.strip().lower())


# ─── log sanitizing (display/log surfaces only — never the transport) ───────
_UNSAFE_LOG_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[@-Z\\-_]"  # other escapes
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # control chars except \n and \t
)


def sanitize_log_text(text: str, limit: int = MAX_LOG_DETAIL_CHARS) -> str:
    """Strip terminal escapes/control characters and bound a log-only string."""
    return _UNSAFE_LOG_RE.sub("", text)[:limit]


# ─── normalization ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Normalized:
    """A successfully normalized payload plus whether anything was omitted."""

    result: Any
    truncated: bool = False


def _position(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    line = value.get("line")
    character = value.get("character")
    for number in (line, character):
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 0
            or number > MAX_POSITION_VALUE
        ):
            return None
    return {"line": line, "character": character}


def normalize_range(value: Any) -> dict[str, dict[str, int]] | None:
    """Validate an LSP range into ``{start, end}``; ``None`` means malformed."""
    if not isinstance(value, Mapping):
        return None
    start = _position(value.get("start"))
    end = _position(value.get("end"))
    if start is None or end is None:
        return None
    if (end["line"], end["character"]) < (start["line"], start["character"]):
        return None
    return {"start": start, "end": end}


def _bounded(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def flatten_hover_contents(contents: Any) -> str | None:
    """Flatten Hover.contents (string / MarkedString / MarkupContent / array).

    The result is inert text: markup is preserved for the UI to render safely,
    never executed, and the caller caps it at :data:`MAX_HOVER_CHARS`.
    """
    parts: list[str] = []
    if isinstance(contents, str):
        parts.append(contents)
    elif isinstance(contents, Mapping):
        value = contents.get("value")
        if isinstance(value, str):
            parts.append(value)
    elif isinstance(contents, (list, tuple)):
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                value = item.get("value")
                if isinstance(value, str):
                    parts.append(value)
    if not parts:
        return None
    return "\n\n".join(parts)


def normalize_hover(result: Any) -> Normalized | None:
    """Hover result → ``{"contents": str, "range"?: range}`` (capped ~4 KiB)."""
    if not isinstance(result, Mapping):
        return None
    text = flatten_hover_contents(result.get("contents"))
    if text is None:
        return None
    payload: dict[str, Any] = {"contents": text[:MAX_HOVER_CHARS]}
    rng = normalize_range(result.get("range"))
    if rng is not None:
        payload["range"] = rng
    return Normalized(payload, truncated=len(text) > MAX_HOVER_CHARS)


def _completion_edit(value: Any) -> dict[str, Any] | None:
    """Normalize a TextEdit / InsertReplaceEdit; malformed ranges are dropped."""
    if not isinstance(value, Mapping):
        return None
    new_text = value.get("newText")
    if not isinstance(new_text, str):
        return None
    rng = normalize_range(value.get("range"))
    if rng is None:
        # InsertReplaceEdit: prefer the insert range, fall back to replace.
        rng = normalize_range(value.get("insert")) or normalize_range(value.get("replace"))
    if rng is None:
        return None
    return {"range": rng, "newText": new_text[:MAX_EDIT_TEXT_CHARS]}


def _documentation_preview(value: Any) -> str | None:
    if isinstance(value, str):
        return value[:MAX_DOCUMENTATION_CHARS]
    if isinstance(value, Mapping):
        inner = value.get("value")
        if isinstance(inner, str):
            return inner[:MAX_DOCUMENTATION_CHARS]
    return None


def _completion_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    label = value.get("label")
    if not isinstance(label, str) or not label:
        return None
    item: dict[str, Any] = {"label": label[:MAX_LABEL_CHARS]}
    kind = value.get("kind")
    if isinstance(kind, int) and not isinstance(kind, bool) and 1 <= kind <= 25:
        item["kind"] = kind
    for key, limit in (
        ("detail", MAX_DETAIL_CHARS),
        ("filterText", MAX_LABEL_CHARS),
        ("sortText", MAX_LABEL_CHARS),
    ):
        text = value.get(key)
        if isinstance(text, str):
            item[key] = text[:limit]
    insert_text_format = value.get("insertTextFormat")
    if (
        isinstance(insert_text_format, int)
        and not isinstance(insert_text_format, bool)
        and insert_text_format in (1, 2)
    ):
        item["insertTextFormat"] = insert_text_format
    documentation = _documentation_preview(value.get("documentation"))
    if documentation is not None:
        item["documentation"] = documentation
    edit = _completion_edit(value.get("textEdit"))
    if edit is not None:
        item["textEdit"] = edit
    extra = value.get("additionalTextEdits")
    if isinstance(extra, (list, tuple)):
        edits = []
        for raw in extra[:MAX_ADDITIONAL_EDITS]:
            normalized = _completion_edit(raw)
            if normalized is not None:
                edits.append(normalized)
        if edits:
            item["additionalTextEdits"] = edits
    return item


def normalize_completion_list(result: Any) -> Normalized | None:
    """CompletionList / CompletionItem[] → bounded item payload (cap 100)."""
    if result is None:
        return None
    if isinstance(result, Mapping):
        raw_items = result.get("items")
        is_incomplete = bool(result.get("isIncomplete"))
    elif isinstance(result, (list, tuple)):
        raw_items = result
        is_incomplete = False
    else:
        return None
    if not isinstance(raw_items, (list, tuple)):
        return None
    items = []
    for raw in raw_items[:MAX_COMPLETION_ITEMS]:
        item = _completion_item(raw)
        if item is not None:
            items.append(item)
    omitted = max(0, len(raw_items) - MAX_COMPLETION_ITEMS)
    return Normalized(
        {"isIncomplete": is_incomplete, "items": items, "omitted": omitted},
        truncated=omitted > 0,
    )


def _location(value: Any) -> dict[str, Any] | None:
    """Normalize Location / LocationLink; malformed entries are dropped."""
    if not isinstance(value, Mapping):
        return None
    if isinstance(value.get("targetUri"), str):
        # LocationLink: prefer the selection range, fall back to the full one.
        uri = value["targetUri"]
        rng = normalize_range(value.get("targetSelectionRange")) or normalize_range(
            value.get("targetRange")
        )
    else:
        uri = value.get("uri")
        rng = normalize_range(value.get("range"))
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_CHARS:
        return None
    if rng is None:
        return None
    return {"uri": uri, "range": rng}


def normalize_locations(result: Any) -> Normalized | None:
    """Location / Location[] / LocationLink[] → bounded ``{uri, range}`` list."""
    if result is None:
        return None
    raw = result if isinstance(result, (list, tuple)) else [result]
    locations = []
    for entry in raw:
        location = _location(entry)
        if location is not None:
            locations.append(location)
    omitted = max(0, len(locations) - MAX_LOCATION_RESULTS)
    return Normalized(
        {"locations": locations[:MAX_LOCATION_RESULTS], "omitted": omitted},
        truncated=omitted > 0,
    )


def _symbol_fields(entry: Mapping[str, Any], *, depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"name": str(entry["name"])[:MAX_SYMBOL_NAME_CHARS]}
    kind = entry.get("kind")
    if isinstance(kind, int) and not isinstance(kind, bool) and 1 <= kind <= 26:
        node["kind"] = kind
    detail = entry.get("detail")
    if isinstance(detail, str) and detail:
        node["detail"] = detail[:MAX_DETAIL_CHARS]
    rng = normalize_range(entry.get("range"))
    if rng is not None:
        node["range"] = rng
    selection = normalize_range(entry.get("selectionRange"))
    if selection is not None:
        node["selectionRange"] = selection
    node["depth"] = depth
    return node


def _count_document_symbols(entries: Any) -> int:
    """Count valid DocumentSymbol nodes in a subtree (cap disclosure only)."""
    if not isinstance(entries, (list, tuple)):
        return 0
    count = 0
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        count += 1 + _count_document_symbols(entry.get("children"))
    return count


def _normalize_document_symbols(raw: list[Any]) -> Normalized:
    hierarchical: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    total = 0
    stack: list[tuple[Mapping[str, Any], int, list[dict[str, Any]]]] = [
        (entry, 0, hierarchical) for entry in reversed(raw) if isinstance(entry, Mapping)
    ]
    while stack:
        entry, depth, parent_list = stack.pop()
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        total += 1
        if len(flat) >= MAX_SYMBOL_RESULTS:
            # Past the cap nothing is materialized, but the remainder is still
            # counted so ``omitted`` stays exact (bounded by the response).
            total += _count_document_symbols(entry.get("children"))
            continue
        node = _symbol_fields(entry, depth=depth)
        parent_list.append(node)
        flat.append({key: value for key, value in node.items() if key != "detail"})
        children = entry.get("children")
        if isinstance(children, (list, tuple)):
            child_list = node.setdefault("children", [])
            for child in reversed(children):
                if isinstance(child, Mapping):
                    stack.append((child, depth + 1, child_list))
    omitted = max(0, total - len(flat))
    return Normalized(
        {"hierarchical": hierarchical, "flat": flat, "omitted": omitted},
        truncated=omitted > 0,
    )


def _normalize_symbol_information(raw: list[Any]) -> Normalized:
    flat: list[dict[str, Any]] = []
    total = 0
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        location = entry.get("location")
        if not isinstance(name, str) or not name or not isinstance(location, Mapping):
            continue
        rng = normalize_range(location.get("range"))
        if rng is None:
            continue
        total += 1
        if len(flat) >= MAX_SYMBOL_RESULTS:
            continue
        node: dict[str, Any] = {"name": name[:MAX_SYMBOL_NAME_CHARS], "range": rng, "depth": 0}
        kind = entry.get("kind")
        if isinstance(kind, int) and not isinstance(kind, bool) and 1 <= kind <= 26:
            node["kind"] = kind
        uri = location.get("uri")
        if isinstance(uri, str) and uri and len(uri) <= MAX_URI_CHARS:
            node["uri"] = uri
        container = entry.get("containerName")
        if isinstance(container, str) and container:
            node["containerName"] = container[:MAX_SYMBOL_NAME_CHARS]
        flat.append(node)
    # A deterministic tree derived from containerName; container names that do
    # not match a node (or a node itself) stay top-level. Nothing is invented:
    # the flat list remains the authoritative server data.
    nodes = [{key: value for key, value in node.items() if key != "containerName"} for node in flat]
    roots: list[dict[str, Any]] = []
    names: dict[str, dict[str, Any]] = {}
    for node in nodes:
        names.setdefault(node["name"], node)
    for node, source in zip(nodes, flat, strict=True):
        container = source.get("containerName")
        parent = names.get(container) if container else None
        if parent is not None and parent is not node:
            parent.setdefault("children", []).append(node)
        else:
            roots.append(node)
    omitted = max(0, total - len(flat))
    return Normalized(
        {"hierarchical": roots, "flat": flat, "omitted": omitted},
        truncated=omitted > 0,
    )


def normalize_symbols(result: Any) -> Normalized | None:
    """DocumentSymbol[] / SymbolInformation[] → ``{hierarchical, flat}``."""
    if not isinstance(result, (list, tuple)):
        return None
    if any(isinstance(entry, Mapping) and "location" in entry for entry in result):
        return _normalize_symbol_information(list(result))
    return _normalize_document_symbols(list(result))


def normalize_diagnostic(value: Any) -> dict[str, Any] | None:
    """Validate one published Diagnostic; malformed entries are dropped."""
    if not isinstance(value, Mapping):
        return None
    rng = normalize_range(value.get("range"))
    if rng is None:
        return None
    message = value.get("message")
    if not isinstance(message, str):
        return None
    severity = value.get("severity")
    if not (isinstance(severity, int) and not isinstance(severity, bool) and 1 <= severity <= 4):
        # LSP leaves an omitted severity to the client; "error" is conventional
        # and visible — never silently presented as information.
        severity = 1
    diagnostic: dict[str, Any] = {
        "range": rng,
        "severity": severity,
        "message": message[:MAX_DIAGNOSTIC_MESSAGE_CHARS],
    }
    source = value.get("source")
    if isinstance(source, str) and source:
        diagnostic["source"] = source[:MAX_SOURCE_CHARS]
    code = value.get("code")
    if isinstance(code, str):
        diagnostic["code"] = code[:MAX_CODE_CHARS]
    elif isinstance(code, int) and not isinstance(code, bool):
        diagnostic["code"] = code
    return diagnostic


# ─── fixed request mapping (the allowlist; no raw method passthrough) ───────
def lsp_request_for(
    operation: str, uri: str, position: dict[str, int] | None
) -> tuple[str, dict[str, Any]]:
    """Map one allowlisted operation to its standard LSP request.

    The application-side correlation fields (workspace/client/epoch/generation)
    are deliberately *not* part of the LSP message; the service attaches them
    to the result envelope only.
    """
    text_document = {"uri": uri}
    if operation == "completion":
        return "textDocument/completion", {
            "textDocument": text_document,
            "position": position,
        }
    if operation == "hover":
        return "textDocument/hover", {
            "textDocument": text_document,
            "position": position,
        }
    if operation == "definition":
        return "textDocument/definition", {
            "textDocument": text_document,
            "position": position,
        }
    if operation == "references":
        return "textDocument/references", {
            "textDocument": text_document,
            "position": position,
            "context": {"includeDeclaration": True},
        }
    if operation == "symbols":
        return "textDocument/documentSymbol", {"textDocument": text_document}
    raise ValueError(f"no LSP request for operation {operation!r}")


_NORMALIZERS: dict[str, Callable[[Any], Normalized | None]] = {
    "completion": normalize_completion_list,
    "hover": normalize_hover,
    "definition": normalize_locations,
    "references": normalize_locations,
    "symbols": normalize_symbols,
}


def normalize_for(operation: str, raw: Any) -> Normalized | None:
    """Normalize a raw server result for one operation (``None`` = nothing)."""
    normalizer = _NORMALIZERS.get(operation)
    if normalizer is None:
        return None
    return normalizer(raw)


# ─── capability helpers ──────────────────────────────────────────────────────
def lsp_sync_kind(capabilities: Mapping[str, Any] | None) -> int | None:
    """Numeric textDocumentSync kind (0 none / 1 full / 2 incremental)."""
    if not isinstance(capabilities, Mapping):
        return None
    sync = capabilities.get("textDocumentSync")
    change = sync.get("change") if isinstance(sync, Mapping) else sync
    if isinstance(change, int) and not isinstance(change, bool) and change in (0, 1, 2):
        return change
    return None


def lsp_save_include_text(capabilities: Mapping[str, Any] | None) -> bool:
    """True when the server asked for ``didSave`` to carry the full text."""
    if not isinstance(capabilities, Mapping):
        return False
    sync = capabilities.get("textDocumentSync")
    if not isinstance(sync, Mapping):
        return False
    save = sync.get("save")
    return bool(isinstance(save, Mapping) and save.get("includeText"))


def capability_summary(capabilities: Mapping[str, Any] | None) -> dict[str, Any]:
    """Booleans for the subset this client advertises, plus sync facts.

    Never the raw server capability object: the summary is a bounded display
    aid, not a promise that an operation will produce a result.
    """
    caps = capabilities if isinstance(capabilities, Mapping) else {}

    def advertised(key: str) -> bool:
        # LSP: the key's presence advertises support; the value may be `true`
        # or an options object (possibly empty). Only an explicit `false` or an
        # absent key means unsupported.
        return key in caps and caps[key] is not False

    return {
        "completion": advertised("completionProvider"),
        "hover": advertised("hoverProvider"),
        "definition": advertised("definitionProvider"),
        "references": advertised("referencesProvider"),
        "documentSymbol": advertised("documentSymbolProvider"),
        "rename": advertised("renameProvider"),
        "signatureHelp": advertised("signatureHelpProvider"),
        "formatting": advertised("documentFormattingProvider"),
        "codeAction": advertised("codeActionProvider"),
        "syncKind": lsp_sync_kind(caps),
        "saveIncludeText": lsp_save_include_text(caps),
    }


# ─── diagnostics store (plan §16) ────────────────────────────────────────────
@dataclass(frozen=True)
class PublishOutcome:
    """A stored publish: bounded diagnostics plus what was omitted/cleared."""

    uri: str
    producer: str
    version: int | None
    generation: int
    epoch: int
    diagnostics: tuple[dict[str, Any], ...]
    omitted: int


@dataclass
class _DiagnosticSet:
    producer: str
    version: int | None
    generation: int
    epoch: int
    diagnostics: tuple[dict[str, Any], ...]
    omitted: int
    cleared: bool = False


GenerationLookup = Callable[[str], "int | None"]


class DiagnosticsStore:
    """One replaceable set per ``(uri, producer)``, version-gated per set.

    A publish replaces its producer's set; an empty list clears only that
    producer (kept as a version tombstone so a later older-version publish is
    still rejected). Sets whose producer generation no longer matches the live
    connection are never served (plan §16: a dead generation's diagnostics can
    never become current).
    """

    def __init__(self, *, per_file_cap: int = MAX_DIAGNOSTICS_PER_FILE) -> None:
        self._per_file_cap = max(1, per_file_cap)
        self._sets: dict[tuple[str, str], _DiagnosticSet] = {}

    def publish(
        self,
        *,
        uri: str,
        producer: str,
        diagnostics: Any,
        version: int | None,
        generation: int,
        epoch: int,
    ) -> PublishOutcome | None:
        """Store one publish; ``None`` means it was rejected as older-version."""
        key = (uri, producer)
        existing = self._sets.get(key)
        if (
            existing is not None
            and version is not None
            and existing.version is not None
            and version < existing.version
        ):
            return None
        stored: list[dict[str, Any]] = []
        omitted = 0
        entries = diagnostics if isinstance(diagnostics, (list, tuple)) else ()
        valid = 0
        for raw in entries:
            item = normalize_diagnostic(raw)
            if item is None:
                continue
            valid += 1
            if len(stored) < self._per_file_cap:
                stored.append(item)
        omitted = valid - len(stored)
        self._sets[key] = _DiagnosticSet(
            producer=producer,
            version=version,
            generation=generation,
            epoch=epoch,
            diagnostics=tuple(stored),
            omitted=omitted,
            cleared=not stored,
        )
        return PublishOutcome(
            uri=uri,
            producer=producer,
            version=version,
            generation=generation,
            epoch=epoch,
            diagnostics=tuple(stored),
            omitted=omitted,
        )

    def entries(
        self, uri: str, *, epoch: int, generation_for: GenerationLookup
    ) -> list[_DiagnosticSet]:
        """Current, non-cleared producer sets for one URI, sorted by producer."""
        kept: list[_DiagnosticSet] = []
        for (entry_uri, _producer), stored in self._sets.items():
            if entry_uri != uri or stored.cleared or not stored.diagnostics:
                continue
            if stored.epoch != epoch:
                continue
            current = generation_for(stored.producer)
            if current is None or current != stored.generation:
                continue
            kept.append(stored)
        kept.sort(key=lambda stored: stored.producer)
        return kept

    def prune(self, *, epoch: int, generation_for: GenerationLookup) -> None:
        """Drop entries from other epochs or dead producer generations."""
        for key, stored in list(self._sets.items()):
            current = generation_for(stored.producer)
            if stored.epoch != epoch or current is None or current != stored.generation:
                del self._sets[key]

    def clear(self) -> None:
        self._sets.clear()
