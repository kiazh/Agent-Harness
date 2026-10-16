// Interactive `/help` command palette.
//
// Product rule: menu-shaped surfaces are interactive searchable overlays,
// never a printed wall of text. `/help` opens this palette when the host
// offers the surface (`helpPalette()`); hosts without it keep the legacy
// printed text, byte-pinned here.
//
// Part A drives HelpPalette directly (real layout, real hit map, real key
// handling) over deterministic fixtures.
//
// Part B boots the real App on a TuiAltScreen with a scripted terminal and a
// recording client: `/help` opens the overlay through the real command path,
// typing filters both sections, Enter runs a no-argument command or prefills
// the composer for a command that needs arguments, Esc closes, and the `?`
// shortcuts menu still reaches the palette.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	stripTerminalSequences,
	TuiAltScreen,
	visibleWidth,
	type Terminal,
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { helpText, parseCommand } from "../src/commands.ts";
import {
	HELP_KEYS,
	HelpPalette,
	helpPaletteCommands,
	type HelpCommandItem,
	type HelpKeyItem,
} from "../src/components/HelpPalette.ts";
import { runCommand, SLASH_COMMANDS, type FeatureHost } from "../src/features/index.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { SessionInfo } from "../src/protocol.ts";

// Keep the app chat-only in these tests (workspace is covered elsewhere).
process.env.MACHINA_WORKSPACE = "0";
process.env.MACHINA_UI_REDUCED_MOTION = "1";

const SESSION: SessionInfo = {
	id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
	title: "test",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

// ─── fixtures ───────────────────────────────────────────────────────────────

const COMMANDS: HelpCommandItem[] = [
	{ name: "help", description: "Show available commands" },
	{ name: "status", description: "Database, counts and configuration health" },
	{ name: "theme", description: "Switch the color skin", argumentHint: "<name>" },
	{ name: "skills", description: "Browse, search and learn skills", argumentHint: "[list|search|show|delete]", keywords: ["list", "search", "show", "delete", "curator"] },
	{ name: "quiet", description: "Silent housekeeping job" },
];

const KEYS: HelpKeyItem[] = [
	{ context: "Composer", label: "Enter — send", description: "Send a message or run a command" },
	{ context: "Workspace", label: "Ctrl+P — quick open", description: "Fuzzy file search in the workspace" },
	{ context: "Global", label: "F1 — shortcuts", description: "Open this list from anywhere" },
];

function lines(pane: HelpPalette, width = 70): string[] {
	return pane.render(width).map((line) => stripTerminalSequences(line).trimEnd());
}

function plain(pane: HelpPalette, width = 70): string {
	return lines(pane, width).join("\n");
}

function rowOf(pane: HelpPalette, needle: string, width = 70): number {
	return lines(pane, width).findIndex((line) => line.includes(needle));
}

function type(pane: HelpPalette, text: string): void {
	for (const char of text) pane.handleInput(char);
}

function clearQuery(pane: HelpPalette): void {
	for (let index = pane.getQuery().length; index > 0; index -= 1) pane.handleInput("\x7f");
}

function mouse(type: "press" | "click", y: number, clickCount = 1, width = 70): TuiMouseEvent {
	return {
		type,
		button: "left",
		x: 3,
		y,
		screenX: 3,
		screenY: y,
		width,
		height: 24,
		shift: false,
		alt: false,
		ctrl: false,
		clickCount,
	};
}

// ─── Part A: component ──────────────────────────────────────────────────────

test("HP01 activate renders Commands and Keys grouped, first command selected", () => {
	const pane = new HelpPalette({ commands: COMMANDS, keys: KEYS });
	pane.activate();
	const screen = plain(pane);
	assert.match(screen, /^Help:/m, "the header names the pane");
	assert.match(screen, /Commands/);
	assert.match(screen, /\/status/);
	assert.match(screen, /Keys/);
	assert.match(screen, /Ctrl\+P — quick open/);
	assert.match(screen, /Workspace: Fuzzy file search in the workspace/, "key rows carry their context");
	assert.equal(pane.getView().selected?.id, "command:help", "the first command is selected");
	assert.equal(lines(pane).length, 12, "header + Commands(1+5) + Keys(1+3) + footer");
});

test("HP01 initialQuery opens prefiltered", () => {
	const pane = new HelpPalette({ commands: COMMANDS, keys: KEYS, initialQuery: "theme" });
	pane.activate();
	const screen = plain(pane);
	assert.equal(pane.getView().query, "theme");
	assert.match(screen, /\/theme <name>/);
	assert.doesNotMatch(screen, /\/status/);
	assert.doesNotMatch(screen, /Ctrl\+P/);
});

test("HP02 typing filters across name, description, argument hints and keywords", () => {
	const pane = new HelpPalette({ commands: COMMANDS, keys: KEYS });
	pane.activate();

	type(pane, "quick");
	assert.match(plain(pane), /Ctrl\+P/);
	assert.doesNotMatch(plain(pane), /\/status/, "commands section hides when nothing matches");
	assert.doesNotMatch(plain(pane), /Commands/, "an empty section header is hidden");

	clearQuery(pane);
	type(pane, "silent");
	assert.match(plain(pane), /\/quiet/, "description text is searchable");

	clearQuery(pane);
	type(pane, "curator");
	assert.match(plain(pane), /\/skills/, "argument completions are searchable keywords");

	clearQuery(pane);
	type(pane, "list|search");
	assert.match(plain(pane), /\/skills/, "argument hint text is searchable");
	assert.doesNotMatch(plain(pane), /\/quiet/);

	clearQuery(pane);
	type(pane, "zzzz");
	const empty = plain(pane);
	assert.match(empty, /\(no matches for "zzzz"\)/, "no-match is a distinct state, never blank");
	assert.doesNotMatch(empty, /Commands/);
	assert.doesNotMatch(empty, /Keys/);

	clearQuery(pane);
	assert.match(plain(pane), /\/status/, "clearing the query restores every section");
	assert.match(plain(pane), /Ctrl\+P/);
});

test("HP02 selection is tracked by stable id across filtering", () => {
	const pane = new HelpPalette({ commands: COMMANDS, keys: KEYS });
	pane.activate();
	pane.handleInput("\x1b[B");
	pane.handleInput("\x1b[B");
	assert.equal(pane.getView().selected?.id, "command:theme", "↓ moves down");
	type(pane, "switch");
	assert.equal(pane.getView().selected?.id, "command:theme", "the id survives a narrower filter even when the row index changes");

	clearQuery(pane);
	type(pane, "status");
	assert.equal(pane.getView().selected?.id, "command:status", "a dropped id falls to the first match");

	clearQuery(pane);
	assert.equal(pane.getView().selected?.id, "command:status", "a surviving id is kept when the filter widens");

	// keys keep their identity too (key ids are source indices, not row indexes)
	for (let step = 0; step < 8 && pane.getView().selected?.id !== "key:1"; step += 1) pane.handleInput("\x1b[B");
	assert.equal(pane.getView().selected?.id, "key:1");
	type(pane, "workspace");
	assert.equal(pane.getView().selected?.id, "key:1", "the key id survives filtering");
});

test("HP03 hostile labels stay on one physical row and mouse maps to the rendered row", () => {
	const hostile: HelpCommandItem = { name: "evil\n\u001b[31mname\u001b[0m\ttwo", description: "d1\nd2\u0007" };
	const pane = new HelpPalette({ commands: [hostile, { name: "beta", description: "second" }], keys: [] });
	pane.activate();
	for (const line of pane.render(70)) {
		assert.ok(visibleWidth(line) <= 70, `row wider than 70: ${JSON.stringify(stripTerminalSequences(line))}`);
	}
	const evilAt = rowOf(pane, "evil");
	const betaAt = rowOf(pane, "beta");
	assert.ok(evilAt > 0);
	assert.equal(betaAt, evilAt + 1, "a hostile label adds no physical rows");

	pane.handleMouse(mouse("click", betaAt));
	assert.equal(pane.getView().selected?.id, "command:beta", "the click lands on the rendered row");
	pane.handleMouse(mouse("click", 0)); // header row: inert
	assert.equal(pane.getView().selected?.id, "command:beta");
});

test("HP04 Enter on a no-argument command runs it; an argument command prefills; keys are inert", () => {
	const pane = new HelpPalette({ commands: COMMANDS, keys: KEYS });
	const runs: HelpCommandItem[] = [];
	const prefills: string[] = [];
	let closes = 0;
	pane.onRun = (command) => void runs.push(command);
	pane.onPrefill = (text) => void prefills.push(text);
	pane.onClose = () => void (closes += 1);
	pane.activate();

	pane.handleInput("\r");
	assert.deepEqual(runs.map((command) => command.name), ["help"], "a command with no argument hint runs");
	assert.deepEqual(prefills, []);

	pane.handleInput("\x1b[B");
	pane.handleInput("\x1b[B");
	assert.equal(pane.getView().selected?.id, "command:theme");
	pane.handleInput("\r");
	assert.deepEqual(prefills, ["/theme "], "a command needing arguments prefills `/name ` for the composer");
	assert.equal(runs.length, 1, "the same Enter never runs and prefills");

	// the host owns the close; the picker never dismisses itself
	assert.equal(closes, 0);

	// keys: Enter is a deliberate no-op
	while (pane.getView().selected?.kind !== "key") pane.handleInput("\x1b[B");
	pane.handleInput("\r");
	assert.deepEqual(runs.map((command) => command.name), ["help"]);
	assert.deepEqual(prefills, ["/theme "]);

	pane.handleInput("\x1b");
	assert.equal(closes, 1, "Esc closes through onClose");
});

test("HP04 mouse click activates commands like Enter; key rows only select; press only focuses", () => {
	const pane = new HelpPalette({ commands: COMMANDS, keys: KEYS });
	const runs: string[] = [];
	const prefills: string[] = [];
	pane.onRun = (command) => void runs.push(command.name);
	pane.onPrefill = (text) => void prefills.push(text);
	pane.activate();

	const themeAt = rowOf(pane, "/theme");
	pane.handleMouse(mouse("press", themeAt));
	assert.deepEqual(prefills, [], "a press focuses but never activates");
	const statusAt = rowOf(pane, "/status");
	pane.handleMouse(mouse("click", statusAt));
	assert.deepEqual(runs, ["status"], "clicking a no-argument command runs it");
	pane.handleMouse(mouse("click", themeAt));
	assert.deepEqual(prefills, ["/theme "], "clicking an argument command prefills");

	const keyAt = rowOf(pane, "Ctrl+P");
	pane.handleMouse(mouse("click", keyAt));
	assert.equal(pane.getView().selected?.id, "key:1");
	assert.deepEqual(runs, ["status"], "keys never activate");
	assert.deepEqual(prefills, ["/theme "]);
});

test("HP05 windowing bounds rows, ctrl+n/ctrl+p page, the selection stays visible", () => {
	const many: HelpCommandItem[] = Array.from({ length: 12 }, (_value, index) => ({
		name: `cmd${String(index).padStart(2, "0")}`,
		description: `row ${index}`,
	}));
	const pane = new HelpPalette({ commands: many, keys: [] });
	pane.setVisibleRows(4);
	pane.activate();
	const first = plain(pane);
	assert.match(first, /\+10 more/);
	assert.match(first, /cmd00/);
	assert.doesNotMatch(first, /cmd11/);
	assert.equal(lines(pane).length, 6, "header + section + 2 rows + more + footer");

	pane.handleInput("\x0e"); // ctrl+n pages down
	const selected = pane.getView().selected;
	assert.ok(selected && selected.kind === "command");
	assert.notEqual(selected.command.name, "cmd00", "ctrl+n moves by a page");
	assert.match(plain(pane), new RegExp(selected.command.name), "the selected row stays in the window");

	pane.handleInput("\x10"); // ctrl+p pages up
	assert.equal(pane.getView().selected?.id, "command:cmd00");
});

test("HP06 helpPaletteCommands derives searchable keywords from argument completions", () => {
	const built = helpPaletteCommands(SLASH_COMMANDS);
	assert.equal(built.length, SLASH_COMMANDS.length);
	const skills = built.find((command) => command.name === "skills");
	assert.ok(skills?.keywords?.includes("show"), "argument completions become keywords");
	assert.equal(skills?.argumentHint, "[list|search|show|learn|proposals|approve|reject|revert|delete|curator]");

	const throwing = [{
		name: "boom",
		description: "x",
		getArgumentCompletions: (): never => {
			throw new Error("nope");
		},
	}];
	const mapped = helpPaletteCommands(throwing);
	assert.equal(mapped[0]?.name, "boom");
	assert.deepEqual(mapped[0]?.keywords, [], "a throwing completion provider never breaks the palette");
});

test("HP06 HELP_KEYS covers Composer, Workspace and Global contexts", () => {
	const contexts = new Set(HELP_KEYS.map((key) => key.context));
	assert.ok(contexts.has("Composer"));
	assert.ok(contexts.has("Workspace"));
	assert.ok(contexts.has("Global"));
	assert.ok(HELP_KEYS.some((key) => key.label.includes("Ctrl+P — quick open")));
	assert.ok(HELP_KEYS.some((key) => key.label.includes("F6 / Shift+F6")));
	for (const key of HELP_KEYS) {
		assert.ok(key.label.trim().length > 0, "every key row has a label");
		assert.ok(key.context.trim().length > 0, "every key row has a context");
	}
});

// ─── legacy fallback host ───────────────────────────────────────────────────

/** Byte-pinned legacy `/help` output (the pre-palette wall of text). */
const LEGACY_KEYS = [
	"Keys: Enter send · Shift+Enter newline · Esc stop reply · Tab complete · ↓ on empty input picks a subagent · Ctrl+C exit · Ctrl+Shift+←/→ switch agents",
	"Workspace: Ctrl+P quick open · Ctrl+S save · Ctrl+Space completion (/editor complete when macOS steals it) · F12 definition · Shift+F12 references · F2 rename · F6/Shift+F6 cycle focus · Ctrl+→ maximize chat · Ctrl+← back to the 50/50 split (editor focus; composer word motion: Ctrl+←/→) · Ctrl+T explorer · Ctrl+W close tab (terminal may intercept)",
].join("\n");

class RecordingHost {
	printed: Array<{ text: string; kind: string | undefined }> = [];
	opened: string[] = [];
	helpSurface: { openHelpPalette: (query?: string) => void } | undefined;

	print(text: string, kind?: string): void {
		this.printed.push({ text, kind });
	}

	helpPalette() {
		return this.helpSurface;
	}
}

test("HP07 a host without the palette surface keeps the legacy text byte-for-byte", async () => {
	const host = new RecordingHost();
	await runCommand(parseCommand("/help")!, host as unknown as FeatureHost);
	assert.equal(host.printed.length, 1);
	assert.equal(host.printed[0]!.kind, "plain");
	assert.equal(host.printed[0]!.text, `${helpText(SLASH_COMMANDS)}\n\n${LEGACY_KEYS}`);
});

test("HP07 a host with the palette surface opens it and prints nothing", async () => {
	const host = new RecordingHost();
	host.helpSurface = { openHelpPalette: (query) => void host.opened.push(query ?? "") };
	await runCommand(parseCommand("/help")!, host as unknown as FeatureHost);
	assert.deepEqual(host.opened, [""]);
	assert.equal(host.printed.length, 0, "no wall of text on an overlay host");
	await runCommand(parseCommand("/help theme")!, host as unknown as FeatureHost);
	assert.deepEqual(host.opened, ["", "theme"], "/help <query> opens the palette prefiltered");
});

// ─── Part B: real App command path ──────────────────────────────────────────

class ScriptedHelpClient {
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		switch (method) {
			case "initialize":
				return { version: "test", model: "m", provider: "openrouter", cwd: "/tmp", branch: "main" } as T;
			case "config.get":
				return { config: { nowplaying_enabled: false } } as T;
			case "session.create":
				return { session: SESSION } as T;
			case "status":
				return {
					postgres: "ok",
					model: "m",
					provider: "openrouter",
					openrouterKeySet: true,
					sessions: 2,
					contextChunks: 5,
					memories: 3,
					pendingMemories: 1,
					tools: ["shell"],
				} as T;
			default:
				return {} as T;
		}
	}

	methods(): string[] {
		return this.requests.map((entry) => entry.method);
	}
}

function terminal(columns = 100, rows = 30) {
	let input: (data: string) => void = () => {};
	let onResize: () => void = () => {};
	const term: Terminal & { columns: number; rows: number } = {
		columns, rows, kittyProtocolActive: false,
		start(handler, resize) { input = handler; if (resize) onResize = resize; },
		stop() {}, async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	return {
		term,
		type: (data: string) => input(data),
		resizeTo: (nextColumns: number, nextRows: number) => { term.columns = nextColumns; term.rows = nextRows; onResize(); },
	};
}

interface Booted {
	app: App;
	tui: TuiAltScreen;
	client: ScriptedHelpClient;
	type: (data: string) => void;
	resizeTo: (columns: number, rows: number) => void;
	screen: () => string;
}

async function boot(): Promise<Booted> {
	const { term, type, resizeTo } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new ScriptedHelpClient();
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return {
		app,
		tui,
		client,
		type,
		resizeTo,
		screen: () => tui.getScreenLines().map(stripTerminalSequences).join("\n"),
	};
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const paneOf = (app: App) => (app as unknown as { helpPane: HelpPalette | undefined }).helpPane;
const editorOf = (app: App) => (app as unknown as { editor: { getText(): string } }).editor;

test("HP08 real /help opens the palette; typing filters; Enter runs /status", async (t) => {
	const { app, tui, client, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("/help");
	input("\r");
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /Help:/, "bare /help opens the interactive palette");
	assert.match(screen(), /Commands/);
	const pane = paneOf(app);
	assert.ok(pane, "the overlay is mounted");
	assert.ok(pane.getView().commands.some((command) => command.name === "status"), "the full command set is indexed");
	assert.ok(pane.getView().keys.some((key) => key.label.includes("Ctrl+P")), "the key reference is indexed");

	input("status");
	await settle();
	tui.renderNow(true);
	const view = pane.getView();
	assert.ok(view.commands.some((command) => command.name === "status"));
	assert.doesNotMatch(screen(), /\/theme/, "filtered-out rows disappear");
	for (let step = 0; step < view.shown && pane.getView().selected?.id !== "command:status"; step += 1) input("\x1b[B");
	assert.equal(pane.getView().selected?.id, "command:status");

	const mark = client.requests.length;
	input("\r");
	await settle();
	await settle();
	tui.renderNow(true);
	assert.equal(paneOf(app), undefined, "the palette closes before dispatch");
	assert.ok(client.requests.slice(mark).some((entry) => entry.method === "status"), "the command ran through the real dispatcher");
	assert.match(screen(), /Database/);
	assert.doesNotMatch(screen(), /Help:/);
});

test("HP08 Enter on an argument command closes the palette and prefills the composer", async (t) => {
	const { app, tui, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("/help");
	input("\r");
	await settle();
	input("skills show");
	await settle();
	tui.renderNow(true);
	const pane = paneOf(app);
	assert.ok(pane);
	assert.equal(pane.getView().selected?.id, "command:skills");
	input("\r");
	await settle();
	tui.renderNow(true);
	assert.equal(paneOf(app), undefined, "the palette closes");
	assert.equal(editorOf(app).getText(), "/skills ", "the composer draft awaits the missing argument");
	assert.doesNotMatch(screen(), /Help:/);
});

test("HP08 Esc closes the palette without running anything", async (t) => {
	const { app, tui, client, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("/help");
	input("\r");
	await settle();
	const mark = client.requests.length;
	input("\x1b");
	await settle();
	tui.renderNow(true);
	assert.equal(paneOf(app), undefined);
	assert.doesNotMatch(screen(), /Help:/);
	assert.deepEqual(
		client.requests.slice(mark).filter((entry) => entry.method === "status"),
		[],
		"nothing was dispatched",
	);
});

test("HP08 the ? shortcuts menu row reaches the palette", async (t) => {
	const { app, tui, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("?");
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /Keyboard shortcuts/);
	input("all commands");
	input("\r");
	await settle();
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /Help:/, "the ?-menu row now opens the palette");
	assert.ok(paneOf(app));
	assert.doesNotMatch(screen(), /Keyboard shortcuts/);
});
