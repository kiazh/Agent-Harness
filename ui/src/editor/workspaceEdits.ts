// Workspace-edit staging (plan §18): preview, version/range validation and
// synchronous all-or-nothing staging into the editor model.
//
// All three entry points are pure or synchronous:
// - `previewWorkspaceEdit` never mutates; it derives bounded human-readable
//   hunk summaries and per-file problems.
// - `validateWorkspaceEdit` captures each target document's snapshot exactly
//   once and decodes every LSP range against that snapshot.
// - `applyWorkspaceEdit` re-validates, then stages each document as ONE store
//   transaction in a single synchronous pass. Because there is no await
//   between validation and staging, no refusal path can leave a partial
//   multi-file mutation behind: either every file validates and is staged, or
//   nothing is touched.
//
// Duplicate URI groups inside one payload are merged into a single
// per-document unit before validation and apply. Applying two groups for the
// same URI in sequence would decode the second group's ranges against a
// snapshot the first group already changed; merged groups validate together
// (cross-group overlaps included) and stage in one transaction. Non-null
// versions that disagree across merged groups refuse as `version-mismatch`.
//
// Same-position inserts: the store's `applyEdit` composes one transaction in
// ascending (startOffset, array order) order, which is exactly the
// protocol-defined sequential application. The equivalent descending-apply
// formulation applies equal offsets in reverse-array order so array order is
// preserved in the output; tests pin a fixture where a naive reverse sort
// would invert them.
//
// Summaries are plain text: slices are bounded and line terminators collapse
// to a visible "\n" escape so a hunk stays one row. No terminal-control
// sanitization happens here — the display layer owns that.

import type { DocumentRecord, EditorStore, TextEdit } from "./model.ts";
import { positionToOffset } from "./positions.ts";
import type {
	CodeActionPayload,
	LspPosition,
	LspTextEdit,
	WorkspaceEditPayload,
} from "../workspace/protocol.ts";

/** Refusal vocabulary for one target file (plan §18 validation contract). */
export type WorkspaceEditRefusalReason =
	| "missing-document"
	| "version-mismatch"
	| "invalid-range"
	| "overlapping-edits"
	| "read-only";

export interface WorkspaceEditRefusal {
	uri: string;
	reason: WorkspaceEditRefusalReason;
}

/** One decoded edit, ready for a single store transaction. */
export interface ValidatedWorkspaceEdit {
	startOffset: number;
	endOffset: number;
	newText: string;
	/** Payload array position; preserves same-position insert order. */
	order: number;
}

export interface ValidatedWorkspaceDocument {
	documentId: string;
	uri: string;
	/** Exact text every range was decoded against. */
	snapshot: string;
	edits: ValidatedWorkspaceEdit[];
}

export type WorkspaceEditValidation =
	| { ok: true; perDocument: ValidatedWorkspaceDocument[] }
	| { ok: false; refusals: WorkspaceEditRefusal[] };

export interface WorkspaceEditApplication {
	documentId: string;
	uri: string;
	versionAfter: number;
	/**
	 * Identifies the staged model transaction (`<documentId>#v<versionBefore>`).
	 * Absent for zero-edit groups, which stage no transaction.
	 */
	transactionId?: string;
}

export type WorkspaceEditApplyResult =
	| { ok: true; applied: WorkspaceEditApplication[] }
	| { ok: false; refusals: WorkspaceEditRefusal[] };

export interface WorkspaceEditPreviewHunk {
	summary: string;
}

export interface WorkspaceEditPreviewFile {
	uri: string;
	documentId?: string;
	editCount: number;
	/** At most {@link WORKSPACE_EDIT_HUNK_LIMIT} summaries, in payload order. */
	hunks: WorkspaceEditPreviewHunk[];
	/** Summaries beyond the per-file bound (the edit count minus shown hunks). */
	omittedHunks: number;
	/** Human-readable validation problems for this file; empty when it validates. */
	problems: string[];
}

export interface WorkspaceEditPreview {
	files: WorkspaceEditPreviewFile[];
	/** Passthrough of `payload.refused` (files the backend could not build). */
	refused: Array<{ uri: string; reason: string }>;
	totalEdits: number;
	fileCount: number;
}

/** Shown hunks per file; the remainder is reported through `omittedHunks`. */
export const WORKSPACE_EDIT_HUNK_LIMIT = 20;
/** Upper bound for one hunk summary string. */
export const WORKSPACE_EDIT_SUMMARY_LIMIT = 80;
/** Per-side slice bound inside one summary, so both sides stay visible. */
const SUMMARY_SLICE_LIMIT = 32;

const REFUSAL_PROBLEM_TEXT: Record<WorkspaceEditRefusalReason, string> = {
	"missing-document": "missing document: no open buffer for this URI",
	"version-mismatch": "stale version: the buffer changed after these edits were computed",
	"invalid-range": "invalid edit range: at least one edit does not decode against the buffer",
	"overlapping-edits": "overlapping edits: at least one pair of edits conflicts",
	"read-only": "read-only document: edits cannot be staged",
};

// --------------------------------------------------------------- narrowing

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
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

// ---------------------------------------------------------------- merging

interface MergedChange {
	uri: string;
	/** First non-null version seen; `null` stays unversioned. Unknowns mismatch. */
	version: unknown;
	/** True when two non-null versions disagreed; the payload is inconsistent. */
	versionConflict: boolean;
	edits: unknown[];
}

/**
 * Group payload changes by exact URI, preserving first-seen order. A per-file
 * group is the protocol contract, but duplicate groups must not stage twice
 * against one snapshot: they merge, and their edits keep their global payload
 * array order (`order` is assigned when decoding).
 */
function mergePayloadChanges(payload: WorkspaceEditPayload): MergedChange[] {
	const changes = Array.isArray(payload?.changes) ? payload.changes : [];
	const byUri = new Map<string, MergedChange>();
	const groups: MergedChange[] = [];
	for (const change of changes) {
		if (!isRecord(change) || typeof change.uri !== "string") continue;
		let group = byUri.get(change.uri);
		if (!group) {
			group = { uri: change.uri, version: null, versionConflict: false, edits: [] };
			byUri.set(change.uri, group);
			groups.push(group);
		}
		if (group.version === null) {
			group.version = change.version;
		} else if (change.version !== null && group.version !== change.version) {
			group.versionConflict = true;
		}
		if (Array.isArray(change.edits)) group.edits.push(...change.edits);
	}
	return groups;
}

function documentByUri(store: EditorStore, uri: string): DocumentRecord | undefined {
	// The store's dedup key is the exact URI string; canonicalization is the
	// file service's job (model §open). Never create buffers for unknown URIs.
	for (const record of store.documents()) {
		if (record.uri === uri) return record;
	}
	return undefined;
}

// --------------------------------------------------------------- decoding

/**
 * Decode one protocol edit against the snapshot captured for its document.
 * Malformed envelopes (non-integer/negative positions, non-string `newText`)
 * and reversed decoded spans are `invalid-range`. Positions outside the
 * snapshot are clamped by `positionToOffset` exactly like the rest of the
 * editor; the explicit bounds check keeps the decoded offsets honest.
 */
function decodeEdit(raw: unknown, snapshot: string, order: number): ValidatedWorkspaceEdit | undefined {
	if (!isRecord(raw)) return undefined;
	const range = raw.range;
	if (!isRecord(range)) return undefined;
	if (!isLspPosition(range.start) || !isLspPosition(range.end)) return undefined;
	if (typeof raw.newText !== "string") return undefined;
	const startOffset = positionToOffset(snapshot, range.start);
	const endOffset = positionToOffset(snapshot, range.end);
	if (startOffset < 0 || endOffset < startOffset || endOffset > snapshot.length) return undefined;
	return { startOffset, endOffset, newText: raw.newText, order };
}

/**
 * Pairwise range rule, identical to the model's transaction rule (plan §18.3):
 * - two zero-width inserts never conflict (protocol array order is preserved);
 * - an insert inside another range's [start, end) span conflicts, including
 *   exactly at its start (ambiguous), while exactly at its end is allowed;
 * - two replaced spans conflict when their half-open intervals intersect.
 */
function editsOverlap(a: ValidatedWorkspaceEdit, b: ValidatedWorkspaceEdit): boolean {
	const aInsert = a.startOffset === a.endOffset;
	const bInsert = b.startOffset === b.endOffset;
	if (aInsert && bInsert) return false;
	if (aInsert) return b.startOffset <= a.startOffset && a.startOffset < b.endOffset;
	if (bInsert) return a.startOffset <= b.startOffset && b.startOffset < a.endOffset;
	return a.startOffset < b.endOffset && b.startOffset < a.endOffset;
}

function hasOverlappingEdits(edits: ValidatedWorkspaceEdit[]): boolean {
	for (let a = 0; a < edits.length; a++) {
		for (let b = a + 1; b < edits.length; b++) {
			const first = edits[a];
			const second = edits[b];
			if (first && second && editsOverlap(first, second)) return true;
		}
	}
	return false;
}

// ------------------------------------------------------------- validation

/**
 * Validate every change group against the CURRENT store state. One refusal
 * per file, in first-seen file order, with a deterministic reason precedence:
 * missing document, then version, then read-only, then range, then overlap.
 * A valid result carries the captured snapshots and decoded edits; nothing
 * is mutated.
 */
export function validateWorkspaceEdit(
	store: EditorStore,
	payload: WorkspaceEditPayload,
): WorkspaceEditValidation {
	const groups = mergePayloadChanges(payload);
	const refusals: WorkspaceEditRefusal[] = [];
	const perDocument: ValidatedWorkspaceDocument[] = [];
	for (const group of groups) {
		const record = documentByUri(store, group.uri);
		if (!record) {
			refusals.push({ uri: group.uri, reason: "missing-document" });
			continue;
		}
		if (group.versionConflict || (group.version !== null && group.version !== record.version)) {
			refusals.push({ uri: group.uri, reason: "version-mismatch" });
			continue;
		}
		if (record.readOnly) {
			refusals.push({ uri: group.uri, reason: "read-only" });
			continue;
		}
		const snapshot = record.text;
		const edits: ValidatedWorkspaceEdit[] = [];
		let invalidRange = false;
		for (let order = 0; order < group.edits.length; order++) {
			const decoded = decodeEdit(group.edits[order], snapshot, order);
			if (!decoded) {
				invalidRange = true;
				break;
			}
			edits.push(decoded);
		}
		if (invalidRange) {
			refusals.push({ uri: group.uri, reason: "invalid-range" });
			continue;
		}
		if (hasOverlappingEdits(edits)) {
			refusals.push({ uri: group.uri, reason: "overlapping-edits" });
			continue;
		}
		perDocument.push({ documentId: record.id, uri: record.uri, snapshot, edits });
	}
	if (refusals.length > 0) return { ok: false, refusals };
	return { ok: true, perDocument };
}

// ------------------------------------------------------------------ apply

function reasonFromModelError(code: string): WorkspaceEditRefusalReason {
	if (code === "read-only") return "read-only";
	if (code === "not-found") return "missing-document";
	return "invalid-range";
}

/**
 * Stage an accepted proposal. Re-validates first: the caller's earlier
 * validation may be stale by the time the user confirms. Every document gets
 * exactly one `store.applyEdit` call (one version increment, one undo group);
 * the whole pass is synchronous, so a refusal proves no document was touched.
 */
export function applyWorkspaceEdit(
	store: EditorStore,
	payload: WorkspaceEditPayload,
): WorkspaceEditApplyResult {
	const validation = validateWorkspaceEdit(store, payload);
	if (!validation.ok) return { ok: false, refusals: validation.refusals };

	const applied: WorkspaceEditApplication[] = [];
	for (const document of validation.perDocument) {
		const record = store.getDocument(document.documentId);
		if (!record) {
			// Unreachable: validation resolved the document synchronously above.
			return { ok: false, refusals: [{ uri: document.uri, reason: "missing-document" }] };
		}
		const versionBefore = record.version;
		if (document.edits.length === 0) {
			// A validated zero-edit group stages no transaction and keeps its version.
			applied.push({ documentId: document.documentId, uri: document.uri, versionAfter: versionBefore });
			continue;
		}
		const edits: TextEdit[] = document.edits.map((edit) => ({
			range: { startOffset: edit.startOffset, endOffset: edit.endOffset },
			text: edit.newText,
		}));
		const result = store.applyEdit(document.documentId, edits);
		if (!result.ok) {
			// Unreachable under the synchronous store contract after validation
			// above; kept as an honest structured guard rather than a throw.
			return { ok: false, refusals: [{ uri: document.uri, reason: reasonFromModelError(result.error.code) }] };
		}
		applied.push({
			documentId: document.documentId,
			uri: document.uri,
			versionAfter: result.version,
			transactionId: `${document.documentId}#v${versionBefore}`,
		});
	}
	return { ok: true, applied };
}

// ---------------------------------------------------------------- preview

function truncateSummarySlice(text: string): string {
	if (text.length <= SUMMARY_SLICE_LIMIT) return text;
	let cut = SUMMARY_SLICE_LIMIT - 1;
	const code = text.charCodeAt(cut - 1);
	if (code >= 0xd800 && code <= 0xdbff) cut -= 1; // never split a surrogate pair
	return `${text.slice(0, cut)}…`;
}

/** Plain quoted slice; line terminators collapse so one hunk stays one row. */
function quoteSlice(text: string): string {
	return `"${truncateSummarySlice(text.replace(/\r\n|\r|\n/g, "\\n"))}"`;
}

function boundSummary(summary: string): string {
	if (summary.length <= WORKSPACE_EDIT_SUMMARY_LIMIT) return summary;
	return `${summary.slice(0, WORKSPACE_EDIT_SUMMARY_LIMIT - 1)}…`;
}

function fallbackSummary(raw: unknown): string {
	const range = isRecord(raw) ? raw.range : undefined;
	const start = isRecord(range) ? range.start : undefined;
	const end = isRecord(range) ? range.end : undefined;
	const newText = isRecord(raw) && typeof raw.newText === "string" ? raw.newText : undefined;
	const where = isLspPosition(start) ? `@${start.line}:${start.character}` : "@?";
	if (newText === undefined) return `${where} (unreadable edit)`;
	const zeroWidth =
		isLspPosition(start) && isLspPosition(end) && start.line === end.line && start.character === end.character;
	if (newText === "") return zeroWidth ? `${where} (no-op)` : `${where} (delete)`;
	return `${where} ${zeroWidth ? "+" : "~"} ${quoteSlice(newText)}`;
}

/**
 * One hunk summary. When the file validated, the summary is derived from the
 * before/after slices against the captured snapshot (`- "old" + "new"`);
 * otherwise (missing/stale/refused files) it falls back to a position-anchored
 * description, because no authoritative "before" text exists there.
 */
function summarizeEdit(
	raw: unknown,
	snapshot: string | undefined,
	decoded: ValidatedWorkspaceEdit | undefined,
): string {
	if (snapshot !== undefined && decoded) {
		const before = snapshot.slice(decoded.startOffset, decoded.endOffset);
		const parts: string[] = [];
		if (before.length > 0) parts.push(`- ${quoteSlice(before)}`);
		if (decoded.newText.length > 0) parts.push(`+ ${quoteSlice(decoded.newText)}`);
		if (parts.length === 0) return "(no-op)";
		return boundSummary(parts.join(" "));
	}
	return boundSummary(fallbackSummary(raw));
}

function copyRefused(refused: unknown): Array<{ uri: string; reason: string }> {
	if (!Array.isArray(refused)) return [];
	const out: Array<{ uri: string; reason: string }> = [];
	for (const entry of refused) {
		if (!isRecord(entry)) continue;
		if (typeof entry.uri !== "string" || typeof entry.reason !== "string") continue;
		out.push({ uri: entry.uri, reason: entry.reason });
	}
	return out;
}

/**
 * Bounded multi-file preview (plan §18.5). Pure: the store is only read.
 * `problems` mirrors validation; hunks are capped at
 * {@link WORKSPACE_EDIT_HUNK_LIMIT} per file with `omittedHunks` disclosing
 * the remainder.
 */
export function previewWorkspaceEdit(store: EditorStore, payload: WorkspaceEditPayload): WorkspaceEditPreview {
	const groups = mergePayloadChanges(payload);
	const validation = validateWorkspaceEdit(store, payload);
	const validatedByUri = new Map<string, ValidatedWorkspaceDocument>();
	const refusalByUri = new Map<string, WorkspaceEditRefusal>();
	if (validation.ok) {
		for (const document of validation.perDocument) validatedByUri.set(document.uri, document);
	} else {
		for (const refusal of validation.refusals) {
			if (!refusalByUri.has(refusal.uri)) refusalByUri.set(refusal.uri, refusal);
		}
	}

	let totalEdits = 0;
	const files: WorkspaceEditPreviewFile[] = [];
	for (const group of groups) {
		totalEdits += group.edits.length;
		const record = documentByUri(store, group.uri);
		const validated = validatedByUri.get(group.uri);
		const decodedByOrder = validated
			? new Map(validated.edits.map((edit) => [edit.order, edit]))
			: undefined;
		const summaries: string[] = [];
		for (let order = 0; order < group.edits.length; order++) {
			summaries.push(summarizeEdit(group.edits[order], validated?.snapshot, decodedByOrder?.get(order)));
		}
		const refusal = refusalByUri.get(group.uri);
		const file: WorkspaceEditPreviewFile = {
			uri: group.uri,
			editCount: group.edits.length,
			hunks: summaries.slice(0, WORKSPACE_EDIT_HUNK_LIMIT).map((summary) => ({ summary })),
			omittedHunks: Math.max(0, summaries.length - WORKSPACE_EDIT_HUNK_LIMIT),
			problems: refusal ? [REFUSAL_PROBLEM_TEXT[refusal.reason]] : [],
		};
		if (record) file.documentId = record.id;
		files.push(file);
	}
	return { files, refused: copyRefused(payload?.refused), totalEdits, fileCount: files.length };
}

// ------------------------------------------------------------ formatting

/**
 * Wrap one server formatting result as a payload targeting the document's
 * CURRENT version. Unknown URIs are refused instead of inventing a version.
 */
export function formatEditsForDocument(store: EditorStore, uri: string, edits: LspTextEdit[]): WorkspaceEditPayload {
	const record = documentByUri(store, uri);
	if (!record) {
		return { label: "Format Document", changes: [], refused: [{ uri, reason: "missing-document" }] };
	}
	const source = Array.isArray(edits) ? edits : [];
	return {
		label: "Format Document",
		changes: [
			{
				uri,
				version: record.version,
				// Clone the ranges: the payload must not alias the caller's result.
				edits: source.map((edit) => ({
					range: { start: { ...edit.range.start }, end: { ...edit.range.end } },
					newText: edit.newText,
				})),
			},
		],
		refused: [],
	};
}

// ---------------------------------------------------------- code actions

function isWorkspaceEditPayload(value: unknown): value is WorkspaceEditPayload {
	if (!isRecord(value)) return false;
	if (typeof value.label !== "string") return false;
	if (!Array.isArray(value.changes) || !Array.isArray(value.refused)) return false;
	// Envelope only: per-edit structure is checked defensively at decode time.
	return value.changes.every(
		(change) =>
			isRecord(change) &&
			typeof change.uri === "string" &&
			(change.version === null || typeof change.version === "number") &&
			Array.isArray(change.edits),
	);
}

type CodeActionCommand = { title: string; command: string; arguments?: unknown[] };

function isCodeActionCommand(value: unknown): value is CodeActionCommand {
	if (!isRecord(value)) return false;
	return typeof value.title === "string" && typeof value.command === "string";
}

function narrowCommand(value: unknown): CodeActionCommand | undefined {
	if (!isCodeActionCommand(value)) return undefined;
	const command: CodeActionCommand = { title: value.title, command: value.command };
	// Record arguments only when they are the protocol's array shape; a
	// malformed field makes the whole command unusable, not partially usable.
	if (value.arguments !== undefined && !Array.isArray(value.arguments)) return undefined;
	if (Array.isArray(value.arguments)) command.arguments = value.arguments;
	return command;
}

/**
 * Defensive narrowing of an untrusted intelligence result (plan §18 commands).
 * Never throws; anything unrecognized yields `{ actions: [] }`. Actions that
 * carry neither a valid edit payload nor a valid command are dropped; actions
 * with a command are returned as plain data and are NEVER executed here.
 */
export function codeActionsFromPayload(payload: unknown): CodeActionPayload {
	const actions: CodeActionPayload["actions"] = [];
	const rawActions = isRecord(payload) && Array.isArray(payload.actions) ? payload.actions : [];
	for (const raw of rawActions) {
		if (!isRecord(raw) || typeof raw.title !== "string") continue;
		const edits = isWorkspaceEditPayload(raw.edits) ? raw.edits : undefined;
		const command = narrowCommand(raw.command);
		if (!edits && !command) continue;
		const action: CodeActionPayload["actions"][number] = { title: raw.title };
		if (typeof raw.kind === "string") action.kind = raw.kind;
		if (edits) action.edits = edits;
		if (command) action.command = command;
		actions.push(action);
	}
	return { actions };
}
