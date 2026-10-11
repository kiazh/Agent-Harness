// Bottom-docking proof: a real TuiAltScreen against a mock terminal.
// Asserts the composer and footer occupy the bottom rows whether the
// transcript is short (empty space above) or long (follow-end scroll).

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	Container,
	isViewportTUI,
	stripTerminalSequences,
	Text,
	TuiAltScreen,
	visibleWidth,
	type Terminal,
} from "@earendil-works/pi-tui";
import { ComposerBox, Footer } from "../src/components.ts";
import { AppShell } from "../src/components/AppShell.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";
import { editorTheme } from "../src/theme.ts";

interface MockTerminal extends Terminal {
	writes: string[];
	columns: number;
	rows: number;
}

function mockTerminal(columns: number, rows: number): MockTerminal {
	return {
		writes: [],
		columns,
		rows,
		kittyProtocolActive: false,
		start() {},
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

function boot(columns: number, rows: number, transcriptLines: string[]): {
	tui: TuiAltScreen;
	term: MockTerminal;
	footer: Footer;
	coordinator: AnimationCoordinator;
	shell: AppShell;
	composer: ComposerBox;
} {
	const term = mockTerminal(columns, rows);
	const tui = new TuiAltScreen(term);
	const transcript = new Container();
	for (const line of transcriptLines) transcript.addChild(new Text(line, 0, 0));
	const coordinator = new AnimationCoordinator(() => tui.requestRender());
	const composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
	const footer = new Footer(coordinator);
	footer.model = "test-model";
	footer.cwd = "/repo";
	footer.tokens = 7;
	const shell = new AppShell(tui, transcript, composer, footer);
	tui.setLayoutRoot(shell.root);
	tui.start();
	tui.renderNow(true);
	return { tui, term, footer, coordinator, shell, composer };
}

test("the learning square stays on the bottom-right without moving transcript or composer", () => {
	for (const [columns, rows] of [[40, 9], [80, 24], [120, 40]]) {
		const { tui, footer, coordinator } = boot(columns!, rows!, ["Keep this conversation still"]);
		try {
			const before = plain(tui.getScreenLines());
			footer.pulseGrowth();
			tui.renderNow();
			const during = plain(tui.getScreenLines());
			assert.deepEqual(during.slice(0, -1), before.slice(0, -1));
			assert.match(during.at(-1)!, /▪$/);
			assert.equal(during.at(-1)!.length, columns);
		} finally {
			footer.dispose();
			coordinator.dispose();
			tui.stop();
		}
	}
});

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

test("short transcript docks composer and footer to the bottom rows", () => {
	const { tui, footer, coordinator } = boot(80, 24, ["hello world"]);
	try {
		const screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 24);
		assert.match(screen[0] ?? "", /hello world/);
		assert.match(screen[23] ?? "", /test-model/);
		assert.match(screen[23] ?? "", /\? shortcuts/);
		assert.match(screen[20] ?? "", /Ask anything, or type \/ for commands\./);
		assert.equal((screen[18] ?? "").trim(), "", "one blank row separates the transcript from the composer");
		assert.equal((screen[22] ?? "").trim(), "", "one blank row separates the composer from the footer");
	} finally {
		footer.dispose();
		coordinator.dispose();
		tui.stop();
	}
});

test("long transcript follows the end above the docked composer", () => {
	const lines = Array.from({ length: 100 }, (_, i) => `line ${i}`);
	const { tui, footer, coordinator } = boot(80, 24, lines);
	try {
		const screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 24);
		// 17-row viewport over 100 lines: last line sits one gap row above the composer.
		assert.match(screen[17] ?? "", /line 99/);
		assert.match(screen[0] ?? "", /line 82/);
		assert.match(screen[23] ?? "", /test-model/);
		assert.match(screen[23] ?? "", /\? shortcuts/);
	} finally {
		footer.dispose();
		coordinator.dispose();
		tui.stop();
	}
});

test("a real alt screen reports itself as a viewport TUI", () => {
	const tui = new TuiAltScreen(mockTerminal(80, 24));
	try {
		assert.equal(isViewportTUI(tui), true, "App takes the setLayoutRoot path in production");
	} finally {
		tui.stop();
	}
});

test("docking holds at multiple viewport heights", () => {
	for (const rows of [16, 40, 60]) {
		const term = mockTerminal(80, rows);
		const tui = new TuiAltScreen(term);
		const coordinator = new AnimationCoordinator(() => tui.requestRender());
		const footer = new Footer(coordinator);
		try {
			const transcript = new Container();
			transcript.addChild(new Text("hello", 0, 0));
			const composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
			footer.tokens = 1;
			const shell = new AppShell(tui, transcript, composer, footer);
			tui.setLayoutRoot(shell.root);
			tui.start();
			tui.renderNow(true);
			const screen = plain(tui.getScreenLines());
			assert.equal(screen.length, rows, `frame fills ${rows} rows`);
			assert.match(screen[rows - 1] ?? "", /\? shortcuts/, `hints on last row at height ${rows}`);
			assert.match(screen[rows - 1] ?? "", /1 token/, `usage shares hints row at height ${rows}`);
			shell.dispose();
		} finally {
			footer.dispose();
			coordinator.dispose();
			tui.stop();
		}
	}
});

test("alt screen takes over on start and restores on stop", () => {
	const { tui, term, footer, coordinator } = boot(80, 24, ["hi"]);
	try {
		assert.ok(term.writes.join("").includes("?1049h"), "enters the alternate screen");
	} finally {
		footer.dispose();
		coordinator.dispose();
		tui.stop();
	}
	assert.ok(term.writes.join("").includes("?1049l"), "leaves the alternate screen");
});

test("V04/V05 unicode, ANSI and paths stay inside every width without throwing", async () => {
	const { AssistantTurn, DiffBlock, Notice, Picker, ToolTurn, UserTurn } = await import("../src/components.ts");
	const { OutputViewer, OutputStore } = await import("../src/components/OutputViewer.ts");
	const { AnimationCoordinator } = await import("../src/design/motion.ts");
	const activity = { frame: (frames: readonly string[]) => frames[0] ?? "" };
	const store = new OutputStore();
	store.put("s", "t1", "terminal", "日本語 👩‍💻\x1b[31mred\x1b[0m", false);
	const widgets: Array<[string, { render(width: number): string[] }]> = [
		["user", new UserTurn("日本語テスト 👩‍💻 é /very/long/unbroken/path/that/never/breaks/0123456789")],
		["assistant", new AssistantTurn("# 日本語 **bold** `code` 👩‍💻")],
		["tool", (() => {
			const turn = new ToolTurn("terminal", { command: "/very/long/unbroken/path/with/args --flag=value" }, activity);
			turn.settle({ text: "日本語 output\n\x1b[31mred\x1b[0m\n+added\n-removed", backendTruncated: false, locallyTruncated: false }, "success");
			return turn;
		})()],
		["notice", new Notice("日本語 notice 👩‍💻", "warning")],
		["diff", new DiffBlock(["@@ -1 +1 @@", "+日本語", "-old", "    indented"])],
		["viewer", new OutputViewer(store.get("s", "t1")!)],
		["picker", new Picker("T", [{ value: "日本語", label: "日本語ラベル" }], () => {}, () => {})],
	];
	for (const width of [0, 1, 2, 20, 40, 80, 120]) {
		for (const [name, widget] of widgets) {
			let lines: string[] = [];
			assert.doesNotThrow(() => {
				lines = widget.render(width);
			}, `${name} throws at width ${width}`);
			for (const line of lines) {
				assert.ok(visibleWidth(line) <= width, `${name} exceeds width ${width}: ${stripTerminalSequences(line)}`);
			}
		}
		const coordinator = new AnimationCoordinator(() => {}, false);
		const footer = new Footer(coordinator);
		footer.model = "日本語-model";
		footer.cwd = "/repo/日本語";
		for (const line of footer.render(width)) assert.ok(visibleWidth(line) <= width, `footer exceeds ${width}`);
		footer.dispose();
		coordinator.dispose();
	}
});

test("V06 production shell docks at 40x9, 80x24 and 120x40", () => {
	for (const [columns, rows] of [[40, 9], [80, 24], [120, 40]] as const) {
		const { tui, footer, coordinator, shell } = boot(columns, rows, ["hello"]);
		try {
			const screen = plain(tui.getScreenLines());
			assert.equal(screen.length, rows, `frame fills ${columns}x${rows}`);
			for (const line of screen) assert.ok(line.length <= columns, `row exceeds ${columns}`);
			if (columns >= 80) assert.match(screen[rows - 1] ?? "", /\? shortcuts/, "hints share footer when space permits");
			else assert.match(screen[rows - 1] ?? "", /□$/, "quiet status remains at narrow widths");
			assert.match(screen[rows - 1] ?? "", /test-model/, "status shares hints row");
			assert.ok(screen.some((line) => line.includes("Ask anything")), "composer visible");
			assert.equal(shell.currentDensity, rows >= 16 ? "full" : "reduced");
			assert.ok(shell.transcriptScroll.isFollowingEnd, "following the end on boot");
		} finally {
			footer.dispose();
			coordinator.dispose();
			shell.dispose();
			tui.stop();
		}
	}
});

test("density budgets follow live resizes without losing scroll anchor", () => {
	const lines = Array.from({ length: 60 }, (_, i) => `row ${i}`);
	const { tui, term, footer, coordinator, shell, composer } = boot(80, 24, lines);
	try {
		assert.equal(shell.currentDensity, "full");
		const scrolled = shell.transcriptScroll;
		const before = scrolled.scrollTop;
		assert.ok(before > 0, "tall transcript starts scrolled with end followed");
		term.rows = 12;
		shell.sync();
		tui.renderNow(true);
		assert.equal(shell.currentDensity, "reduced");
		let screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 12);
		assert.match(screen[11] ?? "", /\? shortcuts/);
		assert.match(screen[11] ?? "", /test-model/);
		assert.ok(composer.render(80).length <= 4, "composer capped to its 1-4 row window");
		term.rows = 7;
		shell.sync();
		tui.renderNow(true);
		assert.equal(shell.currentDensity, "compact");
		screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 7);
		assert.doesNotMatch(screen.join("\n"), /\? shortcuts/, "hints hide first at small heights");
		assert.match(screen[6] ?? "", /test-model/, "status row survives");
		assert.ok(composer.render(80).length <= 2, "composer uses 1-2 editable rows");
		assert.equal(scrolled, shell.transcriptScroll, "ScrollView identity retained across rebuilds");
	} finally {
		footer.dispose();
		coordinator.dispose();
		shell.dispose();
		tui.stop();
	}
});

test("V08 scrolling up freezes follow while streaming until return-to-end", () => {
	const lines = Array.from({ length: 100 }, (_, i) => `line ${i}`);
	const { tui, footer, coordinator, shell } = boot(80, 24, lines);
	try {
		const scrolled = shell.transcriptScroll;
		assert.ok(scrolled.isFollowingEnd);
		scrolled.scrollBy(-20);
		tui.renderNow(true);
		assert.equal(scrolled.isFollowingEnd, false, "scrolling up freezes follow");
		const anchored = scrolled.scrollTop;
		tui.renderNow(true);
		assert.equal(scrolled.scrollTop, anchored, "anchor stays while unfollowed");
		scrolled.scrollToEnd();
		tui.renderNow(true);
		assert.ok(scrolled.isFollowingEnd, "explicit return-to-end resumes follow");
	} finally {
		footer.dispose();
		coordinator.dispose();
		shell.dispose();
		tui.stop();
	}
});

test("degraded terminals stay safe with the draft preserved", () => {
	const { tui, term, footer, coordinator, shell, composer } = boot(80, 24, ["hi"]);
	try {
		composer.editor.setText("precious draft");
		term.columns = 15;
		term.rows = 5;
		shell.sync();
		tui.renderNow(true);
		assert.equal(shell.currentDensity, "degraded");
		const screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 5);
		assert.match(screen.join("\n"), /Too small/, "minimal resize instruction");
		assert.equal(composer.editor.getText(), "precious draft", "draft preserved");
		assert.equal(footer.render(15).length, 1, "one status row");
	} finally {
		footer.dispose();
		coordinator.dispose();
		shell.dispose();
		tui.stop();
	}
});

test("composer caps a large paste around the visible cursor", () => {
	const { tui, footer, coordinator, shell, composer } = boot(80, 24, ["hi"]);
	try {
		composer.editor.setText(Array.from({ length: 30 }, (_, i) => `pasted ${i}`).join("\n"));
		const rows = composer.render(80).map((line) => stripTerminalSequences(line));
		assert.ok(rows.length <= 8, `composer bounded, got ${rows.length}`);
		assert.match(rows.join("\n"), /pasted 29/, "window keeps the cursor region");
	} finally {
		footer.dispose();
		coordinator.dispose();
		shell.dispose();
		tui.stop();
	}
});

test("terminal resize updates density and chrome without needing a key or gateway event", () => {
 const {tui, term, footer, coordinator, shell, composer} = boot(80, 24, ["hi"]);
 try {
  term.rows = 7;
  term.columns = 40;
  tui.renderNow(true);
  assert.equal(shell.currentDensity, "compact");
  assert.ok(composer.render(40).length <= 2);
  assert.doesNotMatch(plain(tui.getScreenLines()).join("\n"), /\? shortcuts/);
  term.rows = 24;
  term.columns = 80;
  tui.renderNow(true);
  assert.equal(shell.currentDensity, "full");
  assert.match(plain(tui.getScreenLines()).at(-1)!, /\? shortcuts/);
 } finally {footer.dispose(); coordinator.dispose(); tui.stop();}
});
