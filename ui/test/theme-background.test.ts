import assert from "node:assert/strict";
import { test } from "node:test";
import {
	Text,
	TuiAltScreen,
	backgroundAnsi,
	getTerminalColorMode,
	parseColor,
	type Terminal,
} from "@earendil-works/pi-tui";
import { ThemeBackground } from "../src/design/terminal.ts";
import {
	isOpaqueBackground,
	repairFill,
	selectListTheme,
	selectionBar,
	selectionFill,
	semanticPalette,
	setOpaqueBackground,
	surfaceLine,
	theme,
} from "../src/design/theme.ts";
import { getSkin, isSelectionRow, setSkin } from "../src/theme.ts";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";

test("theme background fills alternate-screen clears and survives nested resets", () => {
	const previous = getSkin();
	try {
		setSkin("nord");
		const painter = new ThemeBackground();
		const opened = painter.paint("\x1b[?1049h\x1b[2J");
		// eslint-disable-next-line no-control-regex
		const fg = opened.match(/\x1b\[38;[^m]+m/)?.[0];
		assert.ok(fg, "screen clear carries explicit theme ink");
		// eslint-disable-next-line no-control-regex
		const bg = opened.match(/\x1b\[48;[^m]+m/)?.[0];
		assert.ok(bg, "screen clear carries the theme background");
		const frame = painter.paint("\x1b[2Khello\x1b[0m world\x1b[49m padding");
		assert.ok(frame.includes(`\x1b[0m${fg}${bg} world`));
		assert.ok(frame.includes(`\x1b[49m${bg} padding`));
		assert.ok(frame.endsWith("\x1b[0m"), "writes never leak active attributes");
		for (const fill of ["\x1b[48;2;0;20;0m", "\x1b[48;5;0m", "\x1b[0;48;2;0;20;0m"]) {
			assert.ok(painter.paint(`${fill}panel`).split(fg).join("").includes(`${fill}panel`), "explicit panel colours survive");
		}
	} finally { setSkin(previous); }
});

test("theme preview and cancel change the entire screen background", () => {
	const previous = getSkin();
	try {
		const painter = new ThemeBackground();
		setSkin("nord");
		painter.paint("\x1b[?1049h");
		const original = painter.paint("\x1b[2J");
		setSkin("light");
		assert.notEqual(painter.paint("\x1b[2J"), original);
		setSkin("nord");
		assert.equal(painter.paint("\x1b[2J"), original);
	} finally { setSkin(previous); }
});

test("theme background leaves the operator's normal terminal unchanged", () => {
	const painter = new ThemeBackground();
	assert.equal(painter.paint("normal output"), "normal output");
	painter.paint("\x1b[?1049h");
	const closed = painter.paint("cleanup\x1b[?1049lrestored shell\x1b[0m");
	assert.ok(closed.endsWith("\x1b[0m\x1b[?1049lrestored shell\x1b[0m"));
	assert.equal(painter.paint("next prompt"), "next prompt");
	assert.ok(!closed.includes("\x1b]10;") && !closed.includes("\x1b]11;"), "no persistent terminal palette changes");
});

test("blank viewport rows are explicitly painted on terminals without background-colour erase", () => {
	const painter = new ThemeBackground();
	painter.paint("\x1b[?1049h", 241);
	const frame = painter.paint("\x1b[2;1H\x1b[2K\x1b[0m\x1b[3;1H\x1b[2K", 241);
	assert.equal(frame.split(" ".repeat(241)).length - 1, 2, "every cleared row has actual coloured cells");
	assert.ok(frame.includes(`\x1b7\r${" ".repeat(241)}\x1b8`), "row filling preserves the draw cursor");
	const resized = painter.paint("\x1b[2K", 80);
	assert.ok(resized.includes(" ".repeat(80)));
	assert.ok(!resized.includes(" ".repeat(81)), "resize uses current dimensions");
});

test("real fullscreen launch paints every viewport row, including empty transcript space", () => {
	const painter = new ThemeBackground();
	const writes: string[] = [];
	const terminal = {
		columns: 80, rows: 24, kittyProtocolActive: false,
		start() {}, stop() {}, hideCursor() {}, showCursor() {},
		async drainInput() {}, moveBy() {}, clearLine() {}, clearFromCursor() {},
		clearScreen() {}, setTitle() {}, setProgress() {},
		write(data: string) { writes.push(painter.paint(data, this.columns)); },
	} as Terminal;
	const tui = new TuiAltScreen(terminal);
	tui.addChild(new Text("Machina", 0, 0));
	try {
		tui.start();
		tui.renderNow(true);
		const frame = writes.findLast(write => write.includes("Machina"));
		assert.ok(frame);
		assert.equal(frame.split(" ".repeat(80)).length - 1, 24, "all 24 rows have explicit background cells");
	} finally { tui.stop({ preserveScreen: true }); }
});

test("saved theme arriving after the first launch frame repaints the entire viewport without input", async () => {
	const previous = getSkin();
	setSkin("codex");
	const painter = new ThemeBackground();
	const writes: string[] = [];
	const terminal = {
		columns: 80, rows: 24, kittyProtocolActive: false,
		start() {}, stop() {}, hideCursor() {}, showCursor() {},
		async drainInput() {}, moveBy() {}, clearLine() {}, clearFromCursor() {},
		clearScreen() {}, setTitle() {}, setProgress() {},
		write(data: string) { writes.push(painter.paint(data, this.columns)); },
	} as Terminal;
	let loadTheme!: (value: { config: { theme: string; nowplaying_enabled: boolean } }) => void;
	const config = new Promise(resolve => { loadTheme = resolve; });
	let requestedTheme = false;
	const client = {
		start() {}, async stop() {}, stderr() { return []; },
		async request(method: string) {
			if (method === "initialize") return { version: "test", cwd: "/tmp", model: "test", provider: "test" };
			if (method === "config.get") { requestedTheme = true; return config; }
			if (method === "mode.get") return { mode: "ask" };
			if (method === "profile.get") return {};
			if (method === "session.create") return { session: { id: "test", title: "new", model: "test", provider: "test", status: "active", lastActivity: null } };
			throw new Error(`Unexpected ${method}`);
		},
	};
	const tui = new TuiAltScreen(terminal);
	const app = new App(tui, client as unknown as GatewayClient);
	try {
		const starting = app.start();
		for (let i = 0; i < 10 && !requestedTheme; i++) await Promise.resolve();
		assert.ok(requestedTheme);
		tui.renderNow(true);
		assert.equal(getSkin(), "codex", "first frame precedes saved configuration");
		writes.length = 0;
		loadTheme({ config: { theme: "gruvbox", nowplaying_enabled: false } });
		await starting;
		tui.renderNow();
		assert.equal(getSkin(), "gruvbox");
		assert.ok(writes.some(frame => frame.includes("\x1b[2J") && frame.split(" ".repeat(80)).length - 1 === 24), "saved theme forces all rows to repaint before any / or theme interaction");
	} finally {
		await app.exit();
		setSkin(previous);
	}
});

// ─── Transparent background (terminal default) ──────────────────────────────
// One central switch (`setOpaqueBackground`) drives every background emission.
// Default ON keeps the byte-identical opaque canvas; OFF drops the background
// SGR everywhere while keeping foreground styling.
/* eslint-disable no-control-regex -- SGR inspection needs the ESC character */

test("opaque canvas is the default and toggling it off then on is byte-identical", () => {
	const previousSkin = getSkin();
	const previousOpaque = isOpaqueBackground();
	try {
		assert.equal(previousOpaque, true, "the module default keeps the opaque canvas");
		setSkin("nord");
		setOpaqueBackground(true);
		const mode = getTerminalColorMode();
		const sample = () => ({
			surface: surfaceLine(theme.accent("menu") + theme.dim(" 2"), 20),
			selection: selectionBar(theme.text("row"), 8),
			userBg: theme.userBg("composer"),
			toolFill: repairFill("toolBg", theme.toolBg("tool")),
			frame: new ThemeBackground().paint("\x1b[?1049h\x1b[2J\x1b[2Khello"),
		});
		const opaque = sample();
		// Formula pin: opaque output is exactly the skin's background SGR.
		const surfaceBg = backgroundAnsi(parseColor(semanticPalette("nord")!.surface), mode);
		const selectionBg = backgroundAnsi(parseColor(selectionFill()), mode);
		assert.ok(opaque.surface.includes(surfaceBg), "opaque surface carries the skin surface fill");
		assert.ok(opaque.selection.includes(selectionBg), "opaque selection carries the selection fill");
		assert.ok(opaque.userBg.includes("\x1b[48;"), "opaque chrome carries a background");
		setOpaqueBackground(false);
		assert.notEqual(sample().surface, opaque.surface, "transparent differs from opaque");
		setOpaqueBackground(true);
		assert.deepEqual(sample(), opaque, "switching back on restores byte-identical output");
	} finally {
		setSkin(previousSkin);
		setOpaqueBackground(previousOpaque);
	}
});

test("transparent canvas emits no background SGR for surfaces, bars and chrome, keeping ink", () => {
	const previousSkin = getSkin();
	const previousOpaque = isOpaqueBackground();
	try {
		setSkin("nord");
		setOpaqueBackground(false);

		const surface = surfaceLine(theme.accent("menu"), 20);
		assert.ok(!/\x1b\[48;/.test(surface), "surface paints no background colour");
		assert.ok(!surface.includes("\x1b[49m"), "surface paints no background reset");
		assert.ok(/\x1b\[38;/.test(surface), "surface keeps explicit foreground ink");
		assert.ok(surface.endsWith("\x1b[0m"), "surface still terminates in a full reset");

		assert.ok(!/\x1b\[48;/.test(selectionBar("row", 5)), "selection bar paints no background");
		assert.ok(!/\x1b\[48;/.test(selectListTheme.selectedText("x")), "select list paints no background");

		assert.equal(theme.userBg("composer"), "composer", "composer chrome paints nothing");
		assert.equal(theme.assistantBg("turn"), "turn", "assistant card paints nothing");
		assert.equal(theme.toolBg("tool"), "tool", "tool card paints nothing");
		assert.equal(repairFill("toolBg", theme.toolBg("tool")), "tool", "no fill to repair");

		const frame = new ThemeBackground().paint("\x1b[?1049h\x1b[2J\x1b[2Khello");
		assert.ok(!/\x1b\[48;/.test(frame), "frame paints no canvas background");
		assert.ok(!frame.includes("\x1b[49m"), "frame paints no canvas background reset");
		assert.ok(/\x1b\[38;/.test(frame), "frame keeps explicit foreground ink");
	} finally {
		setSkin(previousSkin);
		setOpaqueBackground(previousOpaque);
	}
});

test("selection rows stay visible without a background via reverse video and bold", () => {
	const previousSkin = getSkin();
	const previousOpaque = isOpaqueBackground();
	try {
		setSkin("nord");
		setOpaqueBackground(false);
		const bar = selectionBar(theme.text("row"), 8);
		assert.ok(!/\x1b\[48;/.test(bar), "no fill is painted");
		assert.ok(bar.includes("\x1b[7m"), "reverse video makes the row unmistakable");
		assert.ok(bar.includes("\x1b[1m"), "bold adds emphasis");
		assert.ok(bar.includes("\x1b[27m") && bar.includes("\x1b[22m"), "reverse and bold close so the row is delimited");
		assert.equal(bar.replace(/\x1b\[[0-9;]*m/g, "").length, 8, "bar spans the requested width");
		assert.equal(isSelectionRow(bar), true, "selection is still recognizable without color");

		const menuRow = selectListTheme.selectedText("item");
		assert.ok(!/\x1b\[48;/.test(menuRow), "menu selection paints no background");
		assert.ok(menuRow.includes("\x1b[7m"), "menu selection uses reverse video");
	} finally {
		setSkin(previousSkin);
		setOpaqueBackground(previousOpaque);
	}
});

test("transparent mode leaves blank-row erases to the terminal instead of painting cells", () => {
	const previousOpaque = isOpaqueBackground();
	try {
		setOpaqueBackground(false);
		const painter = new ThemeBackground();
		painter.paint("\x1b[?1049h", 80);
		const frame = painter.paint("\x1b[2;1H\x1b[2K\x1b[0m\x1b[3;1H\x1b[2K", 80);
		assert.ok(!frame.includes(" ".repeat(80)), "no 80-cell background fill");
		assert.ok(frame.includes("\x1b[2K"), "the erase-to-default stays");
		assert.ok(!/\x1b\[48;/.test(frame), "no background SGR introduced");
	} finally {
		setOpaqueBackground(previousOpaque);
	}
});

test("transparent background from the env override is applied before the first frame", async () => {
	const previousEnv = process.env.MACHINA_TRANSPARENT_BACKGROUND;
	const previousOpaque = isOpaqueBackground();
	process.env.MACHINA_TRANSPARENT_BACKGROUND = "1";
	const painter = new ThemeBackground();
	const writes: string[] = [];
	const opaqueAtWrite: boolean[] = [];
	const terminal = {
		columns: 80, rows: 24, kittyProtocolActive: false,
		start() {}, stop() {}, hideCursor() {}, showCursor() {},
		async drainInput() {}, moveBy() {}, clearLine() {}, clearFromCursor() {},
		clearScreen() {}, setTitle() {}, setProgress() {},
		write(data: string) { writes.push(painter.paint(data, this.columns)); opaqueAtWrite.push(isOpaqueBackground()); },
	} as Terminal;
	const client = {
		start() {}, async stop() {}, stderr() { return []; },
		async request(method: string) {
			if (method === "initialize") return { version: "test", cwd: "/tmp", model: "test", provider: "test" };
			if (method === "config.get") return { config: {} };
			if (method === "mode.get") return { mode: "ask" };
			if (method === "profile.get") return {};
			if (method === "session.create") return { session: { id: "test", title: "new", model: "test", provider: "test", status: "active", lastActivity: null } };
			throw new Error(`Unexpected ${method}`);
		},
	};
	const tui = new TuiAltScreen(terminal);
	const app = new App(tui, client as unknown as GatewayClient);
	try {
		await app.start();
		assert.ok(opaqueAtWrite.length > 0, "the first frame was painted");
		assert.equal(opaqueAtWrite[0], false, "the very first frame already renders transparent");
		assert.ok(!/\x1b\[48;/.test(writes.join("")), "no canvas background in the boot frame");
		assert.ok(writes.join("").includes("\x1b[38;"), "foreground ink is still painted");
	} finally {
		await app.exit();
		setOpaqueBackground(previousOpaque);
		if (previousEnv === undefined) delete process.env.MACHINA_TRANSPARENT_BACKGROUND;
		else process.env.MACHINA_TRANSPARENT_BACKGROUND = previousEnv;
	}
});

test("a saved transparent background repaints the whole viewport after config.get", async () => {
	const previousEnv = process.env.MACHINA_TRANSPARENT_BACKGROUND;
	delete process.env.MACHINA_TRANSPARENT_BACKGROUND;
	const previousOpaque = isOpaqueBackground();
	setOpaqueBackground(true);
	const painter = new ThemeBackground();
	const writes: string[] = [];
	const terminal = {
		columns: 80, rows: 24, kittyProtocolActive: false,
		start() {}, stop() {}, hideCursor() {}, showCursor() {},
		async drainInput() {}, moveBy() {}, clearLine() {}, clearFromCursor() {},
		clearScreen() {}, setTitle() {}, setProgress() {},
		write(data: string) { writes.push(painter.paint(data, this.columns)); },
	} as Terminal;
	let loadConfig!: (value: { config: { theme: string; transparent_background: boolean } }) => void;
	const config = new Promise(resolve => { loadConfig = resolve; });
	let requestedConfig = false;
	const client = {
		start() {}, async stop() {}, stderr() { return []; },
		async request(method: string) {
			if (method === "initialize") return { version: "test", cwd: "/tmp", model: "test", provider: "test" };
			if (method === "config.get") { requestedConfig = true; return config; }
			if (method === "mode.get") return { mode: "ask" };
			if (method === "profile.get") return {};
			if (method === "session.create") return { session: { id: "test", title: "new", model: "test", provider: "test", status: "active", lastActivity: null } };
			throw new Error(`Unexpected ${method}`);
		},
	};
	const tui = new TuiAltScreen(terminal);
	const app = new App(tui, client as unknown as GatewayClient);
	try {
		const starting = app.start();
		for (let i = 0; i < 10 && !requestedConfig; i++) await Promise.resolve();
		assert.ok(requestedConfig);
		tui.renderNow(true);
		assert.equal(isOpaqueBackground(), true, "first frame precedes the saved background setting");
		writes.length = 0;
		loadConfig({ config: { theme: "codex", transparent_background: true } });
		await starting;
		tui.renderNow();
		assert.equal(isOpaqueBackground(), false, "the saved setting was applied");
		const repaint = writes.find(frame => frame.includes("\x1b[2J"));
		assert.ok(repaint, "the setting forces a full repaint");
		assert.ok(!/\x1b\[48;/.test(repaint!), "the repaint paints no canvas background");
		assert.ok(/\x1b\[38;/.test(repaint!), "the repaint keeps explicit foreground ink");
	} finally {
		await app.exit();
		setOpaqueBackground(previousOpaque);
		if (previousEnv === undefined) delete process.env.MACHINA_TRANSPARENT_BACKGROUND;
		else process.env.MACHINA_TRANSPARENT_BACKGROUND = previousEnv;
	}
});
