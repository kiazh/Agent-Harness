// Wire types for the UI <-> gateway JSON-RPC protocol.
// Spec: communications/ui-gateway-protocol.md (gateway side: ah/gateway/server.py).

export interface SessionInfo {
	id: string;
	title: string;
	model: string;
	provider: string;
	status: string;
	lastActivity: string | null;
}

export interface InitializeResult {
	version: string;
	model: string;
	provider: string;
	cwd: string;
	branch: string;
}

export interface HistoryEntry {
	role: "user" | "assistant" | "tool" | "system";
	content: string;
	tool?: string;
}

export interface SessionResult {
	session: SessionInfo;
}

export interface SessionListResult {
	sessions: SessionInfo[];
}

export interface ResumeResult {
	session: SessionInfo;
	history: HistoryEntry[];
}

export interface ConfigResult {
	model: string;
	provider: string;
}

export interface ConfigSetResult extends ConfigResult {
	key: string;
	value: unknown;
}

export interface ConfigGetResult {
	config: Record<string, unknown>;
	secrets: string[];
}

export interface MemoryInfo {
	id: string;
	content: string;
	category: string;
	importance: number;
	accessCount: number;
	createdAt: string | null;
	sessionId: string | null;
	score?: number;
}

export interface PendingMemoryInfo {
	id: string;
	content: string;
	category: string;
	importance: number;
	redactions: string[];
	status: string;
	createdAt: string | null;
}

export interface SkillInfo {
	name: string;
	description: string;
	triggers: string[];
	version: string;
	sourceType: string;
	usageCount: number;
	enabled: boolean;
	content?: string;
}

export interface ProfileInfo {
	userId: string;
	displayName: string;
	preferences: Record<string, unknown>;
	interactionCount: number;
	topTopics: Array<{ topic: string; count: number }>;
	updatedAt: string | null;
}

export interface ContextResult {
	chunks: Array<{ type: string; agent: string; tokens: number; createdAt: string | null; preview: string }>;
	totalTokens: number;
	budget: number;
	goal: string | null;
}

export type CompressResult =
	| { compressed: false }
	| {
			compressed: true;
			originalCount: number;
			newCount: number;
			originalTokens: number;
			compressedTokens: number;
			ratio: number;
			method: string;
	  };

export interface StatusResult {
	postgres: string;
	sessions: number;
	contextChunks: number;
	memories: number;
	pendingMemories: number;
	tools: string[];
	openrouterKeySet: boolean;
	model: string;
	provider: string;
}

interface EventBase {
	sessionId: string;
	turnId: string;
}

export type GatewayEvent = EventBase &
	(
		| { type: "message.start" }
		| { type: "message.delta"; text: string }
		| { type: "tool.start"; id: string; name: string; args: Record<string, unknown> }
		| { type: "tool.complete"; id: string; name: string; result: string; isError: boolean }
		| { type: "usage"; tokens: number }
		| {
				type: "message.complete";
				text: string;
				tokens: number;
				iterations: number;
				toolCalls: number;
				cancelled: boolean;
		  }
		| { type: "error"; message: string }
	);

/** Error returned by the gateway for a failed request. */
export class RpcError extends Error {
	readonly code: number;

	constructor(code: number, message: string) {
		super(message);
		this.name = "RpcError";
		this.code = code;
	}
}

// Application error codes (see protocol spec).
export const DATABASE_UNAVAILABLE = 1001;
export const SESSION_NOT_FOUND = 1002;
export const TURN_IN_PROGRESS = 1003;
