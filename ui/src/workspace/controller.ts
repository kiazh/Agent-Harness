// Workspace controller (T3, plan §7/§8/§19/§21): the orchestration hub between
// the editor model (`EditorStore`/`TabModel`), the workspace shell and the
// `workspace.*` RPC family.
//
// Contract:
// - Orchestration only: no rendering, no component imports. Every side effect
//   leaves through an injected callback (`requestRender`, `notify`, `focus`)
//   or an injected adapter (`gateway`, `persistence`), so tests drive the
//   whole lifecycle with fakes and a real editor store.
// - Chat continuity: no public method throws. Gateway failures surface through
//   `notify` and typed outcomes; the controller stays usable.
// - One binding per open workspace: `openWorkspace` mints a server identity
//   (`workspaceId` + `epoch`); workspace events whose identity does not match
//   are dropped (epoch check), and lazily-created mirror state never outlives
//   its document.
// - Edit sync is debounced per document (trailing, coalesced). Small runs of
//   edits are sent as an incremental chain; version gaps and long runs fall
//   back to a single range replacement computed against the last acknowledged
//   text (kind-agnostic: always a valid LSP change, never a fabricated one).
//   `resync: true` from the backend triggers a full snapshot at the current
//   version.
// - Save is a compare-and-swap handshake with the store: dirty clears only
//   when the store says the acknowledged text is still current; conflicts
//   become explicit descriptors the UI resolves (keep/reload/overwrite).

import type { DocumentRecord, EditorStore, SaveToken } from "../editor/model.ts";
import { offsetToPosition } from "../editor/positions.ts";
import type { TabModel } from "../editor/tabs.ts";
import type {
	LspPosition,
	LspRange,
	WorkspaceDiagnostic,
	WorkspaceDocumentChange,
	WorkspaceEvent,
	WorkspaceIntelligenceResult,
	WorkspaceLspStatusResult,
	WorkspaceOpenResult,
	WorkspaceReadResult,
	WorkspaceSaveConflict,
	WorkspaceServerState,
} from "./protocol.ts";
import {
	isWorkspaceRefusal,
	type WorkspaceGateway,
	type WorkspaceIntelligenceOperation,
	type WorkspaceLspAction,
} from "./gateway.ts";
import { adjustSplitRatio, clampSplitRatio } from "./layout.ts";
import {
	WORKSPACE_STATE_VERSION,
	type ChatSide,
	type FocusRegion,
	type LayoutInput,
	type PanePreference,
	type WorkspaceMode,
	type WorkspacePersistedState,
} from "./state.ts";

// ─── injected dependencies ──────────────────────────────────────────────────

/** Persistence seam (the app binds `WorkspacePersistence` and unwraps `.state`). */
export interface WorkspacePersistenceLike {
	load(rootKey: string): Promise<WorkspacePersistedState | undefined>;
	save(rootKey: string, state: WorkspacePersistedState): Promise<void>;
}

export type NoticeKind = "info" | "warning" | "error";

export interface WorkspaceControllerOptions {
	gateway: WorkspaceGateway;
	persistence: WorkspacePersistenceLike;
	store: EditorStore;
	tabs: TabModel;
	requestRender(): void;
	notify(kind: NoticeKind, text: string): void;
	/** Ask the UI to move focus to a region (called after an open succeeds). */
	focus(region: FocusRegion): void;
	/** Per-document edit-sync debounce; tests shrink it. Default 150 ms. */
	editDebounceMs?: number;
	/** Trailing persistence debounce; tests shrink it. Default 250 ms. */
	persistDebounceMs?: number;
}

// ─── public outcome and view types ──────────────────────────────────────────

export type WorkspacePhase = "closed" | "opening" | "ready" | "closing";

export interface OpenUriOptions {
	/** Explorer single-click selection: may take over the single preview slot. */
	preview?: boolean;
	/** Suppress per-file notices (reopenRecent emits one summary instead). */
	quiet?: boolean;
}

export type OpenUriOutcome =
	| { status: "opened"; documentId: string; uri: string }
	| { status: "focused"; documentId: string; uri: string }
	| { status: "refused"; uri: string; reason: string; message: string }
	| { status: "failed"; uri: string; message: string }
	| { status: "no-workspace"; uri: string };

export type DirtyCloseChoice = "save" | "discard" | "cancel";

/** Structured dirty-close decision the caller resolves via `resolveClose`. */
export interface DirtyCloseDecision {
	kind: "dirty-close";
	documentId: string;
	choices: readonly ["save", "discard", "cancel"];
}

export type CloseOutcome =
	| { status: "closed"; documentId: string; activeDocumentId: string | null }
	| { status: "needs-decision"; decision: DirtyCloseDecision }
	| { status: "cancelled"; documentId: string }
	| { status: "save-conflict"; conflict: SaveConflictDescriptor }
	| { status: "refused"; documentId: string; reason: string; message: string }
	| { status: "failed"; documentId: string; message: string }
	| { status: "not-open"; documentId: string };

export interface SaveConflictDescriptor {
	documentId: string;
	uri: string;
	diskRevision: string;
	baseRevision: string;
	reason: string;
}

export type SaveOutcome =
	| { status: "saved"; documentId: string; revision: string | undefined }
	| { status: "conflict"; conflict: SaveConflictDescriptor }
	| { status: "refused"; documentId: string; reason: string; message: string }
	| { status: "failed"; documentId: string; message: string }
	| { status: "not-found"; documentId: string };

export type ConflictResolution =
	| { status: "kept"; documentId: string }
	| { status: "reloaded"; documentId: string; revision: string }
	| { status: "saved"; documentId: string; revision: string | undefined }
	| { status: "conflict"; conflict: SaveConflictDescriptor }
	| { status: "refused"; documentId: string; reason: string; message: string }
	| { status: "failed"; documentId: string; message: string }
	| { status: "no-conflict"; documentId: string }
	| { status: "not-found"; documentId: string };

export type IntelligenceOutcome =
	| { status: "ok"; result: WorkspaceIntelligenceResult }
	| { status: "superseded" }
	| { status: "refused"; reason: string; message: string }
	| { status: "failed"; message: string }
	| { status: "no-workspace" };

/** Latest producer set for one URI (a producer replaces its previous set). */
export interface WorkspaceDiagnosticsProducer {
	source: string;
	version: number | null;
	serverGeneration: number;
	diagnostics: WorkspaceDiagnostic[];
}

export interface WorkspaceDiagnosticsView {
	uri: string;
	producers: WorkspaceDiagnosticsProducer[];
	/** Flattened latest sets, producer order. */
	diagnostics: WorkspaceDiagnostic[];
	/** Diagnostics currently retained for this URI. */
	total: number;
	/** Diagnostics dropped for this URI by the global retention cap. */
	omitted: number;
}

export interface WorkspaceLspStatusSnapshot {
	servers: WorkspaceLspStatusResult["servers"];
	/** Latest event-derived state per profile (may be newer than the last poll). */
	profiles: Record<string, { state: WorkspaceServerState; generation: number; detail?: string }>;
}

export type LspControlOutcome =
	| { status: "ok"; profile: string; action: WorkspaceLspAction; state: WorkspaceServerState; detail?: string }
	| { status: "refused"; reason: string; message: string }
	| { status: "failed"; message: string }
	| { status: "no-workspace" };

export interface ExternalChangeNotice {
	documentId: string;
	uri: string;
	revision: string;
}

export type LspStatusListener = (snapshot: WorkspaceLspStatusSnapshot) => void;

// ─── constants ──────────────────────────────────────────────────────────────

export const DEFAULT_EDIT_DEBOUNCE_MS = 150;
export const DEFAULT_PERSIST_DEBOUNCE_MS = 250;
/** More pending edits than this in one flush window send a full replacement. */
export const WORKSPACE_EDIT_COALESCE_LIMIT = 4;
/** Open URIs retained in one persisted state (plan §21 bound). */
export const WORKSPACE_PERSIST_OPEN_URI_LIMIT = 20;
/** Files reopenRecent attempts (plan §21: revalidate, never auto-restore all). */
export const WORKSPACE_REOPEN_LIMIT = 8;
/** Global retained diagnostics cap; overflow drops the oldest producer sets. */
export const WORKSPACE_DIAGNOSTICS_CAP = 1000;
/** Bounded best-effort window for close/dispose RPCs. */
export const WORKSPACE_DISPOSE_TIMEOUT_MS = 2000;

// ─── pure helpers ───────────────────────────────────────────────────────────

function messageOf(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/** Pane preference implied by a focus region; modal keeps the previous pane. */
export function preferredPaneForRegion(region: FocusRegion): PanePreference | undefined {
	switch (region) {
		case "chat-composer":
		case "transcript":
			return "chat";
		case "editor":
		case "explorer":
		case "tabs":
		case "problems":
			return "editor";
		case "modal":
			return undefined;
	}
}

/** One-line display name for notices (basename of a scheme-stripped URI). */
function displayName(uri: string): string {
	let path = uri;
	const scheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.exec(path);
	if (scheme) path = path.slice(scheme[0].length);
	try {
		path = decodeURIComponent(path);
	} catch {
		// Malformed percent-encoding: keep the raw path.
	}
	const parts = path.split("/").filter((part) => part.length > 0);
	return parts[parts.length - 1] ?? uri;
}

function isHighSurrogate(code: number): boolean {
	return code >= 0xd800 && code <= 0xdbff;
}

function isLowSurrogate(code: number): boolean {
	return code >= 0xdc00 && code <= 0xdfff;
}

/** Move a start boundary left out of a surrogate pair. */
function snapStart(text: string, offset: number): number {
	if (offset > 0 && offset < text.length && isLowSurrogate(text.charCodeAt(offset)) && isHighSurrogate(text.charCodeAt(offset - 1))) {
		return offset - 1;
	}
	return offset;
}

/** Move an end boundary right out of a surrogate pair. */
function snapEnd(text: string, offset: number): number {
	if (offset > 0 && offset < text.length && isLowSurrogate(text.charCodeAt(offset)) && isHighSurrogate(text.charCodeAt(offset - 1))) {
		return offset + 1;
	}
	return offset;
}

/**
 * Minimal single-range replacement turning `before` into `after` (common
 * prefix/suffix trimmed, boundaries snapped to code-point edges). Always a
 * valid LSP change against `before`, including the no-op case (0..0, "").
 */
export function replacementChange(before: string, after: string): { range: LspRange; text: string } {
	const maxStart = Math.min(before.length, after.length);
	let start = 0;
	while (start < maxStart && before.charCodeAt(start) === after.charCodeAt(start)) start += 1;
	let endBefore = before.length;
	let endAfter = after.length;
	while (endBefore > start && endAfter > start && before.charCodeAt(endBefore - 1) === after.charCodeAt(endAfter - 1)) {
		endBefore -= 1;
		endAfter -= 1;
	}
	const startOffset = snapStart(before, start);
	const endOffset = snapEnd(before, endBefore);
	// Compose: before[0, startOffset) + text + before[endOffset,) === after.
	// A start snap keeps a matching surrogate that also exists in `after`
	// (the prefix matched), so the inserted slice starts at startOffset; an
	// end snap re-inserts the matching surrogate the extended span removes.
	const afterStart = startOffset;
	const afterEnd = endAfter + (endOffset - endBefore);
	return {
		range: {
			start: offsetToPosition(before, startOffset),
			end: offsetToPosition(before, endOffset),
		},
		text: after.slice(afterStart, afterEnd),
	};
}

function cloneChange(change: { range: LspRange; text: string }): { range: LspRange; text: string } {
	return {
		range: { start: { ...change.range.start }, end: { ...change.range.end } },
		text: change.text,
	};
}

// ─── internal state shapes ──────────────────────────────────────────────────

type SyncChange = WorkspaceDocumentChange["changes"][number];

interface DocumentSyncState {
	uri: string;
	/** Version the backend mirror is known to hold; -1 = no accepted open yet. */
	ackedVersion: number;
	/** Text at `ackedVersion`; null when the mirror text is unknown. */
	ackedText: string | null;
	/** Document version at the last collected edit (chain base). */
	observedVersion: number;
	observedText: string;
	/** Incremental edits since the last flush, each relative to its predecessor. */
	pending: SyncChange[];
	/** True when the version counter jumped (reloads, missed edits). */
	gap: boolean;
	inFlight: boolean;
	queued: boolean;
	timer: ReturnType<typeof setTimeout> | undefined;
	/** Last refusal reason; repeated identical refusals are not re-announced. */
	lastRefusal?: string;
}

interface DiagnosticsProducerSet {
	source: string;
	version: number | null;
	serverGeneration: number;
	diagnostics: WorkspaceDiagnostic[];
	updatedAt: number;
}

interface DiagnosticsEntry {
	bySource: Map<string, DiagnosticsProducerSet>;
	/** Flattened cache, invalidated on every producer-set replacement. */
	flat: WorkspaceDiagnostic[] | undefined;
}

// ─── controller ─────────────────────────────────────────────────────────────

export class WorkspaceController {
	private readonly gateway: WorkspaceGateway;
	private readonly persistence: WorkspacePersistenceLike;
	private readonly store: EditorStore;
	private readonly tabs: TabModel;
	private readonly requestRenderCallback: () => void;
	private readonly notifyCallback: (kind: NoticeKind, text: string) => void;
	private readonly focusCallback: (region: FocusRegion) => void;
	private readonly editDebounceMs: number;
	private readonly persistDebounceMs: number;

	private phaseValue: WorkspacePhase = "closed";
	private currentIdentity: WorkspaceOpenResult | undefined;
	private rootKeyValue: string | undefined;
	private disposed = false;

	// Layout intent (plan §7/§8).
	private requestedMode: WorkspaceMode = "split";
	/** Mode a chat maximize restores to; only "split" | "editor" are non-chat. */
	private restoreMode: "split" | "editor" = "split";
	private splitRatioValue = 0.5;
	private explorerRequested = true;
	private preferredPane: PanePreference = "chat";
	private chatSideValue: ChatSide = "left";

	// Per-document mirror sync.
	private readonly sync = new Map<string, DocumentSyncState>();

	// Save conflicts awaiting an explicit decision.
	private readonly conflicts = new Map<string, SaveConflictDescriptor>();
	/** Documents with a save request in flight (their own fileChanged echo). */
	private readonly savesInFlight = new Set<string>();
	// Soft external-change notices for clean buffers (never auto-reloaded).
	private readonly externalNotices = new Map<string, ExternalChangeNotice>();

	// Diagnostics store (bounded).
	private readonly diagnosticsByUri = new Map<string, DiagnosticsEntry>();
	private readonly diagnosticsOmittedByUri = new Map<string, number>();
	private diagnosticsRetained = 0;
	private diagnosticsClock = 0;

	// LSP status cache.
	private lspServers: WorkspaceLspStatusResult["servers"] = [];
	private readonly lspProfiles = new Map<
		string,
		{ state: WorkspaceServerState; generation: number; detail?: string }
	>();
	private lspStatusListener: LspStatusListener | undefined;

	// Intelligence supersede bookkeeping (completion/hover).
	private supersedeSeq = 0;
	private readonly superseding = new Map<string, number>();

	// Timers.
	private persistTimer: ReturnType<typeof setTimeout> | undefined;
	private persistFailureNotified = false;

	constructor(options: WorkspaceControllerOptions) {
		this.gateway = options.gateway;
		this.persistence = options.persistence;
		this.store = options.store;
		this.tabs = options.tabs;
		this.requestRenderCallback = options.requestRender;
		this.notifyCallback = options.notify;
		this.focusCallback = options.focus;
		this.editDebounceMs = options.editDebounceMs ?? DEFAULT_EDIT_DEBOUNCE_MS;
		this.persistDebounceMs = options.persistDebounceMs ?? DEFAULT_PERSIST_DEBOUNCE_MS;
	}

	// ─── lifecycle ──────────────────────────────────────────────────────────

	get phase(): WorkspacePhase {
		return this.phaseValue;
	}

	get identity(): WorkspaceOpenResult | undefined {
		return this.currentIdentity;
	}

	get rootKey(): string | undefined {
		return this.rootKeyValue;
	}

	/**
	 * Open a workspace root with an explicit trust decision (default true: the
	 * app only calls this after the user chose the root). Any previously open
	 * workspace is closed first. Failures notify and leave the controller
	 * closed but usable (chat is unaffected).
	 */
	async openWorkspace(
		root: string,
		options: { trust?: boolean } = {},
	): Promise<WorkspaceOpenResult | undefined> {
		if (this.disposed) return undefined;
		if (this.phaseValue !== "closed") await this.closeWorkspace();
		const trimmed = typeof root === "string" ? root.trim() : "";
		if (trimmed.length === 0) {
			this.notifySafe("warning", "a workspace root is required");
			return undefined;
		}
		this.phaseValue = "opening";
		let identity: WorkspaceOpenResult;
		try {
			identity = await this.gateway.open(trimmed, options.trust ?? true);
		} catch (error) {
			this.phaseValue = "closed";
			this.notifySafe("error", `could not open workspace ${trimmed}: ${messageOf(error)}`);
			return undefined;
		}
		this.currentIdentity = identity;
		this.rootKeyValue = identity.resolvedRoot;
		this.phaseValue = "ready";
		// Persist the requested mode/ratio under the resolved root immediately,
		// MERGING with any stored document: a default write here must never
		// clobber the previous session's open files before restore() reads them.
		await this.persistLayoutIntent();
		const trustNote = identity.trusted ? "" : " (untrusted: intelligence disabled)";
		this.notifySafe("info", `workspace open: ${identity.rootLabel}${trustNote}`);
		this.requestRenderSafe();
		return identity;
	}

	/** Retire the remote workspace; local buffers stay (the app decides). */
	async closeWorkspace(): Promise<void> {
		if (this.phaseValue === "closed") return;
		this.phaseValue = "closing";
		const closes: Array<Promise<unknown>> = [];
		for (const state of this.sync.values()) {
			this.clearSyncTimer(state);
			closes.push(this.bounded(this.tryDocumentClose(state.uri)));
		}
		await Promise.allSettled(closes);
		this.sync.clear();
		await this.bounded(this.tryGatewayClose());
		this.currentIdentity = undefined;
		this.rootKeyValue = undefined;
		this.clearPersistTimer();
		this.phaseValue = "closed";
		this.requestRenderSafe();
	}

	/**
	 * Terminal teardown: clear timers, close every open document mirror
	 * (best-effort, bounded) and close the workspace. Idempotent.
	 */
	async dispose(): Promise<void> {
		if (this.disposed) return;
		this.disposed = true;
		this.clearPersistTimer();
		const uris: string[] = [];
		for (const [documentId, state] of this.sync) {
			this.clearSyncTimer(state);
			uris.push(state.uri);
			this.sync.delete(documentId);
		}
		await Promise.allSettled(uris.map((uri) => this.bounded(this.tryDocumentClose(uri))));
		await this.bounded(this.tryGatewayClose());
		this.currentIdentity = undefined;
		this.phaseValue = "closed";
	}

	// ─── layout intent (plan §7/§8) ─────────────────────────────────────────

	/**
	 * Geometry input for `AppShell.setWorkspaceState`. `columns`/`rows` are
	 * placeholders (0): the shell overwrites them from the live terminal so
	 * callers never race a resize.
	 */
	getLayoutInput(): LayoutInput {
		return {
			columns: 0,
			rows: 0,
			requestedMode: this.requestedMode,
			splitRatio: clampSplitRatio(this.splitRatioValue),
			explorerRequested: this.explorerRequested,
			preferredPane: this.preferredPane,
			chatSide: this.chatSideValue,
		};
	}

	/** Requested mode (persisted intent, distinct from effective geometry). */
	get mode(): WorkspaceMode {
		return this.requestedMode;
	}

	setMode(mode: WorkspaceMode): void {
		if (mode === this.requestedMode) return;
		if (mode === "chat" && this.requestedMode !== "chat") {
			// A later toggleChatMaximize restores the mode we are leaving.
			this.restoreMode = this.requestedMode;
		}
		this.requestedMode = mode;
		this.afterIntentChange();
	}

	/** Chat maximize toggle: remember the previous non-chat mode for restore. */
	toggleChatMaximize(): void {
		this.setMode(this.requestedMode === "chat" ? this.restoreMode : "chat");
	}

	/** Editor maximize toggle: editor <-> the remembered restore mode. */
	toggleEditorMaximize(): void {
		if (this.requestedMode === "editor") {
			// Un-maximize: restore the remembered non-chat mode. "editor" is not
			// a meaningful restore target here, so split is the fallback.
			this.setMode(this.restoreMode === "editor" ? "split" : this.restoreMode);
			return;
		}
		this.setMode("editor");
	}

	restoreSplit(): void {
		this.setMode("split");
	}

	adjustRatio(direction: 1 | -1): void {
		this.setRatio(adjustSplitRatio(this.splitRatioValue, direction));
	}

	setRatio(ratio: number): void {
		const next = clampSplitRatio(ratio);
		if (next === this.splitRatioValue) return;
		this.splitRatioValue = next;
		this.afterIntentChange();
	}

	toggleExplorer(): void {
		this.explorerRequested = !this.explorerRequested;
		this.afterIntentChange();
	}

	/**
	 * Place the chat pane on the given side of a split (persisted like the
	 * ratio). The explorer drawer always docks at the viewport's far right;
	 * with "right" it sits right of the chat column.
	 */
	setChatSide(side: ChatSide): void {
		if (side !== "left" && side !== "right") return;
		if (side === this.chatSideValue) return;
		this.chatSideValue = side;
		this.afterIntentChange();
	}

	/** Current chat placement ("left" unless an explicit swap was stored). */
	get chatSide(): ChatSide {
		return this.chatSideValue;
	}

	/**
	 * Observe the last meaningfully focused region (drives the narrow-terminal
	 * fallback pane). `modal` keeps the previous preference.
	 */
	noteFocus(region: FocusRegion): void {
		const pane = preferredPaneForRegion(region);
		if (pane === undefined || pane === this.preferredPane) return;
		this.preferredPane = pane;
		this.requestRenderSafe();
	}

	// ─── documents ──────────────────────────────────────────────────────────

	/**
	 * Open (or focus) a document. Structured read refusals notify and create
	 * no tab; oversize/binary/unsupported files are refused by the backend and
	 * never guessed. A read the backend marks read-only opens read-only. An
	 * explicit (non-preview) open pins an existing preview tab.
	 */
	async openUri(uri: string, options: OpenUriOptions = {}): Promise<OpenUriOutcome> {
		const identity = this.currentIdentity;
		if (this.phaseValue !== "ready" || !identity) {
			if (options.quiet !== true) this.notifySafe("warning", "open a workspace before opening files");
			return { status: "no-workspace", uri };
		}
		const existing = this.findDocumentByUri(uri);
		if (existing) {
			const focused = this.tabs.open(existing.id, { preview: options.preview ?? false });
			if (!focused.ok) return { status: "failed", uri, message: focused.error.message };
			if (options.preview !== true) this.tabs.pin(existing.id);
			this.schedulePersist();
			this.requestRenderSafe();
			this.focusEditor();
			return { status: "focused", documentId: existing.id, uri: existing.uri };
		}

		let read: WorkspaceReadResult | undefined;
		try {
			const result = await this.gateway.read(uri);
			if (isWorkspaceRefusal(result)) {
				if (options.quiet !== true) {
					this.notifySafe("warning", `${displayName(uri)}: ${result.refused.message}`);
				}
				return { status: "refused", uri, reason: result.refused.reason, message: result.refused.message };
			}
			read = result;
		} catch (error) {
			if (options.quiet !== true) this.notifySafe("error", `could not read ${displayName(uri)}: ${messageOf(error)}`);
			return { status: "failed", uri, message: messageOf(error) };
		}

		const opened = this.store.openDocument({
			workspaceId: identity.workspaceId,
			uri: read.uri,
			text: read.text,
			languageId: read.languageId,
			revision: read.revision,
			readOnly: read.readOnly,
		});
		if (!opened.ok) {
			if (options.quiet !== true) this.notifySafe("warning", `${displayName(read.uri)}: ${opened.error.message}`);
			return { status: "refused", uri: read.uri, reason: opened.error.code, message: opened.error.message };
		}
		const document = opened.document;
		const tab = this.tabs.open(document.id, { preview: options.preview ?? false });
		if (!tab.ok) {
			this.store.closeDocument(document.id);
			return { status: "failed", uri: read.uri, message: tab.error.message };
		}
		if (tab.replacedDocumentId) await this.evictReplaced(tab.replacedDocumentId);

		const state: DocumentSyncState = {
			uri: document.uri,
			ackedVersion: -1,
			ackedText: null,
			observedVersion: document.version,
			observedText: document.text,
			pending: [],
			gap: false,
			inFlight: false,
			queued: false,
			timer: undefined,
		};
		this.sync.set(document.id, state);
		await this.syncDocumentOpen(document, state, options.quiet === true);

		if (read.readOnly && options.quiet !== true) {
			this.notifySafe("warning", `${displayName(document.uri)} opened read-only`);
		}
		this.schedulePersist();
		this.requestRenderSafe();
		this.focusEditor();
		return { status: "opened", documentId: document.id, uri: document.uri };
	}

	/**
	 * Close a tab. A dirty tab returns a decision request (save/discard/cancel)
	 * and changes nothing; a clean tab closes its mirror and buffer. Callers
	 * resolve the decision through `resolveClose` (cancel is a no-op).
	 */
	async closeTab(documentId: string): Promise<CloseOutcome> {
		const record = this.store.getDocument(documentId);
		const closed = this.tabs.close(documentId);
		if (!closed.ok) {
			if (closed.needsDecision) {
				return {
					status: "needs-decision",
					decision: { kind: "dirty-close", documentId, choices: ["save", "discard", "cancel"] },
				};
			}
			return { status: "not-open", documentId };
		}
		await this.finishClose(documentId, record?.uri);
		return { status: "closed", documentId, activeDocumentId: closed.activeDocumentId };
	}

	/**
	 * Resolve a dirty-close decision. `cancel` is the absence of any further
	 * action (no-op); `discard` forces the close; `save` saves first and only
	 * closes when the buffer really became clean (edits typed during the save
	 * keep the tab open with a fresh decision).
	 */
	async resolveClose(documentId: string, choice: DirtyCloseChoice): Promise<CloseOutcome> {
		if (choice === "cancel") return { status: "cancelled", documentId };
		if (choice === "discard") {
			const record = this.store.getDocument(documentId);
			const closed = this.tabs.close(documentId, { force: true });
			if (!closed.ok) return { status: "not-open", documentId };
			await this.finishClose(documentId, record?.uri);
			return { status: "closed", documentId, activeDocumentId: closed.activeDocumentId };
		}
		const saved = await this.save(documentId);
		if (saved.status === "conflict") return { status: "save-conflict", conflict: saved.conflict };
		if (saved.status === "refused") return { status: "refused", documentId, reason: saved.reason, message: saved.message };
		if (saved.status === "failed") return { status: "failed", documentId, message: saved.message };
		if (saved.status === "not-found") return { status: "not-open", documentId };
		if (this.store.getDocument(documentId)?.dirty === true) {
			// The save committed but the buffer moved on: stay open and ask again.
			return {
				status: "needs-decision",
				decision: { kind: "dirty-close", documentId, choices: ["save", "discard", "cancel"] },
			};
		}
		const record = this.store.getDocument(documentId);
		const closed = this.tabs.close(documentId, { force: true });
		if (!closed.ok) return { status: "not-open", documentId };
		await this.finishClose(documentId, record?.uri);
		return { status: "closed", documentId, activeDocumentId: closed.activeDocumentId };
	}

	/**
	 * The pane calls this after every store change. Coalesces a trailing
	 * debounce per document, promotes a preview tab to persistent, and
	 * schedules the `documentChange` flush.
	 */
	noteEdit(documentId: string): void {
		const document = this.store.getDocument(documentId);
		if (!document) return;
		this.tabs.noteEdited(documentId);
		const state = this.ensureSyncState(document);
		const delta = document.version - state.observedVersion;
		if (delta === 0) {
			state.observedText = document.text;
		} else if (delta === 1) {
			state.pending.push(replacementChange(state.observedText, document.text));
			state.observedVersion = document.version;
			state.observedText = document.text;
		} else {
			state.gap = true;
			state.pending = [];
			state.observedVersion = document.version;
			state.observedText = document.text;
		}
		this.scheduleSyncFlush(documentId);
		this.requestRenderSafe();
	}

	/**
	 * Flush pending edit sync now (all documents when omitted). Useful before
	 * save/close and in tests instead of waiting for the debounce.
	 */
	async flushPending(documentId?: string): Promise<void> {
		if (documentId !== undefined) {
			await this.flushSync(documentId);
			return;
		}
		for (const id of [...this.sync.keys()]) await this.flushSync(id);
	}

	// ─── save and conflicts ─────────────────────────────────────────────────

	/**
	 * Compare-and-swap save: the token captures the exact text/version written;
	 * dirty clears only when the store confirms nothing moved underneath.
	 * Conflicts return an explicit descriptor for Compare/Reload/Save As/
	 * Overwrite; refusals notify with the structured reason.
	 */
	async save(documentId: string): Promise<SaveOutcome> {
		const document = this.store.getDocument(documentId);
		if (!document) return { status: "not-found", documentId };
		const begun = this.store.beginSave(documentId);
		if (!begun.ok) {
			this.notifySafe("warning", `could not save ${displayName(document.uri)}: ${begun.error.message}`);
			return { status: "refused", documentId, reason: begun.error.code, message: begun.error.message };
		}
		const token = begun.token;
		let result;
		this.savesInFlight.add(documentId);
		try {
			result = await this.gateway.save(document.uri, token.savedRevision, token.text, token.version);
		} catch (error) {
			this.notifySafe("error", `save failed for ${displayName(document.uri)}: ${messageOf(error)}`);
			return { status: "failed", documentId, message: messageOf(error) };
		} finally {
			this.savesInFlight.delete(documentId);
		}
		if (result.refused) {
			this.notifySafe("warning", `save refused for ${displayName(document.uri)}: ${result.refused.message}`);
			return { status: "refused", documentId, reason: result.refused.reason, message: result.refused.message };
		}
		if (result.conflict) {
			return this.recordSaveConflict(documentId, document.uri, result.conflict);
		}
		if (result.ok && typeof result.revision === "string") {
			this.acknowledgeSaved(documentId, token, result.revision);
			return { status: "saved", documentId, revision: result.revision };
		}
		const message = "the gateway reported a save without a revision";
		this.notifySafe("error", `${displayName(document.uri)}: ${message}`);
		return { status: "failed", documentId, message };
	}

	/**
	 * Resolve a recorded save conflict.
	 * - `keep`: keep the buffer (still dirty), dismiss the conflict.
	 * - `reload`: re-read from disk and adopt it (the buffer is replaced).
	 * - `overwrite-explicit`: re-save with the DISK revision as the expectation
	 *   (the user's explicit overwrite; the backend re-validates).
	 */
	async resolveConflict(
		documentId: string,
		choice: "keep" | "reload" | "overwrite-explicit",
	): Promise<ConflictResolution> {
		const document = this.store.getDocument(documentId);
		if (!document) return { status: "not-found", documentId };
		const conflict = this.conflicts.get(documentId);
		if (choice === "keep") {
			if (!conflict) return { status: "no-conflict", documentId };
			this.store.resolveConflictKeep(documentId);
			this.conflicts.delete(documentId);
			this.requestRenderSafe();
			return { status: "kept", documentId };
		}
		if (!conflict) return { status: "no-conflict", documentId };
		if (choice === "reload") return this.reloadConflict(documentId, document.uri, document.id);
		return this.overwriteConflict(documentId, document.uri, conflict);
	}

	/** Conflict descriptor currently tracked for a document, if any. */
	conflictFor(documentId: string): SaveConflictDescriptor | undefined {
		const conflict = this.conflicts.get(documentId);
		return conflict ? { ...conflict } : undefined;
	}

	// ─── workspace events ───────────────────────────────────────────────────

	/**
	 * Route one workspace event. Events from a different workspace identity or
	 * epoch are dropped. Diagnostics are retained per URI (latest producer set,
	 * replacement semantics) under a global cap; LSP states cache per profile;
	 * a fileChanged on a clean, non-authoritative-dirty buffer only raises a
	 * soft notice — nothing is auto-reloaded and a later save will conflict.
	 */
	handleEvent(event: WorkspaceEvent): void {
		const identity = this.currentIdentity;
		if (!identity || event.workspaceId !== identity.workspaceId || event.epoch !== identity.epoch) {
			return;
		}
		switch (event.type) {
			case "workspace.diagnostics":
				this.applyDiagnostics(event.uri, event.source, event.version, event.serverGeneration, event.diagnostics);
				this.requestRenderSafe();
				return;
			case "workspace.lspState":
				this.applyLspState(event.profile, event.state, event.generation, event.detail);
				this.requestRenderSafe();
				return;
			case "workspace.fileChanged":
				this.applyFileChanged(event.uri, event.revision, event.origin);
				this.requestRenderSafe();
				return;
			case "workspace.indexProgress":
				return;
		}
	}

	/** Latest retained diagnostics for a URI (empty view when none). */
	diagnosticsFor(uri: string): WorkspaceDiagnosticsView {
		const entry = this.diagnosticsByUri.get(uri);
		const omitted = this.diagnosticsOmittedByUri.get(uri) ?? 0;
		if (!entry) return { uri, producers: [], diagnostics: [], total: 0, omitted };
		if (!entry.flat) {
			entry.flat = [...entry.bySource.values()].flatMap((set) => set.diagnostics);
		}
		return {
			uri,
			producers: [...entry.bySource.values()].map((set) => ({
				source: set.source,
				version: set.version,
				serverGeneration: set.serverGeneration,
				diagnostics: set.diagnostics,
			})),
			diagnostics: entry.flat,
			total: entry.flat.length,
			omitted,
		};
	}

	/** Diagnostics dropped across all URIs by the retention cap so far. */
	diagnosticsOmitted(): number {
		let total = 0;
		for (const count of this.diagnosticsOmittedByUri.values()) total += count;
		return total;
	}

	lspStateFor(profile: string): { state: WorkspaceServerState; generation: number; detail?: string } | undefined {
		const entry = this.lspProfiles.get(profile);
		return entry ? { ...entry } : undefined;
	}

	externalChangeFor(documentId: string): ExternalChangeNotice | undefined {
		const notice = this.externalNotices.get(documentId);
		return notice ? { ...notice } : undefined;
	}

	/** Dismiss a soft external-change notice without adopting disk content. */
	dismissExternalChange(documentId: string): void {
		this.externalNotices.delete(documentId);
		this.store.clearPendingReload(documentId);
		this.requestRenderSafe();
	}

	// ─── intelligence and LSP ───────────────────────────────────────────────

	/**
	 * One intelligence request. Never throws: refusals, transport failures and
	 * a closed workspace all become typed outcomes. Completion/hover use
	 * supersede semantics — a response is dropped when a newer request for the
	 * same document arrived first.
	 */
	async request(
		method: WorkspaceIntelligenceOperation,
		uri: string | null,
		version: number | null,
		position?: LspPosition,
	): Promise<IntelligenceOutcome> {
		if (this.phaseValue !== "ready" || !this.currentIdentity) return { status: "no-workspace" };
		const document = uri === null ? undefined : this.findDocumentByUri(uri);
		const superseding = method === "completion" || method === "hover";
		const key = superseding && document ? `${document.id}\u0000${method}` : undefined;
		const seq = key === undefined ? 0 : ++this.supersedeSeq;
		if (key !== undefined) this.superseding.set(key, seq);
		const isStale = (): boolean => key !== undefined && this.superseding.get(key) !== seq;
		try {
			const result = await this.gateway.intelligence(method, document?.uri ?? uri, version, position ?? null);
			if (isStale()) return { status: "superseded" };
			if (isWorkspaceRefusal(result)) {
				return { status: "refused", reason: result.refused.reason, message: result.refused.message };
			}
			return { status: "ok", result };
		} catch (error) {
			if (isStale()) return { status: "superseded" };
			return { status: "failed", message: messageOf(error) };
		}
	}

	/** Poll `workspace.lspStatus`; caches the result and notifies the listener. */
	async lspStatus(): Promise<WorkspaceLspStatusResult | undefined> {
		if (this.phaseValue !== "ready" || !this.currentIdentity) return undefined;
		try {
			const result = await this.gateway.lspStatus();
			if (isWorkspaceRefusal(result)) {
				this.notifySafe("warning", `language-server status unavailable: ${result.refused.message}`);
				return undefined;
			}
			this.lspServers = result.servers;
			this.emitLspStatus();
			return result;
		} catch (error) {
			this.notifySafe("warning", `language-server status failed: ${messageOf(error)}`);
			return undefined;
		}
	}

	/** Cached status: last polled server rows + latest event-derived states. */
	lspStatusSnapshot(): WorkspaceLspStatusSnapshot {
		return { servers: this.lspServers, profiles: this.lspProfilesCopy() };
	}

	/** start/stop/restart passthrough; updates the status cache on success. */
	async lspControl(profile: string, action: WorkspaceLspAction): Promise<LspControlOutcome> {
		if (this.phaseValue !== "ready" || !this.currentIdentity) return { status: "no-workspace" };
		try {
			const result = await this.gateway.lspControl(profile, action);
			if (isWorkspaceRefusal(result)) {
				this.notifySafe("warning", `language-server ${action} refused: ${result.refused.message}`);
				return { status: "refused", reason: result.refused.reason, message: result.refused.message };
			}
			const entry: { state: WorkspaceServerState; generation: number; detail?: string } = {
				state: result.state,
				// The control result carries no generation; keep the event-derived
				// one rather than regressing to a fabricated zero.
				generation: this.lspProfiles.get(result.profile)?.generation ?? 0,
			};
			if (result.detail !== undefined) entry.detail = result.detail;
			this.lspProfiles.set(result.profile, entry);
			this.emitLspStatus();
			this.requestRenderSafe();
			const outcome: LspControlOutcome = {
				status: "ok",
				profile: result.profile,
				action,
				state: result.state,
			};
			if (result.detail !== undefined) outcome.detail = result.detail;
			return outcome;
		} catch (error) {
			this.notifySafe("error", `language-server ${action} failed: ${messageOf(error)}`);
			return { status: "failed", message: messageOf(error) };
		}
	}

	setLspStatusListener(listener: LspStatusListener | undefined): void {
		this.lspStatusListener = listener;
	}

	// ─── persistence ────────────────────────────────────────────────────────

	/**
	 * Build and save the persisted state for the current root (whitelisted
	 * fields only; open URIs bounded, cursors from the store's view states).
	 * Best-effort: failures notify once and never block editing or chat.
	 */
	async persist(): Promise<WorkspacePersistedState | undefined> {
		const rootKey = this.rootKeyValue;
		if (!rootKey) return undefined;
		const state = this.buildPersistedState();
		try {
			await this.persistence.save(rootKey, state);
			this.persistFailureNotified = false;
		} catch (error) {
			if (!this.persistFailureNotified) {
				this.persistFailureNotified = true;
				this.notifySafe("warning", `workspace state could not be saved: ${messageOf(error)}`);
			}
		}
		return state;
	}

	/**
	 * Persist only the layout intent, preserving any stored document list and
	 * cursors for this root. Used on open, before `restore()` reads the stored
	 * state: a full `persist()` here would replace the previous session's
	 * open-file metadata with the current (empty) tab set.
	 */
	private async persistLayoutIntent(): Promise<void> {
		const rootKey = this.rootKeyValue;
		if (!rootKey) return;
		try {
			const existing = await this.persistence.load(rootKey);
			const base = existing ?? this.buildPersistedState();
			await this.persistence.save(rootKey, {
				...base,
				requestedMode: this.requestedMode,
				splitRatio: clampSplitRatio(this.splitRatioValue),
				explorerRequested: this.explorerRequested,
				chatSide: this.chatSideValue,
			});
			this.persistFailureNotified = false;
		} catch (error) {
			if (!this.persistFailureNotified) {
				this.persistFailureNotified = true;
				this.notifySafe("warning", `workspace state could not be saved: ${messageOf(error)}`);
			}
		}
	}

	/** Load the stored state for a root; undefined when absent or unreadable. */
	async restore(rootKey: string): Promise<WorkspacePersistedState | undefined> {
		try {
			return await this.persistence.load(rootKey);
		} catch {
			return undefined;
		}
	}

	/** Adopt persisted layout intent (the app decides when); no document I/O. */
	applyRestoredLayout(state: WorkspacePersistedState): void {
		this.requestedMode = state.requestedMode;
		this.splitRatioValue = clampSplitRatio(state.splitRatio);
		this.explorerRequested = state.explorerRequested === true;
		// Absent/unknown sides fall back to the default, never to the previous
		// root's side (each stored document is self-contained).
		this.chatSideValue = state.chatSide === "right" ? "right" : "left";
		this.requestRenderSafe();
	}

	/**
	 * Attempt to reopen the remembered files (bounded to 8). The app decides
	 * whether to call this after `restore`; missing/unreadable files are
	 * skipped with a single summary notice. Layout intent is the app's call
	 * (`applyRestoredLayout`).
	 */
	async reopenRecent(state: WorkspacePersistedState): Promise<{ opened: number; skipped: number }> {
		const uris = Array.isArray(state.openUris) ? state.openUris.slice(0, WORKSPACE_REOPEN_LIMIT) : [];
		if (this.phaseValue !== "ready") return { opened: 0, skipped: uris.length };
		let opened = 0;
		let skipped = 0;
		for (const uri of uris) {
			const outcome = await this.openUri(uri, { quiet: true });
			if (outcome.status === "opened" || outcome.status === "focused") opened += 1;
			else skipped += 1;
		}
		if (typeof state.activeUri === "string") {
			const active = this.findDocumentByUri(state.activeUri);
			if (active) this.tabs.setActive(active.id);
		}
		if (skipped > 0) {
			this.notifySafe("info", `${opened} of ${uris.length} remembered file(s) reopened; ${skipped} unavailable`);
		} else if (opened > 0) {
			this.notifySafe("info", `reopened ${opened} file(s)`);
		}
		this.schedulePersist();
		this.requestRenderSafe();
		return { opened, skipped };
	}

	buildPersistedState(): WorkspacePersistedState {
		const openUris: string[] = [];
		const cursorByUri: NonNullable<WorkspacePersistedState["cursorByUri"]> = {};
		for (const tab of this.tabs.list()) {
			if (openUris.length >= WORKSPACE_PERSIST_OPEN_URI_LIMIT) break;
			const document = this.store.getDocument(tab.documentId);
			if (!document) continue;
			openUris.push(document.uri);
			const view = this.store.getViewState(document.id);
			if (!view) continue;
			const cursor: { offset: number; anchorOffset?: number; scrollLine?: number } = {
				offset: view.cursorOffset,
			};
			if (view.selectionAnchorOffset !== undefined) cursor.anchorOffset = view.selectionAnchorOffset;
			if (view.scrollLine > 0) cursor.scrollLine = view.scrollLine;
			cursorByUri[document.uri] = cursor;
		}
		const state: WorkspacePersistedState = {
			version: WORKSPACE_STATE_VERSION,
			requestedMode: this.requestedMode,
			splitRatio: clampSplitRatio(this.splitRatioValue),
			explorerRequested: this.explorerRequested,
			chatSide: this.chatSideValue,
			openUris,
		};
		const activeId = this.tabs.activeDocumentId();
		const active = activeId ? this.store.getDocument(activeId) : undefined;
		if (active) state.activeUri = active.uri;
		if (Object.keys(cursorByUri).length > 0) state.cursorByUri = cursorByUri;
		return state;
	}

	// ─── internals: layout ──────────────────────────────────────────────────

	private afterIntentChange(): void {
		this.schedulePersist();
		this.requestRenderSafe();
	}

	private focusEditor(): void {
		try {
			this.focusCallback("editor");
		} catch {
			// A UI focus callback must never break orchestration.
		}
	}

	// ─── internals: documents ───────────────────────────────────────────────

	private findDocumentByUri(uri: string): DocumentRecord | undefined {
		for (const document of this.store.documents()) {
			if (document.uri === uri) return document;
		}
		return undefined;
	}

	private async syncDocumentOpen(
		document: DocumentRecord,
		state: DocumentSyncState,
		quiet: boolean,
	): Promise<void> {
		try {
			const result = await this.gateway.documentOpen(
				document.uri,
				document.languageId,
				document.version,
				document.text,
			);
			if (this.sync.get(document.id) !== state) return;
			if (isWorkspaceRefusal(result)) {
				if (!quiet) {
					this.notifySafe("warning", `editor sync unavailable for ${displayName(document.uri)}: ${result.refused.message}`);
				}
				return;
			}
			state.ackedVersion = result.acceptedVersion;
			state.ackedText = result.acceptedVersion === document.version ? document.text : null;
			state.observedVersion = document.version;
			state.observedText = document.text;
		} catch (error) {
			if (!quiet) this.notifySafe("warning", `editor sync unavailable for ${displayName(document.uri)}: ${messageOf(error)}`);
		}
	}

	private async evictReplaced(documentId: string): Promise<void> {
		const state = this.sync.get(documentId);
		if (state) {
			this.clearSyncTimer(state);
			this.sync.delete(documentId);
		}
		const record = this.store.getDocument(documentId);
		this.store.closeDocument(documentId);
		if (record) await this.bounded(this.tryDocumentClose(record.uri));
		this.requestRenderSafe();
	}

	private async finishClose(documentId: string, uri: string | undefined): Promise<void> {
		const state = this.sync.get(documentId);
		if (state) {
			this.clearSyncTimer(state);
			this.sync.delete(documentId);
		}
		this.conflicts.delete(documentId);
		this.externalNotices.delete(documentId);
		if (uri !== undefined) await this.bounded(this.tryDocumentClose(uri));
		this.store.closeDocument(documentId);
		this.schedulePersist();
		this.requestRenderSafe();
	}

	private ensureSyncState(document: DocumentRecord): DocumentSyncState {
		let state = this.sync.get(document.id);
		if (!state) {
			// Lazily requested sync for a document the controller did not open
			// (misuse): first flush sends a full snapshot open.
			state = {
				uri: document.uri,
				ackedVersion: -1,
				ackedText: null,
				observedVersion: document.version,
				observedText: document.text,
				pending: [],
				gap: false,
				inFlight: false,
				queued: false,
				timer: undefined,
			};
			this.sync.set(document.id, state);
		}
		return state;
	}

	// ─── internals: edit sync ───────────────────────────────────────────────

	private scheduleSyncFlush(documentId: string): void {
		const state = this.sync.get(documentId);
		if (!state) return;
		this.clearSyncTimer(state);
		state.timer = setTimeout(() => {
			state.timer = undefined;
			void this.flushSync(documentId);
		}, this.editDebounceMs);
	}

	private clearSyncTimer(state: DocumentSyncState): void {
		if (state.timer !== undefined) {
			clearTimeout(state.timer);
			state.timer = undefined;
		}
	}

	/**
	 * Send the pending sync for one document. At most one request is in flight
	 * per document; results are ignored when the document's sync state was
	 * retired (tab closed) while the request was outstanding.
	 */
	private async flushSync(documentId: string): Promise<void> {
		const state = this.sync.get(documentId);
		const document = this.store.getDocument(documentId);
		if (!state || !document || this.disposed) return;
		this.clearSyncTimer(state);
		if (this.phaseValue !== "ready") return;
		if (state.inFlight) {
			state.queued = true;
			return;
		}
		if (document.version === state.ackedVersion && state.ackedText !== null) {
			state.pending = [];
			state.gap = false;
			return;
		}
		if (state.ackedText === null || state.ackedVersion < 0) {
			await this.sendSnapshot(documentId);
			return;
		}
		const base = state.ackedVersion;
		const chain =
			!state.gap &&
			state.pending.length > 0 &&
			state.pending.length <= WORKSPACE_EDIT_COALESCE_LIMIT &&
			base + state.pending.length === document.version;
		const changes = chain
			? state.pending.map(cloneChange)
			: [replacementChange(state.ackedText, document.text)];
		const pendingAtRequest = state.pending.length;
		const gapAtRequest = state.gap;
		const flushVersion = document.version;
		const flushText = document.text;
		state.inFlight = true;
		state.gap = false;
		let result;
		try {
			result = await this.gateway.documentChange(document.uri, {
				baseVersion: base,
				version: flushVersion,
				changes,
			});
		} catch (error) {
			if (this.sync.get(documentId) !== state) return;
			state.inFlight = false;
			state.gap = state.gap || gapAtRequest;
			this.notifySyncRefusal(state, "transport", messageOf(error));
			return;
		}
		if (this.sync.get(documentId) !== state) return;
		state.inFlight = false;
		if (isWorkspaceRefusal(result)) {
			state.gap = state.gap || gapAtRequest;
			this.notifySyncRefusal(state, result.refused.reason, result.refused.message);
			return;
		}
		if (result.resync) {
			// The backend demands a full snapshot: resend at the current version.
			state.ackedVersion = result.acceptedVersion;
			state.ackedText = null;
			state.pending = [];
			state.gap = false;
			state.observedVersion = document.version;
			state.observedText = document.text;
			this.clearSyncRefusal(state);
			await this.sendSnapshot(documentId);
			return;
		}
		if (result.acceptedVersion === flushVersion) {
			state.ackedVersion = flushVersion;
			state.ackedText = flushText;
			state.pending = state.pending.slice(pendingAtRequest);
			this.clearSyncRefusal(state);
		} else {
			// Accepted an older version without the resync flag: treat the
			// mirror text as unknown and re-establish it with a snapshot.
			state.ackedVersion = result.acceptedVersion;
			state.ackedText = null;
			state.pending = [];
			state.gap = false;
			await this.sendSnapshot(documentId);
			return;
		}
		if (state.queued) {
			state.queued = false;
			await this.flushSync(documentId);
		}
	}

	/**
	 * Re-establish the whole document at the current version: a strictly
	 * newer `documentOpen` replaces the mirror in one request; otherwise the
	 * mirror is dropped first so the version counter never regresses.
	 */
	private async sendSnapshot(documentId: string): Promise<void> {
		const state = this.sync.get(documentId);
		const document = this.store.getDocument(documentId);
		if (!state || !document || this.disposed || this.phaseValue !== "ready") return;
		if (state.inFlight) {
			state.queued = true;
			return;
		}
		state.inFlight = true;
		const version = document.version;
		const text = document.text;
		let result;
		try {
			if (state.ackedVersion >= 0 && version <= state.ackedVersion) {
				await this.tryDocumentClose(document.uri);
			}
			result = await this.gateway.documentOpen(document.uri, document.languageId, version, text);
		} catch (error) {
			if (this.sync.get(documentId) !== state) return;
			state.inFlight = false;
			this.notifySyncRefusal(state, "transport", messageOf(error));
			return;
		}
		if (this.sync.get(documentId) !== state) return;
		state.inFlight = false;
		if (isWorkspaceRefusal(result)) {
			this.notifySyncRefusal(state, result.refused.reason, result.refused.message);
			return;
		}
		state.ackedVersion = result.acceptedVersion;
		if (result.acceptedVersion === version) {
			state.ackedText = text;
			state.pending = [];
			state.gap = false;
			state.observedVersion = version;
			state.observedText = text;
			this.clearSyncRefusal(state);
		} else {
			// The mirror did not adopt the snapshot (e.g. it is ahead): the
			// next version bump retries; until then the mirror text is unknown.
			state.ackedText = null;
			this.scheduleSyncFlush(documentId);
		}
		if (state.queued) {
			state.queued = false;
			await this.flushSync(documentId);
		}
	}

	/** Announce a sync failure once per distinct reason (typing must not spam). */
	private notifySyncRefusal(state: DocumentSyncState, reason: string, message: string): void {
		if (state.lastRefusal === reason) return;
		state.lastRefusal = reason;
		this.notifySafe("warning", `editor sync for ${displayName(state.uri)}: ${message}`);
	}

	private clearSyncRefusal(state: DocumentSyncState): void {
		state.lastRefusal = undefined;
	}

	// ─── internals: save/conflict ───────────────────────────────────────────

	private recordSaveConflict(
		documentId: string,
		uri: string,
		conflict: WorkspaceSaveConflict,
	): SaveOutcome {
		const descriptor: SaveConflictDescriptor = {
			documentId,
			uri,
			diskRevision: conflict.diskRevision,
			baseRevision: conflict.baseRevision,
			reason: conflict.reason,
		};
		this.conflicts.set(documentId, descriptor);
		// Record the store's conflict marker for a dirty buffer (the marker the
		// editor pane renders). A clean buffer records a pending external change
		// instead; both stay explicit and neither auto-adopts disk text.
		this.store.noteExternalChange(documentId, conflict.diskRevision);
		this.notifySafe(
			"warning",
			`save conflict for ${displayName(uri)}: ${conflict.reason}; choose keep/reload/overwrite`,
		);
		this.requestRenderSafe();
		return { status: "conflict", conflict: descriptor };
	}

	private acknowledgeSaved(documentId: string, token: SaveToken, revision: string): void {
		this.store.acknowledgeSave(documentId, token, revision);
		const record = this.store.getDocument(documentId);
		if (record?.conflict) this.store.resolveConflictKeep(documentId);
		this.conflicts.delete(documentId);
		this.externalNotices.delete(documentId);
		this.requestRenderSafe();
	}

	private async reloadConflict(
		documentId: string,
		uri: string,
		recordId: string,
	): Promise<ConflictResolution> {
		let read: WorkspaceReadResult | undefined;
		try {
			const result = await this.gateway.read(uri);
			if (isWorkspaceRefusal(result)) {
				this.notifySafe("warning", `reload refused for ${displayName(uri)}: ${result.refused.message}`);
				return { status: "refused", documentId, reason: result.refused.reason, message: result.refused.message };
			}
			read = result;
		} catch (error) {
			this.notifySafe("error", `reload failed for ${displayName(uri)}: ${messageOf(error)}`);
			return { status: "failed", documentId, message: messageOf(error) };
		}
		const record = this.store.getDocument(recordId);
		if (!record) return { status: "not-found", documentId };
		const adopted = record.conflict
			? this.store.resolveConflictReload(documentId, read.text, read.revision)
			: this.store.setDiskState(documentId, read.text, read.revision);
		if (!adopted.ok) {
			this.notifySafe("warning", `could not reload ${displayName(uri)}: ${adopted.error.message}`);
			return { status: "refused", documentId, reason: adopted.error.code, message: adopted.error.message };
		}
		this.conflicts.delete(documentId);
		this.externalNotices.delete(documentId);
		this.resyncAfterDiskAdoption(documentId, adopted.document);
		this.requestRenderSafe();
		return { status: "reloaded", documentId, revision: read.revision };
	}

	private async overwriteConflict(
		documentId: string,
		uri: string,
		conflict: SaveConflictDescriptor,
	): Promise<ConflictResolution> {
		const begun = this.store.beginSave(documentId);
		if (!begun.ok) {
			return { status: "refused", documentId, reason: begun.error.code, message: begun.error.message };
		}
		const token = begun.token;
		let result;
		this.savesInFlight.add(documentId);
		try {
			// The DISK revision is the explicit expectation; "" means the user
			// accepted that the file currently does not exist on disk.
			result = await this.gateway.save(uri, conflict.diskRevision, token.text, token.version);
		} catch (error) {
			this.notifySafe("error", `overwrite failed for ${displayName(uri)}: ${messageOf(error)}`);
			return { status: "failed", documentId, message: messageOf(error) };
		} finally {
			this.savesInFlight.delete(documentId);
		}
		if (result.refused) {
			this.notifySafe("warning", `overwrite refused for ${displayName(uri)}: ${result.refused.message}`);
			return { status: "refused", documentId, reason: result.refused.reason, message: result.refused.message };
		}
		if (result.conflict) {
			const outcome = this.recordSaveConflict(documentId, uri, result.conflict);
			return outcome.status === "conflict" ? { status: "conflict", conflict: outcome.conflict } : outcome;
		}
		if (result.ok && typeof result.revision === "string") {
			this.acknowledgeSaved(documentId, token, result.revision);
			return { status: "saved", documentId, revision: result.revision };
		}
		const message = "the gateway reported an overwrite without a revision";
		this.notifySafe("error", `${displayName(uri)}: ${message}`);
		return { status: "failed", documentId, message };
	}

	/** A disk adoption bumped the model version: force a replacement snapshot. */
	private resyncAfterDiskAdoption(documentId: string, document: DocumentRecord): void {
		const state = this.sync.get(documentId);
		if (!state) return;
		this.clearSyncTimer(state);
		state.pending = [];
		state.gap = true;
		state.observedVersion = document.version;
		state.observedText = document.text;
		this.scheduleSyncFlush(documentId);
	}

	// ─── internals: events ──────────────────────────────────────────────────

	private applyDiagnostics(
		uri: string,
		source: string,
		version: number | null,
		serverGeneration: number,
		diagnostics: WorkspaceDiagnostic[],
	): void {
		let entry = this.diagnosticsByUri.get(uri);
		if (!entry) {
			entry = { bySource: new Map(), flat: undefined };
			this.diagnosticsByUri.set(uri, entry);
		}
		const previous = entry.bySource.get(source);
		this.diagnosticsRetained -= previous?.diagnostics.length ?? 0;
		entry.bySource.set(source, {
			source,
			version,
			serverGeneration,
			diagnostics: [...diagnostics],
			updatedAt: ++this.diagnosticsClock,
		});
		this.diagnosticsRetained += diagnostics.length;
		entry.flat = undefined;
		this.enforceDiagnosticsCap();
	}

	/** Drop the oldest producer sets until the global cap holds. */
	private enforceDiagnosticsCap(): void {
		while (this.diagnosticsRetained > WORKSPACE_DIAGNOSTICS_CAP && this.diagnosticsByUri.size > 0) {
			let oldestUri: string | undefined;
			let oldestSource: string | undefined;
			let oldestAt = Number.POSITIVE_INFINITY;
			for (const [uri, entry] of this.diagnosticsByUri) {
				for (const [source, set] of entry.bySource) {
					if (set.updatedAt < oldestAt) {
						oldestAt = set.updatedAt;
						oldestUri = uri;
						oldestSource = source;
					}
				}
			}
			if (oldestUri === undefined || oldestSource === undefined) {
				this.diagnosticsRetained = 0;
				return;
			}
			const entry = this.diagnosticsByUri.get(oldestUri);
			const dropped = entry?.bySource.get(oldestSource);
			if (!entry) break;
			entry.bySource.delete(oldestSource);
			entry.flat = undefined;
			this.diagnosticsRetained -= dropped?.diagnostics.length ?? 0;
			this.diagnosticsOmittedByUri.set(
				oldestUri,
				(this.diagnosticsOmittedByUri.get(oldestUri) ?? 0) + (dropped?.diagnostics.length ?? 0),
			);
			if (entry.bySource.size === 0) this.diagnosticsByUri.delete(oldestUri);
			if (this.diagnosticsRetained < 0) this.diagnosticsRetained = 0;
		}
	}

	private applyLspState(
		profile: string,
		state: WorkspaceServerState,
		generation: number,
		detail: string | undefined,
	): void {
		const entry: { state: WorkspaceServerState; generation: number; detail?: string } = {
			state,
			generation,
		};
		if (detail !== undefined) entry.detail = detail;
		this.lspProfiles.set(profile, entry);
		this.emitLspStatus();
	}

	private applyFileChanged(uri: string, revision: string, origin: "save" | "external" | "agent"): void {
		const document = this.findDocumentByUri(uri);
		if (!document) return;
		if (origin === "save" && this.savesInFlight.has(document.id)) {
			// Our own save echo can race ahead of the save response; the
			// handshake settles savedRevision, so never treat it as external.
			return;
		}
		if (origin === "save" && document.savedRevision === revision) return;
		const result = this.store.noteExternalChange(document.id, revision);
		if (!result.ok) return;
		switch (result.outcome) {
			case "unchanged":
				this.externalNotices.delete(document.id);
				return;
			case "reload-pending":
				// Clean buffer: soft notice only, never an auto-reload.
				this.externalNotices.set(document.id, {
					documentId: document.id,
					uri: document.uri,
					revision,
				});
				this.notifySafe(
					"info",
					`${displayName(uri)} changed on disk; reload to apply (a save will conflict)`,
				);
				return;
			case "conflict":
				this.notifySafe(
					"warning",
					`${displayName(uri)} changed on disk while the buffer has unsaved changes; a save will conflict`,
				);
				return;
		}
	}

	private emitLspStatus(): void {
		const listener = this.lspStatusListener;
		if (!listener) return;
		try {
			listener(this.lspStatusSnapshot());
		} catch {
			// A UI listener must never break event routing.
		}
	}

	private lspProfilesCopy(): WorkspaceLspStatusSnapshot["profiles"] {
		const profiles: WorkspaceLspStatusSnapshot["profiles"] = {};
		for (const [profile, entry] of this.lspProfiles) {
			const copy: { state: WorkspaceServerState; generation: number; detail?: string } = {
				state: entry.state,
				generation: entry.generation,
			};
			if (entry.detail !== undefined) copy.detail = entry.detail;
			profiles[profile] = copy;
		}
		return profiles;
	}

	// ─── internals: persistence and teardown helpers ────────────────────────

	private schedulePersist(): void {
		if (!this.rootKeyValue) return;
		this.clearPersistTimer();
		this.persistTimer = setTimeout(() => {
			this.persistTimer = undefined;
			void this.persist();
		}, this.persistDebounceMs);
	}

	private clearPersistTimer(): void {
		if (this.persistTimer !== undefined) {
			clearTimeout(this.persistTimer);
			this.persistTimer = undefined;
		}
	}

	private async tryDocumentClose(uri: string): Promise<void> {
		try {
			await this.gateway.documentClose(uri);
		} catch {
			// Best-effort: a closing tab must not be blocked by a dead gateway.
		}
	}

	private async tryGatewayClose(): Promise<void> {
		try {
			await this.gateway.close();
		} catch {
			// Best-effort teardown.
		}
	}

	/** Bound one teardown RPC so dispose/close can never hang on the gateway. */
	private async bounded<T>(promise: Promise<T>): Promise<T | undefined> {
		let timer: ReturnType<typeof setTimeout> | undefined;
		try {
			return await Promise.race([
				promise,
				new Promise<undefined>((resolve) => {
					timer = setTimeout(() => resolve(undefined), WORKSPACE_DISPOSE_TIMEOUT_MS);
				}),
			]);
		} catch {
			return undefined;
		} finally {
			if (timer !== undefined) clearTimeout(timer);
		}
	}

	private notifySafe(kind: NoticeKind, text: string): void {
		try {
			this.notifyCallback(kind, text);
		} catch {
			// A UI notice must never break orchestration.
		}
	}

	private requestRenderSafe(): void {
		try {
			this.requestRenderCallback();
		} catch {
			// A render request must never break orchestration.
		}
	}
}
