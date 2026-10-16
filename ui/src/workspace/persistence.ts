// Versioned workspace UI persistence (plan §21): pure serialize/parse helpers
// over the frozen `WorkspacePersistedState` plus a bounded adapter for the
// single config key `workspace_ui_state`.
//
// Security/privacy contract:
// - Only the whitelisted, non-sensitive fields already in the frozen type are
//   ever serialized: layout mode/ratio, explorer state, open non-sensitive
//   file URIs and cursor offsets. Document text, buffer contents, secrets and
//   unknown fields are never copied into the output, even when hostile input
//   carries them.
// - Parsing is total: corrupt, wrong-shaped or unknown-version data yields
//   `undefined` (callers apply `defaultWorkspacePersistedState()`); it never
//   throws and never blocks chat. The adapter reports a `migrated` flag when
//   unsupported stored data was replaced with safe defaults.
// - The document is bounded: at most ~100 roots, LRU by last save, and a
//   ~64 KiB backend bound on the whole config value (see core/config.py).
//   A store that rejects an oversized write surfaces that rejection; callers
//   treat persistence as best-effort and never block chat on it.
//
// Do NOT auto-restore tabs here. Restoring open URIs requires workspace/path
// revalidation first (plan §21); this module only round-trips the data.

import { clampSplitRatio } from "./layout.ts";
import {
	WORKSPACE_STATE_VERSION,
	defaultWorkspacePersistedState,
	type WorkspacePersistedState,
} from "./state.ts";

/** Outer document version (distinct from the per-root state version). */
export const PERSISTENCE_DOCUMENT_VERSION = 1 as const;
/** Bounded root retention, LRU by last save. */
export const MAX_PERSISTED_ROOTS = 100;
/** Backend byte bound for the whole stored document (`WORKSPACE_UI_STATE_MAX_BYTES` in core/config.py). */
export const MAX_PERSISTED_DOCUMENT_BYTES = 64 * 1024;
/** Headroom kept under the backend bound so a near-full save is never rejected for size. */
const PERSISTED_DOCUMENT_SAFETY_MARGIN_BYTES = 1024;
/** Bounded URI/cursor retention inside one root's state. */
export const MAX_PERSISTED_URIS = 100;
/** Longest URI string accepted in a persisted state. */
export const MAX_URI_CHARS = 4096;

export interface ParsedWorkspaceState {
	state: WorkspacePersistedState;
	/** True when unsupported stored data was replaced with safe defaults. */
	migrated: boolean;
}

/** The controller binds this to `config.get`/`config.set` for `workspace_ui_state`. */
export interface WorkspacePersistenceStore {
	get(): Promise<string | undefined>;
	set(value: string): Promise<void>;
}

interface PersistenceDocument {
	version: 1;
	byRoot: Record<string, unknown>;
}

export interface CursorPosition {
	offset: number;
	anchorOffset?: number;
	scrollLine?: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function sanitizeUri(value: unknown): string | undefined {
	if (typeof value !== "string" || value.length === 0 || value.length > MAX_URI_CHARS) return undefined;
	return value;
}

function sanitizeUriList(value: unknown): string[] {
	if (!Array.isArray(value)) return [];
	const seen = new Set<string>();
	for (const candidate of value) {
		const uri = sanitizeUri(candidate);
		if (uri === undefined) continue;
		seen.add(uri);
		if (seen.size >= MAX_PERSISTED_URIS) break;
	}
	return [...seen];
}

function sanitizeCursor(value: unknown): CursorPosition | undefined {
	if (!isRecord(value)) return undefined;
	const offset = finiteNumber(value.offset);
	if (offset === undefined || offset < 0) return undefined;
	const cursor: CursorPosition = { offset: Math.floor(offset) };
	const anchor = finiteNumber(value.anchorOffset);
	if (anchor !== undefined && anchor >= 0) cursor.anchorOffset = Math.floor(anchor);
	const scroll = finiteNumber(value.scrollLine);
	if (scroll !== undefined && scroll >= 0) cursor.scrollLine = Math.floor(scroll);
	return cursor;
}

function sanitizeCursors(value: unknown): Record<string, CursorPosition> | undefined {
	if (!isRecord(value)) return undefined;
	const out: Record<string, CursorPosition> = {};
	for (const [uri, candidate] of Object.entries(value)) {
		const cleanUri = sanitizeUri(uri);
		if (cleanUri === undefined) continue;
		const cursor = sanitizeCursor(candidate);
		if (!cursor) continue;
		out[cleanUri] = cursor;
		if (Object.keys(out).length >= MAX_PERSISTED_URIS) break;
	}
	return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Build a fresh, whitelisted state from untrusted input. Returns undefined
 * unless the value is an object at the current version; every field falls
 * back to its default instead of propagating junk, and unknown keys are
 * dropped (never copied).
 *
 * `explorerRequested` defaults to true when the field is absent (fresh states
 * and older stored documents open the explorer); an explicitly stored boolean
 * is honored as-is.
 */
function sanitizeState(value: unknown): WorkspacePersistedState | undefined {
	if (!isRecord(value) || value.version !== WORKSPACE_STATE_VERSION) return undefined;
	const requestedMode = value.requestedMode;
	const state: WorkspacePersistedState = {
		version: WORKSPACE_STATE_VERSION,
		requestedMode: requestedMode === "chat" || requestedMode === "editor" ? requestedMode : "split",
		splitRatio: clampSplitRatio(finiteNumber(value.splitRatio) ?? Number.NaN),
		explorerRequested: value.explorerRequested === undefined ? true : value.explorerRequested === true,
		openUris: sanitizeUriList(value.openUris),
	};
	if (value.chatSide === "left" || value.chatSide === "right") state.chatSide = value.chatSide;
	const width = finiteNumber(value.explorerWidth);
	if (width !== undefined && width > 0) state.explorerWidth = Math.floor(width);
	const activeUri = sanitizeUri(value.activeUri);
	if (activeUri !== undefined) state.activeUri = activeUri;
	const cursors = sanitizeCursors(value.cursorByUri);
	if (cursors !== undefined) state.cursorByUri = cursors;
	return state;
}

/**
 * Serialize to the exact whitelisted shape. Throws only on a wrong state
 * version (caller bug); invalid individual fields fall back to defaults and
 * unknown keys are skipped.
 */
export function serializeWorkspaceState(state: WorkspacePersistedState): string {
	const sanitized = sanitizeState(state);
	if (!sanitized) {
		throw new Error(
			`unsupported workspace state version: ${String(
				isRecord(state) ? state.version : state,
			)}`,
		);
	}
	return JSON.stringify(sanitized);
}

/**
 * Parse one persisted state. Corrupt JSON, non-objects and unknown versions
 * return undefined; callers apply safe defaults and never surface an error.
 */
export function parseWorkspaceState(raw: string | undefined): ParsedWorkspaceState | undefined {
	if (typeof raw !== "string" || raw.trim() === "") return undefined;
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return undefined;
	}
	const state = sanitizeState(value);
	if (!state) return undefined;
	// Version 1 is the only supported version today; a future supported
	// version would upgrade here and set `migrated: true`.
	return { state, migrated: false };
}

type DocumentRead =
	| { kind: "document"; document: PersistenceDocument }
	| { kind: "unknown-version" }
	| { kind: "absent" };

function parseDocument(raw: string): DocumentRead {
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return { kind: "absent" };
	}
	if (!isRecord(value)) return { kind: "absent" };
	if (value.version !== PERSISTENCE_DOCUMENT_VERSION) {
		// A numeric-but-different version is a real future document (migration
		// path); a missing/wrong-typed version is corrupt, not future.
		return typeof value.version === "number" ? { kind: "unknown-version" } : { kind: "absent" };
	}
	if (!isRecord(value.byRoot)) return { kind: "absent" };
	return {
		kind: "document",
		document: { version: PERSISTENCE_DOCUMENT_VERSION, byRoot: { ...value.byRoot } },
	};
}

const utf8Encoder = new TextEncoder();

/** UTF-8 byte length of the exact document serialization about to be written. */
function documentByteLength(byRoot: Record<string, unknown>): number {
	return utf8Encoder.encode(JSON.stringify({ version: PERSISTENCE_DOCUMENT_VERSION, byRoot })).length;
}

/**
 * Bounded adapter over one injected string store. One JSON document holds a
 * per-root map: `{ version: 1, byRoot: { [resolvedRoot]: state } }`. Saves
 * re-insert the touched root last, so eviction drops the least recently saved
 * root first until the document fits both the root count cap and the backend
 * byte bound. Insertion order is meaningful for the non-integer string keys
 * resolved absolute paths produce.
 */
export class WorkspacePersistence {
	private readonly store: WorkspacePersistenceStore;

	constructor(store: WorkspacePersistenceStore) {
		this.store = store;
	}

	/**
	 * Read the stored state for one root. No entry -> undefined; corrupt
	 * stored text -> undefined (nothing to restore, chat unaffected);
	 * unknown document/entry versions -> safe defaults with `migrated: true`.
	 */
	async load(rootKey: string): Promise<ParsedWorkspaceState | undefined> {
		const raw = await this.read();
		if (raw === undefined) return undefined;
		const read = parseDocument(raw);
		if (read.kind === "unknown-version") {
			return { state: defaultWorkspacePersistedState(), migrated: true };
		}
		if (read.kind !== "document") return undefined;
		const entry = read.document.byRoot[rootKey];
		if (entry === undefined) return undefined;
		const state = sanitizeState(entry);
		if (!state) return { state: defaultWorkspacePersistedState(), migrated: true };
		return { state, migrated: false };
	}

	/**
	 * Replace one root's state inside the document and write it back. A
	 * corrupt or unknown stored document is rebuilt from scratch (recovery,
	 * never a throw); the write itself may reject and the caller decides.
	 */
	async save(rootKey: string, state: WorkspacePersistedState): Promise<void> {
		const serialized = JSON.parse(serializeWorkspaceState(state)) as unknown;
		const document = await this.readForWrite();
		const byRoot = document.byRoot;
		delete byRoot[rootKey];
		byRoot[rootKey] = serialized;
		this.evictToFit(byRoot, rootKey);
		await this.store.set(
			JSON.stringify({ version: PERSISTENCE_DOCUMENT_VERSION, byRoot }),
		);
	}

	/**
	 * Evict least-recently-saved roots until the document fits both the root
	 * count cap and the backend byte bound (minus a safety margin). The root
	 * being saved is kept even when it alone is oversized: that write is still
	 * attempted, and the backend rejection surfaces to the caller.
	 */
	private evictToFit(byRoot: Record<string, unknown>, keepRootKey: string): void {
		for (;;) {
			const keys = Object.keys(byRoot);
			const overCount = keys.length > MAX_PERSISTED_ROOTS;
			const overBytes =
				documentByteLength(byRoot) > MAX_PERSISTED_DOCUMENT_BYTES - PERSISTED_DOCUMENT_SAFETY_MARGIN_BYTES;
			if (!overCount && !overBytes) return;
			const oldest = keys.find((key) => key !== keepRootKey);
			if (oldest === undefined) return;
			delete byRoot[oldest];
		}
	}

	/** Best-effort read: a throwing store read is "nothing stored". */
	private async read(): Promise<string | undefined> {
		try {
			const raw = await this.store.get();
			return typeof raw === "string" ? raw : undefined;
		} catch {
			return undefined;
		}
	}

	/** Read the current document, sanitizing every root entry; junk rebuilds fresh. */
	private async readForWrite(): Promise<PersistenceDocument> {
		const document: PersistenceDocument = {
			version: PERSISTENCE_DOCUMENT_VERSION,
			byRoot: {},
		};
		const raw = await this.read();
		if (raw === undefined) return document;
		const read = parseDocument(raw);
		if (read.kind !== "document") return document;
		for (const [key, value] of Object.entries(read.document.byRoot)) {
			const state = sanitizeState(value);
			if (state) document.byRoot[key] = state;
		}
		return document;
	}
}
