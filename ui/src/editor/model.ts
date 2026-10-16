// Editor document store (plan §9, §22): the single authoritative unsaved-text
// model for the UI. Pure TypeScript; no renderer, no gateway, no App imports.
//
// Decisions locked here (plan §9/§22 and T4a contract):
// - `version` starts at 0 and increments once per accepted transaction,
//   including undo/redo and disk reloads (monotonic per document).
// - `dirty` is derived: `savedTextHash === null` means "no saved identity yet"
//   (untitled), so any non-empty text is dirty; otherwise dirty means the text
//   hash differs from the last acknowledged save.
// - Undo groups: consecutive single-grapheme inserts coalesce into one group;
//   backspace/forward-delete runs coalesce; cursor movement, atomic
//   transactions (completion/refactor) and selection replacements break runs.
// - Undo budget: 32 MiB of stored snapshot text lengths (UTF-16 code units),
//   counted across all documents; overflow trims the globally oldest whole
//   groups first and sets the per-document `trimmedUndo` disclosure flag.
//   Enforcement runs after every history mutation (edit, undo, redo).
// - Total open text budget: 16 MiB of UTF-8 bytes across open documents,
//   enforced at open time only; refused opens never evict existing buffers.
// - Dedup key: the exact URI string. Canonicalization (case, symlinks, file
//   URI escaping) is the file service's job (plan §11, T5).
// - Stored text is RAW: control characters are preserved verbatim; sanitization
//   happens only in the view layer (plan §10).

import { createHash } from "node:crypto";
import { graphemeCount, nextGraphemeOffset, prevGraphemeOffset } from "./positions.ts";

export type LineEnding = "LF" | "CRLF" | "CR" | "mixed";

export interface DocumentRecord {
	id: string;
	workspaceId: string;
	uri: string;
	languageId: string;
	/** Authoritative unsaved text (raw, never sanitized). */
	text: string;
	/** Grows on every model edit, including undo/redo and disk reloads. */
	version: number;
	/** Backend-issued disk identity of the last acknowledged save. */
	savedRevision: string | null;
	savedTextHash: string | null;
	dirty: boolean;
	readOnly: boolean;
	lineEnding: LineEnding;
	encoding: "utf-8";
	conflict?: { diskRevision: string; reason: string };
}

export interface EditorViewState {
	documentId: string;
	cursorOffset: number;
	selectionAnchorOffset?: number;
	scrollLine: number;
	horizontalCellOffset: number;
	preferredColumn?: number;
}

/** One replacement over a UTF-16 offset range. */
export interface TextEdit {
	range: { startOffset: number; endOffset: number };
	text: string;
}

export type ModelErrorCode =
	| "not-found"
	| "read-only"
	| "invalid-edit"
	| "duplicate-id"
	| "budget-exceeded"
	| "invalid-token"
	| "conflict"
	| "no-undo"
	| "no-redo"
	| "no-conflict";

export interface ModelError {
	code: ModelErrorCode;
	message: string;
	/** Present for `conflict` refusals. */
	conflict?: { diskRevision: string; reason: string };
}

/** Per-file editable budget (plan §22). Advisory: the caller decides `readOnly`. */
export const DEFAULT_EDITABLE_FILE_BUDGET_BYTES = 1024 * 1024;
/** Total open document text, logical UTF-8 bytes (plan §22). */
export const DEFAULT_TOTAL_TEXT_BUDGET_BYTES = 16 * 1024 * 1024;
/** Undo snapshot budget, logical UTF-16 code units of stored snapshots (plan §22). */
export const DEFAULT_UNDO_BUDGET_BYTES = 32 * 1024 * 1024;

export type TypingCoalesceKind = "typing-insert" | "typing-delete-back" | "typing-delete-forward";

export interface ApplyEditOptions {
	/**
	 * Typing hint: extend the open typing run instead of starting a new undo
	 * group. Only single-grapheme inserts and single-grapheme back/forward
	 * deletes that are contiguous with the run end are accepted; anything else
	 * falls back to an atomic group so paste/completion never merges silently.
	 */
	coalesce?: TypingCoalesceKind;
}

export interface EditorStoreOptions {
	totalTextBudgetBytes?: number;
	undoBudgetBytes?: number;
}

export interface OpenDocumentOptions {
	/** Optional explicit id; must be unique or the open is refused. */
	id?: string;
	workspaceId: string;
	uri: string;
	text: string;
	languageId: string;
	/** Disk identity; `null`/absent means untitled (no saved hash). */
	revision?: string | null;
	readOnly?: boolean;
}

export interface SaveToken {
	documentId: string;
	uri: string;
	version: number;
	text: string;
	savedRevision: string | null;
}

export type OpenDocumentResult =
	| { ok: true; created: boolean; document: DocumentRecord }
	| { ok: false; error: ModelError };

export type ApplyEditResult =
	| { ok: true; document: DocumentRecord; version: number }
	| { ok: false; error: ModelError };

export type UndoRedoResult =
	| { ok: true; document: DocumentRecord; version: number; cursorOffset: number; anchorOffset?: number }
	| { ok: false; error: ModelError };

export type SelectionResult = { ok: true; viewState: EditorViewState } | { ok: false; error: ModelError };

export type BeginSaveResult = { ok: true; token: SaveToken } | { ok: false; error: ModelError };

export type AcknowledgeSaveResult =
	| { ok: true; cleared: boolean; ignored: boolean; document: DocumentRecord }
	| { ok: false; error: ModelError };

export type ReloadResult = { ok: true; document: DocumentRecord } | { ok: false; error: ModelError };

export type ExternalChangeOutcome = "unchanged" | "reload-pending" | "conflict";

export type ExternalChangeResult =
	| { ok: true; outcome: ExternalChangeOutcome; document: DocumentRecord; diskRevision: string }
	| { ok: false; error: ModelError };

export type SerializeResult = { ok: true; text: string } | { ok: false; error: ModelError };

const utf8Encoder = new TextEncoder();

export function hashText(text: string): string {
	return createHash("sha256").update(text, "utf8").digest("hex");
}

function utf8Length(text: string): number {
	return utf8Encoder.encode(text).length;
}

interface LineEndingCounts {
	lf: number;
	crlf: number;
	cr: number;
}

function countLineEndings(text: string): LineEndingCounts {
	let lf = 0;
	let crlf = 0;
	let cr = 0;
	for (let index = 0; index < text.length; index++) {
		const code = text.charCodeAt(index);
		if (code === 0x0d) {
			if (text.charCodeAt(index + 1) === 0x0a) {
				crlf += 1;
				index += 1;
			} else {
				cr += 1;
			}
		} else if (code === 0x0a) {
			lf += 1;
		}
	}
	return { lf, crlf, cr };
}

/** Dominant ending kind; "mixed" when more than one kind is present. */
export function detectLineEnding(text: string): LineEnding {
	const { lf, crlf, cr } = countLineEndings(text);
	const kinds = (lf > 0 ? 1 : 0) + (crlf > 0 ? 1 : 0) + (cr > 0 ? 1 : 0);
	if (kinds === 0) return "LF"; // no endings yet: LF is the insertion default
	if (kinds > 1) return "mixed";
	if (crlf > 0) return "CRLF";
	if (cr > 0) return "CR";
	return "LF";
}

/**
 * Ending used for newline insertion. Most frequent kind wins; ties resolve to
 * the earliest occurrence in the text. Documents without endings use LF.
 */
export function dominantLineEnding(text: string): "LF" | "CRLF" | "CR" {
	const { lf, crlf, cr } = countLineEndings(text);
	const max = Math.max(lf, crlf, cr);
	if (max === 0) return "LF";
	const tied: Array<"LF" | "CRLF" | "CR"> = [];
	if (crlf === max) tied.push("CRLF");
	if (lf === max) tied.push("LF");
	if (cr === max) tied.push("CR");
	if (tied.length === 1) {
		const winner = tied[0];
		if (winner) return winner;
	}
	const first = text.search(/\r\n|\n|\r/);
	if (first < 0) return "LF";
	if (text.charCodeAt(first) === 0x0a) return "LF";
	return text.charCodeAt(first + 1) === 0x0a ? "CRLF" : "CR";
}

/** Literal insertion text for a (non-mixed) line ending kind. */
export function lineEndingSequence(ending: "LF" | "CRLF" | "CR"): "\n" | "\r\n" | "\r" {
	if (ending === "CRLF") return "\r\n";
	if (ending === "CR") return "\r";
	return "\n";
}

interface StoredSelection {
	cursorOffset: number;
	anchorOffset?: number;
}

interface UndoGroup {
	kind: TypingCoalesceKind | "atomic";
	seq: number;
	before: { text: string; selection: StoredSelection };
	/** Stored snapshot length in UTF-16 code units (the budget unit). */
	bytes: number;
}

interface RedoEntry {
	seq: number;
	text: string;
	selection: StoredSelection;
	bytes: number;
}

interface DocHistory {
	undo: UndoGroup[];
	redo: RedoEntry[];
	openKind?: TypingCoalesceKind;
	openAfterCursor?: number;
	openAfterAnchor?: number;
	trimmed: boolean;
}

interface NormalizedEdit {
	startOffset: number;
	endOffset: number;
	text: string;
	/** Caller array position; preserves order for same-position inserts. */
	order: number;
}

function refusal(code: ModelErrorCode, message: string): { ok: false; error: ModelError } {
	return { ok: false, error: { code, message } };
}

function conflictRefusal(conflict: { diskRevision: string; reason: string }): { ok: false; error: ModelError } {
	return {
		ok: false,
		error: {
			code: "conflict",
			message: `the buffer has unsaved changes; disk revision ${conflict.diskRevision} was not adopted`,
			conflict: { ...conflict },
		},
	};
}

function validateBudget(value: number | undefined, fallback: number, name: string): number {
	if (value === undefined) return fallback;
	if (!Number.isFinite(value) || value < 0) {
		throw new RangeError(`${name} must be a finite non-negative number`);
	}
	return value;
}

function clampSelectionOffset(offset: number, length: number): number {
	if (!Number.isFinite(offset)) return 0;
	return Math.min(length, Math.max(0, Math.floor(offset)));
}

function clampNonNegative(value: number): number {
	if (!Number.isFinite(value)) return 0;
	return Math.max(0, Math.floor(value));
}

/** Non-empty selection range derived from a view state, if any. */
function selectionRange(view: EditorViewState): { startOffset: number; endOffset: number } | undefined {
	const anchor = view.selectionAnchorOffset;
	if (anchor === undefined || anchor === view.cursorOffset) return undefined;
	return {
		startOffset: Math.min(anchor, view.cursorOffset),
		endOffset: Math.max(anchor, view.cursorOffset),
	};
}

/**
 * Ranges must be pairwise separable. Two insertions never conflict (their
 * caller order is preserved); an insertion point inside another range's
 * [start, end) span conflicts, including exactly at its start (ambiguous),
 * while an insertion exactly at its end is allowed.
 */
function editsConflict(a: NormalizedEdit, b: NormalizedEdit): boolean {
	const aEmpty = a.startOffset === a.endOffset;
	const bEmpty = b.startOffset === b.endOffset;
	if (aEmpty && bEmpty) return false;
	if (aEmpty) return b.startOffset <= a.startOffset && a.startOffset < b.endOffset;
	if (bEmpty) return a.startOffset <= b.startOffset && b.startOffset < a.endOffset;
	return a.startOffset < b.endOffset && b.startOffset < a.endOffset;
}

/** One pass, ascending order; same-position inserts keep caller order. */
function composeText(text: string, edits: NormalizedEdit[]): string {
	const sorted = [...edits].sort((a, b) => a.startOffset - b.startOffset || a.order - b.order);
	let out = "";
	let cursor = 0;
	for (const edit of sorted) {
		if (edit.startOffset > cursor) out += text.slice(cursor, edit.startOffset);
		out += edit.text;
		cursor = Math.max(cursor, edit.endOffset);
	}
	out += text.slice(cursor);
	return out;
}

export class EditorStore {
	private readonly totalTextBudget: number;
	private readonly undoBudget: number;
	private readonly records = new Map<string, DocumentRecord>();
	/** URI string -> document id. Dedup key; canonicalization is the file service's job. */
	private readonly uriIndex = new Map<string, string>();
	private readonly views = new Map<string, EditorViewState>();
	private readonly histories = new Map<string, DocHistory>();
	private readonly pendingReload = new Map<string, string>();
	private readonly acknowledgedVersion = new Map<string, number>();
	private totalTextBytesValue = 0;
	private undoBytesValue = 0;
	private undoSeq = 0;
	private idCounter = 0;

	constructor(options: EditorStoreOptions = {}) {
		this.totalTextBudget = validateBudget(
			options.totalTextBudgetBytes,
			DEFAULT_TOTAL_TEXT_BUDGET_BYTES,
			"totalTextBudgetBytes",
		);
		this.undoBudget = validateBudget(options.undoBudgetBytes, DEFAULT_UNDO_BUDGET_BYTES, "undoBudgetBytes");
	}

	// ------------------------------------------------------------------ open

	/**
	 * Open a document. Dedup: an existing document with the same URI string is
	 * returned unchanged (no competing buffer). The total text budget is
	 * checked before creation; a refused open never evicts existing documents.
	 */
	openDocument(options: OpenDocumentOptions): OpenDocumentResult {
		const existingId = this.uriIndex.get(options.uri);
		const existing = existingId === undefined ? undefined : this.records.get(existingId);
		if (existing) return { ok: true, created: false, document: existing };
		if (options.id !== undefined && this.records.has(options.id)) {
			return refusal("duplicate-id", `document id already in use: ${options.id}`);
		}
		const bytes = utf8Length(options.text);
		if (this.totalTextBytesValue + bytes > this.totalTextBudget) {
			return refusal(
				"budget-exceeded",
				`total open text budget exceeded: ${this.totalTextBytesValue + bytes} > ${this.totalTextBudget} bytes`,
			);
		}
		const id = options.id ?? `doc-${++this.idCounter}`;
		const revision = options.revision ?? null;
		const record: DocumentRecord = {
			id,
			workspaceId: options.workspaceId,
			uri: options.uri,
			languageId: options.languageId,
			text: options.text,
			version: 0,
			savedRevision: revision,
			savedTextHash: revision === null ? null : hashText(options.text),
			dirty: false,
			readOnly: options.readOnly ?? false,
			lineEnding: detectLineEnding(options.text),
			encoding: "utf-8",
		};
		this.recomputeDirty(record);
		this.records.set(id, record);
		this.uriIndex.set(options.uri, id);
		this.views.set(id, {
			documentId: id,
			cursorOffset: 0,
			selectionAnchorOffset: undefined,
			scrollLine: 0,
			horizontalCellOffset: 0,
		});
		this.histories.set(id, { undo: [], redo: [], trimmed: false });
		this.totalTextBytesValue += bytes;
		return { ok: true, created: true, document: record };
	}

	closeDocument(documentId: string): boolean {
		const record = this.records.get(documentId);
		if (!record) return false;
		this.totalTextBytesValue -= utf8Length(record.text);
		const history = this.histories.get(documentId);
		if (history) {
			for (const group of history.undo) this.undoBytesValue -= group.bytes;
			for (const entry of history.redo) this.undoBytesValue -= entry.bytes;
		}
		if (this.totalTextBytesValue < 0) this.totalTextBytesValue = 0;
		if (this.undoBytesValue < 0) this.undoBytesValue = 0;
		this.records.delete(documentId);
		this.uriIndex.delete(record.uri);
		this.views.delete(documentId);
		this.histories.delete(documentId);
		this.pendingReload.delete(documentId);
		this.acknowledgedVersion.delete(documentId);
		return true;
	}

	getDocument(documentId: string): DocumentRecord | undefined {
		// Live record: the store is the only writer; callers treat it as read-only.
		return this.records.get(documentId);
	}

	documents(): DocumentRecord[] {
		return [...this.records.values()];
	}

	totalTextBytes(): number {
		return this.totalTextBytesValue;
	}

	// ------------------------------------------------------------- view state

	getViewState(documentId: string): EditorViewState | undefined {
		const view = this.views.get(documentId);
		return view ? { ...view } : undefined;
	}

	/** Clamps both offsets into the current text; movement closes typing runs. */
	setSelection(documentId: string, selection: { cursorOffset: number; anchorOffset?: number }): SelectionResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		const view = this.requireView(documentId);
		const history = this.requireHistory(documentId);
		const cursorOffset = clampSelectionOffset(selection.cursorOffset, record.text.length);
		const anchorOffset =
			selection.anchorOffset === undefined
				? undefined
				: clampSelectionOffset(selection.anchorOffset, record.text.length);
		if (
			history.openKind !== undefined &&
			!(cursorOffset === history.openAfterCursor && anchorOffset === history.openAfterAnchor)
		) {
			this.closeTypingRun(history);
		}
		view.cursorOffset = cursorOffset;
		view.selectionAnchorOffset = anchorOffset;
		return { ok: true, viewState: { ...view } };
	}

	setViewport(
		documentId: string,
		patch: { scrollLine?: number; horizontalCellOffset?: number; preferredColumn?: number | null },
	): SelectionResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		const view = this.requireView(documentId);
		if (patch.scrollLine !== undefined) view.scrollLine = clampNonNegative(patch.scrollLine);
		if (patch.horizontalCellOffset !== undefined) {
			view.horizontalCellOffset = clampNonNegative(patch.horizontalCellOffset);
		}
		if (patch.preferredColumn !== undefined) {
			view.preferredColumn = patch.preferredColumn === null ? undefined : clampNonNegative(patch.preferredColumn);
		}
		return { ok: true, viewState: { ...view } };
	}

	selectAll(documentId: string): SelectionResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		return this.setSelection(documentId, { cursorOffset: record.text.length, anchorOffset: 0 });
	}

	// ------------------------------------------------------------ transactions

	/**
	 * Apply all edits as ONE transaction: exactly one version increment, one
	 * undo group, validated before any mutation (out-of-range or conflicting
	 * ranges refuse the whole transaction). The caret collapses to the end of
	 * the last edit in caller order.
	 */
	applyEdit(documentId: string, edits: TextEdit[], options: ApplyEditOptions = {}): ApplyEditResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		if (!Array.isArray(edits) || edits.length === 0) {
			return refusal("invalid-edit", "a transaction requires at least one edit");
		}
		const normalized: NormalizedEdit[] = [];
		for (let order = 0; order < edits.length; order++) {
			const raw = edits[order] as
				| { range?: { startOffset?: number; endOffset?: number }; text?: string }
				| null
				| undefined;
			const startOffset = raw?.range?.startOffset;
			const endOffset = raw?.range?.endOffset;
			const text = raw?.text;
			if (
				typeof startOffset !== "number" ||
				!Number.isInteger(startOffset) ||
				typeof endOffset !== "number" ||
				!Number.isInteger(endOffset) ||
				typeof text !== "string"
			) {
				return refusal("invalid-edit", `edit ${order} has a malformed range or text`);
			}
			if (startOffset < 0 || endOffset < startOffset || endOffset > record.text.length) {
				return refusal(
					"invalid-edit",
					`edit ${order} range [${startOffset}, ${endOffset}) is outside the document (length ${record.text.length})`,
				);
			}
			normalized.push({ startOffset, endOffset, text, order });
		}
		for (let a = 0; a < normalized.length; a++) {
			for (let b = a + 1; b < normalized.length; b++) {
				const first = normalized[a];
				const second = normalized[b];
				if (first && second && editsConflict(first, second)) {
					return refusal(
						"invalid-edit",
						`edits ${first.order} and ${second.order} overlap or touch ambiguously`,
					);
				}
			}
		}

		const view = this.requireView(documentId);
		const history = this.requireHistory(documentId);
		const beforeText = record.text;
		const beforeSelection: StoredSelection = {
			cursorOffset: view.cursorOffset,
			anchorOffset: view.selectionAnchorOffset,
		};
		const requested = options.coalesce;
		const extendsRun = this.runExtends(history, beforeText, normalized, requested);
		const nextText = composeText(beforeText, normalized);

		record.text = nextText;
		record.version += 1;
		record.lineEnding = detectLineEnding(nextText);
		this.trackTextBytesChange(beforeText, nextText);
		this.recomputeDirty(record);
		const primary = normalized[normalized.length - 1];
		if (primary) {
			view.cursorOffset = Math.min(primary.startOffset + primary.text.length, nextText.length);
			view.selectionAnchorOffset = undefined;
		}

		if (extendsRun && requested !== undefined) {
			history.openAfterCursor = view.cursorOffset;
			history.openAfterAnchor = view.selectionAnchorOffset;
		} else if (requested !== undefined && this.runStartable(beforeText, normalized, requested)) {
			this.pushUndoGroup(history, beforeSelection, requested, beforeText);
			history.openKind = requested;
			history.openAfterCursor = view.cursorOffset;
			history.openAfterAnchor = view.selectionAnchorOffset;
		} else {
			this.closeTypingRun(history);
			this.pushUndoGroup(history, beforeSelection, "atomic", beforeText);
		}
		return { ok: true, document: record, version: record.version };
	}

	/** Undo one logical group; restores the exact text and selection, bumps version. */
	undo(documentId: string): UndoRedoResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		const history = this.requireHistory(documentId);
		const group = history.undo.pop();
		if (!group) return refusal("no-undo", "no undo history available");
		const view = this.requireView(documentId);
		this.closeTypingRun(history);
		const currentText = record.text;
		history.redo.push({
			seq: ++this.undoSeq,
			text: currentText,
			selection: { cursorOffset: view.cursorOffset, anchorOffset: view.selectionAnchorOffset },
			bytes: currentText.length,
		});
		this.undoBytesValue += currentText.length - group.bytes;
		record.text = group.before.text;
		record.version += 1;
		record.lineEnding = detectLineEnding(record.text);
		this.trackTextBytesChange(currentText, record.text);
		this.recomputeDirty(record);
		this.restoreSelection(view, group.before.selection, record.text.length);
		this.trimUndoBudget();
		return {
			ok: true,
			document: record,
			version: record.version,
			cursorOffset: view.cursorOffset,
			anchorOffset: view.selectionAnchorOffset,
		};
	}

	/** Redo one group previously undone; bumps version like any model edit. */
	redo(documentId: string): UndoRedoResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		const history = this.requireHistory(documentId);
		const entry = history.redo.pop();
		if (!entry) return refusal("no-redo", "no redo history available");
		const view = this.requireView(documentId);
		this.closeTypingRun(history);
		const currentText = record.text;
		history.undo.push({
			kind: "atomic",
			seq: ++this.undoSeq,
			before: {
				text: currentText,
				selection: { cursorOffset: view.cursorOffset, anchorOffset: view.selectionAnchorOffset },
			},
			bytes: currentText.length,
		});
		this.undoBytesValue += currentText.length - entry.bytes;
		record.text = entry.text;
		record.version += 1;
		record.lineEnding = detectLineEnding(record.text);
		this.trackTextBytesChange(currentText, record.text);
		this.recomputeDirty(record);
		this.restoreSelection(view, entry.selection, record.text.length);
		this.trimUndoBudget();
		return {
			ok: true,
			document: record,
			version: record.version,
			cursorOffset: view.cursorOffset,
			anchorOffset: view.selectionAnchorOffset,
		};
	}

	// ----------------------------------------------------------------- helpers

	/** Insert text at the caret, replacing a non-empty selection. */
	insertText(documentId: string, text: string): ApplyEditResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		const view = this.requireView(documentId);
		const selection = selectionRange(view);
		if (selection) return this.applyEdit(documentId, [{ range: selection, text }]);
		if (text === "") return { ok: true, document: record, version: record.version };
		const cursor = view.cursorOffset;
		const options: ApplyEditOptions = graphemeCount(text) === 1 ? { coalesce: "typing-insert" } : {};
		return this.applyEdit(
			documentId,
			[{ range: { startOffset: cursor, endOffset: cursor }, text }],
			options,
		);
	}

	/** Replace the current selection (or insert at the caret when collapsed). */
	replaceSelection(documentId: string, text: string): ApplyEditResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		const view = this.requireView(documentId);
		const selection = selectionRange(view);
		if (selection) return this.applyEdit(documentId, [{ range: selection, text }]);
		return this.insertText(documentId, text);
	}

	/** Delete backward: the selection, or exactly one grapheme before the caret. */
	backspace(documentId: string): ApplyEditResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		const view = this.requireView(documentId);
		const selection = selectionRange(view);
		if (selection) return this.applyEdit(documentId, [{ range: selection, text: "" }]);
		const cursor = view.cursorOffset;
		const start = prevGraphemeOffset(record.text, cursor);
		if (start === cursor) return { ok: true, document: record, version: record.version };
		return this.applyEdit(
			documentId,
			[{ range: { startOffset: start, endOffset: cursor }, text: "" }],
			{ coalesce: "typing-delete-back" },
		);
	}

	/** Delete forward: the selection, or exactly one grapheme after the caret. */
	deleteForward(documentId: string): ApplyEditResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		const view = this.requireView(documentId);
		const selection = selectionRange(view);
		if (selection) return this.applyEdit(documentId, [{ range: selection, text: "" }]);
		const cursor = view.cursorOffset;
		const end = nextGraphemeOffset(record.text, cursor);
		if (end === cursor) return { ok: true, document: record, version: record.version };
		return this.applyEdit(
			documentId,
			[{ range: { startOffset: cursor, endOffset: end }, text: "" }],
			{ coalesce: "typing-delete-forward" },
		);
	}

	/** Insert a newline using the document's dominant line ending. */
	newline(documentId: string): ApplyEditResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		return this.insertText(documentId, lineEndingSequence(dominantLineEnding(record.text)));
	}

	// -------------------------------------------------------------- save flow

	/** Capture the exact state a save is writing. Does not mutate the record. */
	beginSave(documentId: string): BeginSaveResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.readOnly) return refusal("read-only", `document is read-only: ${record.uri}`);
		return {
			ok: true,
			token: {
				documentId,
				uri: record.uri,
				version: record.version,
				text: record.text,
				savedRevision: record.savedRevision,
			},
		};
	}

	/**
	 * Acknowledge a completed write. Dirty clears only when the document version
	 * still equals the captured version AND the current text hash matches the
	 * captured text; otherwise the later edits stay dirty (the saved identity
	 * still advances, because that text is what reached the disk).
	 */
	acknowledgeSave(documentId: string, token: SaveToken, newRevision: string): AcknowledgeSaveResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (token.documentId !== documentId) {
			return refusal("invalid-token", `save token belongs to ${token.documentId}, not ${documentId}`);
		}
		const acknowledged = this.acknowledgedVersion.get(documentId) ?? -1;
		const tokenHash = hashText(token.text);
		if (record.version === token.version && hashText(record.text) === tokenHash) {
			record.savedRevision = newRevision;
			record.savedTextHash = tokenHash;
			record.dirty = false;
			record.conflict = undefined;
			if (token.version > acknowledged) this.acknowledgedVersion.set(documentId, token.version);
			return { ok: true, cleared: true, ignored: false, document: record };
		}
		if (token.version > record.version || token.version <= acknowledged) {
			// A token for a state this document has moved beyond (or that was
			// already acknowledged later): ignore it rather than regress state.
			return { ok: true, cleared: false, ignored: true, document: record };
		}
		record.savedRevision = newRevision;
		record.savedTextHash = tokenHash;
		this.acknowledgedVersion.set(documentId, token.version);
		this.recomputeDirty(record);
		return { ok: true, cleared: false, ignored: false, document: record };
	}

	/** Exact bytes for a save: never normalizes whitespace, endings or final newline. */
	serializeForSave(documentId: string): SerializeResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		return { ok: true, text: record.text };
	}

	// ------------------------------------------------------------ disk state

	/**
	 * Adopt disk state for a clean buffer. A dirty buffer is never auto-reloaded:
	 * the structured conflict result is returned instead and `record.conflict`
	 * is recorded for explicit resolution.
	 */
	reloadFromDisk(documentId: string, text: string, revision: string): ReloadResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (record.dirty) {
			const conflict = { diskRevision: revision, reason: "dirty-reload" };
			record.conflict = conflict;
			return conflictRefusal(conflict);
		}
		this.adoptDiskState(record, text, revision);
		return { ok: true, document: record };
	}

	/** Alias of {@link reloadFromDisk}: explicit disk-state adoption. */
	setDiskState(documentId: string, text: string, revision: string): ReloadResult {
		return this.reloadFromDisk(documentId, text, revision);
	}

	/**
	 * External/agent update hook. Clean buffers are marked for reload (the
	 * caller owns the visible notice); dirty buffers record a conflict.
	 */
	noteExternalChange(documentId: string, diskRevision: string): ExternalChangeResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (!record.dirty && record.savedRevision === diskRevision) {
			return { ok: true, outcome: "unchanged", document: record, diskRevision };
		}
		if (!record.dirty) {
			this.pendingReload.set(documentId, diskRevision);
			return { ok: true, outcome: "reload-pending", document: record, diskRevision };
		}
		record.conflict = { diskRevision, reason: "external-change" };
		return { ok: true, outcome: "conflict", document: record, diskRevision };
	}

	hasPendingReload(documentId: string): boolean {
		return this.pendingReload.has(documentId);
	}

	pendingReloadRevision(documentId: string): string | undefined {
		return this.pendingReload.get(documentId);
	}

	/** Dismiss a clean-buffer pending reload without adopting disk content. */
	clearPendingReload(documentId: string): void {
		this.pendingReload.delete(documentId);
	}

	/** Conflict resolution: keep the buffer (still dirty), dismiss the conflict. */
	resolveConflictKeep(documentId: string): ReloadResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (!record.conflict) return refusal("no-conflict", `document has no recorded conflict: ${documentId}`);
		record.conflict = undefined;
		return { ok: true, document: record };
	}

	/** Conflict resolution: explicitly discard the buffer and adopt disk text. */
	resolveConflictReload(documentId: string, text: string, revision: string): ReloadResult {
		const record = this.records.get(documentId);
		if (!record) return refusal("not-found", `unknown document: ${documentId}`);
		if (!record.conflict) return refusal("no-conflict", `document has no recorded conflict: ${documentId}`);
		this.adoptDiskState(record, text, revision);
		return { ok: true, document: record };
	}

	// ------------------------------------------------------------- disclosure

	undoDepth(documentId: string): number {
		return this.histories.get(documentId)?.undo.length ?? 0;
	}

	/** Stored snapshot text length (UTF-16 code units) for one document. */
	undoBytes(documentId: string): number {
		const history = this.histories.get(documentId);
		if (!history) return 0;
		let bytes = 0;
		for (const group of history.undo) bytes += group.bytes;
		for (const entry of history.redo) bytes += entry.bytes;
		return bytes;
	}

	undoBytesTotal(): number {
		return this.undoBytesValue;
	}

	/** True once the undo budget has trimmed this document's history. */
	trimmedUndo(documentId: string): boolean {
		return this.histories.get(documentId)?.trimmed ?? false;
	}

	// -------------------------------------------------------------- internals

	private requireView(documentId: string): EditorViewState {
		const view = this.views.get(documentId);
		if (!view) throw new Error(`view state missing for open document ${documentId}`);
		return view;
	}

	private requireHistory(documentId: string): DocHistory {
		const history = this.histories.get(documentId);
		if (!history) throw new Error(`history missing for open document ${documentId}`);
		return history;
	}

	private recomputeDirty(record: DocumentRecord): void {
		record.dirty =
			record.savedTextHash === null
				? record.text.length > 0
				: hashText(record.text) !== record.savedTextHash;
	}

	/** Keep the reported open-text total truthful; budgets are checked at open. */
	private trackTextBytesChange(beforeText: string, afterText: string): void {
		this.totalTextBytesValue += utf8Length(afterText) - utf8Length(beforeText);
		if (this.totalTextBytesValue < 0) this.totalTextBytesValue = 0;
	}

	private restoreSelection(view: EditorViewState, selection: StoredSelection, length: number): void {
		view.cursorOffset = clampSelectionOffset(selection.cursorOffset, length);
		view.selectionAnchorOffset =
			selection.anchorOffset === undefined ? undefined : clampSelectionOffset(selection.anchorOffset, length);
	}

	private closeTypingRun(history: DocHistory): void {
		history.openKind = undefined;
		history.openAfterCursor = undefined;
		history.openAfterAnchor = undefined;
	}

	/** The transaction continues the open typing run (shape + contiguity). */
	private runExtends(
		history: DocHistory,
		beforeText: string,
		edits: NormalizedEdit[],
		requested: TypingCoalesceKind | undefined,
	): boolean {
		if (requested === undefined) return false;
		if (history.openKind !== requested) return false;
		const after = history.openAfterCursor;
		if (after === undefined || history.openAfterAnchor !== undefined) return false;
		const edit = edits.length === 1 ? edits[0] : undefined;
		if (!edit) return false;
		if (requested === "typing-insert") {
			return (
				edit.startOffset === edit.endOffset &&
				edit.startOffset === after &&
				graphemeCount(edit.text) === 1
			);
		}
		if (requested === "typing-delete-back") {
			return edit.text === "" && edit.endOffset === after && edit.startOffset === prevGraphemeOffset(beforeText, after);
		}
		return edit.text === "" && edit.startOffset === after && edit.endOffset === nextGraphemeOffset(beforeText, after);
	}

	/** The transaction has the right shape to open a fresh run of this kind. */
	private runStartable(beforeText: string, edits: NormalizedEdit[], requested: TypingCoalesceKind): boolean {
		const edit = edits.length === 1 ? edits[0] : undefined;
		if (!edit) return false;
		if (requested === "typing-insert") {
			return edit.startOffset === edit.endOffset && graphemeCount(edit.text) === 1;
		}
		if (requested === "typing-delete-back") {
			return (
				edit.text === "" &&
				edit.endOffset > edit.startOffset &&
				edit.startOffset === prevGraphemeOffset(beforeText, edit.endOffset)
			);
		}
		return (
			edit.text === "" &&
			edit.endOffset > edit.startOffset &&
			edit.endOffset === nextGraphemeOffset(beforeText, edit.startOffset)
		);
	}

	private pushUndoGroup(
		history: DocHistory,
		selection: StoredSelection,
		kind: TypingCoalesceKind | "atomic",
		beforeText: string,
	): void {
		this.clearRedo(history);
		const group: UndoGroup = {
			kind,
			seq: ++this.undoSeq,
			before: { text: beforeText, selection: { ...selection } },
			bytes: beforeText.length,
		};
		history.undo.push(group);
		this.undoBytesValue += group.bytes;
		this.trimUndoBudget();
	}

	private clearRedo(history: DocHistory): void {
		for (const entry of history.redo) this.undoBytesValue -= entry.bytes;
		if (this.undoBytesValue < 0) this.undoBytesValue = 0;
		history.redo = [];
	}

	/**
	 * Enforce the global undo budget: trim the globally oldest whole groups
	 * first (undo fronts, then the farthest redo entries), disclosing trimming
	 * on the affected document. Trimming may remove the newest group when it is
	 * the only one left and still over budget.
	 */
	private trimUndoBudget(): void {
		while (this.undoBytesValue > this.undoBudget) {
			let target: { documentId: string; source: "undo" | "redo"; seq: number } | undefined;
			for (const [documentId, history] of this.histories) {
				const undoFront = history.undo.length > 0 ? history.undo[0] : undefined;
				if (undoFront && (!target || undoFront.seq < target.seq)) {
					target = { documentId, source: "undo", seq: undoFront.seq };
				}
				const redoFront = history.redo.length > 0 ? history.redo[0] : undefined;
				if (redoFront && (!target || redoFront.seq < target.seq)) {
					target = { documentId, source: "redo", seq: redoFront.seq };
				}
			}
			if (!target) {
				this.undoBytesValue = 0;
				return;
			}
			const history = this.histories.get(target.documentId);
			if (!history) {
				this.undoBytesValue = 0;
				return;
			}
			const removed = target.source === "undo" ? history.undo.shift() : history.redo.shift();
			if (removed) this.undoBytesValue -= removed.bytes;
			if (this.undoBytesValue < 0) this.undoBytesValue = 0;
			history.trimmed = true;
		}
	}

	private adoptDiskState(record: DocumentRecord, text: string, revision: string): void {
		this.trackTextBytesChange(record.text, text);
		record.text = text;
		record.version += 1;
		record.savedRevision = revision;
		record.savedTextHash = hashText(text);
		record.dirty = false;
		record.lineEnding = detectLineEnding(text);
		record.conflict = undefined;
		this.pendingReload.delete(record.id);
		const history = this.histories.get(record.id);
		if (history) {
			for (const group of history.undo) this.undoBytesValue -= group.bytes;
			for (const entry of history.redo) this.undoBytesValue -= entry.bytes;
			if (this.undoBytesValue < 0) this.undoBytesValue = 0;
			history.undo = [];
			history.redo = [];
			history.trimmed = false;
			this.closeTypingRun(history);
		}
	}
}
