// Generated from ah/protocol/events.json; edit the schema, then regenerate.
export type CanonicalEvent = { protocolVersion: 2; sessionId: string; turnId: string } & (
	{ type: "message.start" }
	| { type: "turn.started" }
	| { type: "message.delta"; text: string }
	| { type: "message.stopping"; reason: string }
	| { type: "tool.start"; id: string; name: string; args: Record<string, unknown> }
	| { type: "tool.complete"; id: string; name: string; result: string; isError: boolean; truncated?: boolean }
	| { type: "usage"; tokens: number }
	| { type: "message.complete"; text: string; tokens: number; iterations: number; toolCalls: number; cancelled: boolean; needsApproval?: Record<string, unknown>[] }
	| { type: "error"; message: string }
	| { type: "needs_approval"; requestId: string; operation: string; target: string; argv: string[]; cwd: string; backend: string; capabilities: string[]; tool?: string; shell?: string; timeout?: number; network?: string[]; contentDigest?: string; contentPreview?: string; contentLength?: number; fileDiff?: Record<string, unknown>; durable?: boolean|null; status?: string; originTurnId?: string }
	| { type: "approval.resolved"; requestId: string; status: string; originTurnId?: string }
	| { type: "approval.resumed"; requestId: string; originTurnId: string }
	| { type: "turn.ownership_lost"; reason?: string }
	| { type: "turn.cleanup_pending"; reason?: string }
);
export type LegacyGatewayEvent = { protocolVersion?: 1; sessionId: string; turnId: string } & (
	{ type: "message.start" }
	| { type: "turn.started" }
	| { type: "message.delta"; text: string }
	| { type: "message.stopping"; reason: string }
	| { type: "tool.start"; id: string; name: string; args: Record<string, unknown> }
	| { type: "tool.complete"; id: string; name: string; result: string; isError: boolean; truncated?: boolean }
	| { type: "usage"; tokens: number }
	| { type: "message.complete"; text: string; tokens: number; iterations: number; toolCalls: number; cancelled: boolean; needsApproval?: Record<string, unknown>[] }
	| { type: "error"; message: string }
	| { type: "permission.required"; requestId: string; operation: string; target: string; argv: string[]; cwd: string; backend: string; capabilities: string[]; tool?: string; shell?: string; timeout?: number; network?: string[]; contentDigest?: string; contentPreview?: string; contentLength?: number; fileDiff?: Record<string, unknown>; durable?: boolean|null; status?: string; originTurnId?: string }
	| { type: "permission.resolved"; requestId: string; status: string; originTurnId?: string }
	| { type: "approval.resumed"; requestId: string; originTurnId: string }
	| { type: "turn.ownership_lost"; reason?: string }
	| { type: "turn.cleanup_pending"; reason?: string }
);
export type GatewayEvent = CanonicalEvent | LegacyGatewayEvent;
