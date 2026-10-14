// Multi-agent orchestration tabs: hidden-session live tails, the family tab
// strip, lazy tab discovery, and the /agents sessions switch menu. Rendered
// through a real TuiAltScreen against a fake terminal (dialogs.test.ts
// pattern) so the strip's visibility is asserted on actual screen lines.
//
// Pinned merge rule (see app.ts switchTo/tailEntries): a hidden session's
// buffered tail is the conversation suffix and WINS over the freshly fetched
// history; matching trailing history entries are trimmed so a completed turn
// or settled tool renders exactly once.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { parseCommand } from "../src/commands.ts";
import { runCommand } from "../src/features/index.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, ResumeResult, SessionInfo } from "../src/protocol.ts";

const MAIN: SessionInfo = {
	id: "11111111-1111-4111-8111-111111111111",
	title: "Main work",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const CHILD: SessionInfo = {
	id: "22222222-2222-4222-8222-222222222222",
	title: "researcher: find X",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
	parentSessionId: MAIN.id,
};

const UNRELATED: SessionInfo = {
	id: "33333333-3333-4333-8333-333333333333",
	title: "Other chat",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
	parentSessionId: null,
};

function terminal(columns = 100, rows = 30) {
	let input: (data: string) => void = () => {};
	const term: Terminal & { columns: number; rows: number } = {
		columns,
		rows,
		kittyProtocolActive: false,
		start(onInput) { input = onInput; },
		stop() {},
		async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	return { term, type: (data: string) => input(data) };
}

class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	/** session.list page content (newest first); replaced per test. */
	sessions: SessionInfo[] = [];
	/** session.resume responses keyed by session id. */
	resume = new Map<string, ResumeResult>();
	autoTurnId = "t-auto";

	start(): void {}
	stderr(): string[] { return []; }
	async stop(): Promise<void> {}

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		switch (method) {
			case "initialize":
				return { version: "test", model: "m", provider: "p", cwd: "/tmp", branch: "main" } as T;
			case "config.get":
				return { config: {} } as T;
			case "mode.get":
				return { mode: "ask" } as T;
			case "profile.get":
				return {} as T;
			case "session.create":
				return { session: this.sessions[0] ?? MAIN } as T;
			case "session.list":
				return { sessions: this.sessions } as T;
			case "session.resume": {
				const fixture = this.resume.get(String(params.sessionId));
				if (fixture) return fixture as T;
				// Default: resume any listed session with empty history so tab
				// cycling round-trips mirror the real gateway.
				const session = this.sessions.find((candidate) => candidate.id === params.sessionId);
				if (!session) throw new Error(`no resume fixture for ${params.sessionId}`);
				return { session, history: [] } as T;
			}
			case "prompt.submit":
				return { turnId: this.autoTurnId } as T;
			case "prompt.cancel":
			case "approvals.resolve":
				return {} as T;
			default:
				throw new Error(`unexpected request ${method}`);
		}
	}

	emit(sessionId: string, turnId: string, event: Record<string, unknown>): void {
		this.onEvent?.({ sessionId, turnId, ...event } as GatewayEvent);
	}
}

async function settle(times = 8): Promise<void> {
	for (let index = 0; index < times; index++) await new Promise((resolve) => setImmediate(resolve));
}

function screenOf(tui: TuiAltScreen): string {
	return tui.getScreenLines().map(stripTerminalSequences).join("\n");
}

interface BootOptions {
	sessions?: SessionInfo[];
	resume?: Array<[string, ResumeResult]>;
}

async function boot(options: BootOptions = {}) {
	const { term, type } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new FakeClient();
	client.sessions = options.sessions ?? [MAIN];
	for (const [id, result] of options.resume ?? []) client.resume.set(id, result);
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return { app, tui, type, client, term };
}

function internals(app: App) {
	return app as unknown as {
		inFlight: Set<string>;
		turns: Map<string, { turnId?: string }>;
		sessionTokensById: Map<string, number>;
		sessionTails: Map<string, { blocks: unknown[] }>;
		footer: { tokens: number };
	};
}

// ─── (a) hidden-session buffering ──────────────────────────────────────────

test("(a) events for a hidden agent session are buffered and appear when switching", async (t) => {
	const resumeChild: ResumeResult = { session: CHILD, history: [] };
	const { app, tui, type, client } = await boot({ sessions: [MAIN, CHILD], resume: [[CHILD.id, resumeChild]] });
	t.after(() => app.exit());
	await settle();

	// The child works while the main session stays visible.
	client.emit(CHILD.id, "c1", { type: "message.start" });
	client.emit(CHILD.id, "c1", { type: "message.delta", text: "child " });
	client.emit(CHILD.id, "c1", { type: "message.delta", text: "working" });
	client.emit(CHILD.id, "c1", { type: "tool.start", id: "tool-1", name: "terminal", args: { command: "ls" } });
	client.emit(CHILD.id, "c1", { type: "tool.complete", id: "tool-1", name: "terminal", result: "a\nb", isError: false });
	tui.renderNow(true);
	assert.doesNotMatch(screenOf(tui), /child working/, "hidden work never bleeds into the visible transcript");
	assert.equal(internals(app).sessionTails.get(CHILD.id)?.blocks.length, 2, "deltas and tool are buffered while hidden");

	// Ctrl+Shift+→ cycles to the child tab.
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, CHILD.id, "tab key switched to the child session");
	tui.renderNow(true);
	const screen = screenOf(tui);
	assert.match(screen, /child working/, "buffered tail renders on switch");
	assert.match(screen, /recorded output/, "settled tool renders once from the tail");
	assert.equal(internals(app).sessionTails.has(CHILD.id), false, "the tail is consumed by the switch");
});

// ─── (b) tail/history merge without duplicates ─────────────────────────────

test("(b) buffered tail over fetched history has no duplicates", async (t) => {
	// The real backend persists the tool chunk and the final assistant
	// message; the fetched history therefore already contains both.
	const history = [
		{ role: "tool" as const, tool: "terminal", content: "ok" },
		{ role: "assistant" as const, content: "child answer" },
	];
	const { app, tui, type, client } = await boot({
		sessions: [MAIN, CHILD],
		resume: [[CHILD.id, { session: CHILD, history }]],
	});
	t.after(() => app.exit());
	await settle();

	client.emit(CHILD.id, "c2", { type: "message.start" });
	client.emit(CHILD.id, "c2", { type: "message.delta", text: "child " });
	client.emit(CHILD.id, "c2", { type: "tool.start", id: "tool-2", name: "terminal", args: {} });
	client.emit(CHILD.id, "c2", { type: "tool.complete", id: "tool-2", name: "terminal", result: "ok", isError: false });
	client.emit(CHILD.id, "c2", {
		type: "message.complete",
		text: "child answer",
		tokens: 3,
		iterations: 1,
		toolCalls: 1,
		cancelled: false,
	});
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, CHILD.id);
	tui.renderNow(true);
	const screen = screenOf(tui);
	assert.equal((screen.match(/child answer/g) ?? []).length, 1, "assistant text renders exactly once");
	assert.equal((screen.match(/recorded output/g) ?? []).length, 1, "the settled tool renders exactly once");
});

// ─── (c) tab strip: children from session.list + run state ─────────────────

test("(c) the strip lists session.list children with run state and hides strangers", async (t) => {
	const { app, tui, type, client } = await boot({
		sessions: [MAIN, CHILD, UNRELATED],
		resume: [[CHILD.id, { session: CHILD, history: [] }]],
	});
	t.after(() => app.exit());
	await settle();
	tui.renderNow(true);
	let screen = screenOf(tui);
	assert.match(screen, /Main work \(main\)/, "main session is marked");
	assert.match(screen, /researcher: find X/, "listed child renders as a tab");
	assert.doesNotMatch(screen, /Other chat/, "sessions outside the family stay out of the strip");
	assert.match(screen, /ctrl\+shift/, "cycling hint is discoverable");

	// Give the child a live turn, then hide it again: the tab must show
	// running state while the main session is visible.
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, CHILD.id);
	client.autoTurnId = "child-turn";
	await app.submitTurn("child task");
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, MAIN.id, "cycling wraps back to the main tab");
	assert.equal(app.familySessions().find((row) => row.session.id === CHILD.id)?.running, true);
	tui.renderNow(true);
	screen = screenOf(tui);
	assert.match(screen, /● researcher: find X/, "running child carries the running marker");
});

// ─── (d) switching preserves per-session state ─────────────────────────────

test("(d) tab switching preserves in-flight guards and token totals", async (t) => {
	const { app, type, client } = await boot({
		sessions: [MAIN, CHILD],
		resume: [[CHILD.id, { session: CHILD, history: [] }]],
	});
	t.after(() => app.exit());
	await settle();
	const state = internals(app);

	client.autoTurnId = "m1";
	await app.submitTurn("first main turn");
	client.emit(MAIN.id, "m1", { type: "message.start" });
	client.emit(MAIN.id, "m1", {
		type: "message.complete", text: "done", tokens: 42, iterations: 1, toolCalls: 0, cancelled: false,
	});
	assert.equal(state.footer.tokens, 42);

	client.autoTurnId = "m2";
	await app.submitTurn("second main turn");
	assert.equal(state.inFlight.has(MAIN.id), true);

	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, CHILD.id);
	assert.equal(state.inFlight.has(MAIN.id), true, "hidden in-flight guard survives the switch");
	assert.equal(state.turns.get(MAIN.id)?.turnId, "m2", "turn binding survives the switch");
	assert.equal(state.sessionTokensById.get(MAIN.id), 42, "token totals survive the switch");
	assert.equal(state.sessionTokensById.get(CHILD.id), undefined, "the child's token state is untouched");

	type("\x1b[1;6D");
	await settle();
	assert.equal(app.session()?.id, MAIN.id);
	assert.equal(state.footer.tokens, 42, "returning restores the preserved total");
	client.emit(MAIN.id, "m2", {
		type: "message.complete", text: "more", tokens: 8, iterations: 1, toolCalls: 0, cancelled: false,
	});
	assert.equal(state.footer.tokens, 50, "the completed hidden-time turn still accounts once");
	assert.equal(state.inFlight.has(MAIN.id), false);
});

// ─── (e) single-session use is unchanged ───────────────────────────────────

test("(e) the strip stays hidden for single-session use", async (t) => {
	const { app, tui } = await boot({ sessions: [MAIN] });
	t.after(() => app.exit());
	await settle();
	tui.renderNow(true);
	const screen = screenOf(tui);
	assert.doesNotMatch(screen, /\(main\)/, "no main marker without a family");
	assert.doesNotMatch(screen, /ctrl\+shift/, "no cycling hint without a family");
	assert.equal(app.familySessions().length, 1);
});

// ─── lazy discovery + /agents sessions menu ────────────────────────────────

test("an event from an unknown child lazily adds its tab once the list shows the parent link", async (t) => {
	const { app, tui, client } = await boot({ sessions: [MAIN] });
	t.after(() => app.exit());
	await settle();
	tui.renderNow(true);
	assert.doesNotMatch(screenOf(tui), /researcher/);

	// The delegated child appears in the session list after creation; its
	// first streamed event triggers a throttled family refresh.
	client.sessions = [MAIN, CHILD];
	client.emit(CHILD.id, "c9", { type: "message.delta", text: "late child" });
	await settle();
	tui.renderNow(true);
	assert.match(screenOf(tui), /researcher: find X/, "matching parent link adds the tab lazily");
	assert.equal(internals(app).sessionTails.get(CHILD.id)?.blocks.length, 1, "the event is buffered for switching");
});

test("/agents sessions lists the family and switches to the picked child", async (t) => {
	const { app, tui, type } = await boot({
		sessions: [MAIN, CHILD],
		resume: [[CHILD.id, { session: CHILD, history: [] }]],
	});
	t.after(() => app.exit());
	await settle();

	const pending = runCommand(parseCommand("/agents sessions")!, app);
	await settle();
	tui.renderNow(true);
	const screen = screenOf(tui);
	assert.match(screen, /Switch to an agent session/);
	assert.match(screen, /main · Main work/);
	assert.match(screen, /researcher: find X/);
	assert.match(screen, /running|idle/, "status is shown per row");

	type("\x1b[B");
	type("\r");
	await pending;
	await settle();
	assert.equal(app.session()?.id, CHILD.id, "picking the child switches through session.resume");
});
