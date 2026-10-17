import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { Component, OverlayHandle, SelectItem, TUI } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { JsonRpcChannel } from "../src/gateway.ts";
import type { GatewayClient } from "../src/gateway.js";
import type { GatewayEvent, SessionInfo } from "../src/protocol.js";

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
	started = false;
	stopped = false;
	renderCount = 0;
	mode = "fullscreen" as const;
	terminal = {} as TUI["terminal"];
	fullRedraws = 0;

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
	clearOnShrink = false;
	start(): void {
		this.started = true;
	}
	stop(): void {
		this.stopped = true;
	}
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
	responses = new Map<string, unknown>();

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		const response = this.responses.get(method);
		if (response === undefined) return Promise.reject(new Error(`unexpected request ${method}`));
		return Promise.resolve(response as T);
	}

	/** Simulate the gateway sending an event. */
	emit(event: GatewayEvent): void {
		this.onEvent?.(event);
	}
}

/**
 * GatewayClient stand-in backed by the real transport-agnostic channel, so
 * request-timeout semantics under fake timers are the production ones.
 */
class ChannelClient {
	readonly frames: Array<{ id: number; method: string; params: Record<string, unknown> }> = [];
	readonly channel = new JsonRpcChannel((line) => {
		this.frames.push(JSON.parse(line) as (typeof this.frames)[number]);
	});

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	request<T>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
		return this.channel.request<T>(method, params, timeoutMs);
	}

	/** Answer the most recent request frame with *result*. */
	reply(result: unknown): void {
		const frame = this.frames.at(-1)!;
		this.channel.handleLine(JSON.stringify({ jsonrpc: "2.0", id: frame.id, result }));
	}
}

function makeApp(session: SessionInfo) {
	const tui = new FakeTUI();
	const client = new FakeClient();
	client.responses.set("initialize", {
		version: "1.0.0",
		model: "m",
		provider: "openrouter",
		cwd: "/tmp",
		branch: "main",
	});
	client.responses.set("session.create", { session });
	client.responses.set("prompt.submit", {});
	const app = new App(tui, client as unknown as GatewayClient);
	return { app, tui, client };
}

const ev = (e: Record<string, unknown>) => ({ sessionId: SESSION_A.id, turnId: "t", ...e }) as GatewayEvent;

function widgetWith<K extends string>(tui: FakeTUI, property: K): Component & Record<K, unknown> {
	const direct = tui.children.find((child) => property in child);
	if (direct) return direct as Component & Record<K, unknown>;
	// The editor lives nested inside the composer box.
	for (const child of tui.children) {
		if (child && typeof child === "object" && "editor" in child) {
			const inner = (child as { editor: unknown }).editor;
			if (inner && typeof inner === "object" && property in inner) {
				return inner as Component & Record<K, unknown>;
			}
		}
	}
	assert.fail(`widget with ${property} exists`);
}

test("start() enables shrink-clearing so closed menus leave no ghost rows", async () => {
	const { app, tui } = makeApp(SESSION_A);
	await app.start();
	assert.equal(tui.clearOnShrink, true);
	await app.exit();
});

test("cancel() does not set running=false while the turn is still in-flight", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt — this sets running=true and sends prompt.submit
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	// Let the prompt.submit request go through
	await new Promise((r) => setTimeout(r, 10));

	// Now cancel — the turn is still in-flight, so running should stay true
	(app as unknown as { cancel: () => void }).cancel();

	// AH-031: the composer stays enabled during turns so safe commands work;
	// running/footer still reflect the in-flight turn.
	const editor = widgetWith(tui, "disableSubmit");
	assert.equal(editor.disableSubmit, false, "editor stays enabled for safe commands during turn");

	// Footer status should still be "working"
	const footer = widgetWith(tui, "status");
	assert.equal(footer.status, "working", "footer status stays working after cancel while turn is in-flight");

	// Clean up: resolve the prompt.submit so the promise doesn't hang
	client.responses.set("prompt.submit", {});
	await submitPromise;
	await app.exit();
});

test("cancelled turn stays tracked until completion before another prompt is submitted", async () => {
	const { app, client } = makeApp(SESSION_A);
	client.responses.set("prompt.cancel", {});
	await app.start();
	const prompt = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn.bind(app);
	await prompt("first");
	(app as unknown as { cancel: () => void }).cancel();
	await prompt("too soon");
	assert.equal(client.requests.filter((item) => item.method === "prompt.submit").length, 1);
	client.emit(ev({ type: "message.complete", text: "", tokens: 0, iterations: 0, toolCalls: 0, cancelled: true }));
	await prompt("after completion");
	assert.equal(client.requests.filter((item) => item.method === "prompt.submit").length, 2);
	await app.exit();
});

test("message.complete for a non-current session is still processed (in-flight turn tracking)", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt on session A
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	await new Promise((r) => setTimeout(r, 10));

	// Switch to session B while the turn is still in flight
	client.responses.set("session.resume", { session: SESSION_B, history: [] });
	app.switchTo(SESSION_B, []);

	// Now the completion event arrives for session A (the old session)
	client.emit(
		ev({ type: "message.complete", text: "done", tokens: 42, iterations: 1, toolCalls: 0, cancelled: false }),
	);

	// The app should have processed it: running should be false
	const editor = widgetWith(tui, "disableSubmit");
	assert.equal(editor.disableSubmit, false, "running is false after in-flight turn completes on old session");

	// Clean up
	client.responses.set("prompt.submit", {});
	await submitPromise;
	await app.exit();
});

test("events for sessions with no in-flight turn are ignored", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Emit an event for a session that has no in-flight turn
	client.emit(
		ev({ type: "message.complete", text: "done", tokens: 42, iterations: 1, toolCalls: 0, cancelled: false }),
	);

	// The app should NOT have processed it: running should still be false (it was never set)
	// and no tokens should have been added
	const footer = widgetWith(tui, "tokens");
	assert.equal(footer.tokens, 0, "no tokens added for event from session with no in-flight turn");

	await app.exit();
});

test("late events from a previous session do not enter the current transcript", async () => {
	const { app, client } = makeApp(SESSION_A);
	await app.start();
	await (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	app.switchTo(SESSION_B, []);

	const internal = app as unknown as { transcript: { apply: (event: GatewayEvent) => unknown } };
	let applied = 0;
	internal.transcript.apply = () => {
		applied++;
		return undefined;
	};
	client.emit(ev({ type: "message.delta", text: "from A" }));
	client.emit(ev({ type: "message.complete", text: "from A", tokens: 3, iterations: 1, toolCalls: 0, cancelled: false }));
	assert.equal(applied, 0);
	await app.exit();
});

// ─── Bug fix tests ─────────────────────────────────────────────────────────

test("onGatewayExit clears in-flight tracking so session is not permanently locked", async () => {
	const { app, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt — this adds to inFlight
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	await new Promise((r) => setTimeout(r, 10));

	// Simulate gateway exit
	client.onExit!(1, ["some error"]);

	// Now a new prompt should be accepted (inFlight was cleared)
	client.responses.set("prompt.submit", {});
	await submitPromise;

	const submitPromise2 = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("second");
	await new Promise((r) => setTimeout(r, 10));
	client.responses.set("prompt.submit", {});
	await submitPromise2;

	const submits = client.requests.filter((r) => r.method === "prompt.submit");
	assert.equal(submits.length, 2, "both prompts were submitted after gateway exit cleared in-flight");
	await app.exit();
});

test("cancel() targets the in-flight session, not the current session", async () => {
	const { app, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt on session A
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	await new Promise((r) => setTimeout(r, 10));

	// Switch to session B while A's turn is still in flight
	app.switchTo(SESSION_B, []);

	// Cancel — should target session A (the in-flight one), not B (the current one)
	(app as unknown as { cancel: () => void }).cancel();

	const cancels = client.requests.filter((r) => r.method === "prompt.cancel");
	assert.equal(cancels.length, 1, "one cancel request sent");
	assert.equal(cancels[0]!.params.sessionId, SESSION_A.id, "cancel targets the in-flight session A, not current session B");

	client.responses.set("prompt.submit", {});
	await submitPromise;
	await app.exit();
});

test("submit() does not clear editor when a turn is in-flight (lost input fix)", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt — this sets running=true and adds to inFlight
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("first");
	await new Promise((r) => setTimeout(r, 10));

	// Try to submit again — should be rejected, editor text preserved
	const editor = widgetWith(tui, "getText") as Component & Record<"getText", () => string>;
	// Set some text in the editor
	const editorWidget = widgetWith(tui, "setText") as Component & Record<"setText", (text: string) => void>;
	editorWidget.setText("second prompt");

	// Call submit directly
	await (app as unknown as { submit: (text: string) => Promise<void> }).submit("second prompt");

	// Editor text should still be "second prompt" (not cleared)
	assert.equal(editor.getText(), "second prompt", "editor text preserved when submit rejected due to in-flight");

	// Only one prompt.submit should have been sent
	const submits = client.requests.filter((r) => r.method === "prompt.submit");
	assert.equal(submits.length, 1, "only one prompt.submit sent");

	client.responses.set("prompt.submit", {});
	await submitPromise;
	await app.exit();
});

test("error event keeps in-flight until completion (AH-032)", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	await new Promise((r) => setTimeout(r, 10));

	// Emit an error event for the current session — turn stays in-flight
	// until the terminal message.complete (which carries final accounting
	// and tool-card cleanup). Clearing early would drop that completion.
	client.emit(ev({ type: "error", message: "something went wrong" }));

	// running stays true; composer stays enabled for safe commands (AH-031).
	const editor = widgetWith(tui, "disableSubmit");
	assert.equal(editor.disableSubmit, false, "editor stays enabled after error event");

	// A new prompt is still rejected while the turn is in-flight.
	client.responses.set("prompt.submit", {});
	await submitPromise;

	const submitPromise2 = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("second");
	await new Promise((r) => setTimeout(r, 10));
	// Still only one prompt.submit: the second was rejected as in-flight.
	let submits = client.requests.filter((r) => r.method === "prompt.submit");
	assert.equal(submits.length, 1, "second prompt blocked until terminal completion");
	await submitPromise2;

	// Terminal completion clears the turn; a new prompt is accepted.
	client.emit(
		ev({ type: "message.complete", text: "", tokens: 0, iterations: 0, toolCalls: 0, cancelled: false }),
	);
	const submitPromise3 = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("third");
	await new Promise((r) => setTimeout(r, 10));
	client.responses.set("prompt.submit", {});
	await submitPromise3;

	submits = client.requests.filter((r) => r.method === "prompt.submit");
	assert.equal(submits.length, 2, "prompt accepted after terminal completion");
	await app.exit();
});

test("growth.status quietly pulses the footer after retirement without transcript notices", async () => {
	const { app, client, tui } = makeApp(SESSION_A);
	await app.start();
	const internal = app as unknown as {
		transcript: { addNotice(text: string, kind?: unknown): void };
	};
	const notices: Array<[string, unknown]> = [];
	const orig = internal.transcript.addNotice.bind(internal.transcript);
	internal.transcript.addNotice = (text: string, kind?: unknown) => {
		notices.push([text, kind]);
		orig(text, kind);
	};
	const footer = widgetWith(tui, "tokens");
	const before = footer.render(80).join("\n");
	const rendersBefore = tui.renderCount;
	// No turn is in flight: background evidence still receives a quiet pulse.
	client.emit(ev({ type: "growth.status", stage: "consolidation_done", detail: "3 new memories" }));
	assert.equal(notices.length, 0);
	assert.notEqual(footer.render(80).join("\n"), before);
	assert.ok(tui.renderCount > rendersBefore, "an idle growth acknowledgment requests its first visible frame");
	for (let index = 0; index < 20; index++) client.emit(ev({ type: "growth.status", stage: "rsi_updated" }));
	assert.equal(notices.length, 0);
	// Another session's training never interrupts this transcript.
	client.emit({
		sessionId: SESSION_B.id,
		turnId: "t",
		type: "growth.status",
		stage: "consolidation_done",
		detail: "1 new memory",
	});
	assert.equal(notices.length, 0);
	await app.exit();
});

test("exit() calls the input listener disposer", async () => {
	const { app } = makeApp(SESSION_A);
	await app.start();

	// Track whether the disposer was called
	let disposerCalled = false;
	const originalDisposer = (app as unknown as { inputListenerDisposer?: () => void }).inputListenerDisposer;
	if (originalDisposer) {
		(app as unknown as { inputListenerDisposer: () => void }).inputListenerDisposer = () => {
			disposerCalled = true;
			originalDisposer();
		};
	}

	await app.exit();
	assert.equal(disposerCalled, true, "input listener disposer was called on exit");
});

test("local watchdog warns once and retains the guard (never ownership)", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { app, client } = makeApp(SESSION_A);
	await app.start();

	client.responses.set("prompt.submit", { turnId: "t" });
	const submitPromise = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("hello");
	await submitPromise;

	const internal = app as unknown as {
		inFlight: Set<string>;
		transcript: { apply: (event: GatewayEvent) => unknown };
	};
	// Exercise the real production watchdog callback with fake time.
	t.mock.timers.tick(300_000);
	assert.equal(internal.inFlight.has(SESSION_A.id), true, "guard retained after the diagnostic deadline");
	await submitPromise.catch(() => {});
	const second = (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("too soon");
	await second;
	assert.equal(
		client.requests.filter((item) => item.method === "prompt.submit").length,
		1,
		"no resubmission is advertised or accepted on clock alone",
	);

	// A second deadline never repeats the warning.
	let applied = 0;
	const origApply = internal.transcript.apply.bind(internal.transcript);
	internal.transcript.apply = (event) => {
		applied++;
		return origApply(event);
	};
	t.mock.timers.tick(300_000);
	assert.equal(applied, 0, "watchdog warns once and never fabricates turn events");

	// The matching completion still retires and accounts.
	client.emit(ev({ type: "message.start", turnId: "t" }));
	client.emit(ev({ type: "message.complete", text: "late", tokens: 11, iterations: 1, toolCalls: 0, cancelled: false }));
	client.responses.set("prompt.submit", { turnId: "t2" });
	await (app as unknown as { submitTurn: (text: string) => Promise<void> }).submitTurn("after");
	assert.equal(client.requests.filter((item) => item.method === "prompt.submit").length, 2);
	await app.exit();
});

test("writeFile creates new files but never overwrites existing ones", async () => {
	const { app } = makeApp(SESSION_A);
	const dir = await mkdtemp(join(tmpdir(), "machina-writefile-"));
	try {
		const existing = join(dir, "AGENTS.md");
		await writeFile(existing, "# important existing instructions\n", "utf8");
		await assert.rejects(
			() => app.writeFile(existing, "# replacement\n"),
			/Refusing to overwrite existing file/,
		);
		assert.equal(
			await readFile(existing, "utf8"),
			"# important existing instructions\n",
			"existing content must survive a refused write",
		);

		// Fresh files (including missing parent directories) still work.
		const fresh = join(dir, "nested", "notes.md");
		assert.equal(await app.writeFile(fresh, "created\n"), fresh);
		assert.equal(await readFile(fresh, "utf8"), "created\n");
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test("interop.run client clock outlasts the backend run deadline (H22)", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout"] });
	const tui = new FakeTUI();
	const client = new ChannelClient();
	const app = new App(tui, client as unknown as GatewayClient);

	// /harnesses passes the gateway-surfaced deadline as `timeout` seconds.
	const pending = app.request<{ ok: boolean }>("interop.run", {
		platform: "codex",
		prompt: "run a long task",
		timeout: 300,
	});
	const frame = client.frames[0]!;
	assert.equal(frame.method, "interop.run");

	let settled: unknown;
	void pending.then(
		() => { settled = "resolved"; },
		(error) => { settled = error; },
	);

	// The old fixed 120s budget aborted here while the harness kept running.
	t.mock.timers.tick(120_000);
	await Promise.resolve();
	assert.equal(settled, undefined, "must not abort before the backend deadline");

	// At the backend deadline the client is still waiting for the reply.
	t.mock.timers.tick(180_000);
	await Promise.resolve();
	assert.equal(settled, undefined, "must outlast the backend deadline");

	// The late-but-successful reply lands instead of being discarded.
	client.reply({ ok: true, exitCode: 0 });
	assert.deepEqual(await pending, { ok: true, exitCode: 0 });
});

test("interop.run without a reply still stops waiting on the client clock (H22)", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout"] });
	const tui = new FakeTUI();
	const client = new ChannelClient();
	const app = new App(tui, client as unknown as GatewayClient);

	const pending = app.request("interop.run", { platform: "codex", prompt: "x", timeout: 300 });
	const rejection = pending.catch((error: Error) => error);

	t.mock.timers.tick(300_000 + 60_000);
	const error = await rejection;
	assert.match((error as Error).message, /interop\.run/);
});

for (const command of ["/settings", "/config"]) {
	test(`${command} cannot change gateway defaults until the active turn stops`, async () => {
		const { app, client } = makeApp(SESSION_A);
		client.responses.set("prompt.cancel", {});
		client.responses.set("config.set", { key: "model", value: "new-model", model: "new-model", provider: "openrouter" });
		client.responses.set("session.setModel", {});
		await app.start();
		try {
			const actions = app as unknown as {
				submit(text: string): Promise<void>;
				submitTurn(text: string): Promise<void>;
				cancel(): void;
				pick(title: string, items: SelectItem[]): Promise<SelectItem | undefined>;
			};
			let menuCalls = 0;
			actions.pick = async (_title, items) => {
				menuCalls++;
				if (menuCalls === 1) return items.find((item) => item.value === "cat-chat");
				if (menuCalls === 2) return items.find((item) => item.value === "model");
				if (menuCalls === 3) return items[0];
				return undefined;
			};
			await actions.submitTurn("hello");
			await actions.submit(command);
			assert.equal(menuCalls, 0, "the settings menu stays closed during the turn");
			assert.ok(!client.requests.some((entry) => entry.method === "config.set"));

			actions.cancel();
			await actions.submit(command);
			assert.equal(menuCalls, 0, "a cancel request alone does not finish the turn");
			assert.ok(!client.requests.some((entry) => entry.method === "config.set"));

			client.emit(ev({ type: "message.complete", text: "", tokens: 0, iterations: 0, toolCalls: 0, cancelled: true }));
			await actions.submit(command);
			const writes = client.requests.filter((entry) => entry.method === "config.set");
			assert.deepEqual(writes.map((entry) => entry.params.key), ["provider", "model"]);
			assert.ok(writes.every((entry) => entry.params.persist === true));
			assert.ok(client.requests.some((entry) => entry.method === "session.setModel"));
		} finally {
			await app.exit();
		}
	});
}
