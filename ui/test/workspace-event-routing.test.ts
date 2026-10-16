// T8 seam: workspace notifications route to their own observer and never
// enter turn bookkeeping (plan §19: no fabricated session/turn IDs).
import { test } from "node:test";
import assert from "node:assert/strict";
import { JsonRpcChannel } from "../src/gateway.ts";
import type { WorkspaceEvent } from "../src/workspace/protocol.ts";
import type { GatewayEvent } from "../src/protocol.ts";

function frame(params: Record<string, unknown>): string {
	return JSON.stringify({ jsonrpc: "2.0", method: "event", params });
}

test("workspace events route to onWorkspaceEvent only", () => {
	const channel = new JsonRpcChannel(() => {});
	const workspace: WorkspaceEvent[] = [];
	const chat: GatewayEvent[] = [];
	channel.onWorkspaceEvent = (event) => workspace.push(event);
	channel.onEvent = (event) => chat.push(event);

	channel.handleLine(frame({
		type: "workspace.diagnostics",
		workspaceId: "w1",
		epoch: 1,
		serverGeneration: 2,
		uri: "file:///root/main.py",
		version: 3,
		source: "pyright",
		diagnostics: [],
	}));

	assert.equal(workspace.length, 1);
	assert.equal(chat.length, 0);
	assert.equal(workspace[0]!.type, "workspace.diagnostics");
});

test("chat events still route to onEvent", () => {
	const channel = new JsonRpcChannel(() => {});
	const workspace: WorkspaceEvent[] = [];
	const chat: GatewayEvent[] = [];
	channel.onWorkspaceEvent = (event) => workspace.push(event);
	channel.onEvent = (event) => chat.push(event);

	channel.handleLine(frame({
		protocolVersion: 2,
		type: "message.delta",
		sessionId: "s1",
		turnId: "t1",
		text: "hi",
	}));

	assert.equal(chat.length, 1);
	assert.equal(workspace.length, 0);
});

test("responses still resolve with workspace events on the wire", async () => {
	const sent: string[] = [];
	const channel = new JsonRpcChannel((line) => sent.push(line));
	const promise = channel.request<{ ok: boolean }>("workspace.open", { root: "/tmp" });
	channel.handleLine(frame({ type: "workspace.lspState", workspaceId: "w", epoch: 1, profile: "python", state: "starting", generation: 1 }));
	const request = JSON.parse(sent[0]!) as { id: number };
	channel.handleLine(JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { ok: true } }));
	assert.deepEqual(await promise, { ok: true });
});

test("unknown event types fall back to the chat observer (no silent drop)", () => {
	const channel = new JsonRpcChannel(() => {});
	const chat: unknown[] = [];
	channel.onEvent = (event) => chat.push(event);
	channel.handleLine(frame({ type: "future.event", sessionId: "s", turnId: "t" }));
	assert.equal(chat.length, 1);
});
