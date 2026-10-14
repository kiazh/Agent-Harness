// Session/turn-aware presentation state (spec section 4): routing by
// (sessionId, turnId), provisional pre-RPC binding, honest tool outcomes,
// final-text/usage reconciliation, and the local watchdog.
// App-level tests use public input + fake gateway emission; transcript-level
// tests render a plain Container. Apply-call spies always call through to
// production code.

import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
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


const { app, client } = makeApp();
await app.start();
client.autoSubmit = { turnId: "t" };
await app.submitTurn("audit action");
client.emit({ type: "message.start", sessionId: SESSION_A.id, turnId: "t" });
client.emit({ type: "needs_approval", sessionId: SESSION_A.id, turnId: "t", requestId: "req-1", operation: "terminal", target: "host", argv: ["echo", "test"], cwd: "/tmp", backend: "host", capabilities: ["exec"] });
const state = app as unknown as { approvalDialogs: Map<string, unknown>, footer: { status: string }, inFlight: Set<string> };
console.log(JSON.stringify({ phase: "approval received", cards: state.approvalDialogs.size, status: state.footer.status }));
app.switchTo(SESSION_B, []);
app.switchTo(SESSION_A, []);
console.log(JSON.stringify({ phase: "returned to awaiting session", cards: state.approvalDialogs.size, status: state.footer.status, stillRunning: state.inFlight.has(SESSION_A.id), recoveryQueries: client.requests.filter(r => r.method === "approvals.list").length }));
await app.exit();
