// T2 shell composition tests (plan §7): the opt-in workspace split rendered on
// a real TuiAltScreen, divider drags through synthesized SGR mouse input, and
// retained component identity across mode and density changes.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	Container,
	ScrollView,
	stripTerminalSequences,
	Text,
	TuiAltScreen,
	type Component,
	type Terminal,
} from "@earendil-works/pi-tui";
import { ComposerBox, Footer } from "../src/components.ts";
import { AppShell } from "../src/components/AppShell.ts";
import { WorkspaceShell } from "../src/components/WorkspaceShell.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";
import { editorTheme } from "../src/theme.ts";
import { computeLayout } from "../src/workspace/layout.ts";
import { SPLIT_MINIMUM_COLUMNS, type LayoutInput } from "../src/workspace/state.ts";

interface MockTerminal extends Terminal {
	writes: string[];
	columns: number;
	rows: number;
	/** Captured by start(): the exact path real terminal input takes. */
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

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

/** SGR mouse sequence; columns/rows are 0-based like the events the TUI emits. */
function sgr(button: number, x: number, y: number, release = false): string {
	return `\x1b[<${button};${x + 1};${y + 1}${release ? "m" : "M"}`;
}

/** Mutable pane double whose rendered text is its identity marker. */
class PaneStub implements Component {
	static constructions = 0;
	constructor(text: string) {
		this.text = text;
		PaneStub.constructions += 1;
	}
	text: string;
	render(width: number): string[] {
		const visible = Math.max(0, width);
		return [`${this.text.slice(0, visible).padEnd(visible, "·")}`];
	}
	invalidate(): void {}
}

interface Harness {
	tui: TuiAltScreen;
	term: MockTerminal;
	footer: Footer;
	coordinator: AnimationCoordinator;
	shell: AppShell;
	composer: ComposerBox;
	editor: PaneStub;
	explorer: PaneStub;
	state: LayoutInput;
}

function buildHarness(columns: number, rows: number, transcriptLines = ["chat transcript row"]): Harness {
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
	return {
		tui,
		term,
		footer,
		coordinator,
		shell,
		composer,
		editor: new PaneStub("EDITOR"),
		explorer: new PaneStub("EXPLORER"),
		state: {
			columns,
			rows,
			requestedMode: "split",
			splitRatio: 0.5,
			explorerRequested: false,
			preferredPane: "chat",
		},
	};
}

/** Inject both panes and the persisted split intent, then render a frame. */
function configure(h: Harness): void {
	h.shell.setWorkspaceEditor(h.editor);
	h.shell.setWorkspaceExplorer(h.explorer);
	h.shell.setWorkspaceState(h.state);
	h.tui.renderNow(true);
}

function teardown(h: Harness): void {
	h.footer.dispose();
	h.coordinator.dispose();
	h.tui.stop();
}

test("without workspace state the shell keeps today's chat-only tree", () => {
	const h = buildHarness(80, 24);
	try {
		h.shell.setWorkspaceEditor(h.editor);
		h.tui.renderNow(true);
		const screen = plain(h.tui.getScreenLines());
		assert.equal(screen.length, 24);
		assert.doesNotMatch(screen.join("\n"), /EDITOR/, "injected editor stays hidden until a state arrives");
		assert.equal((screen[0] ?? "").trimEnd(), "chat transcript row", "top row is the plain transcript");
		assert.match(screen[20] ?? "", /Ask anything/);
		assert.match(screen[23] ?? "", /test-model/);
	} finally {
		teardown(h);
	}
});

test("160x48 split composes chat | divider | editor with a full-width footer", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const geometry = computeLayout(h.state);
		assert.equal(geometry.effectiveMode, "split");
		assert.ok(geometry.chat && geometry.editor);
		assert.equal(geometry.chat.x, 0);
		assert.ok(geometry.chat.columns >= 78 && geometry.chat.columns <= 81, `chat ≈79, got ${geometry.chat.columns}`);
		assert.equal(geometry.editor.x, geometry.chat.columns + 1);
		assert.ok(Math.abs(geometry.chat.columns - geometry.editor.columns) <= 2, "an approximately even split");
		const dividerX = geometry.chat.columns;
		const screen = plain(h.tui.getScreenLines());
		assert.equal(screen.length, 48);
		for (const row of [0, 11, 45]) {
			assert.equal(screen[row]!.charAt(dividerX), "│", `divider rule on row ${row}`);
		}
		assert.equal((screen[46] ?? "").trim(), "", "gap row separates the columns from the footer");
		assert.ok(screen[0]!.startsWith("chat transcript row"), "chat pane starts at column 0");
		assert.ok(screen[0]!.slice(0, dividerX).includes("chat transcript row"), "chat pane keeps its columns");
		assert.equal(
			screen[0]!.slice(geometry.editor.x, geometry.editor.x + geometry.editor.columns),
			"EDITOR".padEnd(geometry.editor.columns, "·"),
			"editor pane occupies exactly its geometry columns",
		);
		const footer = screen[47]!;
		assert.equal(footer.length, 160, "footer still spans the full width");
		assert.match(footer, /test-model/);
		assert.doesNotMatch(footer, /│/);
	} finally {
		teardown(h);
	}
});

test("88 columns falls back to a single chat pane and restores the split on resize", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const constructions = PaneStub.constructions;
		h.term.columns = 88;
		h.shell.sync();
		h.tui.renderNow(true);
		const narrow = plain(h.tui.getScreenLines());
		assert.equal(narrow.length, 48);
		assert.equal((narrow[0] ?? "").trimEnd(), "chat transcript row", "no divider or editor column on the fallback");
		assert.doesNotMatch(narrow.join("\n"), /EDITOR/, "editor pane hidden");
		assert.ok(narrow[0]!.includes("chat transcript row"), "chat pane keeps the full width");
		const narrowGeometry = computeLayout({ ...h.state, columns: 88 });
		assert.equal(narrowGeometry.effectiveMode, "chat");
		assert.equal(narrowGeometry.reason, "narrow");
		// Restore: the split returns with the very same injected editor.
		h.term.columns = 160;
		h.shell.sync();
		h.editor.text = "SAME-EDITOR";
		h.tui.renderNow(true);
		assert.equal(PaneStub.constructions, constructions, "the editor was never recreated");
		const restored = plain(h.tui.getScreenLines());
		const geometry = computeLayout(h.state);
		assert.equal(restored[0]!.charAt(geometry.chat!.columns), "│", "divider is back");
		assert.ok(restored[0]!.includes("SAME-EDITOR"), "the retained editor instance renders its mutated state");
	} finally {
		teardown(h);
	}
});

test("transcript ScrollView identity and follow state survive mode and density changes", () => {
	const lines = Array.from({ length: 100 }, (_, i) => `chat row ${i}`);
	const h = buildHarness(160, 48, lines);
	try {
		configure(h);
		const scrolled = h.shell.transcriptScroll;
		assert.ok(scrolled.viewportHeight > 0, "live inside the split composition");
		assert.ok(scrolled.isFollowingEnd);
		for (const requestedMode of ["chat", "editor", "split"] as const) {
			h.shell.setWorkspaceState({ ...h.state, requestedMode });
			h.tui.renderNow(true);
			assert.equal(h.shell.transcriptScroll, scrolled, `identity retained in ${requestedMode} mode`);
			if (requestedMode === "editor") {
				assert.doesNotMatch(plain(h.tui.getScreenLines()).join("\n"), /chat row 99/, "chat hidden in editor mode");
			}
		}
		assert.ok(scrolled.isFollowingEnd, "follow state retained across mode switches");
		h.term.rows = 12;
		h.shell.sync();
		h.tui.renderNow(true);
		assert.equal(h.shell.currentDensity, "reduced");
		assert.equal(h.shell.transcriptScroll, scrolled, "density rebuilds keep the instance");
		assert.ok(scrolled.isFollowingEnd);
	} finally {
		teardown(h);
	}
});

test("divider drag maps screen columns to clamped ratios without zero-width panes", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const ratios: number[] = [];
		h.shell.onWorkspaceRatioChange = (ratio) => {
			ratios.push(ratio);
			h.state = { ...h.state, splitRatio: ratio };
			h.shell.setWorkspaceState(h.state);
		};
		const inject = h.term.onInput!;
		const usable = 160 - 1;
		const initial = computeLayout(h.state);
		const dividerX = initial.chat!.columns;
		const before = plain(h.tui.getScreenLines());
		// Press on the divider, drag to column 40, release.
		inject(sgr(0, dividerX, 5));
		inject(sgr(32, 40, 5));
		assert.deepEqual(plain(h.tui.getScreenLines()), before, "drag coalesces: no render during the move");
		inject(sgr(0, 40, 5, true));
		assert.equal(ratios.length, 1, "one ratio per completed drag");
		assert.ok(Math.abs(ratios[0]! - 40 / usable) < 1e-9, `ratio follows the pointer: ${ratios[0]}`);
		const dragged = computeLayout(h.state);
		assert.equal(dragged.chat!.columns, 40, "the divider lands on the pointer column");
		assert.ok(dragged.editor!.columns >= 48);
		h.tui.renderNow(true);
		assert.equal(plain(h.tui.getScreenLines())[0]!.charAt(40), "│", "the rule moved with the drag");
		// Drag beyond the left edge clamps to the persisted minimum.
		inject(sgr(0, 40, 5));
		inject(sgr(32, 0, 5));
		inject(sgr(0, 0, 5, true));
		assert.equal(ratios.at(-1), 0.25);
		const left = computeLayout(h.state);
		assert.equal(left.chat!.columns, 40, "the chat minimum holds at the left edge");
		assert.ok(left.chat!.columns >= 40 && left.editor!.columns >= 48, "minima hold at the left edge");
		// Drag far outside the screen clamps to the persisted maximum.
		inject(sgr(0, left.chat!.columns, 5));
		inject(sgr(32, 5000, 200));
		inject(sgr(0, 5000, 200, true));
		assert.equal(ratios.at(-1), 0.75);
		const right = computeLayout(h.state);
		assert.equal(right.editor!.columns, 48, "the editor minimum wins over the persisted 0.75");
		assert.ok(right.chat!.columns >= 40, "no zero or negative chat pane");
		// Negative controls: a drag without a press and a non-left press never resize.
		const count = ratios.length;
		inject(sgr(32, 20, 5));
		inject(sgr(2, 60, 5));
		inject(sgr(2, 20, 5, true));
		assert.equal(ratios.length, count, "no capture without a left-button press");
	} finally {
		teardown(h);
	}
});

test("a docking card stays inside the chat column in split mode", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		h.shell.setDock(new PaneStub("DOCK-ROW"));
		h.tui.renderNow(true);
		const geometry = computeLayout(h.state);
		const chatColumns = geometry.chat!.columns;
		const screen = plain(h.tui.getScreenLines());
		const dockRow = screen.findIndex((line) => line.includes("DOCK-ROW"));
		assert.ok(dockRow >= 0, "dock card renders");
		assert.ok(screen[dockRow]!.indexOf("DOCK-ROW") < chatColumns, "inside the chat column");
		assert.equal(screen[dockRow]!.charAt(chatColumns), "│", "divider still at the geometry column");
	} finally {
		teardown(h);
	}
});

test("an injected ScrollView editor keeps its instance and anchor across recomposition", () => {
	const h = buildHarness(160, 48);
	try {
		const content = new Container();
		for (let i = 0; i < 100; i++) content.addChild(new Text(`editor line ${i}`, 0, 0));
		const editorScroll = new ScrollView(content, { follow: "none" });
		h.shell.setWorkspaceEditor(editorScroll);
		h.shell.setWorkspaceState(h.state);
		h.tui.renderNow(true);
		assert.ok(editorScroll.viewportHeight > 0, "editor scroll is live in the split");
		editorScroll.scrollTo(20);
		h.tui.renderNow(true);
		let geometry = computeLayout(h.state);
		assert.ok(
			plain(h.tui.getScreenLines())[0]!.slice(geometry.editor!.x).startsWith("editor line 20"),
			"editor anchored at its scroll position",
		);
		// Recomposition in both directions must not re-scroll or recreate it.
		h.shell.setWorkspaceState({ ...h.state, requestedMode: "chat" });
		h.tui.renderNow(true);
		h.shell.setWorkspaceState(h.state);
		h.tui.renderNow(true);
		geometry = computeLayout(h.state);
		assert.equal(editorScroll.scrollTop, 20, "anchor survives the chat detour");
		assert.ok(
			plain(h.tui.getScreenLines())[0]!.slice(geometry.editor!.x).startsWith("editor line 20"),
			"the same ScrollView renders again",
		);
	} finally {
		teardown(h);
	}
});

test("editor mode gives the editor the full top row and hides the chat", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		h.shell.setWorkspaceState({ ...h.state, requestedMode: "editor" });
		h.tui.renderNow(true);
		const geometry = computeLayout({ ...h.state, requestedMode: "editor" });
		assert.equal(geometry.effectiveMode, "editor");
		assert.equal(geometry.editor!.columns, 160);
		const screen = plain(h.tui.getScreenLines());
		assert.ok(screen[0]!.startsWith("EDITOR"), "editor pane starts at column 0");
		assert.doesNotMatch(screen.join("\n"), /chat transcript row/, "chat hidden but retained");
		assert.doesNotMatch(screen.join("\n"), /Ask anything/, "composer hidden with the chat column");
		assert.match(screen[47] ?? "", /test-model/, "footer still spans the bottom");
		assert.equal(screen[47]!.length, 160);
		// Back to split: the same chat objects return.
		h.shell.setWorkspaceState(h.state);
		h.tui.renderNow(true);
		assert.match(plain(h.tui.getScreenLines())[0]!, /chat transcript row/);
	} finally {
		teardown(h);
	}
});

test("a docked explorer takes its column off the editor, not the chat", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const base = computeLayout(h.state);
		const explorerState = { ...h.state, explorerRequested: true };
		h.shell.setWorkspaceState(explorerState);
		h.tui.renderNow(true);
		const geometry = computeLayout(explorerState);
		assert.ok(geometry.explorer, "docked at this width");
		assert.equal(geometry.chat!.columns, base.chat!.columns, "chat unchanged by the explorer");
		assert.equal(geometry.editor!.columns, base.editor!.columns - geometry.explorer.columns, "editor pays the budget");
		const screen = plain(h.tui.getScreenLines());
		assert.equal(screen[0]!.charAt(geometry.chat!.columns), "│");
		assert.equal(
			screen[0]!.slice(geometry.editor!.x, geometry.editor!.x + geometry.editor!.columns),
			"EDITOR".padEnd(geometry.editor!.columns, "·"),
		);
		assert.equal(
			screen[0]!.slice(geometry.explorer!.x, geometry.explorer!.x + geometry.explorer!.columns),
			"EXPLORER".padEnd(geometry.explorer!.columns, "·"),
		);
		assert.equal(geometry.explorer!.x + geometry.explorer!.columns, 160, "the drawer reaches the right edge");
	} finally {
		teardown(h);
	}
});

test("workspace entries follow the frozen geometry and report overlay mode", () => {
	const term = mockTerminal(SPLIT_MINIMUM_COLUMNS, 48);
	const tui = new TuiAltScreen(term);
	const shell = new WorkspaceShell(tui, { onRatioChange: () => {} });
	try {
		const editor = new PaneStub("EDITOR");
		const explorer = new PaneStub("EXPLORER");
		shell.setEditor(editor);
		shell.setExplorer(explorer);
		const input: LayoutInput = {
			columns: SPLIT_MINIMUM_COLUMNS,
			rows: 48,
			requestedMode: "split",
			splitRatio: 0.5,
			explorerRequested: true,
			preferredPane: "chat",
		};
		shell.setState(input);
		const layout = shell.layout!;
		assert.equal(layout.effectiveMode, "split");
		assert.equal(layout.explorer, undefined);
		assert.equal(shell.explorerOverlay, true, "overlay is reported instead of a column");
		const narrowEntries = shell.entries(layout);
		assert.equal(narrowEntries.length, 2, "no explorer column when the editor would starve");
		assert.equal(narrowEntries[0]!.basis, 1);
		assert.equal(narrowEntries[0]!.component, shell.divider);
		assert.equal(narrowEntries[1]!.basis, layout.editor!.columns);
		assert.equal(narrowEntries[1]!.component, editor, "the injected component is used directly");
		shell.setState({ ...input, columns: 160 });
		const wide = shell.layout!;
		const wideEntries = shell.entries(wide);
		assert.equal(wideEntries.length, 3, "docked explorer adds one column");
		assert.equal(wideEntries[0]!.basis, 1);
		assert.equal(wideEntries[1]!.basis, wide.editor!.columns);
		assert.equal(wideEntries[2]!.basis, wide.explorer!.columns);
		assert.equal(wideEntries[2]!.component, explorer, "injected explorer is used when present");
	} finally {
		tui.stop();
	}
});

test("setState demands a rebuild only when the composed geometry changes", () => {
	const term = mockTerminal(160, 48);
	const tui = new TuiAltScreen(term);
	let demands = 0;
	const shell = new WorkspaceShell(tui, { onRatioChange: () => {}, onResizeDemand: () => { demands += 1; } });
	try {
		const input: LayoutInput = {
			columns: 160,
			rows: 48,
			requestedMode: "split",
			splitRatio: 0.5,
			explorerRequested: false,
			preferredPane: "chat",
		};
		shell.setState(input);
		assert.equal(demands, 1, "the first geometry composes");
		shell.setState({ ...input });
		assert.equal(demands, 1, "identical fields never rebuild (no identity compare)");
		shell.setState({ ...input, splitRatio: 0.5 + 1e-9 });
		assert.equal(demands, 1, "a ratio nudge that keeps the rectangles does not rebuild");
		shell.setState({ ...input, columns: 161 });
		assert.equal(demands, 2, "a real geometry change rebuilds");
	} finally {
		tui.stop();
	}
});

test("degenerate terminals never throw with a workspace configured", () => {
	for (const [columns, rows] of [[0, 6], [0, 0], [1, 6], [6, 1], [20, 6]] as const) {
		const h = buildHarness(columns, rows);
		try {
			assert.doesNotThrow(() => {
				configure(h);
				h.shell.sync();
				h.tui.renderNow(true);
			}, `workspace at ${columns}x${rows}`);
			const screen = h.tui.getScreenLines();
			assert.ok(screen.length >= 1 && screen.length <= Math.max(1, rows), `frame bounded at ${columns}x${rows}`);
		} finally {
			teardown(h);
		}
	}
});
