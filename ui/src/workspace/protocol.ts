// Workspace RPC and event payload types (plan §19).
// Hand-written mirror of `machina/workspace/models.py`, like the rest of protocol.ts.
// Workspace events are a separate family from the chat stream: they never carry
// sessionId/turnId and must not enter the transcript reducer.

export type WorkspaceServerState =
	| "disabled"
	| "missing"
	| "stopped"
	| "starting"
	| "initializing"
	| "ready"
	| "degraded"
	| "failed"
	| "stopping";

export type WorkspaceFreshness = "versioned" | "unversioned" | "pending" | "stale" | "unavailable";

export interface WorkspaceOpenResult {
	workspaceId: string;
	clientId: string;
	epoch: number;
	rootLabel: string;
	resolvedRoot: string;
	trusted: boolean;
	capabilities: string[];
}

export interface WorkspaceListEntry {
	name: string;
	path: string; // root-relative POSIX path
	kind: "file" | "directory";
	sizeBytes?: number;
	excluded?: boolean;
	symlink?: boolean;
}

export interface WorkspaceListResult {
	entries: WorkspaceListEntry[];
	nextCursor: string | null;
	exclusions: string[];
	truncated: boolean;
	/** Structured refusal for denied/unavailable listings (plan §11). */
	refused?: { reason: string; message: string };
}

/** One Quick Open hit (plan §12): root-relative path + display fields. */
export interface WorkspaceQuickOpenEntry {
	/** Root-relative POSIX path. */
	path: string;
	/** Basename of the path. */
	name: string;
	/** Parent directory (root-relative POSIX; "" for the workspace root). */
	directory: string;
}

/**
 * Bounded ranked Quick Open answer. Partial, truncated and stale states are
 * disclosure fields — never errors — so the pane can render them honestly.
 */
export interface WorkspaceQuickOpenResult {
	entries: WorkspaceQuickOpenEntry[];
	/** Matches before the limit; index-relative while `indexing`. */
	total: number;
	/** The result page or the underlying index was cut (see `reasons`). */
	truncated: boolean;
	/** Disclosed bounds, e.g. "result-limit", "file-cap", "deadline", "directory-entry-cap", "stale-epoch". */
	reasons: string[];
	/** True while the recursive index is still building; entries are partial. */
	indexing: boolean;
	/** Files indexed so far (progress disclosure). */
	scanned: number;
	/** The workspace epoch changed mid-query; results were discarded. */
	stale: boolean;
	workspaceEpoch: number;
	/** Declared exclusion policy (folder names never indexed). */
	exclusions: string[];
	/** Structured refusal for denied/unavailable queries. */
	refused?: { reason: string; message: string };
}

export interface WorkspaceReadResult {
	uri: string;
	text: string;
	revision: string;
	languageId: string;
	lineEnding: "LF" | "CRLF" | "CR" | "mixed";
	encoding: "utf-8";
	readOnly: boolean;
	sizeBytes: number;
}

export interface WorkspaceReadRefusal {
	refused: {
		reason: "binary" | "too-large" | "not-regular" | "unsupported-encoding" | "denied" | "missing";
		message: string;
	}
}

export interface WorkspaceSaveConflict {
	diskRevision: string;
	baseRevision: string;
	reason: string;
}

export interface WorkspaceSaveResult {
	ok: boolean;
	revision?: string;
	/** UI document version captured at save time; the client clears dirty only if unchanged. */
	savedUiVersion?: number;
	conflict?: WorkspaceSaveConflict;
	/** Structured refusal (denied/hardlinked/unsupported) — never a silent failure. */
	refused?: { reason: string; message: string };
}

export interface WorkspaceDocumentOpenResult {
	acceptedVersion: number;
	serverGeneration: number;
	state: WorkspaceServerState;
}

export interface LspPosition {
	line: number;
	character: number;
}

export interface LspRange {
	start: LspPosition;
	end: LspPosition;
}

export interface WorkspaceDocumentChange {
	baseVersion: number;
	version: number;
	changes: Array<{ range: LspRange; text: string }>;
}

export interface WorkspaceDocumentChangeResult {
	acceptedVersion: number;
	resync?: boolean;
}

export interface WorkspaceDiagnostic {
	range: LspRange;
	severity: 1 | 2 | 3 | 4;
	message: string;
	source?: string;
	code?: string | number;
}

export interface WorkspaceIntelligenceResult {
	workspaceId: string;
	source: "editor-buffer" | "disk";
	uri: string;
	documentVersion: number | null;
	serverGeneration: number | null;
	workspaceEpoch: number;
	operation: string;
	state: WorkspaceServerState;
	freshness: WorkspaceFreshness;
	truncated: boolean;
	result: unknown;
}

/** One protocol text edit (plan §18). Positions are UTF-16 LSP positions. */
export interface LspTextEdit {
	range: LspRange;
	newText: string;
}

/** A per-file group inside a workspace edit proposal. */
export interface WorkspaceEditChange {
	uri: string;
	/** Document version the ranges were computed against; null when the server omitted it. */
	version: number | null;
	edits: LspTextEdit[];
}

/**
 * Normalized workspace edit proposal (rename/format/code-action).
 * Resource operations are never produced until they are implemented and
 * tested: the payload type has no rename/delete/create fields by design.
 */
export interface WorkspaceEditPayload {
	label: string;
	changes: WorkspaceEditChange[];
	/** Files the proposal could not be built against (missing/stale) — refused, not dropped silently. */
	refused: Array<{ uri: string; reason: string }>;
}

/** Result payloads per intelligence operation (result field narrowing aids). */
export interface RenameResultPayload {
	edits: WorkspaceEditPayload | null;
	/** True when the server answered prepareRename with a null range (rename not offered here). */
	unavailable: boolean;
}

export interface CodeActionPayload {
	actions: Array<{
		title: string;
		kind?: string;
		edits?: WorkspaceEditPayload;
		/** Server commands are never executed from the editor; recorded for explicit policy decisions. */
		command?: { title: string; command: string; arguments?: unknown[] };
	}>;
}

export interface WorkspaceLspStatusResult {
	servers: Array<{
		profile: string;
		languageId: string;
		state: WorkspaceServerState;
		generation: number;
		capabilities?: Record<string, unknown>;
		detail?: string;
	}>;
}

export type WorkspaceEvent =
	| {
			type: "workspace.fileChanged";
			workspaceId: string;
			epoch: number;
			uri: string;
			revision: string;
			origin: "save" | "external" | "agent";
	}
	| {
			type: "workspace.diagnostics";
			workspaceId: string;
			epoch: number;
			serverGeneration: number;
			uri: string;
			version: number | null;
			source: string;
			diagnostics: WorkspaceDiagnostic[];
	}
	| {
			type: "workspace.lspState";
			workspaceId: string;
			epoch: number;
			profile: string;
			state: WorkspaceServerState;
			generation: number;
			detail?: string;
	}
	| {
			type: "workspace.indexProgress";
			workspaceId: string;
			epoch: number;
			scanned: number;
			total: number | null;
			done: boolean;
	};

export const WORKSPACE_EVENT_TYPES = [
	"workspace.fileChanged",
	"workspace.diagnostics",
	"workspace.lspState",
	"workspace.indexProgress",
] as const;

export function isWorkspaceEvent(value: { type?: string }): value is WorkspaceEvent {
	return WORKSPACE_EVENT_TYPES.includes(value.type as (typeof WORKSPACE_EVENT_TYPES)[number]);
}
