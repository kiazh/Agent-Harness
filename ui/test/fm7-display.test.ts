// FM7 display/state regression tests (RED-first for the wave-4 fixes):
// W1 case-normalized /keys history guard, W4 canonical final after clear,
// W5 bounded transcript + bound per-session maps, W6 pagination bounds.
//
// The App harness mirrors state.test.ts (a minimal fake TUI + gateway) so the
// tests exercise the real submit path and the real feature commands.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	Container,
	stripTerminalSequences,
	type Component,
	type OverlayHandle,
	type TUI,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, SessionInfo } from "../src/protocol.ts";
import { Transcript } from "../src/transcript.ts";
import { sessionCommands } from "../src/features/sessions.ts";
import { jobsCommand } from "../src/features/jobs.ts";
import type { Command, FeatureHost } from "../src/features/types.ts";

// ─── minimal App harness (mirrors state.test.ts, kept local) ────────────────

class FakeTUI implements TUI {
	children: Component[] = [];
	focused: Component | null = null;
	overlay: OverlayHandle | undefined;
	inputListeners: Array<(data: string) => void> = [];
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
	start(): void {}
	stop(): void {}
	requestRender(): void {}
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
	responses = new Map<string, unknown>([
		["initialize", { version: "1.0.0", model: "m", provider: "openrouter", cwd: "/tmp", branch: "main" }],
		["session.create", { session: SESSION_A }],
		["secrets.set", { key: "OPENROUTER_API_KEY", set: true, persisted: false, envFile: null }],
		["secrets.list", { secrets: [], envFile: "/tmp/.env" }],
	]);

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	async request<T>(method: string, _params: Record<string, unknown> = {}): Promise<T> {
		const response = this.responses.get(method);
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		return response as T;
	}

	emit(event: GatewayEvent): void {
		this.onEvent?.(event);
	}
}

const SESSION_A: SessionInfo = {
	id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
	title: "session A",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

function sessionInfo(id: string): SessionInfo {
	return { ...SESSION_A, id, title: id };
}

function makeApp() {
	const tui = new FakeTUI();
	const client = new FakeClient();
	const app = new App(tui, client as unknown as GatewayClient);
	return { app, tui, client };
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

// ─── transcript helpers (mirror state.test.ts) ──────────────────────────────

class FakeSpinner implements Component {
	start(): void {}
	stop(): void {}
	render(): string[] {
		return ["(spinner)"];
	}
	invalidate(): void {}
}

const tev = (e: Record<string, unknown>) => ({ sessionId: "s", turnId: "t", ...e }) as GatewayEvent;

function textOf(view: Container, width = 100): string {
	return view.children
		.flatMap((child) => (child as Component).render(width))
		.map((line) => stripTerminalSequences(line))
		.join("\n");
}

// ─── W1: /KEYS SET must never enter input history ───────────────────────────

test("W1 uppercase /KEYS SET never enters editor history", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	client.responses.set("secrets.list", { secrets: [], envFile: "/tmp/.env" });
	const submit = (app as unknown as { submit(text: string): Promise<void> }).submit.bind(app);
	await submit("/KEYS SET OPENROUTER_API_KEY SENTINEL-uppercase");
	await submit("/keys SET OPENROUTER_API_KEY SENTINEL-mixed");
	await submit("/keys LIST");
	const editor = widgetWith(tui, "addToHistory") as unknown as { history: string[] };
	assert.ok(!editor.history.some((h) => h.includes("SENTINEL")), "secret values never enter history");
	assert.ok(editor.history.some((h) => h.includes("/keys LIST")), "non-set subcommands are still recorded");
	await app.exit();
});

// ─── W4: clear() must reset the canonical-final guard ───────────────────────

test("W4 canonical final text is rendered after a clear between delta and complete", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(tev({ type: "message.delta", text: "streamed prefix " }));
	transcript.clear();
	transcript.apply(
		tev({ type: "message.complete", text: "canonical final answer", tokens: 5, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	const out = textOf(view);
	assert.match(out, /canonical final answer/, "AH-AUDIT-027 canonical text survives clear()");
	assert.equal(out.match(/canonical final answer/g)?.length, 1, "canonical text appears exactly once");
});

// ─── W5: bounded transcript + per-session maps ──────────────────────────────

test("W5 transcript scrollback is capped and marked with a trim notice", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	for (let i = 0; i < 2050; i++) transcript.addNotice(`notice ${i}`);
	assert.ok(view.children.length <= 2000, `children bounded to <= 2000, got ${view.children.length}`);
	const out = textOf(view);
	assert.match(out, /scrollback trimmed/i, "trim event is disclosed");
	assert.match(out, /notice 2049/, "newest block is retained");
	assert.doesNotMatch(out, /notice 0\b/, "oldest block was dropped");
});

test("W5 dormant per-session state is pruned with an LRU bound", () => {
	const { app } = makeApp();
	const internal = app as unknown as {
		turns: Map<string, unknown>;
		sessionTokensById: Map<string, number>;
		inFlight: Set<string>;
		sessionTokens: number;
	};
	// Oldest session: in flight, so it must never be evicted.
	const active = sessionInfo("aaaaaaaa-0000-0000-0000-000000000000");
	app.switchTo(active, []);
	internal.inFlight.add(active.id);
	internal.turns.set(active.id, {});
	internal.sessionTokensById.set(active.id, 42);
	internal.sessionTokens = 42; // the visible counter mirrors the active session

	for (let i = 0; i < 60; i++) {
		const s = sessionInfo(`session-${i}`);
		app.switchTo(s, []);
		internal.turns.set(s.id, {});
		internal.sessionTokensById.set(s.id, i);
	}
	assert.ok(internal.turns.size <= 50, `turns bounded, got ${internal.turns.size}`);
	assert.ok(internal.sessionTokensById.size <= 50, `tokens bounded, got ${internal.sessionTokensById.size}`);
	assert.ok(internal.turns.has(active.id), "in-flight session state is never pruned");
	assert.equal(internal.sessionTokensById.get(active.id), 42, "in-flight token count is preserved");
});

// ─── W6: cursor pagination scans are bounded ────────────────────────────────

function makeHost(request: FeatureHost["request"], session?: SessionInfo): FeatureHost {
	return {
		request,
		print() {},
		printMarkdown() {},
		session: () => session,
		submitTurn: async () => {},
		switchTo() {},
		updateSession() {},
		pick: async () => undefined,
		pickMany: async () => undefined,
		pickTheme: async () => undefined,
		askText: async () => undefined,
		showOAuthProgress: () => ({ update() {}, dispose() {} }),
		inspectOutput: async () => {},
		profileId: () => "tester",
		setProfile() {},
		sessionAgent: () => undefined,
		writeFile: async (path) => path,
		onConfig() {},
		clear() {},
		banner() {},
		restyle() {},
		exit: async () => {},
	};
}

/** Endless cursor feed with a harness cap so an unbounded loop fails loudly instead of hanging. */
function endlessPages(method: string, response: Record<string, unknown>): {
	request: FeatureHost["request"];
	requestCount(): number;
} {
	let requests = 0;
	const request = (async (called: string, params: Record<string, unknown> = {}) => {
		requests++;
		if (requests > 150) throw new Error(`harness cap exceeded (${method} ${called})`);
		assert.equal(called, method);
		void params;
		return { ...response, nextCursor: `cursor-${requests}` };
	}) as FeatureHost["request"];
	return { request, requestCount: () => requests };
}

test("W6 session prefix resolution refuses an endless cursor chain", async () => {
	const pages = endlessPages("session.list", { sessions: [] });
	const resume = sessionCommands.find((c) => c.name === "resume") as Command;
	await assert.rejects(() => resume.run("abc", makeHost(pages.request)), /too many sessions/i);
	assert.ok(pages.requestCount() <= 101, `pagination bounded, made ${pages.requestCount()} requests`);
});

test("W6 session delete listing refuses an endless cursor chain", async () => {
	const pages = endlessPages("session.list", { sessions: [] });
	const del = sessionCommands.find((c) => c.name === "delete") as Command;
	await assert.rejects(() => del.run("", makeHost(pages.request)), /too many sessions/i);
	assert.ok(pages.requestCount() <= 101, `pagination bounded, made ${pages.requestCount()} requests`);
});

test("W6 job prefix resolution refuses an endless cursor chain", async () => {
	const pages = endlessPages("jobs.list", { jobs: [] });
	await assert.rejects(
		() => jobsCommand.run("on abc", makeHost(pages.request, sessionInfo("session-x"))),
		/too many jobs/i,
	);
	assert.ok(pages.requestCount() <= 101, `pagination bounded, made ${pages.requestCount()} requests`);
});
