// T2 shell composition tests (plan §7 + UX batch): the opt-in workspace split
// rendered on a real TuiAltScreen, divider drags through synthesized SGR mouse
// input, the far-right explorer grip, full-width composer/dock rows, and
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
import { LAYOUT_BUDGETS, SPLIT_MINIMUM_COLUMNS, type LayoutInput } from "../src/workspace/state.ts";

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
	explorerToggles: () => number;
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
	let toggles = 0;
	shell.onWorkspaceExplorerToggle = () => {
		toggles += 1;
	};
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
		explorerToggles: () => toggles,
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

/** First screen row of the composed panes area (top row minus the chrome). */
function paneAreaRows(h: Harness): number {
	const geometry = computeLayout(h.state);
	return geometry.editor?.rows ?? 0;
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
		assert.equal(screen.length, 24);
		const composerRow = screen.findIndex((line) => line.includes("Ask anything"));
		assert.ok(composerRow > 0, "composer visible");
		assert.match(screen[23] ?? "", /test-model/);
	} finally {
		teardown(h);
	}
});

test("160x48 split composes chat | divider | editor | grip, docked composer full width", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const geometry = computeLayout(h.state);
		assert.equal(geometry.effectiveMode, "split");
		assert.ok(geometry.chat && geometry.editor && geometry.grip);
		assert.equal(geometry.chat.x, 0);
		assert.ok(geometry.chat.columns >= 78 && geometry.chat.columns <= 81, `chat ≈79, got ${geometry.chat.columns}`);
		assert.equal(geometry.editor.x, geometry.chat.columns + 1);
		assert.ok(Math.abs(geometry.chat.columns - geometry.editor.columns) <= 2, "an approximately even split");
		assert.equal(geometry.grip.x, 159);
		const dividerX = geometry.chat.columns;
		const gripX = geometry.grip.x;
		const screen = plain(h.tui.getScreenLines());
		assert.equal(screen.length, 48);
		const topRow = paneAreaRows(h) - h.shell.chromeRows();
		assert.ok(topRow > 0, "the panes area keeps rows");
		for (const row of [0, 11, topRow - 1]) {
			assert.equal(screen[row]!.charAt(dividerX), "│", `divider rule on row ${row}`);
		}
		assert.ok(screen[0]!.startsWith("chat transcript row"), "chat pane starts at column 0");
		assert.ok(screen[0]!.slice(0, dividerX).includes("chat transcript row"), "chat pane keeps its columns");
		assert.equal(
			screen[0]!.slice(geometry.editor.x, geometry.editor.x + geometry.editor.columns),
			"EDITOR".padEnd(geometry.editor.columns, "·"),
			"editor pane occupies exactly its geometry columns",
		);
		// The grip: faint top notch in the far-right column, never wider.
		assert.equal(screen[0]!.charAt(gripX), "▐");
		for (const line of screen) assert.ok(line.length <= 160);
		// The composer is a FULL-WIDTH row under the panes (starts at column 0).
		const composerRow = screen.findIndex((line) => line.includes("Ask anything"));
		assert.ok(composerRow > topRow, "composer sits below the panes area");
		assert.ok(screen[composerRow]!.indexOf("Ask anything") < 10, "composer prompt starts at the left edge");
		assert.notEqual(screen[composerRow]!.charAt(dividerX), "│", "no divider crosses the composer row");
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
		// The resizable span excludes the divider and the grip columns.
		const usable = 160 - LAYOUT_BUDGETS.dividerColumns - LAYOUT_BUDGETS.gripColumns;
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

test("double-clicking the divider resets the split ratio to 0.5", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const ratios: number[] = [];
		h.shell.onWorkspaceRatioChange = (ratio) => {
			ratios.push(ratio);
			h.state = { ...h.state, splitRatio: ratio };
			h.shell.setWorkspaceState(h.state);
		};
		// Move off-center first so the reset is observable.
		const injected = h.term.onInput!;
		const dividerX = computeLayout(h.state).chat!.columns;
		injected(sgr(0, dividerX, 5));
		injected(sgr(32, 120, 5));
		injected(sgr(0, 120, 5, true));
		assert.ok((ratios.at(-1) ?? 0) > 0.6, "dragged right first");
		h.tui.renderNow(true); // the drag moved the divider: dispatch against the new frame
		const movedDivider = computeLayout(h.state).chat!.columns;
		// Double-click the divider: press/release + synthesized click(1), twice.
		injected(sgr(0, movedDivider, 5));
		injected(sgr(0, movedDivider, 5, true));
		assert.equal(ratios.length, 1, "a single click alone never resizes");
		injected(sgr(0, movedDivider, 5));
		injected(sgr(0, movedDivider, 5, true));
		assert.equal(ratios.length, 2, "the double-click resets");
		assert.equal(ratios.at(-1), 0.5);
		h.tui.renderNow(true);
		assert.equal(computeLayout(h.state).chat!.columns, computeLayout({ ...h.state, splitRatio: 0.5 }).chat!.columns);
	} finally {
		teardown(h);
	}
});

test("the far-right grip toggles the explorer and never widens past its column", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		const geometry = computeLayout(h.state);
		const gripX = geometry.grip!.x;
		assert.equal(geometry.grip!.columns, 1);
		const before = h.explorerToggles();
		const inject = h.term.onInput!;
		inject(sgr(0, gripX, 0));
		assert.equal(h.explorerToggles(), before, "a bare press never toggles");
		inject(sgr(0, gripX, 0, true));
		assert.equal(h.explorerToggles(), before + 1, "the completed click toggles exactly once");
		// The grip is pure chrome: the panes keep their geometry columns.
		const after = plain(h.tui.getScreenLines());
		assert.equal(after[0]!.length, 160);
		// It also exists in editor-maximized mode.
		h.shell.setWorkspaceState({ ...h.state, requestedMode: "editor" });
		h.tui.renderNow(true);
		const editorGeometry = computeLayout({ ...h.state, requestedMode: "editor" });
		assert.equal(editorGeometry.grip!.x, 159);
		const screen = plain(h.tui.getScreenLines());
		assert.equal(screen[0]!.charAt(159), "▐");
		assert.equal(screen[0]!.length, 160, "the grip never widens the frame");
		const togglesBefore = h.explorerToggles();
		inject(sgr(0, 159, 0));
		inject(sgr(0, 159, 0, true));
		assert.equal(h.explorerToggles(), togglesBefore + 1, "the grip works in editor-maximized mode");
	} finally {
		teardown(h);
	}
});

test("a docking card becomes a full-width row under the panes in split mode", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		h.shell.setDock(new PaneStub("DOCK-ROW"));
		h.tui.renderNow(true);
		const geometry = computeLayout(h.state);
		const screen = plain(h.tui.getScreenLines());
		const dockRow = screen.findIndex((line) => line.includes("DOCK-ROW"));
		assert.ok(dockRow >= 0, "dock card renders");
		assert.ok(dockRow > geometry.editor!.rows - h.shell.chromeRows() - 1, "below the panes area");
		assert.ok(screen[dockRow]!.startsWith("DOCK-ROW"), "the dock spans from the left edge");
		assert.ok(screen[dockRow]!.length <= 160);
		assert.doesNotMatch(screen[dockRow]!, /│/, "the divider never crosses a dock row");
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

test("editor mode gives the editor the full top row and keeps the composer full width", () => {
	const h = buildHarness(160, 48);
	try {
		configure(h);
		h.shell.setWorkspaceState({ ...h.state, requestedMode: "editor" });
		h.tui.renderNow(true);
		const geometry = computeLayout({ ...h.state, requestedMode: "editor" });
		assert.equal(geometry.effectiveMode, "editor");
		assert.equal(geometry.editor!.columns, 160 - LAYOUT_BUDGETS.gripColumns);
		const screen = plain(h.tui.getScreenLines());
		assert.ok(screen[0]!.startsWith("EDITOR"), "editor pane starts at column 0");
		assert.equal(screen[0]!.charAt(159), "▐", "the grip closes the editor row");
		assert.doesNotMatch(screen.join("\n"), /chat transcript row/, "chat transcript hidden but retained");
		const composerRow = screen.findIndex((line) => line.includes("Ask anything"));
		assert.ok(composerRow > 0, "the full-width composer stays available in editor mode");
		assert.ok(screen[composerRow]!.indexOf("Ask anything") < 10);
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

test("chatSide right mirrors the pane order without recreating components", () => {
	const rows = Array.from({ length: 80 }, (_, index) => `chat row ${index}`);
	const h = buildHarness(160, 48, rows);
	try {
		configure(h);
		const constructions = PaneStub.constructions;
		const scrolled = h.shell.transcriptScroll;
		scrolled.scrollTo(12);
		h.tui.renderNow(true);
		assert.equal(scrolled.scrollTop, 12);
		h.shell.setWorkspaceState({ ...h.state, chatSide: "right" });
		h.tui.renderNow(true);
		const geometry = computeLayout({ ...h.state, chatSide: "right" });
		assert.ok(geometry.chat && geometry.editor);
		assert.equal(geometry.editor.x, 0, "the editor leads");
		const screen = plain(h.tui.getScreenLines());
		assert.ok(screen[0]!.startsWith("EDITOR"), "editor pane starts at column 0 on the right-side chat");
		assert.equal(screen[0]!.charAt(geometry.editor.columns), "│", "divider after the editor");
		assert.ok(
			screen[0]!.slice(geometry.chat.x).includes("chat row 12"),
			"chat transcript sits right of the divider at its retained scroll anchor",
		);
		assert.equal(PaneStub.constructions, constructions, "the side swap never recreates panes");
		assert.equal(h.shell.transcriptScroll, scrolled, "the transcript ScrollView is retained");
		assert.equal(scrolled.scrollTop, 12, "the side swap never resets scroll");
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
		assert.equal(screen[0]!.charAt(159), "▐", "the grip stays at the far right");
		assert.equal(geometry.explorer!.x + geometry.explorer!.columns, 159, "the drawer reaches the grip");
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
		const chat = new PaneStub("CHAT");
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
		// entries() composes the whole row in visual order: chat, divider,
		// editor, (explorer), grip.
		const narrowEntries = shell.entries(layout, { component: chat, basis: layout.chat!.columns, shrink: 0, grow: 0 });
		assert.equal(narrowEntries.length, 4, "no explorer column when the editor would starve");
		assert.equal(narrowEntries[0]!.component, chat);
		assert.equal(narrowEntries[1]!.basis, 1);
		assert.equal(narrowEntries[1]!.component, shell.divider);
		assert.equal(narrowEntries[2]!.basis, layout.editor!.columns);
		assert.equal(narrowEntries[2]!.component, editor, "the injected component is used directly");
		assert.equal(narrowEntries[3]!.component, shell.grip, "the grip closes the row");
		assert.equal(narrowEntries[3]!.basis, LAYOUT_BUDGETS.gripColumns);
		shell.setState({ ...input, columns: 160 });
		const wide = shell.layout!;
		const wideEntries = shell.entries(wide, { component: chat, basis: wide.chat!.columns, shrink: 0, grow: 0 });
		assert.equal(wideEntries.length, 5, "docked explorer adds one column");
		assert.equal(wideEntries[0]!.component, chat);
		assert.equal(wideEntries[1]!.basis, 1);
		assert.equal(wideEntries[2]!.basis, wide.editor!.columns);
		assert.equal(wideEntries[3]!.basis, wide.explorer!.columns);
		assert.equal(wideEntries[3]!.component, explorer, "injected explorer is used when present");
		assert.equal(wideEntries[4]!.component, shell.grip);
		// A right-side chat swaps the two pane entries.
		shell.setState({ ...input, columns: 160, chatSide: "right" });
		const right = shell.layout!;
		const rightEntries = shell.entries(right, { component: chat, basis: right.chat!.columns, shrink: 0, grow: 0 });
		assert.equal(rightEntries[0]!.component, editor);
		assert.equal(rightEntries[1]!.component, shell.divider);
		assert.equal(rightEntries[2]!.component, chat);
		assert.equal(rightEntries[4]!.component, shell.grip);
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
		shell.setState({ ...input, chatSide: "right" });
		assert.equal(demands, 3, "a side swap rebuilds with the mirrored rectangles");
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

test("a 40x9 editor-mode frame keeps the full-width composer and the grip", () => {
	const h = buildHarness(40, 9);
	try {
		h.state = { ...h.state, requestedMode: "editor", preferredPane: "editor" };
		configure(h);
		h.tui.renderNow(true);
		const screen = plain(h.tui.getScreenLines());
		assert.equal(screen.length, 9, "the frame still fills the terminal");
		for (const line of screen) assert.ok(line.length <= 40, `row exceeds 40: ${line}`);
		const composerRow = screen.findIndex((line) => line.includes("Ask anything"));
		assert.ok(composerRow > 0, "the composer survives at the reduced density");
		const geometry = computeLayout({ ...h.state, columns: 40, rows: 9 });
		assert.equal(geometry.editor!.columns, 39, "the grip takes exactly its column");
		assert.equal(screen[0]!.charAt(39), "▐");
		assert.match(screen[8] ?? "", /test-model/);
	} finally {
		teardown(h);
	}
});
