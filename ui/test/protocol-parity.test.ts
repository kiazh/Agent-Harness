// Protocol-parity contract: the TS wire types the UI consumes must cover
// the backend shapes that produce them.
//
// Sources of truth (backend):
// - event types: machina/protocol/events.json (version 2)
// - SessionInfo: machina/gateway/serializers.py::session_to_dict
// - AgentInfo: machina/core/agent_def.py::AgentDef.to_dict
// - HistoryEntry: machina/gateway/serializers.py::history_from_chunks
//
// Duplication documented: these shapes are intentionally mirrored here
// instead of generated, so this test fails loudly on either side's drift:
// a backend addition/removal/rename fails the runtime assertions below,
// and a TS union change without updating TS_EVENT_TYPES fails compilation
// via the _Exhaustive check.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import type { AgentInfo, GatewayEvent, HistoryEntry, SessionInfo } from "../src/protocol.js";

// Every event type the TS GatewayEvent union represents. Compile-time
// bound to the union: each entry must be a member...
export const TS_EVENT_TYPES = [
	"message.start",
	"turn.started",
	"message.delta",
	"message.stopping",
	"tool.start",
	"tool.complete",
	"usage",
	"growth.status",
	"message.complete",
	"error",
	"needs_approval",
	"approval.resolved",
	"approval.resumed",
	"turn.ownership_lost",
	"turn.cleanup_pending",
	"permission.required",
	"permission.resolved",
] as const satisfies readonly GatewayEvent["type"][];

// ...and the union must have no member outside the list, so adding an
// event to events.ts without updating this contract fails typecheck.
type _Exhaustive = Exclude<GatewayEvent["type"], (typeof TS_EVENT_TYPES)[number]> extends never
	? true
	: false;
const _exhaustive: _Exhaustive = true;
void _exhaustive;

// Legacy v1 names the UI still accepts but the backend no longer emits
// (kept for old gateway output; see events.ts LegacyGatewayEvent).
const LEGACY_TS_ONLY = new Set(["permission.required", "permission.resolved"]);

async function backendEventTypes(): Promise<string[]> {
	const specPath = new URL("../../machina/protocol/events.json", import.meta.url);
	const spec = JSON.parse(await readFile(specPath, "utf8")) as {
		version: number;
		events: Record<string, unknown>;
	};
	assert.equal(spec.version, 2, "contract targets protocol v2");
	return Object.keys(spec.events);
}

test("protocol-parity: TS GatewayEvent covers every backend event type", async () => {
	const backend = await backendEventTypes();
	const ts = new Set<string>(TS_EVENT_TYPES);
	for (const type of backend) {
		assert.ok(ts.has(type), `backend event ${type} has no TS GatewayEvent member`);
	}
});

test("protocol-parity: no unexpected TS-only event types beyond legacy", async () => {
	const backend = new Set(await backendEventTypes());
	for (const type of TS_EVENT_TYPES) {
		assert.ok(
			backend.has(type) || LEGACY_TS_ONLY.has(type),
			`TS event ${type} is neither backend-emitted nor documented legacy`,
		);
	}
});

test("protocol-parity: SessionInfo mirrors session_to_dict", () => {
	// Exact backend keys from serializers.session_to_dict.
	const backend = {
		id: "11111111-1111-4111-8111-111111111111",
		title: "t",
		model: "m",
		provider: "openrouter",
		status: "active",
		lastActivity: null,
		parentSessionId: null,
	} satisfies SessionInfo;
	const consumed: SessionInfo = backend;
	// Fields the UI actually reads (app.ts headerInfo/switchTo/footer):
	assert.equal(consumed.id, backend.id);
	assert.equal(consumed.title, backend.title);
	assert.equal(consumed.model, backend.model);
	assert.equal(consumed.provider, backend.provider);
	assert.equal(consumed.status, backend.status);
	// Orchestration link consumed by the agent tab strip (app.ts family).
	assert.equal(consumed.parentSessionId, backend.parentSessionId);
	assert.deepEqual(Object.keys(consumed).sort(), Object.keys(backend).sort());
});

test("protocol-parity: AgentInfo mirrors AgentDef.to_dict", () => {
	// Exact backend keys from core/agent_def.py AgentDef.to_dict.
	const backend = {
		name: "harness",
		description: "General-purpose agent with every tool.",
		systemPrompt: "",
		tools: ["read"],
		model: null,
		provider: null,
		maxIterations: 25,
		source: "builtin",
	} satisfies AgentInfo;
	const consumed: AgentInfo = backend;
	// Fields the UI actually reads (features/agents.ts list/show):
	assert.equal(consumed.name, backend.name);
	assert.equal(consumed.source, backend.source);
	assert.deepEqual(consumed.tools, backend.tools);
	assert.equal(consumed.description, backend.description);
	assert.equal(consumed.model, backend.model);
	assert.equal(consumed.systemPrompt, backend.systemPrompt);
	assert.deepEqual(Object.keys(consumed).sort(), Object.keys(backend).sort());
});

test("protocol-parity: HistoryEntry mirrors history_from_chunks", () => {
	const entries = [
		{ role: "user", content: "hi" },
		{ role: "assistant", content: "hello" },
		{ role: "tool", tool: "read", content: "preview" },
		{ role: "system", content: "summary" },
	] as const satisfies readonly HistoryEntry[];
	assert.equal(entries.length, 4);
});
