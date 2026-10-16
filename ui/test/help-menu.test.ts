// Interactive `/help` rebuilt on the house searchable `Menu` (owner directive:
// do it like the theme menu — bounded size, info/hints in the bottom hint row —
// replacing the custom full-screen HelpPalette).
//
// Product rule: menu-shaped surfaces are interactive searchable menus, never a
// printed wall of text. `/help` opens this menu when the host offers the
// surface (`helpPalette()`); hosts without it keep the legacy printed text,
// byte-pinned here.
//
// Part A drives HelpMenu directly (real Menu layout, real filtering, real key
// handling) over deterministic fixtures.
//
// Part B boots the real App on a TuiAltScreen with a scripted terminal and a
// recording client: `/help` opens the menu through the real command path,
// typing filters both sections, Enter runs a no-argument command or prefills
// the composer for a command that needs arguments, Esc closes, and the `?`
// shortcuts menu still reaches the menu.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	stripTerminalSequences,
	TuiAltScreen,
	visibleWidth,
	type Terminal,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { helpText, parseCommand } from "../src/commands.ts";
import { DEFAULT_MENU_VISIBLE_ROWS } from "../src/components/Menu.ts";
import {
	HELP_KEYS,
	HelpMenu,
	helpMenuCommands,
	type HelpCommandItem,
	type HelpKeyItem,
} from "../src/components/HelpMenu.ts";
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

function lines(menu: HelpMenu, width = 70): string[] {
	return menu.render(width).map((line) => stripTerminalSequences(line).trimEnd());
}

function plain(menu: HelpMenu, width = 70): string {
	return lines(menu, width).join("\n");
}

function type(menu: HelpMenu, text: string): void {
	for (const char of text) menu.handleInput(char);
}

function clearQuery(menu: HelpMenu): void {
	for (let index = menu.getView().query.length; index > 0; index -= 1) menu.handleInput("\x7f");
}

// ─── Part A: component ──────────────────────────────────────────────────────

test("HM-01 grouped Commands/Keys in a bounded Menu with the hint last", () => {
	const menu = new HelpMenu({ commands: COMMANDS, keys: KEYS });
	menu.activate();
	const screen = plain(menu);
	assert.match(screen, /^Help\b/m, "the title names the menu");
	assert.match(screen, /Commands/);
	assert.match(screen, /\/status/);
	assert.match(screen, /Keys/);
	assert.match(screen, /Ctrl\+P — quick open/);
	assert.match(screen, /Workspace: Fuzzy file search in the workspace/, "key rows carry their context");
	assert.equal(menu.getView().selected?.id, "command:help", "the first command is selected");
	assert.ok(lines(menu).length <= DEFAULT_MENU_VISIBLE_ROWS + 3, "bounded to the default cap plus chrome");
	assert.equal(lines(menu).length, 13, "10 grouped rows + title/filter/hint");
	assert.match(lines(menu).at(-1)!, /esc close/, "the hint is the last row");
});

test("HM-01 a long command set stays bounded", () => {
	const many = Array.from({ length: 40 }, (_v, index) => ({ name: `cmd${index}`, description: `row ${index}` }));
	const menu = new HelpMenu({ commands: many, keys: [] });
	menu.activate();
	const frame = lines(menu, 80);
	assert.equal(frame.length, DEFAULT_MENU_VISIBLE_ROWS + 3);
	assert.ok(frame.length < 24, "the dialog is shorter than a 24-row page");
	assert.match(frame.at(-1)!, /type to filter/);
});

test("HM-01 initialQuery opens prefiltered", () => {
	const menu = new HelpMenu({ commands: COMMANDS, keys: KEYS, initialQuery: "theme" });
	menu.activate();
	const screen = plain(menu);
	assert.equal(menu.getView().query, "theme");
	assert.match(screen, /\/theme <name>/);
	assert.doesNotMatch(screen, /\/status/);
	assert.doesNotMatch(screen, /Ctrl\+P/);
});

test("HM-02 typing filters name, description, argument hints, keywords and key labels", () => {
	const menu = new HelpMenu({ commands: COMMANDS, keys: KEYS });
	menu.activate();

	type(menu, "quick");
	assert.match(plain(menu), /Ctrl\+P/);
	assert.doesNotMatch(plain(menu), /\/status/, "commands hide when nothing matches");
	assert.doesNotMatch(plain(menu), /^Commands$/m, "an empty section header is hidden");

	clearQuery(menu);
	type(menu, "silent");
	assert.match(plain(menu), /\/quiet/, "description text is searchable");

	clearQuery(menu);
	type(menu, "curator");
	assert.match(plain(menu), /\/skills/, "argument completions are searchable keywords");

	clearQuery(menu);
	type(menu, "list|search");
	assert.match(plain(menu), /\/skills/, "argument hint text is searchable");
	assert.doesNotMatch(plain(menu), /\/quiet/);

	clearQuery(menu);
	type(menu, "zzzz");
	const empty = plain(menu);
	assert.match(empty, /\(no matches — esc to go back\)/, "no-match is a distinct state, never blank");
	assert.doesNotMatch(empty, /\/status/);

	clearQuery(menu);
	assert.match(plain(menu), /\/status/, "clearing the query restores every section");
	assert.match(plain(menu), /Ctrl\+P/);
});

test("HM-02 selection is tracked by stable id across filtering and resize", () => {
	const menu = new HelpMenu({ commands: COMMANDS, keys: KEYS });
	menu.activate();
	menu.handleInput("\x1b[B");
	menu.handleInput("\x1b[B");
	assert.equal(menu.getView().selected?.id, "command:theme", "↓ moves down");
	type(menu, "switch");
	assert.equal(menu.getView().selected?.id, "command:theme", "the id survives a narrower filter even when the row index changes");

	clearQuery(menu);
	type(menu, "status");
	assert.equal(menu.getView().selected?.id, "command:status", "a dropped id falls to the first match");

	clearQuery(menu);
	assert.equal(menu.getView().selected?.id, "command:status", "a surviving id is kept when the filter widens");

	for (let step = 0; step < 8 && menu.getView().selected?.id !== "key:1"; step += 1) menu.handleInput("\x1b[B");
	assert.equal(menu.getView().selected?.id, "key:1");
	type(menu, "workspace");
	assert.equal(menu.getView().selected?.id, "key:1", "the key id survives filtering");

	menu.setVisibleRows(2);
	assert.equal(menu.getView().selected?.id, "key:1", "the id survives a row-budget change");
});

test("HM-03 hostile labels stay on one physical row and never exceed the width", () => {
	const hostile: HelpCommandItem = { name: "evil\n\u001b[31mname\u001b[0m\ttwo", description: "d1\nd2\u0007" };
	const menu = new HelpMenu({ commands: [hostile, { name: "beta", description: "second" }], keys: [] });
	menu.activate();
	const rendered = menu.render(70);
	for (const line of rendered) {
		assert.ok(visibleWidth(line) <= 70, `row wider than 70: ${JSON.stringify(stripTerminalSequences(line))}`);
	}
	const flat = plain(menu);
	assert.match(flat, /evil/);
	assert.match(flat, /beta/);
	assert.ok(!flat.includes("\x1b"), "control sequences never survive");
});

test("HM-04 Enter runs a no-arg command, prefills an argument command, keys are inert", () => {
	const menu = new HelpMenu({ commands: COMMANDS, keys: KEYS });
	const runs: string[] = [];
	const prefills: string[] = [];
	let closes = 0;
	menu.onRun = (name) => void runs.push(name);
	menu.onPrefill = (text) => void prefills.push(text);
	menu.onClose = () => void (closes += 1);
	menu.activate();

	menu.handleInput("\r");
	assert.deepEqual(runs, ["help"], "a command with no argument hint runs");
	assert.deepEqual(prefills, []);

	menu.handleInput("\x1b[B");
	menu.handleInput("\x1b[B");
	assert.equal(menu.getView().selected?.id, "command:theme");
	menu.handleInput("\r");
	assert.deepEqual(prefills, ["/theme "], "a command needing arguments prefills `/name ` for the composer");
	assert.equal(runs.length, 1, "the same Enter never runs and prefills");
	assert.equal(closes, 0, "the host owns the close; the menu never dismisses itself");

	while (menu.getView().selected?.kind !== "key") menu.handleInput("\x1b[B");
	menu.handleInput("\r");
	assert.deepEqual(runs, ["help"], "key rows are reference-only");
	assert.deepEqual(prefills, ["/theme "]);

	menu.handleInput("\x1b");
	assert.equal(closes, 1, "Esc closes through onClose");
});

test("HM-06 helpMenuCommands derives searchable keywords from argument completions", () => {
	const built = helpMenuCommands(SLASH_COMMANDS);
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
	const mapped = helpMenuCommands(throwing);
	assert.equal(mapped[0]?.name, "boom");
	assert.deepEqual(mapped[0]?.keywords, [], "a throwing completion provider never breaks the menu");
});

test("HM-06 HELP_KEYS covers Composer, Workspace and Global contexts", () => {
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

test("HM-07 a host without the palette surface keeps the legacy text byte-for-byte", async () => {
	const host = new RecordingHost();
	await runCommand(parseCommand("/help")!, host as unknown as FeatureHost);
	assert.equal(host.printed.length, 1);
	assert.equal(host.printed[0]!.kind, "plain");
	assert.equal(host.printed[0]!.text, `${helpText(SLASH_COMMANDS)}\n\n${LEGACY_KEYS}`);
});

test("HM-07 a host with the palette surface opens it and prints nothing", async () => {
	const host = new RecordingHost();
	host.helpSurface = { openHelpPalette: (query) => void host.opened.push(query ?? "") };
	await runCommand(parseCommand("/help")!, host as unknown as FeatureHost);
	assert.deepEqual(host.opened, [""]);
	assert.equal(host.printed.length, 0, "no wall of text on an overlay host");
	await runCommand(parseCommand("/help theme")!, host as unknown as FeatureHost);
	assert.deepEqual(host.opened, ["", "theme"], "/help <query> opens the menu prefiltered");
});

// ─── Part B: real App command path ──────────────────────────────────────────

class ScriptedHelpClient {
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];

	start(): void {}
	stderr(): string[] { return []; }
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
					postgres: "ok", model: "m", provider: "openrouter", openrouterKeySet: true,
					sessions: 2, contextChunks: 5, memories: 3, pendingMemories: 1, tools: ["shell"],
				} as T;
			default:
				return {} as T;
		}
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
	screen: () => string;
}

async function boot(): Promise<Booted> {
	const { term, type } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new ScriptedHelpClient();
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return { app, tui, client, type, screen: () => tui.getScreenLines().map(stripTerminalSequences).join("\n") };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const menuOf = (app: App) => (app as unknown as { helpMenu: HelpMenu | undefined }).helpMenu;
const editorOf = (app: App) => (app as unknown as { editor: { getText(): string } }).editor;

test("HM-08 real /help opens the Menu; typing filters; Enter runs /status", async (t) => {
	const { app, tui, client, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("/help");
	input("\r");
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /Help/, "bare /help opens the interactive Menu");
	assert.match(screen(), /Commands/);
	const menu = menuOf(app);
	assert.ok(menu, "the dialog is mounted");
	assert.ok(menu.getView().commands.some((command) => command.name === "status"), "the full command set is indexed");
	assert.ok(menu.getView().keys.some((key) => key.label.includes("Ctrl+P")), "the key reference is indexed");

	input("status");
	await settle();
	tui.renderNow(true);
	assert.doesNotMatch(screen(), /\/theme/, "filtered-out rows disappear");
	for (let step = 0; step < 12 && menu.getView().selected?.id !== "command:status"; step += 1) input("\x1b[B");
	assert.equal(menu.getView().selected?.id, "command:status");

	const mark = client.requests.length;
	input("\r");
	await settle();
	await settle();
	tui.renderNow(true);
	assert.equal(menuOf(app), undefined, "the dialog closes before dispatch");
	assert.ok(client.requests.slice(mark).some((entry) => entry.method === "status"), "the command ran through the real dispatcher");
	assert.match(screen(), /Database/);
	assert.doesNotMatch(screen(), /Help/);
});

test("HM-08 Enter on an argument command closes the Menu and prefills the composer", async (t) => {
	const { app, tui, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("/help");
	input("\r");
	await settle();
	input("skills show");
	await settle();
	tui.renderNow(true);
	const menu = menuOf(app);
	assert.ok(menu);
	assert.equal(menu.getView().selected?.id, "command:skills");
	input("\r");
	await settle();
	tui.renderNow(true);
	assert.equal(menuOf(app), undefined, "the dialog closes");
	assert.equal(editorOf(app).getText(), "/skills ", "the composer draft awaits the missing argument");
	assert.doesNotMatch(screen(), /Help/);
});

test("HM-08 Esc closes the Menu without running anything", async (t) => {
	const { app, tui, client, type: input, screen } = await boot();
	t.after(() => app.exit());
	input("/help");
	input("\r");
	await settle();
	const mark = client.requests.length;
	input("\x1b");
	await settle();
	tui.renderNow(true);
	assert.equal(menuOf(app), undefined);
	assert.doesNotMatch(screen(), /Help/);
	assert.deepEqual(client.requests.slice(mark).filter((entry) => entry.method === "status"), [], "nothing was dispatched");
});

test("HM-08 the ? shortcuts menu row reaches the Menu", async (t) => {
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
	assert.match(screen(), /Help/, "the ?-menu row now opens the Menu");
	assert.ok(menuOf(app));
	assert.doesNotMatch(screen(), /Keyboard shortcuts/);
});
