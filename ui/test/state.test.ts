// Session/turn-aware presentation state (spec section 4): routing by
// (sessionId, turnId), provisional pre-RPC binding, honest tool outcomes,
// final-text/usage reconciliation, and the local watchdog.
// App-level tests use public input + fake gateway emission; transcript-level
// tests render a plain Container. Apply-call spies always call through to
// production code.

import assert from "node:assert/strict";
import { test } from "node:test";
import { Container, stripTerminalSequences, type Component, type OverlayHandle, type TUI } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, SessionInfo } from "../src/protocol.ts";
import { Transcript } from "../src/transcript.ts";

const SESSION_A: SessionInfo = {
	id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
	title: "session A",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const SESSION_B: SessionInfo = {
	id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
	title: "session B",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

class FakeTUI implements TUI {
	children: Component[] = [];
	focused: Component | null = null;
	overlay: OverlayHandle | undefined;
	inputListeners: Array<(data: string) => void> = [];
	renderCount = 0;
	mode = "fullscreen" as const;
	terminal = {} as TUI["terminal"];
	fullRedraws = 0;
	clearOnShrink = false;

	addChild(c: Component): void {
		this.children.push(c);
	}
	removeChild(c: Component): void {
		const i = this.children.indexOf(c);
		if (i >= 0) this.children.splice(i, 1);
	}
	clear(): void {
		this.children = [];
	}
	setFocus(c: Component | null): void {
		this.focused = c;
	}
	showOverlay(_c: Component): OverlayHandle {
		const handle: OverlayHandle = {
			hide: () => {},
			setHidden: () => {},
			isHidden: () => false,
			focus: () => {},
			unfocus: () => {},
			isFocused: () => false,
			getBounds: () => ({ row: 0, col: 0, width: 80, height: 24 }),
		};
		this.overlay = handle;
		return handle;
	}
	hideOverlay(): void {
		this.overlay = undefined;
	}
	hasOverlay(): boolean {
		return this.overlay !== undefined;
	}
	start(): void {}
	stop(): void {}
	requestRender(): void {
		this.renderCount++;
	}
	addInputListener(listener: (data: string) => void): () => void {
		this.inputListeners.push(listener);
		return () => {};
	}
	removeInputListener(): void {}
	onTerminalColorSchemeChange(): () => void {
		return () => {};
	}
	setTerminalColorSchemeNotifications(): void {}
	queryTerminalColors(): Promise<never> {
		throw new Error("not implemented");
	}
	render(): string[] {
		return [];
	}
	invalidate(): void {}
	handleMouse(): undefined {
		return undefined;
	}
	getShowHardwareCursor(): boolean {
		return false;
	}
	setShowHardwareCursor(): void {}
	getClearOnShrink(): boolean {
		return this.clearOnShrink;
	}
	setClearOnShrink(enabled: boolean): void {
		this.clearOnShrink = enabled;
	}
	renderNow(): void {}
}

class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	responses = new Map<string, unknown>([
		["initialize", { version: "1.0.0", model: "m", provider: "openrouter", cwd: "/tmp", branch: "main" }],
		["session.create", { session: SESSION_A }],
		["prompt.cancel", {}],
	]);
	/** Immediate prompt.submit response; undefined defers until resolveSubmit. */
	autoSubmit: unknown = {};
	/** When set, prompt.submit rejects (pre-acceptance failure). */
	submitError: unknown = undefined;
	pendingSubmits: Array<(value: unknown) => void> = [];

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		if (method === "prompt.submit" && this.submitError !== undefined) {
			return Promise.reject(this.submitError);
		}
		if (method === "prompt.submit" && this.autoSubmit === undefined) {
			return new Promise<T>((resolve) => {
				this.pendingSubmits.push(resolve as (value: unknown) => void);
			});
		}
		const response = method === "prompt.submit" ? this.autoSubmit : this.responses.get(method);
		if (response === undefined) return Promise.reject(new Error(`unexpected request ${method}`));
		return Promise.resolve(response as T);
	}

	emit(event: GatewayEvent): void {
		this.onEvent?.(event);
	}

	resolveSubmit(value: unknown): void {
		this.pendingSubmits.shift()?.(value);
	}

	submitCount(): number {
		return this.requests.filter((r) => r.method === "prompt.submit").length;
	}

	resolveCount(): number {
		return this.requests.filter((r) => r.method === "approvals.resolve").length;
	}
}

function makeApp() {
	const tui = new FakeTUI();
	const client = new FakeClient();
	const app = new App(tui, client as unknown as GatewayClient);
	return { app, tui, client };
}

const ev = (sessionId: string, turnId: string, e: Record<string, unknown>) =>
	({ sessionId, turnId, ...e }) as GatewayEvent;

/** Call-through spy on transcript routing + notices. */
function spyTranscript(app: App): { applied: string[]; notices: string[] } {
	const applied: string[] = [];
	const notices: string[] = [];
	const internal = app as unknown as {
		transcript: {
			apply: (event: GatewayEvent) => unknown;
			addNotice: (text: string, kind?: string) => void;
		};
	};
	const origApply = internal.transcript.apply.bind(internal.transcript);
	internal.transcript.apply = (event) => {
		applied.push(`${event.type}:${event.turnId}`);
		return origApply(event);
	};
	const origNotice = internal.transcript.addNotice.bind(internal.transcript);
	internal.transcript.addNotice = (text, kind) => {
		notices.push(`${kind ?? "info"}:${text}`);
		return origNotice(text, kind as never);
	};
	return { applied, notices };
}

function widgetWith(tui: FakeTUI, property: string): Record<string, unknown> {
	const direct = tui.children.find((child) => child && typeof child === "object" && property in child);
	if (direct) return direct as unknown as Record<string, unknown>;
	for (const child of tui.children) {
		if (child && typeof child === "object" && "editor" in child) {
			const inner = (child as { editor: unknown }).editor;
			if (inner && typeof inner === "object" && property in inner) return inner as Record<string, unknown>;
		}
	}
	assert.fail(`widget with ${property} exists`);
}

async function submitTurn(app: App, text: string): Promise<void> {
	await (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn(text);
}

// ─── S01: cross-session isolation + accounting ──────────────────────────────

test("S01 non-visible session events never bleed but still retire and account", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "tA" };
	await submitTurn(app, "work on A");
	const spy = spyTranscript(app);
	app.switchTo(SESSION_B, []);
	spy.applied.length = 0;
	spy.notices.length = 0;

	client.emit(ev(SESSION_A.id, "tA", { type: "message.delta", text: "from A" }));
	client.emit(ev(SESSION_A.id, "tA", { type: "tool.start", id: "tool-1", name: "terminal", args: {} }));
	client.emit(ev(SESSION_A.id, "tA", { type: "tool.complete", id: "tool-1", name: "terminal", result: "ok", isError: false }));
	client.emit(
		ev(SESSION_A.id, "tA", {
			type: "needs_approval",
			requestId: "req-1",
			operation: "terminal",
			target: "host",
			argv: ["ls"],
			cwd: "/tmp",
			backend: "host",
			capabilities: [],
		}),
	);
	client.emit(ev(SESSION_A.id, "tA", { type: "usage", tokens: 15 }));
	assert.deepEqual(spy.applied, [], "no A event reaches the visible transcript");
	assert.deepEqual(spy.notices, [], "no A notice (including approval card) bleeds into B");
	assert.equal(tui.overlay, undefined, "no approval dialog opens for the non-visible session");
	assert.equal(client.resolveCount(), 0, "no approval decision leaves the wrong session");

	client.emit(
		ev(SESSION_A.id, "tA", { type: "message.complete", text: "done A", tokens: 42, iterations: 1, toolCalls: 1, cancelled: false }),
	);
	assert.deepEqual(
		spy.applied,
		[],
		"non-visible completion retires without rendering",
	);

	// Follow-up on A is accepted (retired) and carries the accounted usage.
	app.switchTo(SESSION_A, []);
	assert.equal(widgetWith(tui, "tokens")["tokens"], 42, "non-visible completion still accounts usage");
	client.autoSubmit = { turnId: "tA2" };
	await submitTurn(app, "follow-up");
	assert.equal(client.submitCount(), 2);
	await app.exit();
});

// ─── S02: stale turn in the same session ────────────────────────────────────

test("S02 stale events from an earlier turn mutate nothing", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	const spy = spyTranscript(app);
	client.autoSubmit = { turnId: "t1" };
	await submitTurn(app, "first");
	client.emit(ev(SESSION_A.id, "t1", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t1", { type: "message.delta", text: "one" }));
	client.emit(ev(SESSION_A.id, "t1", { type: "message.complete", text: "one", tokens: 7, iterations: 1, toolCalls: 0, cancelled: false }));
	assert.equal(widgetWith(tui, "tokens")["tokens"], 7);
	spy.applied.length = 0;

	client.autoSubmit = { turnId: "t2" };
	await submitTurn(app, "second");
	client.emit(ev(SESSION_A.id, "t2", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t1", { type: "message.delta", text: "stale" }));
	client.emit(ev(SESSION_A.id, "t1", { type: "usage", tokens: 999 }));
	client.emit(
		ev(SESSION_A.id, "t1", { type: "message.complete", text: "stale", tokens: 999, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.deepEqual(spy.applied, ["message.start:t2"], "only the new turn reaches the transcript");
	assert.equal(widgetWith(tui, "tokens")["tokens"], 7, "stale usage/completion add nothing");
	await app.exit();
});

// ─── S03: events before the submit RPC resolves ─────────────────────────────

test("S03 pre-RPC events bind provisionally and resolve once", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	const spy = spyTranscript(app);
	client.autoSubmit = undefined;
	const pending = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("task");
	await Promise.resolve();
	client.emit(ev(SESSION_A.id, "t3", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t3", { type: "message.delta", text: "early" }));
	client.emit(ev(SESSION_A.id, "t3", { type: "usage", tokens: 5 }));
	client.emit(
		ev(SESSION_A.id, "t3", { type: "message.complete", text: "early", tokens: 5, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	client.resolveSubmit({ turnId: "t3" });
	await pending;
	assert.equal(widgetWith(tui, "tokens")["tokens"], 5, "output/completion retained once");
	assert.equal(spy.applied.filter((a) => a === "message.complete:t3").length, 1);
	client.autoSubmit = { turnId: "t4" };
	await submitTurn(app, "next");
	assert.equal(client.submitCount(), 2, "retired turn frees the session");
	await app.exit();
});

test("S03 mismatched submit identity keeps the guard with a protocol error", async () => {
	const { app, client } = makeApp();
	await app.start();
	const spy = spyTranscript(app);
	client.autoSubmit = undefined;
	const pending = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("task");
	await Promise.resolve();
	client.emit(ev(SESSION_A.id, "t3", { type: "message.start" }));
	client.resolveSubmit({ turnId: "OTHER" });
	await pending;
	assert.ok(
		spy.notices.some((n) => /protocol/i.test(n)),
		"ID disagreement surfaces a sanitized protocol error",
	);
	await submitTurn(app, "too soon");
	assert.equal(client.submitCount(), 1, "unknown owner retains the guard; no blind retry");
	await app.exit();
});

// ─── S04: duplicate start/complete ──────────────────────────────────────────

test("S04 duplicate start and completion apply exactly once", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	const spy = spyTranscript(app);
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "hi");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t", { type: "message.delta", text: "Hello" }));
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t", { type: "message.complete", text: "Hello", tokens: 9, iterations: 1, toolCalls: 0, cancelled: false }));
	client.emit(ev(SESSION_A.id, "t", { type: "message.complete", text: "Hello", tokens: 9, iterations: 1, toolCalls: 0, cancelled: false }));
	assert.equal(spy.applied.filter((a) => a === "message.start:t").length, 1, "no transcript reset on duplicate start");
	assert.equal(spy.applied.filter((a) => a === "message.complete:t").length, 1, "no duplicate terminal summary");
	assert.equal(widgetWith(tui, "tokens")["tokens"], 9, "no double-counted usage");
	await app.exit();
});

// ─── S06: error does not retire; cleanup is static ──────────────────────────

test("S06 error then cleanup retires only on matching completion", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t", { type: "error", message: "boom" }));
	await submitTurn(app, "too soon");
	assert.equal(client.submitCount(), 1, "error alone never releases the guard");
	client.emit(ev(SESSION_A.id, "t", { type: "turn.cleanup_pending", reason: "retiring" }));
	await submitTurn(app, "still too soon");
	assert.equal(client.submitCount(), 1, "cleanup is static, not ready");
	client.emit(
		ev(SESSION_A.id, "t", { type: "message.complete", text: "recovered", tokens: 7, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.equal(widgetWith(tui, "tokens")["tokens"], 7, "only the matching completion retires and accounts");
	await submitTurn(app, "after");
	assert.equal(client.submitCount(), 2);
	await app.exit();
});

// ─── S10: cumulative usage reconciles once ──────────────────────────────────

test("S10 cumulative usage frames add the turn total once", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(ev(SESSION_A.id, "t", { type: "usage", tokens: 10 }));
	assert.equal(widgetWith(tui, "tokens")["tokens"], 10);
	client.emit(ev(SESSION_A.id, "t", { type: "usage", tokens: 30 }));
	assert.equal(widgetWith(tui, "tokens")["tokens"], 30);
	client.emit(
		ev(SESSION_A.id, "t", { type: "message.complete", text: "done", tokens: 30, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.equal(widgetWith(tui, "tokens")["tokens"], 30, "final total added once, not summed with frames");
	client.emit(
		ev(SESSION_A.id, "t", { type: "message.complete", text: "done", tokens: 30, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.equal(widgetWith(tui, "tokens")["tokens"], 30, "duplicate final adds zero");
	await app.exit();
});

// ─── Transcript-level honesty (S07–S09) ─────────────────────────────────────

class FakeSpinner implements Component {
	running = false;
	start(): void {
		this.running = true;
	}
	stop(): void {
		this.running = false;
	}
	render(): string[] {
		return ["(spinner)"];
	}
	invalidate(): void {}
}

const tev = (e: Record<string, unknown>) => ({ sessionId: "s", turnId: "t", ...e }) as GatewayEvent;

function textOf(view: Container, width = 60): string {
	return view.children
		.flatMap((child) => (child as Component).render(width))
		.map((line) => stripTerminalSequences(line))
		.join("\n");
}

test("S07 missing tool completion at normal retirement is unknown, never success", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.start" }));
	transcript.apply(tev({ type: "tool.start", id: "1", name: "terminal", args: { command: "npm test" } }));
	transcript.apply(tev({ type: "message.complete", text: "", tokens: 4, iterations: 1, toolCalls: 1, cancelled: false }));
	const out = textOf(view);
	assert.match(out, /completion not reported/);
	assert.doesNotMatch(out, /✔ terminal/, "missing completion is never success");
});

test("S07 known tool results survive alongside unknown ones", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.start" }));
	transcript.apply(tev({ type: "tool.start", id: "1", name: "read_file", args: { path: "a" } }));
	transcript.apply(tev({ type: "tool.complete", id: "1", name: "read_file", result: "ok", isError: false }));
	transcript.apply(tev({ type: "tool.start", id: "2", name: "terminal", args: {} }));
	transcript.apply(tev({ type: "message.complete", text: "", tokens: 4, iterations: 1, toolCalls: 2, cancelled: false }));
	const out = textOf(view);
	assert.match(out, /✔ read_file/, "completed tool keeps its reported success");
	assert.match(out, /completion not reported/);
});

test("S08 cancel keeps success and marks the unresolved interrupted", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.start" }));
	transcript.apply(tev({ type: "tool.start", id: "1", name: "read_file", args: {} }));
	transcript.apply(tev({ type: "tool.complete", id: "1", name: "read_file", result: "ok", isError: false }));
	transcript.apply(tev({ type: "tool.start", id: "2", name: "terminal", args: {} }));
	transcript.apply(tev({ type: "message.complete", text: "", tokens: 1, iterations: 1, toolCalls: 2, cancelled: true }));
	const out = textOf(view);
	assert.match(out, /✔ read_file/, "known success survives cancellation");
	assert.match(out, /interrupted/, "unresolved tool is honestly interrupted");
	assert.doesNotMatch(out, /✔ terminal/, "interrupted tool is never success");
});

test("S09 incompatible canonical final replaces instead of duplicating", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.start" }));
	transcript.apply(tev({ type: "message.delta", text: "Hello world" }));
	transcript.apply(tev({ type: "message.complete", text: "Goodbye", tokens: 2, iterations: 1, toolCalls: 0, cancelled: false }));
	const out = textOf(view);
	assert.match(out, /Goodbye/);
	assert.doesNotMatch(out, /Hello world/, "discarded deltas are not exposed");
	assert.equal(out.match(/Goodbye/g)?.length, 1, "canonical text appears exactly once");
});

test("S09 duplicate completion at transcript level applies once", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.start" }));
	transcript.apply(tev({ type: "message.delta", text: "Hi" }));
	const first = transcript.apply(tev({ type: "message.complete", text: "Hi", tokens: 3, iterations: 1, toolCalls: 0, cancelled: false }));
	const second = transcript.apply(
		tev({ type: "message.complete", text: "Hi", tokens: 3, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.deepEqual(first, { tokens: 3, cancelled: false });
	assert.equal(second, undefined, "duplicate completion is inert");
	assert.equal(textOf(view).match(/Hi/g)?.length, 1);
});

test("S09 stopping hides activity but keeps the guard", () => {
	const view = new Container();
	const spinner = new FakeSpinner();
	const transcript = new Transcript(view, spinner);
	transcript.apply(tev({ type: "message.start" }));
	assert.equal(spinner.running, true);
	transcript.apply(tev({ type: "message.stopping", reason: "cancelled" }));
	assert.equal(spinner.running, false, "stopping shows a static status, not dots");
});

test("replay is neutral when typed outcomes are unavailable", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.replay([{ role: "tool", tool: "terminal", content: "Error: boom" }]);
	const out = textOf(view);
	assert.match(out, /recorded output/);
	assert.doesNotMatch(out, /✖ terminal/, "history text never manufactures failure");
	assert.doesNotMatch(out, /✔ terminal/, "history text never manufactures success");
});

test("wire truncation is preserved on tools", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.start" }));
	transcript.apply(tev({ type: "tool.start", id: "1", name: "terminal", args: {} }));
	transcript.apply(tev({ type: "tool.complete", id: "1", name: "terminal", result: "ok", isError: false, truncated: true }));
	assert.match(textOf(view), /server output truncated/);
});

test("failed submit before acceptance restores the draft", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	client.submitError = new Error("gateway exploded");
	const editor = widgetWith(tui, "setText") as { setText(text: string): void; getText(): string };
	editor.setText("hello draft");
	await (app as unknown as { submit(text: string): Promise<void> }).submit("hello draft");
	assert.equal(editor.getText(), "hello draft", "pre-acceptance failure restores the draft");
	client.submitError = undefined;
	client.autoSubmit = { turnId: "t2" };
	await submitTurn(app, "too soon");
	assert.equal(client.submitCount(), 2, "released guard accepts new work");
	await app.exit();
});

test("I06 secret commands never enter transcript echo or command history", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	client.responses.set("secrets.set", { key: "K", set: true, persisted: true, envFile: "/tmp/.env" });
	const submit = (app as unknown as { submit(text: string): Promise<void> }).submit.bind(app);
	await submit("/keys set K SENTINEL-history-value");
	await submit("/sessions");
	const editor = widgetWith(tui, "addToHistory") as unknown as { history: string[] };
	assert.ok(!editor.history.some((h) => h.includes("SENTINEL")), "secret commands excluded from history");
	assert.ok(editor.history.some((h) => h.includes("/sessions")), "ordinary commands still recorded");
	await app.exit();
});

const approvalEvent = (sessionId: string, turnId: string, requestId = "req-1") =>
	ev(sessionId, turnId, {
		type: "needs_approval",
		requestId,
		operation: "terminal",
		target: "host",
		argv: ["ls", "-la"],
		cwd: "/tmp",
		backend: "host",
		capabilities: ["exec"],
	});

function approvalCard(app: App): { handleInput(data: string): void } {
	const entry = (
		app as unknown as { approvalDialogs: Map<string, { card: { handleInput(data: string): void } }> }
	).approvalDialogs.get("req-1");
	assert.ok(entry, "approval dialog is open");
	return entry.card;
}

test("A03 session switch while approval is open sends no decision", async () => {
	const { app, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(approvalEvent(SESSION_A.id, "t"));
	assert.equal(
		(app as unknown as { approvalDialogs: Map<string, unknown> }).approvalDialogs.size,
		1,
		"card opens for the visible bound session",
	);
	app.switchTo(SESSION_B, []);
	assert.equal(
		(app as unknown as { approvalDialogs: Map<string, unknown> }).approvalDialogs.size,
		0,
		"switch invalidates the card",
	);
	assert.equal(client.resolveCount(), 0, "invalidation issues no resolve RPC");
	await app.exit();
});

test("A03 late approval request from another session never opens", async () => {
	const { app, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	app.switchTo(SESSION_B, []);
	client.emit(approvalEvent(SESSION_A.id, "t"));
	assert.equal(
		(app as unknown as { approvalDialogs: Map<string, unknown> }).approvalDialogs.size,
		0,
		"no wrong-session dialog",
	);
	assert.equal(client.resolveCount(), 0);
	await app.exit();
});

test("A04 deliberate Allow sends one bound resolve, Deny is the safe default", async () => {
	const { app, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	client.responses.set("approvals.resolve", {});
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(approvalEvent(SESSION_A.id, "t"));
	const card = approvalCard(app);
	card.handleInput("\x1b[A");
	card.handleInput("\x1b[A");
	card.handleInput("\r");
	card.handleInput("\r");
	await Promise.resolve();
	await Promise.resolve();
	const resolves = client.requests.filter((r) => r.method === "approvals.resolve");
	assert.equal(resolves.length, 1, "duplicate Enter causes one RPC");
	assert.deepEqual(resolves[0]!.params, {
		requestId: "req-1",
		sessionId: SESSION_A.id,
		verdict: "approved",
		grant: "once",
	});
	await app.exit();
});

test("A04 Esc denies explicitly and failures stay recoverable", async () => {
	const { app, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(approvalEvent(SESSION_A.id, "t"));
	approvalCard(app).handleInput("\x1b");
	await Promise.resolve();
	await Promise.resolve();
	const resolves = client.requests.filter((r) => r.method === "approvals.resolve");
	assert.equal(resolves.length, 1, "Esc issues an explicit denial");
	assert.deepEqual(resolves[0]!.params, {
		requestId: "req-1",
		sessionId: SESSION_A.id,
		verdict: "denied",
		grant: "once",
	});
	await app.exit();
});

test("A04 rejected resolve keeps the card with a recoverable error", async () => {
	const { app, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(approvalEvent(SESSION_A.id, "t"));
	const card = approvalCard(app);
	card.handleInput("\r");
	await Promise.resolve();
	await Promise.resolve();
	assert.equal(
		(app as unknown as { approvalDialogs: Map<string, unknown> }).approvalDialogs.size,
		1,
		"rejection leaves the decision open, not a success state",
	);
	client.responses.set("approvals.resolve", {});
	card.handleInput("\r");
	await Promise.resolve();
	await Promise.resolve();
	assert.equal(client.requests.filter((r) => r.method === "approvals.resolve").length, 2, "retry works after failure");
	client.emit(ev(SESSION_A.id, "t", { type: "approval.resolved", requestId: "req-1", status: "approved" }));
	assert.equal(
		client.requests.filter((r) => r.method === "approvals.resolve").length,
		2,
		"recorded approval alone claims no execution",
	);
	await app.exit();
});

test("A03 disconnect retires the local prompt without resolving", async () => {
	const { app, client } = makeApp();
	await app.start();
	client.autoSubmit = { turnId: "t" };
	await submitTurn(app, "go");
	client.emit(ev(SESSION_A.id, "t", { type: "message.start" }));
	client.emit(approvalEvent(SESSION_A.id, "t"));
	assert.equal(
		(app as unknown as { approvalDialogs: Map<string, unknown> }).approvalDialogs.size,
		1,
	);
	client.onExit?.(1, ["boom"]);
	assert.equal(
		(app as unknown as { approvalDialogs: Map<string, unknown> }).approvalDialogs.size,
		0,
		"disconnect retires the prompt",
	);
	assert.equal(client.resolveCount(), 0, "no surprise resolve during shutdown");
	await app.exit();
});

test("cleanup freezes tool activity without reporting success or releasing ownership", (t) => {
 t.mock.timers.enable({apis:["Date"], now:1000});
 const view = new Container();
 const transcript = new Transcript(view, new FakeSpinner());
 transcript.apply(tev({type:"tool.start", id:"one", name:"terminal", args:{command:"long-job"}}));
 transcript.suspend("cleanup_pending");
 const before = textOf(view);
 assert.match(before, /cleanup pending; outcome pending/);
 t.mock.timers.tick(3000);
 assert.equal(textOf(view), before);
 transcript.apply(tev({type:"tool.complete", id:"one", name:"terminal", result:"finished", isError:false}));
 assert.match(textOf(view), /✔ terminal/);
});

test("typed unknown tool completion stays uncertain regardless of output or error flag", () => {
 for (const isError of [false, true]) {
  const view = new Container();
  const transcript = new Transcript(view, new FakeSpinner());
  transcript.apply(tev({ type: "tool.start", id: "unknown", name: "terminal", args: { command: "publish" } }));
  const complete = { type: "tool.complete" as const, id: "unknown", name: "terminal", result: "successfully published", isError, outcome: "outcome_unknown" };
  transcript.apply(tev(complete));
  assert.match(textOf(view), /\? terminal/);
  assert.match(textOf(view), /completion not reported/);
  assert.doesNotMatch(textOf(view), /✔ terminal|✖ terminal/);
 }
});

test("legacy tool completion retains typed success and error flags without inferring from text", () => {
 for (const isError of [false, true]) {
  const view = new Container();
  const transcript = new Transcript(view, new FakeSpinner());
  transcript.apply(tev({ type: "tool.start", id: "legacy", name: "terminal", args: {} }));
  transcript.apply(tev({ type: "tool.complete", id: "legacy", name: "terminal", result: "outcome unknown", isError }));
  assert.match(textOf(view), isError ? /✖ terminal/ : /✔ terminal/);
 }
});
