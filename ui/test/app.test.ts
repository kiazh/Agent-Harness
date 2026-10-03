import assert from "node:assert/strict";
import { test } from "node:test";
import type { Component, OverlayHandle, TUI } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
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
		return false;
	}
	setClearOnShrink(): void {}
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

test("cancel() sets running=false immediately, before the completion event arrives", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt — this sets running=true and sends prompt.submit
	const submitPromise = (app as any).prompt("hello");
	// Let the prompt.submit request go through
	await new Promise((r) => setTimeout(r, 10));

	// Now cancel — running should be false immediately
	(app as any).cancel();

	// The editor should be re-enabled right away (running=false)
	const editor = tui.children.find((c) => (c as any).disableSubmit !== undefined) as any;
	assert.equal(editor.disableSubmit, false, "editor is re-enabled immediately after cancel");

	// Footer status should be "ready" (not "working")
	const footer = tui.children.find((c) => (c as any).status !== undefined) as any;
	assert.equal(footer.status, "ready", "footer status is ready immediately after cancel");

	// Clean up: resolve the prompt.submit so the promise doesn't hang
	client.responses.set("prompt.submit", {});
	await submitPromise;
	await app.exit();
});

test("message.complete for a non-current session is still processed (in-flight turn tracking)", async () => {
	const { app, tui, client } = makeApp(SESSION_A);
	await app.start();

	// Submit a prompt on session A
	const submitPromise = (app as any).prompt("hello");
	await new Promise((r) => setTimeout(r, 10));

	// Switch to session B while the turn is still in flight
	client.responses.set("session.resume", { session: SESSION_B, history: [] });
	await (app as any).switchTo(SESSION_B, []);

	// Now the completion event arrives for session A (the old session)
	client.emit(
		ev({ type: "message.complete", text: "done", tokens: 42, iterations: 1, toolCalls: 0, cancelled: false }),
	);

	// The app should have processed it: running should be false
	const editor = tui.children.find((c) => (c as any).disableSubmit !== undefined) as any;
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
	const footer = tui.children.find((c) => (c as any).tokens !== undefined) as any;
	assert.equal(footer.tokens, 0, "no tokens added for event from session with no in-flight turn");

	await app.exit();
});

test("late events from a previous session do not enter the current transcript", async () => {
	const { app, client } = makeApp(SESSION_A);
	await app.start();
	await (app as unknown as { prompt: (text: string) => Promise<void> }).prompt("hello");
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
