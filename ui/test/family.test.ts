// Multi-agent orchestration: hidden-session live tails, lazy family
// discovery, the footer subagent count, the Down-arrow subagent menu, and
// the /agents sessions switch menu. Rendered through a real TuiAltScreen
// against a fake terminal (dialogs.test.ts pattern) so the counts and menus
// are asserted on actual screen lines.
//
// Pinned merge rule (see tail.ts): a hidden session's buffered tail is the
// conversation suffix and WINS over the freshly fetched history; matching
// trailing history entries are trimmed so a completed turn or settled tool
// renders exactly once.
//
// Delegation is non-streaming (agent.run): a delegated child never streams
// frames of its own. Discovery runs on the parent's `delegate` tool.complete
// or a turn-start frame from an unfamiliar session — never on every frame.

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

const CHILD_B: SessionInfo = {
	id: "44444444-4444-4444-8444-444444444444",
	title: "researcher: find Y",
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
		editor: { getText(): string; isShowingAutocomplete(): boolean };
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

	// Ctrl+Shift+→ cycles to the child session.
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, CHILD.id, "cycle key switched to the child session");
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

// ─── (c) footer subagent count ─────────────────────────────────────────────

test("(c) the footer counts subagents and hides at zero", async (t) => {
	// 0 subagents: no segment at all.
	const single = await boot({ sessions: [MAIN] });
	t.after(() => single.app.exit());
	await settle();
	single.tui.renderNow(true);
	assert.doesNotMatch(screenOf(single.tui), /subagent/, "no count without a family");
	assert.equal(single.app.familySessions().length, 1);

	// 1 subagent: singular form.
	const pair = await boot({ sessions: [MAIN, CHILD] });
	t.after(() => pair.app.exit());
	await settle();
	pair.tui.renderNow(true);
	const pairScreen = screenOf(pair.tui);
	assert.match(pairScreen, /\b1 subagent\b/, "one child counts as one subagent");
	assert.doesNotMatch(pairScreen, /subagents/, "the singular form never pluralizes");

	// 2 subagents: plural form.
	const trio = await boot({ sessions: [MAIN, CHILD, CHILD_B] });
	t.after(() => trio.app.exit());
	await settle();
	trio.tui.renderNow(true);
	const trioScreen = screenOf(trio.tui);
	assert.match(trioScreen, /\b2 subagents\b/, "two children count as two subagents");
	assert.doesNotMatch(trioScreen, /\b1 subagent\b/, "count replaces, never accumulates");
	assert.equal(trio.app.familySessions().length, 3);
});

// ─── (d) Down-arrow subagent menu ──────────────────────────────────────────

test("(d) Down on the empty composer opens the subagent menu with run state and switches", async (t) => {
	const { app, tui, type, client } = await boot({
		sessions: [MAIN, CHILD],
		resume: [[CHILD.id, { session: CHILD, history: [] }]],
	});
	t.after(() => app.exit());
	await settle();

	// Give the child a live turn, then hide it again: the menu must show
	// its run state while the main session stays visible.
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, CHILD.id);
	client.autoTurnId = "child-turn";
	await app.submitTurn("child task");
	type("\x1b[1;6C");
	await settle();
	assert.equal(app.session()?.id, MAIN.id, "cycling wraps back to the main session");
	assert.equal(app.familySessions().find((row) => row.session.id === CHILD.id)?.running, true);

	type("\x1b[B");
	await settle();
	tui.renderNow(true);
	const screen = screenOf(tui);
	assert.match(screen, /Switch to an agent session/, "Down opens the family picker");
	assert.match(screen, /main · Main work/, "the main session is marked");
	const childRow = screen.split("\n").find((row) => row.includes("researcher: find X"));
	assert.ok(childRow, "the child appears in the menu");
	assert.match(childRow, /running/, "the busy child row shows running state");

	type("\x1b[B"); // move off the initially selected main row
	type("\r");
	await settle();
	assert.equal(app.session()?.id, CHILD.id, "selecting a row switches through session.resume");
});

test("(d2) a second Down during the listing does not stack a second picker", async (t) => {
	const { app, tui, type } = await boot({ sessions: [MAIN, CHILD] });
	t.after(() => app.exit());
	await settle();

	type("\x1b[B");
	type("\x1b[B"); // second press inside the list-fetch window
	await settle();
	tui.renderNow(true);
	const dialogs = (app as unknown as { dialogs: unknown[] }).dialogs;
	assert.equal(dialogs.length, 1, "exactly one picker overlay");
	assert.match(screenOf(tui), /Switch to an agent session/);
	type("\x1b"); // cancel so the app can exit cleanly
	await settle();
});

// ─── (e) Down fall-through guards ──────────────────────────────────────────

test("(e) Down falls through with a draft, with a completion open, and with no subagents", async (t) => {
	const { app, tui, type } = await boot({ sessions: [MAIN, CHILD] });
	t.after(() => app.exit());
	await settle();

	// A draft keeps Down for the editor (movement/history).
	type("hello");
	type("\x1b[B");
	await settle();
	tui.renderNow(true);
	let screen = screenOf(tui);
	assert.doesNotMatch(screen, /Switch to an agent session/, "no menu with a draft");
	assert.match(screen, /hello/, "the draft is untouched");

	// An open completion popup keeps Down for completion navigation.
	type("\x03"); // clear the draft
	type("/");
	for (let i = 0; i < 30 && !internals(app).editor.isShowingAutocomplete(); i++) {
		await new Promise((resolve) => setTimeout(resolve, 10));
	}
	assert.equal(internals(app).editor.isShowingAutocomplete(), true, "completion is open");
	type("\x1b[B");
	await settle();
	tui.renderNow(true);
	screen = screenOf(tui);
	assert.doesNotMatch(screen, /Switch to an agent session/, "no family menu while completing");
	assert.equal(internals(app).editor.isShowingAutocomplete(), true, "the completion stays open");

	// No subagents: Down is not a menu key at all.
	const bare = await boot({ sessions: [MAIN] });
	t.after(() => bare.app.exit());
	await settle();
	bare.type("\x1b[B");
	await settle();
	bare.tui.renderNow(true);
	assert.doesNotMatch(screenOf(bare.tui), /Switch to an agent session/, "no menu without subagents");
	assert.equal(bare.app.familySessions().length, 1);
});

// ─── (f) switching preserves per-session state ─────────────────────────────

test("(f) session switching preserves in-flight guards and token totals", async (t) => {
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

// ─── lazy discovery + /agents sessions menu ────────────────────────────────

test("an unknown child's turn start lazily counts it once the list shows the parent link", async (t) => {
	const { app, tui, client } = await boot({ sessions: [MAIN] });
	t.after(() => app.exit());
	await settle();
	tui.renderNow(true);
	assert.doesNotMatch(screenOf(tui), /subagent/, "no count before discovery");

	// Delegation is non-streaming: the child's work arrives as the parent's
	// `delegate` tool.complete (pinned in the next test). A turn-start frame
	// from an unfamiliar session is the other discovery trigger: one bounded
	// refresh, and the matching parent link in the list adds the subagent.
	client.sessions = [MAIN, CHILD];
	client.emit(CHILD.id, "c9", { type: "message.start" });
	client.emit(CHILD.id, "c9", { type: "message.delta", text: "late child" });
	await settle();
	tui.renderNow(true);
	assert.match(screenOf(tui), /\b1 subagent\b/, "the matching parent link counts the child lazily");
	assert.equal(internals(app).sessionTails.get(CHILD.id)?.blocks.length, 1, "the delta is buffered for switching");
});

test("a parent's delegate tool.complete discovers the new child", async (t) => {
	const { app, tui, client } = await boot({ sessions: [MAIN] });
	t.after(() => app.exit());
	await settle();
	tui.renderNow(true);
	assert.doesNotMatch(screenOf(tui), /subagent/);

	// The delegated child is created server-side by agent.run and never
	// streams to this TUI. It is listed by the time the parent's delegate
	// tool completes, so that completion is what counts and buffers it.
	client.sessions = [MAIN, CHILD];
	client.emit(MAIN.id, "m9", { type: "tool.complete", id: "tool-d", name: "delegate", result: "done", isError: false });
	await settle();
	tui.renderNow(true);
	assert.match(screenOf(tui), /\b1 subagent\b/, "the delegate completion counts the child");
});

test("only turn starts scan for an unknown session, and a foreign id is negatively cached", async (t) => {
	const { app, client } = await boot({ sessions: [MAIN] });
	t.after(() => app.exit());
	await settle();
	const listCalls = () => client.requests.filter((request) => request.method === "session.list").length;
	const baseline = listCalls();

	// Deltas (or any non-turn-start frame) from an unfamiliar session are
	// not discovery triggers.
	client.emit(UNRELATED.id, "u1", { type: "message.delta", text: "foreign" });
	await settle();
	assert.equal(listCalls(), baseline, "a foreign delta never scans the catalog");

	// A turn-start frame scans once…
	client.emit(UNRELATED.id, "u1", { type: "message.start" });
	await settle();
	assert.equal(listCalls(), baseline + 1, "a turn-start frame scans once");

	// …and the negative result is cached: later turn starts never rescan it.
	// Move the clock past the throttle window so only the cache can suppress.
	const realNow = Date.now;
	let now = realNow();
	Date.now = () => now;
	t.after(() => { Date.now = realNow; });
	now += 60_000;
	client.emit(UNRELATED.id, "u2", { type: "message.start" });
	await settle();
	assert.equal(listCalls(), baseline + 1, "a foreign session is negatively cached");
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
