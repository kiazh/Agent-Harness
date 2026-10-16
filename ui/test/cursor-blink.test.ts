// Cursor-blink proof (owner request): the focused text surface's block caret
// must visibly blink on/off so focus ownership is obvious, and must stop
// blinking (retire the coordinator subscription) whenever no text caret is on
// screen. These tests drive the REAL App on a TuiAltScreen over a mock
// terminal with mocked timers, so the phase is deterministic.
//
// Mechanism under test (investigated before the change):
// - ui/src/editor/view.ts emits the zero-width CURSOR_MARKER AND a fake
//   inverse-video block ("paintCaret") for the focused caret.
// - pi-tui's alt-screen renderer strips CURSOR_MARKER and positions the REAL
//   hardware cursor there, but showHardwareCursor defaults false, so the
//   terminal cursor stays hidden (\x1b[?25l) — the visible caret is always the
//   rendered inverse block, never a native blinking cursor.
// - The blink therefore toggles the rendered block (kept marker for IME).

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	CURSOR_MARKER,
	stripTerminalSequences,
	TuiAltScreen,
	type Terminal,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, SessionInfo } from "../src/protocol.ts";
import type { WorkspaceEvent } from "../src/workspace/protocol.ts";

const SESSION: SessionInfo = {
	id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
	title: "session",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const WORKSPACE_IDENTITY = {
	workspaceId: "ws1",
	clientId: "cl1",
	epoch: 1,
	rootLabel: "ws",
	resolvedRoot: "/ws",
	trusted: true,
	capabilities: ["list", "read", "save", "documentOpen", "intelligence"],
};

function readResult(uri: string): Record<string, unknown> {
	return {
		uri,
		text: "hello\n",
		revision: "rev-1",
		languageId: "plaintext",
		lineEnding: "LF",
		encoding: "utf-8",
		readOnly: false,
		sizeBytes: 6,
	};
}

class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	onWorkspaceEvent: ((event: WorkspaceEvent) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	responses = new Map<string, unknown>();
	handlers = new Map<string, (params: Record<string, unknown>) => unknown | Promise<unknown>>();

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		const handler = this.handlers.get(method);
		if (handler) return Promise.resolve().then(() => handler(params)) as Promise<T>;
		const response = this.responses.get(method);
		if (response === undefined) return Promise.reject(new Error(`unexpected request ${method}`));
		return Promise.resolve(response as T);
	}
}

function configureClient(client: FakeClient): void {
	client.responses.set("initialize", { version: "1.0.0", model: "m", provider: "openrouter", cwd: "/ws", branch: "main" });
	client.responses.set("session.create", { session: SESSION });
	client.responses.set("prompt.submit", { turnId: "t" });
	client.responses.set("prompt.cancel", {});
	client.responses.set("config.get", { config: { nowplaying_enabled: false }, secrets: [] });
	client.responses.set("config.set", { model: "m", provider: "openrouter", key: "workspace_ui_state", value: "" });
	client.responses.set("workspace.open", { ...WORKSPACE_IDENTITY });
	client.responses.set("workspace.documentOpen", { acceptedVersion: 0, serverGeneration: 1, state: "ready" });
	client.responses.set("workspace.documentChange", { acceptedVersion: 1 });
	client.responses.set("workspace.documentClose", { closed: true, uri: "" });
	client.responses.set("workspace.close", { closed: true });
	client.responses.set("workspace.save", { ok: true, revision: "rev-2", savedUiVersion: 0 });
	client.responses.set("workspace.lspStatus", { servers: [] });
	client.responses.set("workspace.list", { entries: [], nextCursor: null, exclusions: [], truncated: false });
	client.responses.set("workspace.quickOpen", {
		entries: [],
		total: 0,
		truncated: false,
		reasons: [],
		indexing: false,
		scanned: 0,
		stale: false,
		workspaceEpoch: 1,
		exclusions: [],
	});
	client.handlers.set("workspace.read", (params) => readResult(String(params.uri)));
}

interface MockTerminal extends Terminal {
	writes: string[];
	columns: number;
	rows: number;
	onInput?: (data: string) => void;
}

function mockTerminal(columns: number, rows: number): MockTerminal {
	return {
		writes: [],
		columns,
		rows,
		kittyProtocolActive: false,
		start(onInput: (data: string) => void) {
			this.onInput = onInput;
		},
		stop() {},
		async drainInput() {},
		write(data: string) {
			this.writes.push(data);
		},
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

interface Internals {
	integration?: {
		view: { focused: boolean };
		tabs: { count(): number; activeDocumentId(): string | null };
		controller: { phase: string };
	};
	editor: { getText(): string; focused: boolean };
	coordinator: { size: number; timerCount: number; isReducedMotion: boolean };
	cursorBlinkSub: unknown;
	activeRegion: string;
	setFocusForRegion(region: string): void;
	openWorkspacePath(path: string): Promise<void>;
	quickOpenPane?: unknown;
}

function internals(app: App): Internals {
	return app as unknown as Internals;
}

async function settle(times = 40): Promise<void> {
	for (let index = 0; index < times; index++) await new Promise((resolve) => setImmediate(resolve));
}

interface Booted {
	app: App;
	tui: TuiAltScreen;
	term: MockTerminal;
	client: FakeClient;
	restoreEnv(): void;
}

async function boot(options: { reducedMotion?: boolean } = {}): Promise<Booted> {
	const prev = process.env.MACHINA_UI_REDUCED_MOTION;
	if (options.reducedMotion) process.env.MACHINA_UI_REDUCED_MOTION = "1";
	else delete process.env.MACHINA_UI_REDUCED_MOTION;
	const term = mockTerminal(160, 48);
	const tui = new TuiAltScreen(term);
	const client = new FakeClient();
	configureClient(client);
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	for (let index = 0; index < 200 && !internals(app).integration; index++) await new Promise((resolve) => setImmediate(resolve));
	await settle();
	tui.renderNow(true);
	return {
		app,
		tui,
		term,
		client,
		restoreEnv: () => {
			if (prev === undefined) delete process.env.MACHINA_UI_REDUCED_MOTION;
			else process.env.MACHINA_UI_REDUCED_MOTION = prev;
		},
	};
}

/** Render one full frame, capturing both the pre-extraction lines (markers)
 *  and the composed screen the renderer actually flushes. */
function capture(tui: TuiAltScreen): { lines: string[]; screen: string[] } {
	const target = tui as unknown as { extractCursorPosition(lines: string[], height: number): unknown };
	const original = target.extractCursorPosition;
	const lines: string[] = [];
	target.extractCursorPosition = function (this: unknown, input: string[], height: number) {
		lines.length = 0;
		lines.push(...input);
		return original.call(this, input, height);
	};
	tui.renderNow(true);
	target.extractCursorPosition = original;
	return { lines, screen: tui.getScreenLines() };
}

function rowWithMarker(lines: string[]): number {
	return lines.findIndex((line) => line.includes(CURSOR_MARKER));
}

/** The inverse block paintCaret() emits: bold + reverse video + glyph. */
// eslint-disable-next-line no-control-regex -- terminal SGR sequences are the subject here.
const CARET_BLOCK = /\x1b\[1m\x1b\[7m/;

test("focused workspace editor caret blinks on and off", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const booted = await boot();
	try {
		await internals(booted.app).openWorkspacePath("src/a.ts");
		await settle();
		assert.equal(internals(booted.app).activeRegion, "editor", "opening a file focuses the editor region");
		assert.ok(internals(booted.app).cursorBlinkSub, "blink subscribed while the editor caret is visible");
		assert.equal(internals(booted.app).coordinator.timerCount, 1, "one shared coordinator timer drives the blink");

		const on = capture(booted.tui);
		const row = rowWithMarker(on.lines);
		assert.ok(row >= 0, "a caret marker row exists while focused");
		assert.equal(on.lines.join("\n").split(CURSOR_MARKER).length - 1, 1, "exactly one CURSOR_MARKER");
		const onRow = on.screen[row]!;
		assert.match(onRow, CARET_BLOCK, "phase ON paints the inverse block");
		assert.ok(stripTerminalSequences(onRow).includes("hello"), "the block sits on the buffer content");

		// One interval later the phase is OFF: no block, same visible row.
		t.mock.timers.tick(500);
		const off = capture(booted.tui);
		const offRow = off.screen[row]!;
		assert.doesNotMatch(offRow, CARET_BLOCK, "phase OFF shows no block");
		assert.equal(stripTerminalSequences(onRow), stripTerminalSequences(offRow), "visible text is identical in both phases");
		assert.notEqual(onRow, offRow, "the raw rows differ at the caret cell");
		// Removing exactly the caret wrapper yields the OFF row byte-for-byte:
		// the frame differs ONLY at the caret cell.
		// eslint-disable-next-line no-control-regex -- unwrapping the caret SGR wrapper.
		const unwrapped = onRow.replace(/\x1b\[1m\x1b\[7m([\s\S]*?)\x1b\[27m\x1b\[22m/, "$1");
		assert.equal(unwrapped, offRow, "the two frames differ only at the caret cell");
		// The IME marker still rides along in both phases (hardware cursor stays hidden).
		assert.equal(off.lines.join("\n").split(CURSOR_MARKER).length - 1, 1, "marker kept while off");
	} finally {
		await booted.app.exit();
		booted.restoreEnv();
	}
});

test("the focused composer block caret blinks too", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const booted = await boot();
	try {
		// The composer owns focus at boot but an empty draft shows only the
		// placeholder (no caret), so no blink demand is created.
		assert.equal(internals(booted.app).editor.focused, true, "composer owns focus at boot");
		assert.equal(internals(booted.app).cursorBlinkSub, undefined, "empty composer does not burn renders");

		booted.term.onInput?.("h");
		booted.term.onInput?.("i");
		await settle();
		assert.equal(internals(booted.app).editor.getText(), "hi", "the draft landed");
		assert.ok(internals(booted.app).cursorBlinkSub, "a non-empty composer subscribes the blink");

		const on = capture(booted.tui);
		const row = on.screen.findIndex((line) => line.includes("\x1b[7m"));
		assert.ok(row >= 0, "composer caret block present in phase ON");
		const onRow = on.screen[row]!;
		assert.ok(stripTerminalSequences(onRow).includes("hi"), "the block sits on the draft line");

		t.mock.timers.tick(500);
		const off = capture(booted.tui);
		const offRow = off.screen[row]!;
		// eslint-disable-next-line no-control-regex -- asserting the absence of the caret SGR.
		assert.doesNotMatch(offRow, /\x1b\[7m/, "phase OFF draws the composer caret as plain text");
		assert.equal(stripTerminalSequences(onRow), stripTerminalSequences(offRow), "draft text is unchanged by the phase");
	} finally {
		await booted.app.exit();
		booted.restoreEnv();
	}
});

test("an overlay owning focus retires the blink with no toggles", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const booted = await boot();
	try {
		await internals(booted.app).openWorkspacePath("src/a.ts");
		await settle();
		assert.ok(internals(booted.app).cursorBlinkSub);
		assert.equal(internals(booted.app).coordinator.timerCount, 1);

		// Ctrl+P: the quick-open overlay captures focus away from every caret.
		booted.term.onInput?.("\x10");
		await settle();
		assert.ok(internals(booted.app).quickOpenPane, "quick open owns focus");
		assert.equal(internals(booted.app).cursorBlinkSub, undefined, "blink retired while the overlay owns focus");
		assert.equal(internals(booted.app).coordinator.timerCount, 0, "no blink timer remains");

		// Two intervals of wall clock: nothing toggles (no subscription).
		t.mock.timers.tick(1000);
		assert.equal(internals(booted.app).cursorBlinkSub, undefined, "still retired after two intervals");
		assert.equal(internals(booted.app).coordinator.timerCount, 0, "no phase toggles over two intervals");
	} finally {
		await booted.app.exit();
		booted.restoreEnv();
	}
});

test("reduced motion keeps a static caret and creates no timer", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const booted = await boot({ reducedMotion: true });
	try {
		await internals(booted.app).openWorkspacePath("src/a.ts");
		await settle();
		assert.equal(internals(booted.app).coordinator.isReducedMotion, true, "test precondition");
		assert.equal(internals(booted.app).cursorBlinkSub, undefined, "reduced motion never subscribes the blink");
		assert.equal(internals(booted.app).coordinator.timerCount, 0, "no timer at all");

		const first = capture(booted.tui);
		const row = rowWithMarker(first.lines);
		assert.match(first.screen[row]!, CARET_BLOCK, "the caret stays visible (focus must remain obvious)");

		t.mock.timers.tick(1000);
		const second = capture(booted.tui);
		assert.equal(second.screen[row], first.screen[row], "the caret never toggles under reduced motion");
		assert.match(second.screen[row]!, CARET_BLOCK, "still static and visible");
		assert.equal(internals(booted.app).coordinator.timerCount, 0);
	} finally {
		await booted.app.exit();
		booted.restoreEnv();
	}
});

test("repeated focus changes never stack blink subscriptions", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const booted = await boot();
	try {
		await internals(booted.app).openWorkspacePath("src/a.ts");
		await settle();
		const state = internals(booted.app);
		const first = state.cursorBlinkSub;
		assert.ok(first);

		for (let index = 0; index < 5; index++) state.setFocusForRegion("editor");
		assert.equal(state.cursorBlinkSub, first, "refocusing the editor reuses the one subscription");
		assert.equal(state.coordinator.size, 1, "exactly one coordinator subscription");

		state.setFocusForRegion("explorer");
		assert.equal(state.cursorBlinkSub, undefined, "leaving the caret retires the subscription");
		assert.equal(state.coordinator.size, 0);

		state.setFocusForRegion("editor");
		assert.ok(state.cursorBlinkSub, "refocusing re-subscribes");
		assert.equal(state.coordinator.size, 1, "still exactly one (no duplicate) after the round trip");
	} finally {
		await booted.app.exit();
		booted.restoreEnv();
	}
});

test("typing still edits and renders correctly with the blink phase off", async (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const booted = await boot();
	try {
		await internals(booted.app).openWorkspacePath("src/a.ts");
		await settle();
		t.mock.timers.tick(500); // phase OFF
		const off = capture(booted.tui);
		const row = rowWithMarker(off.lines);
		assert.ok(row >= 0);
		assert.doesNotMatch(off.screen[row]!, CARET_BLOCK, "phase is off before typing");

		booted.term.onInput?.("X");
		await settle();
		booted.tui.renderNow(true);
		const joined = stripTerminalSequences(booted.tui.getScreenLines().join("\n"));
		assert.ok(joined.includes("Xhello"), `the character landed in the buffer: ${joined}`);
	} finally {
		// The typed edit dirtied the buffer: answer the quit review so exit
		// settles (the review dialog would otherwise await a real user).
		(booted.app as unknown as { pick: () => Promise<{ value: string }> }).pick = async () => ({ value: "discard" });
		await booted.app.exit();
		booted.restoreEnv();
	}
});
