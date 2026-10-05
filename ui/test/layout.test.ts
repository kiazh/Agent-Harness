// Bottom-docking proof: a real TuiAltScreen against a mock terminal.
// Asserts the composer and footer occupy the bottom rows whether the
// transcript is short (empty space above) or long (follow-end scroll).

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	Container,
	stripTerminalSequences,
	Text,
	TuiAltScreen,
	type Terminal,
} from "@earendil-works/pi-tui";
import { ComposerBox, createLayout, Footer } from "../src/components.ts";
import { editorTheme } from "../src/theme.ts";

interface MockTerminal extends Terminal {
	writes: string[];
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

function boot(columns: number, rows: number, transcriptLines: string[]): { tui: TuiAltScreen; term: MockTerminal } {
	const term = mockTerminal(columns, rows);
	const tui = new TuiAltScreen(term);
	const transcript = new Container();
	for (const line of transcriptLines) transcript.addChild(new Text(line, 0, 0));
	const composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
	const footer = new Footer();
	footer.model = "test-model";
	footer.cwd = "/repo";
	footer.tokens = 7;
	tui.setLayoutRoot(createLayout(transcript, composer, footer));
	tui.start();
	tui.renderNow(true);
	return { tui, term };
}

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

test("short transcript docks composer and footer to the bottom rows", () => {
	const { tui } = boot(80, 24, ["hello world"]);
	try {
		const screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 24);
		assert.match(screen[0] ?? "", /hello world/);
		assert.match(screen[22] ?? "", /test-model/);
		assert.match(screen[23] ?? "", /\? for shortcuts/);
		assert.match(screen[19] ?? "", /Ask AgentHarness to do anything/);
	} finally {
		tui.stop();
	}
});

test("long transcript follows the end above the docked composer", () => {
	const lines = Array.from({ length: 100 }, (_, i) => `line ${i}`);
	const { tui } = boot(80, 24, lines);
	try {
		const screen = plain(tui.getScreenLines());
		assert.equal(screen.length, 24);
		// 19-row viewport over 100 lines: last line sits directly above the composer.
		assert.match(screen[18] ?? "", /line 99/);
		assert.match(screen[0] ?? "", /line 81/);
		assert.match(screen[22] ?? "", /test-model/);
		assert.match(screen[23] ?? "", /\? for shortcuts/);
	} finally {
		tui.stop();
	}
});

test("alt screen takes over on start and restores on stop", () => {
	const { tui, term } = boot(80, 24, ["hi"]);
	try {
		assert.ok(term.writes.join("").includes("?1049h"), "enters the alternate screen");
	} finally {
		tui.stop();
	}
	assert.ok(term.writes.join("").includes("?1049l"), "leaves the alternate screen");
});
