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
