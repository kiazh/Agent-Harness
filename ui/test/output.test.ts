// Retained output inspection (O01): bounded store, honest eviction and
// truncation, viewer keyboard interaction through the real overlay path,
// and the /output command contract.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	stripTerminalSequences,
	TuiAltScreen,
	visibleWidth,
	type Terminal,
} from "@earendil-works/pi-tui";
import { OutputStore, OutputViewer } from "../src/components/OutputViewer.ts";
import { LIMITS } from "../src/design/tokens.ts";

function mockTerminal(columns: number, rows: number): Terminal {
	return {
		columns,
		rows,
		kittyProtocolActive: false,
		start() {},
		stop() {},
		async drainInput() {},
		write() {},
		moveBy() {},
		hideCursor() {},
		showCursor() {},
		clearLine() {},
		clearFromCursor() {},
		clearScreen() {},
		setTitle() {},
		setProgress() {},
	};
}

test("O01 output store enforces count and byte bounds with honest eviction", () => {
	const store = new OutputStore();
	for (let i = 0; i < 10; i++) store.put("s", `tool-${i}`, "terminal", `result ${i}`, false);
	assert.equal(store.size, LIMITS.outputRetainedCount, "only the newest outputs survive");
	assert.equal(store.get("s", "tool-0"), undefined, "evicted output is unavailable, not reconstructed");
	assert.equal(store.get("s", "tool-1"), undefined);
	assert.ok(store.get("s", "tool-9"), "newest output retained");
	const listed = store.list("s").map((r) => r.toolId);
	assert.deepEqual(listed, ["tool-9", "tool-8", "tool-7", "tool-6", "tool-5", "tool-4", "tool-3", "tool-2"]);
});

test("O01 per-output cap is codepoint-safe and marked", () => {
	const store = new OutputStore();
	const big = `ok ${"👩‍💻".repeat(40_000)} tail`;
	store.put("s", "big", "terminal", big, false);
	const record = store.get("s", "big")!;
	assert.ok(record.byteLength <= LIMITS.outputMaxBytesPer, "capped at 128 KiB");
	assert.equal(record.locallyTruncated, true, "local cap truncation is marked");
	assert.equal(record.backendTruncated, false);
	// eslint-disable-next-line no-control-regex
	assert.ok(!/�/.test(record.text), "no split code point leaves replacement output");
	assert.match(record.text, /tail|ok/, "kept a readable prefix");
});

test("O01 total cap evicts oldest first", () => {
	const store = new OutputStore();
	const each = "x".repeat(128 * 1024);
	for (let i = 0; i < 9; i++) store.put("s", `t${i}`, "terminal", `${each}${i}`, false);
	let total = 0;
	for (const record of store.list("s")) total += record.byteLength;
	assert.ok(total <= LIMITS.outputMaxBytesTotal, `total ${total} within 1 MiB`);
	assert.equal(store.get("s", "t0"), undefined, "oldest evicted under total pressure");
});

test("O01 resolve is session-scoped with unambiguous errors", () => {
	const store = new OutputStore();
	store.put("s", "abcdef01", "terminal", "one", false);
	store.put("s", "abcdef02", "terminal", "two", false);
	store.put("other", "abcdef01", "terminal", "cross", false);
	assert.equal(store.resolve("s", "abcdef01").text, "one");
	assert.throws(() => store.resolve("s", "abcdef"), /matches 2 outputs/, "ambiguous IDs error honestly");
	assert.throws(() => store.resolve("s", "nope"), /No retained output/, "missing IDs error honestly");
	assert.equal(store.resolve("other", "abcdef01").text, "cross", "explicit IDs never cross sessions");
});

test("O01 viewer renders title, status and hints inside the width", () => {
	const store = new OutputStore();
	store.put("s", "tool-1", "terminal", "hello\nworld", false);
	const viewer = new OutputViewer(store.get("s", "tool-1")!);
	for (const width of [20, 40, 80]) {
		for (const line of viewer.render(width)) assert.ok(visibleWidth(line) <= width, `line wider than ${width}`);
	}
	const out = viewer.render(80).map((line) => stripTerminalSequences(line)).join("\n");
	assert.match(out, /Output.*terminal/);
	assert.match(out, /bytes/);
	assert.match(out, /esc close/);
});

test("O01 viewer keyboard scrolls through the real overlay and Esc closes", () => {
	const term = mockTerminal(80, 24);
	const tui = new TuiAltScreen(term);
	try {
		const store = new OutputStore();
		store.put("s", "tool-9", "terminal", Array.from({ length: 60 }, (_, i) => `line ${i}`).join("\n"), true);
		let closed = 0;
		const viewer = new OutputViewer(store.get("s", "tool-9")!, { onClose: () => closed++ });
		const handle = tui.showOverlay(viewer, {
			width: "100%",
			maxHeight: "70%",
			anchor: "bottom-center",
			margin: { bottom: 2 },
		});
		tui.start();
		tui.renderNow(true);
		const first = tui.getScreenLines().map((line) => stripTerminalSequences(line)).join("\n");
		assert.match(first, /Output.*terminal/);
		assert.match(first, /server truncated/);
		assert.match(first, /line 0/);
		assert.doesNotMatch(first, /line 59/);
		viewer.handleInput("\x1b[F");
		tui.renderNow(true);
		const last = tui.getScreenLines().map((line) => stripTerminalSequences(line)).join("\n");
		assert.match(last, /line 59/, "End reaches retained output end");
		viewer.handleInput("\x1b[H");
		tui.renderNow(true);
		assert.match(tui.getScreenLines().map((line) => stripTerminalSequences(line)).join("\n"), /line 0/);
		viewer.handleInput("\x1b[B");
		tui.renderNow(true);
		viewer.handleInput("\x1b");
		assert.equal(closed, 1, "Esc closes exactly once");
		handle.hide();
	} finally {
		tui.stop();
	}
});

test("O01 /output command routes bare and explicit forms without re-execution", async () => {
	const { outputCommand } = await import("../src/features/output.ts");
	assert.equal(outputCommand.name, "output");
	const inspected: Array<string | undefined> = [];
	const calls: string[] = [];
	const host = {
		async inspectOutput(toolId?: string) {
			inspected.push(toolId);
		},
		async request(method: string) {
			calls.push(method);
			throw new Error(`unexpected ${method}`);
		},
	};
	await outputCommand.run("", host as never);
	await outputCommand.run("  abcd1234  ", host as never);
	assert.deepEqual(inspected, [undefined, "abcd1234"]);
	assert.deepEqual(calls, [], "inspection never touches the backend");
	const { SLASH_COMMANDS } = await import("../src/features/index.ts");
	assert.ok(SLASH_COMMANDS.some((c) => c.name === "output"), "/output is registered");
	assert.ok((SLASH_COMMANDS.find((c) => c.name === "output")?.description ?? "").length);
});
