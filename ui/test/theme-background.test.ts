import assert from "node:assert/strict";
import { test } from "node:test";
import { Text, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { ThemeBackground } from "../src/design/terminal.ts";
import { getSkin, setSkin } from "../src/theme.ts";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";

test("theme background fills alternate-screen clears and survives nested resets", () => {
	const previous = getSkin();
	try {
		setSkin("nord");
		const painter = new ThemeBackground();
		const opened = painter.paint("\x1b[?1049h\x1b[2J");
		// eslint-disable-next-line no-control-regex
		const bg = opened.match(/\x1b\[48;[^m]+m/)?.[0];
		assert.ok(bg, "screen clear carries the theme background");
		const frame = painter.paint("\x1b[2Khello\x1b[0m world\x1b[49m padding");
		assert.ok(frame.includes(`\x1b[0m${bg} world`));
		assert.ok(frame.includes(`\x1b[49m${bg} padding`));
		assert.ok(frame.endsWith("\x1b[0m"), "writes never leak active attributes");
		for (const fill of ["\x1b[48;2;0;20;0m", "\x1b[48;5;0m", "\x1b[0;48;2;0;20;0m"]) {
			assert.ok(painter.paint(`${fill}panel`).includes(`${fill}panel`), "explicit panel colours survive");
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
	let loadTheme!: (value: { config: { theme: string } }) => void;
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
		loadTheme({ config: { theme: "gruvbox" } });
		await starting;
		tui.renderNow();
		assert.equal(getSkin(), "gruvbox");
		assert.ok(writes.some(frame => frame.includes("\x1b[2J") && frame.split(" ".repeat(80)).length - 1 === 24), "saved theme forces all rows to repaint before any / or theme interaction");
	} finally {
		await app.exit();
		setSkin(previous);
	}
});
