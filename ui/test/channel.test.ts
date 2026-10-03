import assert from "node:assert/strict";
import { test } from "node:test";
import { JsonRpcChannel } from "../src/gateway.ts";
import { type GatewayEvent, RpcError } from "../src/protocol.ts";

function channel() {
	const sent: Array<{ id: number; method: string; params: unknown }> = [];
	const ch = new JsonRpcChannel((line) => sent.push(JSON.parse(line)));
	return { ch, sent };
}

test("resolves a request with the matching response", async () => {
	const { ch, sent } = channel();
	const pending = ch.request<{ ok: boolean }>("initialize", { model: "m" });
	assert.equal(sent[0]!.method, "initialize");
	assert.deepEqual(sent[0]!.params, { model: "m" });
	ch.handleLine(JSON.stringify({ jsonrpc: "2.0", id: sent[0]!.id, result: { ok: true } }));
	assert.deepEqual(await pending, { ok: true });
});

test("matches out-of-order responses by id", async () => {
	const { ch, sent } = channel();
	const a = ch.request<string>("a");
	const b = ch.request<string>("b");
	ch.handleLine(JSON.stringify({ jsonrpc: "2.0", id: sent[1]!.id, result: "B" }));
	ch.handleLine(JSON.stringify({ jsonrpc: "2.0", id: sent[0]!.id, result: "A" }));
	assert.deepEqual(await Promise.all([a, b]), ["A", "B"]);
});

test("rejects with RpcError carrying the gateway's code", async () => {
	const { ch, sent } = channel();
	const pending = ch.request("session.resume");
	ch.handleLine(JSON.stringify({ jsonrpc: "2.0", id: sent[0]!.id, error: { code: 1002, message: "not found" } }));
	await assert.rejects(pending, (error: unknown) => error instanceof RpcError && error.code === 1002);
});

test("dispatches event notifications and ignores garbage", () => {
	const { ch } = channel();
	const events: GatewayEvent[] = [];
	ch.onEvent = (event) => events.push(event);
	ch.handleLine("not json at all");
	ch.handleLine("42");
	ch.handleLine(JSON.stringify({ jsonrpc: "2.0", id: 999, result: "nobody asked" }));
	ch.handleLine(
		JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", sessionId: "s", turnId: "t", text: "hi" } }),
	);
	assert.equal(events.length, 1);
	assert.equal(events[0]!.type, "message.delta");
});

test("times out when the gateway never answers", async () => {
	const { ch } = channel();
	await assert.rejects(ch.request("slow", {}, 20), /did not answer slow/);
});

test("close rejects pending and future requests", async () => {
	const { ch } = channel();
	const pending = ch.request("x");
	ch.close(new Error("gateway exited (code 1)"));
	await assert.rejects(pending, /gateway exited/);
	await assert.rejects(ch.request("y"), /gateway exited/);
});
