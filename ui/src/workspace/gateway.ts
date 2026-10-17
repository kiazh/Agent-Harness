// T3 typed workspace RPC adapter (plan §19): one narrow, injectable seam over
// the JSON-RPC request function. Every method maps 1:1 onto a `workspace.*`
// gateway method and reuses the frozen result types from protocol.ts.
// Structured refusals are returned as data (`{ refused }`); transport
// failures reject, and the controller converts them into typed outcomes.
//
// The adapter owns the open workspace binding: `workspace.open` captures the
// server-issued `workspaceId` + `epoch`, and every later request carries them
// so a stale client can never address a reopened workspace (the backend
// fails closed with NOT_FOUND / a stale-epoch refusal). The controller
// independently drops workspace events whose identity does not match.

import type {
	LspPosition,
	WorkspaceDocumentChange,
	WorkspaceDocumentChangeResult,
	WorkspaceDocumentOpenResult,
	WorkspaceIntelligenceResult,
	WorkspaceListResult,
	WorkspaceLspStatusResult,
	WorkspaceOpenResult,
	WorkspaceReadRefusal,
	WorkspaceReadResult,
	WorkspaceSaveResult,
	WorkspaceServerState,
} from "./protocol.ts";

/** Injected request surface (GatewayClient.request / JsonRpcChannel.request). */
export type WorkspaceRequestFn = <T>(
	method: string,
	params: Record<string, unknown>,
) => Promise<T>;

/** Closed allowlist of intelligence operations (mirrors INTELLIGENCE_OPERATIONS). */
export type WorkspaceIntelligenceOperation =
	| "completion"
	| "hover"
	| "definition"
	| "references"
	| "symbols"
	| "diagnostics";

export type WorkspaceLspAction = "start" | "stop" | "restart";

/** `workspace.lspControl` result; no protocol.ts mirror (backend-local shape). */
export interface WorkspaceLspControlResult {
	action: WorkspaceLspAction;
	profile: string;
	state: WorkspaceServerState;
	detail?: string;
}

/** `workspace.documentClose` result. */
export interface WorkspaceDocumentCloseResult {
	closed: boolean;
	uri: string;
}

/** `workspace.close` result. */
export interface WorkspaceCloseResult {
	closed: boolean;
}

export interface WorkspaceListOptions {
	/** Passed through to `workspace.list` (`includeHidden`). */
	includeHidden?: boolean;
	limit?: number;
}

/**
 * Structural seam the controller depends on. `WorkspaceGatewayAdapter`
 * implements it; tests inject fakes without touching the real transport.
 */
export interface WorkspaceGateway {
	identity(): WorkspaceOpenResult | undefined;
	open(root: string, trust: boolean): Promise<WorkspaceOpenResult>;
	list(path: string, cursor?: string, options?: WorkspaceListOptions): Promise<WorkspaceListResult>;
	read(uri: string): Promise<WorkspaceReadResult | WorkspaceReadRefusal>;
	save(
		uri: string,
		expectedRevision: string | null,
		text: string,
		uiVersion: number,
	): Promise<WorkspaceSaveResult>;
	documentOpen(
		uri: string,
		languageId: string,
		version: number,
		text: string,
	): Promise<WorkspaceDocumentOpenResult | WorkspaceReadRefusal>;
	documentChange(
		uri: string,
		change: WorkspaceDocumentChange,
	): Promise<WorkspaceDocumentChangeResult | WorkspaceReadRefusal>;
	documentClose(uri: string): Promise<WorkspaceDocumentCloseResult | WorkspaceReadRefusal>;
	intelligence(
		operation: WorkspaceIntelligenceOperation,
		uri: string | null,
		version: number | null,
		position?: LspPosition | null,
	): Promise<WorkspaceIntelligenceResult | WorkspaceReadRefusal>;
	lspStatus(): Promise<WorkspaceLspStatusResult | WorkspaceReadRefusal>;
	lspControl(
		profile: string,
		action: WorkspaceLspAction,
	): Promise<WorkspaceLspControlResult | WorkspaceReadRefusal>;
	close(): Promise<WorkspaceCloseResult | WorkspaceReadRefusal>;
}

/** True for the frozen structured-refusal envelope (`{ refused: {...} }`). */
export function isWorkspaceRefusal(value: unknown): value is WorkspaceReadRefusal {
	if (typeof value !== "object" || value === null) return false;
	const refused = (value as { refused?: unknown }).refused;
	return typeof refused === "object" && refused !== null;
}

/**
 * Real adapter. Construct with `(method, params) => client.request(method, params)`;
 * the workspace identity captured by `open` is attached to every bound request.
 */
export class WorkspaceGatewayAdapter implements WorkspaceGateway {
	private readonly request: WorkspaceRequestFn;
	private currentIdentity: WorkspaceOpenResult | undefined;
	/**
	 * Orders overlapping opens/closes: only the newest open may bind. A close
	 * also bumps it so an open that resolves after the close leaves no stale
	 * adapter identity behind (controller-level fencing is separate).
	 */
	private openGeneration = 0;

	constructor(request: WorkspaceRequestFn) {
		this.request = request;
	}

	identity(): WorkspaceOpenResult | undefined {
		return this.currentIdentity;
	}

	async open(root: string, trust: boolean): Promise<WorkspaceOpenResult> {
		const generation = ++this.openGeneration;
		const identity = await this.request<WorkspaceOpenResult>("workspace.open", { root, trust });
		if (generation === this.openGeneration) this.currentIdentity = identity;
		return identity;
	}

	async list(
		path: string,
		cursor?: string,
		options: WorkspaceListOptions = {},
	): Promise<WorkspaceListResult> {
		return this.request<WorkspaceListResult>("workspace.list", {
			...this.bound(),
			path,
			cursor: cursor ?? null,
			includeHidden: options.includeHidden === true,
			...(options.limit !== undefined ? { limit: options.limit } : {}),
		});
	}

	async read(uri: string): Promise<WorkspaceReadResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceReadResult | WorkspaceReadRefusal>("workspace.read", {
			...this.bound(),
			uri,
		});
	}

	async save(
		uri: string,
		expectedRevision: string | null,
		text: string,
		uiVersion: number,
	): Promise<WorkspaceSaveResult> {
		return this.request<WorkspaceSaveResult>("workspace.save", {
			...this.bound(),
			uri,
			text,
			expectedRevision,
			uiVersion,
		});
	}

	async documentOpen(
		uri: string,
		languageId: string,
		version: number,
		text: string,
	): Promise<WorkspaceDocumentOpenResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceDocumentOpenResult | WorkspaceReadRefusal>(
			"workspace.documentOpen",
			{ ...this.bound(), uri, languageId, version, text },
		);
	}

	async documentChange(
		uri: string,
		change: WorkspaceDocumentChange,
	): Promise<WorkspaceDocumentChangeResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceDocumentChangeResult | WorkspaceReadRefusal>(
			"workspace.documentChange",
			{ ...this.bound(), uri, ...change },
		);
	}

	async documentClose(uri: string): Promise<WorkspaceDocumentCloseResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceDocumentCloseResult | WorkspaceReadRefusal>(
			"workspace.documentClose",
			{ ...this.bound(), uri },
		);
	}

	async intelligence(
		operation: WorkspaceIntelligenceOperation,
		uri: string | null,
		version: number | null,
		position?: LspPosition | null,
	): Promise<WorkspaceIntelligenceResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceIntelligenceResult | WorkspaceReadRefusal>(
			"workspace.intelligence",
			{
				...this.bound(),
				operation,
				uri,
				version,
				position: position ?? null,
			},
		);
	}

	async lspStatus(): Promise<WorkspaceLspStatusResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceLspStatusResult | WorkspaceReadRefusal>(
			"workspace.lspStatus",
			this.bound(),
		);
	}

	async lspControl(
		profile: string,
		action: WorkspaceLspAction,
	): Promise<WorkspaceLspControlResult | WorkspaceReadRefusal> {
		return this.request<WorkspaceLspControlResult | WorkspaceReadRefusal>(
			"workspace.lspControl",
			{ ...this.bound(), profile, action },
		);
	}

	async close(): Promise<WorkspaceCloseResult | WorkspaceReadRefusal> {
		// Any open still in flight is stale from here on: it must not bind a
		// new identity after the user (or controller) closed the workspace.
		this.openGeneration += 1;
		const identity = this.currentIdentity;
		if (!identity) return { closed: false };
		const result = await this.request<WorkspaceCloseResult | WorkspaceReadRefusal>(
			"workspace.close",
			{ workspaceId: identity.workspaceId, epoch: identity.epoch },
		);
		// Only clear the binding the close was issued for: a close that resolves
		// after a newer `open` must never orphan the new workspace identity.
		if (!isWorkspaceRefusal(result) && this.currentIdentity === identity) this.currentIdentity = undefined;
		return result;
	}

	/** workspaceId + epoch for every bound request; throws before `open`. */
	private bound(): { workspaceId: string; epoch: number } {
		const identity = this.currentIdentity;
		if (!identity) throw new Error("workspace is not open");
		return { workspaceId: identity.workspaceId, epoch: identity.epoch };
	}
}
