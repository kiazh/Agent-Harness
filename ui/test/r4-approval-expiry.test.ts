// R4-065 (round-4): approval expiry reaches the UI as a resolution, never as
// a live card. The broker/transport announce a dead deadline with a terminal
// status (approval.resolved, or a pause frame whose status is already
// terminal); the UI must retire the card and the awaiting_approval footer
// without issuing any decision RPC.
//
// Fake-terminal harness: the real App, a fake gateway client, and the real
// approval dialog plumbing. Render assertions are not needed here — dialog
// map + footer status + gateway requests are the observable contract.

import assert from "node:assert/strict";
import { test } from "node:test";
import type { Component, OverlayHandle, TUI } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, SessionInfo } from "../src/protocol.ts";

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
	autoSubmit: unknown = {};

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}
	request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		const response = method === "prompt.submit" ? this.autoSubmit : this.responses.get(method);
		if (response === undefined) return Promise.reject(new Error(`unexpected request ${method}`));
		return Promise.resolve(response as T);
	}
	emit(event: GatewayEvent): void {
		this.onEvent?.(event);
	}
	resolveCount(): number {
		return this.requests.filter((r) => r.method === "approvals.resolve").length;
	}
}

const ev = (sessionId: string, turnId: string, e: Record<string, unknown>) =>
	({ sessionId, turnId, ...e }) as GatewayEvent;

const approvalEvent = (
	sessionId: string,
	turnId: string,
	requestId = "req-1",
	extra: Record<string, unknown> = {},
) =>
	ev(sessionId, turnId, {
		type: "needs_approval",
		requestId,
		operation: "terminal",
		target: "host",
		argv: ["ls", "-la"],
		cwd: "/tmp",
		backend: "host",
		capabilities: ["exec"],
		...extra,
	});

function makeApp() {
	const tui = new FakeTUI();
	const client = new FakeClient();
	const app = new App(tui, client as unknown as GatewayClient);
	return { app, tui, client };
}

function internals(app: App) {
	return app as unknown as {
		approvalDialogs: Map<string, unknown>;
		pendingApprovals: Map<string, GatewayEvent>;
		footer: { status: string };
		submitTurn(text: string): Promise<void>;
	};
}

/** Call-through spy on transcript notices (records text + kind). */
function spyNotices(app: App): string[] {
	const notices: string[] = [];
	const transcript = (app as unknown as {
		transcript: { addNotice: (text: string, kind?: string) => void };
	}).transcript;
	const original = transcript.addNotice.bind(transcript);
	transcript.addNotice = (text, kind) => {
		notices.push(`${kind ?? "info"}:${text}`);
		return original(text, kind as never);
	};
	return notices;
}

async function runningTurn(app: App, client: FakeClient, turnId = "t"): Promise<void> {
	client.autoSubmit = { turnId };
	await internals(app).submitTurn("go");
	client.emit(ev(SESSION_A.id, turnId, { type: "message.start" }));
}

test("R4-065 a pause frame with a terminal status never opens a card", async () => {
	const { app, client } = makeApp();
	await app.start();
	await runningTurn(app, client);

	// The record was already expired when the frame was delivered: this is a
	// resolution, not a pending review.
	client.emit(approvalEvent(SESSION_A.id, "t", "req-1", { status: "expired" }));

	const state = internals(app);
	assert.equal(state.approvalDialogs.size, 0, "no card opens for an expired request");
	assert.equal(state.pendingApprovals.size, 0, "no stale review evidence is retained");
	assert.notEqual(state.footer.status, "awaiting_approval", "footer does not claim a pending approval");
	assert.equal(client.resolveCount(), 0, "no decision is sent for it");
	await app.exit();
});

test("R4-065 a terminal-status pause retires the open card and footer without a decision", async () => {
	const { app, client } = makeApp();
	await app.start();
	await runningTurn(app, client);
	const notices = spyNotices(app);

	client.emit(approvalEvent(SESSION_A.id, "t"));
	const state = internals(app);
	assert.equal(state.approvalDialogs.size, 1, "card opens for the visible bound session");
	assert.equal(state.footer.status, "awaiting_approval", "paused turn reads awaiting_approval");

	// The broker's deadline expired the request on another delivery path; the
	// redelivered pause frame carries the terminal status.
	client.emit(approvalEvent(SESSION_A.id, "t", "req-1", { status: "expired" }));

	assert.equal(state.approvalDialogs.size, 0, "card retires on the terminal status");
	assert.equal(state.pendingApprovals.size, 0, "review evidence is cleared");
	assert.notEqual(state.footer.status, "awaiting_approval", "footer leaves the pending state");
	assert.equal(client.resolveCount(), 0, "retirement issues no decision");
	assert.ok(
		notices.some((notice) => notice.startsWith("warning:Approval ") && notice.endsWith("expired.")),
		`an expiry notice is shown: ${notices.join(" | ")}`,
	);
	await app.exit();
});

test("R4-065 an approval.resolved expiry frame retires the card and footer", async () => {
	const { app, client } = makeApp();
	await app.start();
	await runningTurn(app, client);
	const notices = spyNotices(app);

	client.emit(approvalEvent(SESSION_A.id, "t"));
	const state = internals(app);
	assert.equal(state.approvalDialogs.size, 1, "card opens");
	assert.equal(state.footer.status, "awaiting_approval", "footer awaiting the decision");

	client.emit(ev(SESSION_A.id, "t", { type: "approval.resolved", requestId: "req-1", status: "expired" }));

	assert.equal(state.approvalDialogs.size, 0, "card retires on the resolution frame");
	assert.equal(state.pendingApprovals.size, 0, "evidence is cleared");
	assert.notEqual(state.footer.status, "awaiting_approval", "footer leaves the pending state");
	assert.equal(client.resolveCount(), 0, "no decision is sent");
	assert.ok(
		notices.some((notice) => notice === "warning:Approval req-1 expired."),
		`expiry notice shown: ${notices.join(" | ")}`,
	);
	await app.exit();
});

test("R4-065 a hidden terminal-status pause is not resurrected on return", async () => {
	const { app, client } = makeApp();
	await app.start();
	await runningTurn(app, client);
	app.switchTo(SESSION_B, []);

	client.emit(approvalEvent(SESSION_A.id, "t", "req-1", { status: "expired" }));

	const state = internals(app);
	assert.equal(state.pendingApprovals.size, 0, "terminal evidence is never retained");
	app.switchTo(SESSION_A, []);
	assert.equal(state.approvalDialogs.size, 0, "returning never rebuilds an expired card");
	assert.equal(client.resolveCount(), 0, "returning issues no decision");
	await app.exit();
});
