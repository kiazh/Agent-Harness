// Editor intelligence coordination (plan §23, T9 UI): pure, render-free state
// machines for completion, hover, navigation picking, rename preparation and
// the Problems diagnostics model. The controller owns transport; this module
// owns capture/supersede/stale-drop semantics, defensive narrowing of
// untrusted payloads, and acceptance through version-checked workspace-edit
// staging.
//
// Honesty rules encoded here:
// - Unknown or malformed payloads narrow to empty results plus a state flag;
//   nothing throws and nothing is invented. Payload data is data.
// - Completion sessions capture (document, version, position, prefix) at
//   request time. A newer request logically cancels the older one, and a
//   response whose payload version disagrees with the capture is dropped.
// - Acceptance validates against the captured version: a buffer that moved on
//   rejects the edit as stale instead of misplacing text, and closes.
// - Snippet items (insertTextFormat 2) are rejected by default because the UI
//   does not advertise snippet support; `allowLiteralSnippets` only widens
//   that to snippet-syntax-free text, never to tab stops.
// - Rename honestly reports "unavailable" until the intelligence service
//   offers the operation; nothing is simulated.
// - The Problems model is a bounded projection: per (uri, producer)
//   latest-wins with version rules, a 1000-row cap with an omitted count, and
//   unversioned entries labeled as such.

import { sanitizeDisplay } from "../design/text.ts";
import type { EditorStore } from "./model.ts";
import {
	applyWorkspaceEdit,
	previewWorkspaceEdit,
	type WorkspaceEditApplication,
	type WorkspaceEditPreview,
	type WorkspaceEditRefusal,
} from "./workspaceEdits.ts";
import type {
	LspPosition,
	LspRange,
	LspTextEdit,
	WorkspaceEditPayload,
	WorkspaceIntelligenceResult,
} from "../workspace/protocol.ts";

// ------------------------------------------------------------------ the seam

export type IntelligenceOperation =
	| "completion"
	| "hover"
	| "definition"
	| "references"
	| "symbols"
	| "diagnostics";

/** Frozen controller seam (T9 integration): one request fn for every operation. */
export interface IntelligenceRequest {
	(
		operation: IntelligenceOperation,
		uri: string,
		version: number | null,
		position?: LspPosition,
	): Promise<WorkspaceIntelligenceResult>;
}

// ------------------------------------------------------------- narrowing aids

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

function nonNegativeInteger(value: unknown): number {
	return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

function isLspPosition(value: unknown): value is LspPosition {
	if (!isRecord(value)) return false;
	const { line, character } = value;
	return (
		typeof line === "number" &&
		Number.isInteger(line) &&
		line >= 0 &&
		typeof character === "number" &&
		Number.isInteger(character) &&
		character >= 0
	);
}

function isLspRange(value: unknown): value is LspRange {
	if (!isRecord(value)) return false;
	return isLspPosition(value.start) && isLspPosition(value.end);
}

function copyPosition(position: LspPosition): LspPosition {
	return { line: position.line, character: position.character };
}

function copyRange(range: LspRange): LspRange {
	return { start: copyPosition(range.start), end: copyPosition(range.end) };
}

function copyEdit(edit: LspTextEdit): LspTextEdit {
	return { range: copyRange(edit.range), newText: edit.newText };
}

/** Plain completion text edits; InsertReplaceEdit normalizes to its insert range. */
function narrowTextEditValue(value: unknown): { edit: LspTextEdit; replaceRange?: LspRange } | undefined {
	if (!isRecord(value) || typeof value.newText !== "string") return undefined;
	if (isLspRange(value.range)) return { edit: { range: copyRange(value.range), newText: value.newText } };
	if (isLspRange(value.insert) && isLspRange(value.replace)) {
		return { edit: { range: copyRange(value.insert), newText: value.newText }, replaceRange: copyRange(value.replace) };
	}
	return undefined;
}

// ------------------------------------------------------------- completion data

export interface NarrowedCompletionItem {
	label: string;
	kind?: number;
	detail?: string;
	documentation?: string;
	filterText?: string;
	sortText?: string;
	/** 1 = plain text, 2 = snippet (never advertised by this UI). */
	insertTextFormat?: number;
	textEdit?: LspTextEdit;
	/** Present when the server sent an InsertReplaceEdit (replace side). */
	replaceRange?: LspRange;
	additionalTextEdits: LspTextEdit[];
	/** The item carried a textEdit envelope that does not decode. */
	malformedTextEdit?: boolean;
}

export interface NarrowedCompletionPayload {
	isIncomplete: boolean;
	items: NarrowedCompletionItem[];
	/** Items the server itself reported as omitted, plus undecodable entries. */
	omitted: number;
}

function documentationText(value: unknown): string | undefined {
	if (typeof value === "string") return value;
	if (Array.isArray(value)) {
		const parts: string[] = [];
		for (const entry of value) {
			if (typeof entry === "string") parts.push(entry);
			else if (isRecord(entry) && typeof entry.value === "string") parts.push(entry.value);
		}
		return parts.length > 0 ? parts.join("\n") : undefined;
	}
	if (isRecord(value) && typeof value.value === "string") return value.value;
	return undefined;
}

function narrowCompletionItem(raw: unknown): NarrowedCompletionItem | undefined {
	if (!isRecord(raw) || typeof raw.label !== "string" || raw.label.length === 0) return undefined;
	const item: NarrowedCompletionItem = { label: raw.label, additionalTextEdits: [] };
	if (typeof raw.kind === "number" && Number.isInteger(raw.kind)) item.kind = raw.kind;
	if (typeof raw.detail === "string") item.detail = raw.detail;
	const documentation = documentationText(raw.documentation);
	if (documentation !== undefined) item.documentation = documentation;
	if (typeof raw.filterText === "string") item.filterText = raw.filterText;
	if (typeof raw.sortText === "string") item.sortText = raw.sortText;
	if (typeof raw.insertTextFormat === "number" && Number.isInteger(raw.insertTextFormat)) {
		item.insertTextFormat = raw.insertTextFormat;
	}
	if (raw.textEdit !== undefined) {
		const narrowed = narrowTextEditValue(raw.textEdit);
		if (narrowed) {
			item.textEdit = narrowed.edit;
			if (narrowed.replaceRange) item.replaceRange = narrowed.replaceRange;
		} else {
			item.malformedTextEdit = true;
		}
	}
	if (Array.isArray(raw.additionalTextEdits)) {
		for (const entry of raw.additionalTextEdits) {
			const narrowed = narrowTextEditValue(entry);
			if (narrowed) item.additionalTextEdits.push(narrowed.edit);
		}
	}
	return item;
}

/** Defensive narrowing of one completion payload; never throws. */
export function narrowCompletionPayload(payload: unknown): NarrowedCompletionPayload {
	if (!isRecord(payload)) return { isIncomplete: false, items: [], omitted: 0 };
	const rawItems = Array.isArray(payload.items) ? payload.items : [];
	const items: NarrowedCompletionItem[] = [];
	let skipped = 0;
	for (const raw of rawItems) {
		const item = narrowCompletionItem(raw);
		if (item) items.push(item);
		else skipped += 1;
	}
	return {
		isIncomplete: payload.isIncomplete === true,
		items,
		omitted: nonNegativeInteger(payload.omitted) + skipped,
	};
}

// -------------------------------------------------------------------- hover

export interface NarrowedHoverPayload {
	contents: string;
	range?: LspRange;
}

/** Hover contents: string, MarkedString array, or { value } envelopes. */
export function narrowHoverPayload(payload: unknown): NarrowedHoverPayload | undefined {
	if (typeof payload === "string") return { contents: payload };
	if (!isRecord(payload)) return undefined;
	const contents = typeof payload.contents === "string"
		? payload.contents
		: Array.isArray(payload.contents)
			? payload.contents
				.map((entry) => (typeof entry === "string" ? entry : isRecord(entry) && typeof entry.value === "string" ? entry.value : ""))
				.filter((entry) => entry.length > 0)
				.join("\n")
			: isRecord(payload.contents) && typeof payload.contents.value === "string"
				? payload.contents.value
				: undefined;
	if (contents === undefined) return undefined;
	const narrowed: NarrowedHoverPayload = { contents };
	if (isLspRange(payload.range)) narrowed.range = copyRange(payload.range);
	return narrowed;
}

export const HOVER_LINE_CAP = 100;
export const HOVER_CHAR_CAP = 20000;

export interface HoverPreparedText {
	/** Sanitized, line-capped hover text ready for display. */
	text: string;
	omittedLines: number;
	charTruncated: boolean;
}

/**
 * Prepare untrusted hover contents for display: hard character cap (never
 * splitting a surrogate pair), `sanitizeDisplay` (control sequences inert),
 * then a logical line cap. Newlines are preserved; the caller wraps.
 */
export function prepareHoverText(raw: unknown): HoverPreparedText | undefined {
	const narrowed = narrowHoverPayload(raw);
	if (!narrowed) return undefined;
	let source = narrowed.contents;
	let charTruncated = false;
	if (source.length > HOVER_CHAR_CAP) {
		source = source.slice(0, HOVER_CHAR_CAP);
		const last = source.charCodeAt(source.length - 1);
		if (last >= 0xd800 && last <= 0xdbff) source = source.slice(0, -1);
		charTruncated = true;
	}
	const lines = sanitizeDisplay(source).split("\n");
	const shown = lines.slice(0, HOVER_LINE_CAP);
	return {
		text: shown.join("\n"),
		omittedLines: Math.max(0, lines.length - HOVER_LINE_CAP),
		charTruncated,
	};
}

// ------------------------------------------------------- locations / symbols

export interface LocationPayloadEntry {
	uri: string;
	range: LspRange;
}

export interface NarrowedLocationsPayload {
	locations: LocationPayloadEntry[];
	omitted: number;
}

function narrowLocationEntry(raw: unknown): LocationPayloadEntry | undefined {
	if (!isRecord(raw) || typeof raw.uri !== "string" || !isLspRange(raw.range)) return undefined;
	return { uri: raw.uri, range: copyRange(raw.range) };
}

/** Defensive narrowing of definition/reference payloads; never throws. */
export function narrowLocationsPayload(payload: unknown): NarrowedLocationsPayload {
	if (!isRecord(payload)) return { locations: [], omitted: 0 };
	const rawLocations = Array.isArray(payload.locations) ? payload.locations : [];
	const locations: LocationPayloadEntry[] = [];
	let skipped = 0;
	for (const raw of rawLocations) {
		const entry = narrowLocationEntry(raw);
		if (entry) locations.push(entry);
		else skipped += 1;
	}
	return { locations, omitted: nonNegativeInteger(payload.omitted) + skipped };
}

export interface NarrowedSymbolEntry {
	name: string;
	kind?: number;
	uri?: string;
	range?: LspRange;
	line?: number;
	character?: number;
	containerName?: string;
}

export interface NarrowedSymbolsPayload {
	symbols: NarrowedSymbolEntry[];
	omitted: number;
}

export const SYMBOLS_CAP = 1000;

function narrowSymbolEntry(raw: unknown, containerName?: string): NarrowedSymbolEntry | undefined {
	if (!isRecord(raw) || typeof raw.name !== "string" || raw.name.length === 0) return undefined;
	const entry: NarrowedSymbolEntry = { name: raw.name };
	if (typeof raw.kind === "number" && Number.isInteger(raw.kind)) entry.kind = raw.kind;
	if (typeof raw.containerName === "string") entry.containerName = raw.containerName;
	else if (containerName !== undefined) entry.containerName = containerName;
	if (isLspRange(raw.selectionRange)) {
		entry.range = copyRange(raw.selectionRange);
		entry.line = raw.selectionRange.start.line;
		entry.character = raw.selectionRange.start.character;
	} else if (isLspRange(raw.range)) {
		entry.range = copyRange(raw.range);
		entry.line = raw.range.start.line;
		entry.character = raw.range.start.character;
	}
	if (!isRecord(raw.location)) return entry;
	// Flat SymbolInformation: location carries the uri and range.
	if (typeof raw.location.uri === "string") entry.uri = raw.location.uri;
	if (isLspRange(raw.location.range)) {
		entry.range = copyRange(raw.location.range);
		entry.line = raw.location.range.start.line;
		entry.character = raw.location.range.start.character;
	}
	return entry;
}

/**
 * Defensive narrowing of symbol payloads: hierarchical DocumentSymbols are
 * flattened depth-first, flat SymbolInformations pass through. Bounded at
 * {@link SYMBOLS_CAP}; the remainder is disclosed as omitted.
 */
export function narrowSymbolsPayload(payload: unknown): NarrowedSymbolsPayload {
	const rawList = isRecord(payload) && Array.isArray(payload.flat)
		? payload.flat
		: isRecord(payload) && Array.isArray(payload.hierarchical)
			? payload.hierarchical
			: Array.isArray(payload)
				? payload
				: [];
	const symbols: NarrowedSymbolEntry[] = [];
	let skipped = 0;
	const walk = (entries: unknown, containerName?: string): void => {
		if (!Array.isArray(entries)) return;
		for (const raw of entries) {
			const entry = narrowSymbolEntry(raw, containerName);
			if (!entry) {
				skipped += 1;
				continue;
			}
			symbols.push(entry);
			if (isRecord(raw) && Array.isArray(raw.children)) walk(raw.children, raw.name as string);
		}
	};
	walk(rawList);
	const total = symbols.length;
	const shown = symbols.slice(0, SYMBOLS_CAP);
	const payloadOmitted = isRecord(payload) ? nonNegativeInteger(payload.omitted) : 0;
	return { symbols: shown, omitted: payloadOmitted + skipped + (total - shown.length) };
}

// ------------------------------------------------------------- diagnostics

export type DiagnosticSeverity = 1 | 2 | 3 | 4;

export interface NarrowedDiagnostic {
	range: LspRange;
	severity: DiagnosticSeverity;
	message: string;
	source?: string;
	code?: string | number;
	/**
	 * Backend producer key (the same key workspace.diagnostics events use).
	 * Pulled payloads tag every row with it; display `source` stays the
	 * server's own label ("Pyright"), which must never be used as the key.
	 */
	producer?: string;
}

function narrowSeverity(value: unknown): DiagnosticSeverity {
	if (value === 2) return 2;
	if (value === 3) return 3;
	if (value === 4) return 4;
	return 1;
}

function narrowDiagnosticEntry(raw: unknown): NarrowedDiagnostic | undefined {
	if (!isRecord(raw) || typeof raw.message !== "string" || !isLspRange(raw.range)) return undefined;
	const diagnostic: NarrowedDiagnostic = {
		range: copyRange(raw.range),
		severity: narrowSeverity(raw.severity),
		message: raw.message,
	};
	if (typeof raw.source === "string" && raw.source.length > 0) diagnostic.source = raw.source;
	if (typeof raw.producer === "string" && raw.producer.length > 0) diagnostic.producer = raw.producer;
	if (typeof raw.code === "string" || (typeof raw.code === "number" && Number.isFinite(raw.code))) {
		diagnostic.code = raw.code;
	}
	return diagnostic;
}

/** Narrow a raw diagnostics list (push events and pull payloads share it). */
export function narrowDiagnosticList(value: unknown): NarrowedDiagnostic[] {
	if (!Array.isArray(value)) return [];
	const out: NarrowedDiagnostic[] = [];
	for (const raw of value) {
		const entry = narrowDiagnosticEntry(raw);
		if (entry) out.push(entry);
	}
	return out;
}

export interface NarrowedDiagnosticsPayload {
	diagnostics: NarrowedDiagnostic[];
	omitted: number;
	/** Producer names the server reported as the current full set. */
	producers: string[];
}

/** Defensive narrowing of the diagnostics pull payload; never throws. */
export function narrowDiagnosticsPayload(payload: unknown): NarrowedDiagnosticsPayload {
	if (!isRecord(payload)) return { diagnostics: [], omitted: 0, producers: [] };
	const producers: string[] = [];
	if (Array.isArray(payload.producers)) {
		for (const entry of payload.producers) {
			// The backend sends producer objects ({source, version, freshness,
			// count}); older payloads sent bare strings. Accept both.
			if (typeof entry === "string" && entry.length > 0) {
				producers.push(entry);
			} else if (isRecord(entry) && typeof entry.source === "string" && entry.source.length > 0) {
				producers.push(entry.source);
			}
		}
	}
	return {
		diagnostics: narrowDiagnosticList(payload.diagnostics),
		omitted: nonNegativeInteger(payload.omitted),
		producers,
	};
}

// ================================================================ Completion

export const COMPLETION_ITEM_CAP = 100;
export const COMPLETION_STORAGE_CAP = 1000;

export type CompletionSessionState = "idle" | "loading" | "visible" | "applying" | "closed";

export type CompletionDropReason = "superseded" | "stale" | "unavailable" | "error";

export interface CompletionAcceptFailure {
	reason: "not-open" | "no-selection" | "invalid-item" | "snippet" | "stale" | "refused";
	message: string;
	refusals?: WorkspaceEditRefusal[];
}

export type CompletionAcceptResult =
	| { ok: true; applied: WorkspaceEditApplication[] }
	| ({ ok: false } & CompletionAcceptFailure);

export interface CompletionSessionOptions {
	store: EditorStore;
	request: IntelligenceRequest;
	uri: string;
	documentId: string;
	position: LspPosition;
	prefix?: string;
	/**
	 * Default false: snippet-format items are rejected because the UI does not
	 * advertise snippet support. When true, only snippet-syntax-free text may
	 * be inserted literally; tab stops are still rejected.
	 */
	allowLiteralSnippets?: boolean;
	/** Presented-item cap; default {@link COMPLETION_ITEM_CAP}. */
	itemCap?: number;
}

/** True when text carries snippet tab-stop/escape syntax that must not be inserted raw. */
export function snippetSyntaxPresent(text: string): boolean {
	return /[$\\]/.test(text);
}

function refusalSummary(refusals: WorkspaceEditRefusal[]): string {
	if (refusals.length === 0) return "the edit was refused";
	return refusals.map((refusal) => `${refusal.uri}: ${refusal.reason}`).join("; ");
}

/**
 * One completion interaction. The caller (controller/UI) creates a session per
 * request and drives it with `start`/`rerun`/`setPrefix`; `accept` performs the
 * version-checked, single-transaction edit application.
 */
export class CompletionSession {
	private readonly store: EditorStore;
	private readonly request: IntelligenceRequest;
	private readonly uriValue: string;
	private readonly documentIdValue: string;
	private readonly allowLiteralSnippets: boolean;
	private readonly itemCap: number;
	private prefixValue: string;
	private positionValue: LspPosition;
	private stateValue: CompletionSessionState = "idle";
	private capturedVersionValue: number | null = null;
	private allItems: NarrowedCompletionItem[] = [];
	private upstreamOmitted = 0;
	private upstreamIncomplete = false;
	private selectionValue = 0;
	private seq = 0;
	private supersededCount = 0;
	private staleCount = 0;
	private dropReasonValue: CompletionDropReason | undefined;
	private closedReasonValue: string | undefined;

	constructor(options: CompletionSessionOptions) {
		this.store = options.store;
		this.request = options.request;
		this.uriValue = options.uri;
		this.documentIdValue = options.documentId;
		this.positionValue = copyPosition(options.position);
		this.prefixValue = options.prefix ?? "";
		this.allowLiteralSnippets = options.allowLiteralSnippets ?? false;
		this.itemCap = Math.max(1, options.itemCap ?? COMPLETION_ITEM_CAP);
	}

	// ------------------------------------------------------------- inspection

	get state(): CompletionSessionState {
		return this.stateValue;
	}

	get uri(): string {
		return this.uriValue;
	}

	get documentId(): string {
		return this.documentIdValue;
	}

	get prefix(): string {
		return this.prefixValue;
	}

	get position(): LspPosition {
		return copyPosition(this.positionValue);
	}

	/** Version captured when the active request was issued. */
	get capturedVersion(): number | null {
		return this.capturedVersionValue;
	}

	get closedReason(): string | undefined {
		return this.closedReasonValue;
	}

	get isIncomplete(): boolean {
		return this.upstreamIncomplete;
	}

	/** Reason the most recent response was dropped (not set by acceptance). */
	get lastDropReason(): CompletionDropReason | undefined {
		return this.dropReasonValue;
	}

	get supersededDrops(): number {
		return this.supersededCount;
	}

	get staleDrops(): number {
		return this.staleCount;
	}

	/** Filtered, capped items as presented to the popup. */
	items(): readonly NarrowedCompletionItem[] {
		return this.filteredAll().slice(0, this.itemCap);
	}

	itemCount(): number {
		return this.items().length;
	}

	/** Filtered items before the presentation cap. */
	filteredCount(): number {
		return this.filteredAll().length;
	}

	/** Every item the server delivered (after storage capping). */
	receivedCount(): number {
		return this.allItems.length;
	}

	/** Items not presented: filtered overflow plus server-side omission. */
	omitted(): number {
		return this.filteredCount() - this.itemCount() + this.upstreamOmitted;
	}

	selectedIndex(): number {
		return this.selectionValue;
	}

	selectedItem(): NarrowedCompletionItem | undefined {
		return this.items()[this.selectionValue];
	}

	// ------------------------------------------------------------ lifecycle

	/** Issue the initial request for the captured prefix/position. */
	async start(): Promise<CompletionSessionState> {
		return this.requestItems(this.prefixValue, this.positionValue);
	}

	/**
	 * Issue a new request, logically cancelling the in-flight one. Superseded
	 * responses never mutate state.
	 */
	async rerun(prefix: string, position?: LspPosition): Promise<CompletionSessionState> {
		return this.requestItems(prefix, position ?? this.positionValue);
	}

	/**
	 * Client-side refilter for a typed prefix. `filterText ?? label` prefix
	 * match, case-insensitive; the selection index survives when the selected
	 * item is still present.
	 */
	setPrefix(prefix: string): void {
		if (prefix === this.prefixValue) return;
		const previous = this.items()[this.selectionValue];
		this.prefixValue = prefix;
		const next = this.items();
		if (previous) {
			const at = next.indexOf(previous);
			this.selectionValue = at >= 0 ? at : Math.min(this.selectionValue, Math.max(0, next.length - 1));
		} else {
			this.selectionValue = 0;
		}
	}

	/** Clamp a selection index into the presented list; returns the new index. */
	setSelectedIndex(index: number): number {
		const count = this.itemCount();
		const clamped = count === 0 ? 0 : Math.max(0, Math.min(count - 1, Math.floor(index)));
		this.selectionValue = clamped;
		return clamped;
	}

	/** Move the selection by `delta` with clamping; returns the new index. */
	moveSelection(delta: number): number {
		return this.setSelectedIndex(this.selectionValue + delta);
	}

	/**
	 * Close the session, cancelling any in-flight request. Idempotent.
	 */
	close(reason?: string): void {
		if (this.stateValue === "closed") return;
		this.seq += 1;
		this.stateValue = "closed";
		if (reason !== undefined) this.closedReasonValue = reason;
	}

	// ------------------------------------------------------------- acceptance

	/**
	 * Stage the selected item as ONE workspace edit (main textEdit preferred,
	 * InsertReplaceEdit normalized to its insert range, label inserted at the
	 * captured position otherwise, additionalTextEdits in the same group) and
	 * apply it. The captured version is validated first: a mismatch rejects as
	 * stale and closes without mutating anything.
	 */
	accept(): CompletionAcceptResult {
		if (this.stateValue !== "visible") {
			return { ok: false, reason: "not-open", message: "no completion list is visible" };
		}
		const item = this.selectedItem();
		if (!item) {
			return { ok: false, reason: "no-selection", message: "no completion item is selected" };
		}
		if (item.malformedTextEdit) {
			return { ok: false, reason: "invalid-item", message: "the completion item carries a malformed text edit" };
		}
		const insertionText = item.textEdit?.newText ?? item.label;
		if (item.insertTextFormat === 2) {
			if (!this.allowLiteralSnippets) {
				return {
					ok: false,
					reason: "snippet",
					message: "snippet insertions (insertTextFormat 2) are not supported",
				};
			}
			if (snippetSyntaxPresent(insertionText)) {
				return {
					ok: false,
					reason: "snippet",
					message: "snippet item carries tab-stop syntax and cannot be inserted literally",
				};
			}
		}
		const record = this.store.getDocument(this.documentIdValue);
		if (!record) {
			this.close("missing-document");
			return { ok: false, reason: "refused", message: "the buffer for this completion is no longer open" };
		}
		if (this.capturedVersionValue !== null && record.version !== this.capturedVersionValue) {
			this.close("stale");
			return { ok: false, reason: "stale", message: "the buffer changed after this completion was computed" };
		}
		const edits: LspTextEdit[] = [];
		// Additional edits are staged FIRST so the main completion edit stays the
		// transaction's primary (caller-order last) and the caret lands after the
		// inserted completion, not after an auto-import at the top of the file.
		for (const extra of item.additionalTextEdits) edits.push(copyEdit(extra));
		if (item.textEdit) {
			edits.push(copyEdit(item.textEdit));
		} else {
			edits.push({
				range: { start: copyPosition(this.positionValue), end: copyPosition(this.positionValue) },
				newText: item.label,
			});
		}
		const payload: WorkspaceEditPayload = {
			label: `Accept ${item.label}`,
			changes: [{ uri: this.uriValue, version: this.capturedVersionValue, edits }],
			refused: [],
		};
		this.stateValue = "applying";
		const result = applyWorkspaceEdit(this.store, payload);
		if (!result.ok) {
			const stale = result.refusals.some((refusal) => refusal.reason === "version-mismatch");
			this.close(stale ? "stale" : "refused");
			return {
				ok: false,
				reason: stale ? "stale" : "refused",
				message: refusalSummary(result.refusals),
				refusals: result.refusals,
			};
		}
		this.close("accepted");
		return { ok: true, applied: result.applied };
	}

	// -------------------------------------------------------------- internals

	/** CFG-safe closed check (the field mutates from outside this method). */
	private isClosed(): boolean {
		return this.stateValue === "closed";
	}

	private filteredAll(): NarrowedCompletionItem[] {
		const prefix = this.prefixValue.toLowerCase();
		if (prefix.length === 0) return this.allItems;
		return this.allItems.filter((item) => (item.filterText ?? item.label).toLowerCase().startsWith(prefix));
	}

	private async requestItems(prefix: string, position: LspPosition): Promise<CompletionSessionState> {
		const seq = ++this.seq;
		const version = this.store.getDocument(this.documentIdValue)?.version ?? null;
		this.capturedVersionValue = version;
		this.positionValue = copyPosition(position);
		this.prefixValue = prefix;
		this.stateValue = "loading";
		this.dropReasonValue = undefined;
		let result: WorkspaceIntelligenceResult;
		try {
			result = await this.request("completion", this.uriValue, version, copyPosition(position));
		} catch {
			if (seq !== this.seq) {
				this.supersededCount += 1;
				return this.stateValue;
			}
			this.stateValue = "closed";
			this.dropReasonValue = "error";
			return this.stateValue;
		}
		if (seq !== this.seq || this.isClosed()) {
			this.supersededCount += 1;
			return this.stateValue;
		}
		if (result.state !== "ready") {
			this.stateValue = "closed";
			this.dropReasonValue = "unavailable";
			return this.stateValue;
		}
		if (result.documentVersion !== null && version !== null && result.documentVersion !== version) {
			this.stateValue = "closed";
			this.dropReasonValue = "stale";
			this.staleCount += 1;
			return this.stateValue;
		}
		const payload = narrowCompletionPayload(result.result);
		let items = payload.items;
		let storageOmitted = 0;
		if (items.length > COMPLETION_STORAGE_CAP) {
			storageOmitted = items.length - COMPLETION_STORAGE_CAP;
			items = items.slice(0, COMPLETION_STORAGE_CAP);
		}
		this.allItems = items;
		this.upstreamOmitted = payload.omitted + storageOmitted;
		this.upstreamIncomplete = payload.isIncomplete;
		this.selectionValue = 0;
		this.stateValue = "visible";
		return this.stateValue;
	}
}

// ==================================================================== Hover

export type HoverSessionState = "idle" | "loading" | "visible" | "dismissed" | "closed";

export interface HoverSessionOptions {
	request: IntelligenceRequest;
	uri: string;
	/** Version captured by the caller when the hover was requested. */
	version?: number | null;
	position: LspPosition;
}

/**
 * One hover request: prepared (sanitized, capped) text plus dismissal state.
 * The session never touches focus; the UI decides where the box renders.
 */
export class HoverSession {
	private readonly request: IntelligenceRequest;
	private readonly uriValue: string;
	private readonly versionValue: number | null;
	private readonly positionValue: LspPosition;
	private stateValue: HoverSessionState = "idle";
	private textValue: string | undefined;
	private rangeValue: LspRange | undefined;
	private omittedLinesValue = 0;
	private charTruncatedValue = false;
	private dropReasonValue: "superseded" | "stale" | "unavailable" | "error" | undefined;
	private seq = 0;

	constructor(options: HoverSessionOptions) {
		this.request = options.request;
		this.uriValue = options.uri;
		this.versionValue = options.version ?? null;
		this.positionValue = copyPosition(options.position);
	}

	get state(): HoverSessionState {
		return this.stateValue;
	}

	get contents(): string | undefined {
		return this.textValue;
	}

	get range(): LspRange | undefined {
		return this.rangeValue ? copyRange(this.rangeValue) : undefined;
	}

	get omittedLines(): number {
		return this.omittedLinesValue;
	}

	get charTruncated(): boolean {
		return this.charTruncatedValue;
	}

	get lastDropReason(): "superseded" | "stale" | "unavailable" | "error" | undefined {
		return this.dropReasonValue;
	}

	async start(): Promise<HoverSessionState> {
		const seq = ++this.seq;
		this.stateValue = "loading";
		this.dropReasonValue = undefined;
		let result: WorkspaceIntelligenceResult;
		try {
			result = await this.request("hover", this.uriValue, this.versionValue, copyPosition(this.positionValue));
		} catch {
			if (seq !== this.seq) return this.stateValue;
			this.stateValue = "closed";
			this.dropReasonValue = "error";
			return this.stateValue;
		}
		if (seq !== this.seq || this.stateValue !== "loading") return this.stateValue;
		if (result.state !== "ready") {
			this.stateValue = "closed";
			this.dropReasonValue = "unavailable";
			return this.stateValue;
		}
		if (
			result.documentVersion !== null &&
			this.versionValue !== null &&
			result.documentVersion !== this.versionValue
		) {
			this.stateValue = "closed";
			this.dropReasonValue = "stale";
			return this.stateValue;
		}
		const prepared = prepareHoverText(result.result);
		if (!prepared || prepared.text.trim().length === 0) {
			this.stateValue = "closed";
			this.dropReasonValue = "unavailable";
			return this.stateValue;
		}
		this.textValue = prepared.text;
		this.omittedLinesValue = prepared.omittedLines;
		this.charTruncatedValue = prepared.charTruncated;
		this.rangeValue = narrowHoverPayload(result.result)?.range;
		this.stateValue = "visible";
		return this.stateValue;
	}

	/** Dismiss the hover box; any in-flight response is dropped. */
	dismiss(): void {
		this.seq += 1;
		this.stateValue = "dismissed";
	}
}

// ========================================================= LocationPicker

export const LOCATION_PICKER_CAP = 100;

export interface LocationChoice {
	uri: string;
	/** 1-based "line:column" label for rows and hints. */
	lineLabel: string;
	line: number;
	character: number;
	range: LspRange;
}

/**
 * Selection model over multiple definition/reference targets. Bounds are
 * strict: next/previous never wrap, and confirm returns the current target.
 */
export class LocationPicker {
	private readonly choices: LocationChoice[];
	private indexValue = 0;
	private readonly omittedValue: number;

	constructor(locations: unknown, options: { cap?: number; omitted?: number } = {}) {
		const cap = Math.max(1, options.cap ?? LOCATION_PICKER_CAP);
		const entries: LocationPayloadEntry[] = [];
		let skipped = 0;
		if (Array.isArray(locations)) {
			for (const raw of locations) {
				const entry = narrowLocationEntry(raw);
				if (entry) entries.push(entry);
				else skipped += 1;
			}
		}
		const shown = entries.slice(0, cap);
		this.choices = shown.map((entry) => ({
			uri: entry.uri,
			lineLabel: `${entry.range.start.line + 1}:${entry.range.start.character + 1}`,
			line: entry.range.start.line,
			character: entry.range.start.character,
			range: copyRange(entry.range),
		}));
		this.omittedValue = (options.omitted ?? 0) + skipped + (entries.length - shown.length);
	}

	get size(): number {
		return this.choices.length;
	}

	index(): number {
		return this.indexValue;
	}

	omitted(): number {
		return this.omittedValue;
	}

	targets(): readonly LocationChoice[] {
		return this.choices.map((choice) => ({ ...choice, range: copyRange(choice.range) }));
	}

	current(): LocationChoice | undefined {
		const choice = this.choices[this.indexValue];
		return choice ? { ...choice, range: copyRange(choice.range) } : undefined;
	}

	/** Advance one target; undefined at the last target (no wrap). */
	next(): LocationChoice | undefined {
		if (this.indexValue >= this.choices.length - 1) return this.current();
		this.indexValue += 1;
		return this.current();
	}

	/** Step back one target; undefined at the first target (no wrap). */
	previous(): LocationChoice | undefined {
		if (this.indexValue <= 0) return this.current();
		this.indexValue -= 1;
		return this.current();
	}

	/** Jump to an index; out-of-range selections are refused. */
	select(index: number): boolean {
		if (!Number.isInteger(index) || index < 0 || index >= this.choices.length) return false;
		this.indexValue = index;
		return true;
	}

	/** The target Enter would open (undefined when there are no targets). */
	confirm(): LocationChoice | undefined {
		return this.current();
	}
}

// =================================================================== Rename

export type RenameSessionState = "idle" | "preparing" | "unavailable" | "ready" | "applied" | "refused";

/**
 * Wider than the frozen seam on purpose: the intelligence service does not
 * allow-list rename yet, so callers inject a dedicated request only when the
 * operation exists. Absent it, `start()` reports unavailable.
 */
export type RenameRequest = (
	operation: "rename",
	uri: string,
	version: number | null,
	position?: LspPosition,
) => Promise<WorkspaceIntelligenceResult>;

export const RENAME_UNAVAILABLE_REASON =
	"rename is unavailable: the workspace intelligence service does not offer the rename operation yet";

export interface RenameSessionOptions {
	store: EditorStore;
	uri: string;
	documentId: string;
	position: LspPosition;
	requestRename?: RenameRequest;
}

export type RenameStartResult =
	| { ok: true; preview: WorkspaceEditPreview; payload: WorkspaceEditPayload }
	| { ok: false; unavailable: boolean; reason: string };

export type RenameApplyResult =
	| { ok: true; applied: WorkspaceEditApplication[] }
	| { ok: false; reason: "not-ready" | "not-confirmed" | "refused"; message: string; refusals?: WorkspaceEditRefusal[] };

function isWorkspaceEditPayload(value: unknown): value is WorkspaceEditPayload {
	if (!isRecord(value)) return false;
	if (typeof value.label !== "string") return false;
	if (!Array.isArray(value.changes) || !Array.isArray(value.refused)) return false;
	return value.changes.every(
		(change) =>
			isRecord(change) &&
			typeof change.uri === "string" &&
			(change.version === null || typeof change.version === "number") &&
			Array.isArray(change.edits),
	);
}

/**
 * Prepare/confirm one rename. Until the service allow-lists rename, `start()`
 * is an honest `unavailable`; when a payload does arrive, the preview comes
 * from `previewWorkspaceEdit` and application re-validates versions inside
 * `applyWorkspaceEdit`.
 */
export class RenameSession {
	private readonly store: EditorStore;
	private readonly uriValue: string;
	private readonly documentIdValue: string;
	private readonly positionValue: LspPosition;
	private readonly requestRename: RenameRequest | undefined;
	private stateValue: RenameSessionState = "idle";
	private payloadValue: WorkspaceEditPayload | undefined;
	private previewValue: WorkspaceEditPreview | undefined;

	constructor(options: RenameSessionOptions) {
		this.store = options.store;
		this.uriValue = options.uri;
		this.documentIdValue = options.documentId;
		this.positionValue = copyPosition(options.position);
		this.requestRename = options.requestRename;
	}

	get state(): RenameSessionState {
		return this.stateValue;
	}

	get preview(): WorkspaceEditPreview | undefined {
		return this.previewValue;
	}

	async start(): Promise<RenameStartResult> {
		if (!this.requestRename) {
			this.stateValue = "unavailable";
			return { ok: false, unavailable: true, reason: RENAME_UNAVAILABLE_REASON };
		}
		this.stateValue = "preparing";
		const version = this.store.getDocument(this.documentIdValue)?.version ?? null;
		let result: WorkspaceIntelligenceResult;
		try {
			result = await this.requestRename("rename", this.uriValue, version, copyPosition(this.positionValue));
		} catch {
			this.stateValue = "unavailable";
			return { ok: false, unavailable: false, reason: "rename request failed" };
		}
		if (result.state !== "ready") {
			this.stateValue = "unavailable";
			return { ok: false, unavailable: false, reason: `rename is unavailable (server state: ${result.state})` };
		}
		const raw = isRecord(result.result) ? result.result : undefined;
		const unavailable = raw?.unavailable === true;
		const edits = isWorkspaceEditPayload(raw?.edits) ? raw.edits : undefined;
		if (unavailable || !edits) {
			this.stateValue = "unavailable";
			return {
				ok: false,
				unavailable: true,
				reason: "the language server did not offer a rename at this position",
			};
		}
		this.payloadValue = edits;
		this.previewValue = previewWorkspaceEdit(this.store, edits);
		this.stateValue = "ready";
		return { ok: true, preview: this.previewValue, payload: edits };
	}

	/** Apply the prepared payload; `confirmed` gates the user decision. */
	apply(confirmed: boolean): RenameApplyResult {
		if (this.stateValue !== "ready" || !this.payloadValue) {
			return { ok: false, reason: "not-ready", message: "no rename proposal is ready to apply" };
		}
		if (!confirmed) {
			return { ok: false, reason: "not-confirmed", message: "rename was not confirmed" };
		}
		const result = applyWorkspaceEdit(this.store, this.payloadValue);
		if (!result.ok) {
			this.stateValue = "refused";
			return {
				ok: false,
				reason: "refused",
				message: refusalSummary(result.refusals),
				refusals: result.refusals,
			};
		}
		this.stateValue = "applied";
		return { ok: true, applied: result.applied };
	}
}

// ================================================================= Problems

export const PROBLEMS_TOTAL_CAP = 1000;

export interface SeverityCounts {
	error: number;
	warning: number;
	info: number;
	hint: number;
	total: number;
}

export interface ProblemRow {
	uri: string;
	/** LSP producer name (`workspace.diagnostics` event `source`, "" when absent). */
	producer: string;
	/** Version the diagnostics were computed against; null labels unversioned rows. */
	version: number | null;
	severity: DiagnosticSeverity;
	message: string;
	source?: string;
	code?: string | number;
	/** 0-based LSP start line. */
	line: number;
	/** 0-based LSP start character. */
	character: number;
	range: LspRange;
}

export interface DiagnosticPushUpdate {
	uri: string;
	version: number | null;
	/** LSP producer name (workspace.diagnostics event `source`). */
	source: string;
	diagnostics: unknown;
}

export type ProblemsRefreshResult =
	| { ok: true; producers: number; records: number }
	| { ok: false; reason: "no-request" | "unavailable" | "error"; state?: string };

interface ProducerEntry {
	version: number | null;
	rows: ProblemRow[];
	/** Rows this entry lost to the total cap since its last replacement. */
	dropped: number;
}

/** severity (errors first) → uri → line → character → producer. */
function compareRows(a: ProblemRow, b: ProblemRow): number {
	if (a.severity !== b.severity) return a.severity - b.severity;
	if (a.uri !== b.uri) return a.uri < b.uri ? -1 : 1;
	if (a.line !== b.line) return a.line - b.line;
	if (a.character !== b.character) return a.character - b.character;
	if (a.producer !== b.producer) return a.producer < b.producer ? -1 : 1;
	return 0;
}

/**
 * Display identity for the dedupe (finding 4): everything the pane shows
 * except the producer label — including the version and display source the
 * row prints as `[unversioned]` / `(source)`, so rows that render differently
 * never collapse into one. `code` stringifies so 0 stays distinct from
 * undefined; the full range participates so same-start/different-span rows
 * stay distinct; `version` normalizes null to an empty field.
 */
function problemRowKey(row: ProblemRow): string {
	const { start, end } = row.range;
	const code = row.code === undefined ? "" : String(row.code);
	return [
		row.uri,
		String(start.line),
		String(start.character),
		String(end.line),
		String(end.character),
		code,
		String(row.severity),
		row.message,
		row.version === null ? "" : String(row.version),
		row.source ?? "",
	].join("\u0000");
}

/**
 * Per-uri, per-producer diagnostics cache fed by `workspace.diagnostics`
 * pushes and by `refresh(uri)` pulls.
 *
 * Replace semantics: a push replaces ALL rows of its (uri, producer) pair —
 * there is no merge, so stale entries cannot accumulate. Version rule: an
 * incoming versioned push older than the stored version is dropped as stale;
 * an incoming unversioned push replaces and marks its rows unversioned.
 * Refresh pulls replace the producers the server named (an empty list clears);
 * unnamed producers are left untouched because clearing them would invent.
 */
export class ProblemsModel {
	private readonly request: IntelligenceRequest | undefined;
	private readonly entries = new Map<string, Map<string, ProducerEntry>>();
	private readonly upstreamOmitted = new Map<string, number>();
	private rowCount = 0;
	private staleCount = 0;

	constructor(options: { request?: IntelligenceRequest } = {}) {
		this.request = options.request;
	}

	/** Replace one producer's diagnostics for a uri; returns its stored row count. */
	push(update: DiagnosticPushUpdate): number {
		const uri = typeof update.uri === "string" ? update.uri : "";
		if (uri.length === 0) return 0;
		const producer = typeof update.source === "string" ? update.source : "";
		const version = update.version === null || typeof update.version === "number" ? update.version : null;
		const diagnostics = narrowDiagnosticList(update.diagnostics);
		const rows = diagnostics.map((diagnostic) => this.toRow(uri, producer, version, diagnostic));
		return this.replace(uri, producer, version, rows);
	}

	/** Drop entries for a uri (all producers) or one producer. */
	clear(uri: string, producer?: string): boolean {
		const uriMap = this.entries.get(uri);
		if (!uriMap) return false;
		if (producer === undefined) {
			for (const entry of uriMap.values()) this.rowCount -= entry.rows.length;
			this.entries.delete(uri);
			this.upstreamOmitted.delete(uri);
			return true;
		}
		const entry = uriMap.get(producer);
		if (!entry) return false;
		this.rowCount -= entry.rows.length;
		uriMap.delete(producer);
		if (uriMap.size === 0) this.entries.delete(uri);
		return true;
	}

	/**
	 * Pull diagnostics for one uri and replace the producers the payload
	 * names. Unavailable/errors retain the existing projection (T8: stale
	 * diagnostics survive a crash until replaced).
	 */
	async refresh(uri: string, version: number | null = null): Promise<ProblemsRefreshResult> {
		if (!this.request) return { ok: false, reason: "no-request" };
		let result: WorkspaceIntelligenceResult;
		try {
			result = await this.request("diagnostics", uri, version);
		} catch {
			return { ok: false, reason: "error" };
		}
		if (result.state !== "ready") return { ok: false, reason: "unavailable", state: result.state };
		const payload = narrowDiagnosticsPayload(result.result);
		const effectiveVersion = result.documentVersion !== null ? result.documentVersion : version;
		const groups = new Map<string, ProblemRow[]>();
		for (const diagnostic of payload.diagnostics) {
			// Group under the BACKEND producer key so a pull replaces the set a
			// workspace.diagnostics push created instead of adding duplicate
			// rows under the server's display label ("Pyright" vs "pyright").
			// Single-producer payloads attribute unattributed rows to that key.
			const producer =
				diagnostic.producer ??
				(payload.producers.length === 1 ? payload.producers[0] : undefined) ??
				diagnostic.source ??
				"";
			const rows = groups.get(producer) ?? [];
			rows.push(this.toRow(uri, producer, effectiveVersion, diagnostic));
			groups.set(producer, rows);
		}
		for (const producer of payload.producers) {
			if (!groups.has(producer)) groups.set(producer, []);
		}
		let records = 0;
		for (const [producer, rows] of groups) {
			this.replace(uri, producer, effectiveVersion, rows);
			records += rows.length;
		}
		if (groups.size > 0 || payload.omitted > 0) this.upstreamOmitted.set(uri, payload.omitted);
		this.enforceCap();
		return { ok: true, producers: groups.size, records };
	}

	/**
	 * Sorted, deduplicated projection: severity → uri → line → character →
	 * producer. Finding 4: rows a user cannot tell apart (same uri, range,
	 * code, message and severity) collapse to one — the event feed and the
	 * refresh pull can deliver the same diagnostic under different producer
	 * labels; distinct content still renders as distinct rows.
	 */
	rows(): ProblemRow[] {
		return this.projected().map((row) => ({ ...row, range: copyRange(row.range) }));
	}

	/** Deduped projection shared by `rows()` and `counts()` (one truth). */
	private projected(): ProblemRow[] {
		const all: ProblemRow[] = [];
		for (const uriMap of this.entries.values()) {
			for (const entry of uriMap.values()) all.push(...entry.rows);
		}
		all.sort(compareRows);
		const seen = new Set<string>();
		const out: ProblemRow[] = [];
		for (const row of all) {
			const key = problemRowKey(row);
			if (seen.has(key)) continue;
			seen.add(key);
			out.push(row);
		}
		return out;
	}

	counts(): SeverityCounts {
		const counts: SeverityCounts = { error: 0, warning: 0, info: 0, hint: 0, total: 0 };
		for (const row of this.projected()) {
			if (row.severity === 1) counts.error += 1;
			else if (row.severity === 2) counts.warning += 1;
			else if (row.severity === 3) counts.info += 1;
			else counts.hint += 1;
			counts.total += 1;
		}
		return counts;
	}

	total(): number {
		return this.rowCount;
	}

	/**
	 * Rows lost to the total cap (summed per producer entry, so replacing a
	 * producer clears its share) plus the server's latest omitted counts.
	 */
	omitted(): number {
		let upstream = 0;
		for (const value of this.upstreamOmitted.values()) upstream += value;
		let dropped = 0;
		for (const uriMap of this.entries.values()) {
			for (const entry of uriMap.values()) dropped += entry.dropped;
		}
		return dropped + upstream;
	}

	stalePushes(): number {
		return this.staleCount;
	}

	/** Distinct producer names currently carrying rows (sorted). */
	producers(): string[] {
		const names = new Set<string>();
		for (const uriMap of this.entries.values()) {
			for (const [producer, entry] of uriMap.entries()) {
				if (entry.rows.length > 0) names.add(producer);
			}
		}
		return [...names].sort();
	}

	uris(): string[] {
		return [...this.entries.keys()].sort();
	}

	// ------------------------------------------------------------- internals

	private toRow(
		uri: string,
		producer: string,
		version: number | null,
		diagnostic: NarrowedDiagnostic,
	): ProblemRow {
		const row: ProblemRow = {
			uri,
			producer,
			version,
			severity: diagnostic.severity,
			message: diagnostic.message,
			line: diagnostic.range.start.line,
			character: diagnostic.range.start.character,
			range: copyRange(diagnostic.range),
		};
		// The row's display source falls back to the producer name (the
		// `workspace.diagnostics` event source) when the diagnostic omits it.
		const source = diagnostic.source ?? (producer.length > 0 ? producer : undefined);
		if (source !== undefined) row.source = source;
		if (diagnostic.code !== undefined) row.code = diagnostic.code;
		return row;
	}

	/** Version-checked producer replacement; returns the stored row count. */
	private replace(uri: string, producer: string, version: number | null, rows: ProblemRow[]): number {
		let uriMap = this.entries.get(uri);
		if (!uriMap) {
			uriMap = new Map<string, ProducerEntry>();
			this.entries.set(uri, uriMap);
		}
		const existing = uriMap.get(producer);
		if (existing && existing.version !== null && version !== null && version < existing.version) {
			this.staleCount += 1;
			return existing.rows.length;
		}
		if (existing) this.rowCount -= existing.rows.length;
		const entry: ProducerEntry = { version, rows, dropped: 0 };
		uriMap.set(producer, entry);
		this.rowCount += rows.length;
		this.enforceCap();
		return entry.rows.length;
	}

	/** Evict the lowest-priority rows until the total cap holds. */
	private enforceCap(): void {
		if (this.rowCount <= PROBLEMS_TOTAL_CAP) return;
		const ranked: Array<{ uri: string; producer: string; row: ProblemRow }> = [];
		for (const [uri, uriMap] of this.entries.entries()) {
			for (const [producer, entry] of uriMap.entries()) {
				for (const row of entry.rows) ranked.push({ uri, producer, row });
			}
		}
		ranked.sort((a, b) => compareRows(a.row, b.row));
		while (this.rowCount > PROBLEMS_TOTAL_CAP && ranked.length > 0) {
			const worst = ranked.pop() as { uri: string; producer: string; row: ProblemRow };
			const entry = this.entries.get(worst.uri)?.get(worst.producer);
			if (!entry) continue;
			const at = entry.rows.indexOf(worst.row);
			if (at < 0) continue;
			entry.rows.splice(at, 1);
			entry.dropped += 1;
			this.rowCount -= 1;
		}
	}
}
