// T4b contract tests: the editor pane chrome (plan §2 mockup, §9 tabs, §23).
// Frame-level tests over a real EditorStore + TabModel + EditorView; no
// terminal, gateway or shell is required (the shell's `layout(rows)` contract
// is exercised explicitly).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
	CURSOR_MARKER,
	stripTerminalSequences,
	TuiAltScreen,
	visibleWidth,
	type Terminal,
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import { EditorPane, disambiguatedTabLabels, uriDisplayPath } from "../src/components/EditorPane.ts";
import { EditorStore, type DocumentRecord } from "../src/editor/model.ts";
import { TabModel } from "../src/editor/tabs.ts";
import { EditorView } from "../src/editor/view.ts";

interface OpenOptions {
	uri?: string;
	languageId?: string;
	revision?: string | null;
	readOnly?: boolean;
}

function openDoc(store: EditorStore, text: string, options: OpenOptions = {}): DocumentRecord {
	const result = store.openDocument({
		workspaceId: "ws",
		uri: options.uri ?? "file:///w/src/main.ts",
		text,
		languageId: options.languageId ?? "typescript",
		revision: options.revision === undefined ? "rev-1" : options.revision,
		readOnly: options.readOnly ?? false,
	});
	assert.ok(result.ok, "document should open");
	return result.document;
}

interface Harness {
	store: EditorStore;
	doc: DocumentRecord;
	tabModel: TabModel;
	view: EditorView;
	pane: EditorPane;
	renders: () => number;
	selected: string[];
	closed: string[];
	saves: () => number;
	openTab(record: DocumentRecord): void;
}

function setup(text: string, options: OpenOptions = {}): Harness {
	const store = new EditorStore();
	const doc = openDoc(store, text, options);
	const tabModel = new TabModel({ isDirty: (documentId) => store.getDocument(documentId)?.dirty ?? false });
	tabModel.open(doc.id);
	let renders = 0;
	const selected: string[] = [];
	const closed: string[] = [];
	let saves = 0;
	const view = new EditorView({
		store,
		view: store.getViewState(doc.id) as NonNullable<ReturnType<EditorStore["getViewState"]>>,
		requestRender: () => {
			renders += 1;
		},
		clipboard: {
			async copy() {
				return true;
			},
			async paste() {
				return null;
			},
		},
	});
	const pane = new EditorPane({
		store,
		tabModel,
		view,
		requestRender: () => {
			renders += 1;
		},
		onSelectTab: (documentId) => selected.push(documentId),
		onCloseTab: (documentId) => closed.push(documentId),
		onSave: () => {
			saves += 1;
		},
	});
	return {
		store,
		doc,
		tabModel,
		view,
		pane,
		renders: () => renders,
		selected,
		closed,
		saves: () => saves,
		openTab: (record) => {
			tabModel.open(record.id);
		},
	};
}

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

function mouseEvent(
	partial: Partial<TuiMouseEvent> & { type: TuiMouseEvent["type"]; x: number; y: number },
): TuiMouseEvent {
	return {
		button: "left",
		screenX: partial.x,
		screenY: partial.y,
		width: 80,
		height: 20,
		shift: false,
		alt: false,
		ctrl: false,
		...partial,
	};
}

interface MockTerminal extends Terminal {
	columns: number;
	rows: number;
	/** Captured by start(): the exact path real terminal input takes. */
	onInput?: (data: string) => void;
}

function mockTerminal(columns: number, rows: number): MockTerminal {
	return {
		columns,
		rows,
		kittyProtocolActive: false,
		start(onInput: (data: string) => void) {
			this.onInput = onInput;
		},
		stop() {},
		async drainInput() {},
		write() {},
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

/** SGR mouse sequence; columns/rows are 0-based like the events the TUI emits. */
function sgr(button: number, x: number, y: number, release = false): string {
	return `\x1b[<${button};${x + 1};${y + 1}${release ? "m" : "M"}`;
}

test("pane renders tab strip, breadcrumb, viewport and status within its allocation", () => {
	const harness = setup("hello");
	harness.pane.layout(10);
	const rows = plain(harness.pane.render(80));
	assert.equal(rows.length, 10);
	assert.match(rows[0] ?? "", /\[main\.ts\]/);
	assert.match(rows[1] ?? "", /\/w\/src\/main\.ts/);
	assert.match(rows[1] ?? "", /typescript/);
	assert.match(rows[2] ?? "", /1 hello/);
	assert.match(rows[9] ?? "", /Ln 1, Col 1/);
	assert.match(rows[9] ?? "", /UTF-8/);
	assert.match(rows[9] ?? "", /LF/);
	for (const width of [20, 40, 80, 120]) {
		for (const row of harness.pane.render(width)) {
			assert.ok(visibleWidth(row) <= width, `row exceeds ${width}: ${stripTerminalSequences(row)}`);
		}
	}
});

test("layout() allocates viewport rows and render stays exactly that tall", () => {
	const harness = setup("a\nb\nc\nd\ne\nf\ng\nh\ni\nj");
	harness.pane.layout(7);
	assert.equal(harness.pane.getViewportRows(), 4);
	assert.equal(harness.pane.render(40).length, 7);
	assert.equal(harness.view.render(40).length, 4, "the view renders exactly the allocated viewport rows");
	harness.pane.layout(1);
	assert.equal(harness.pane.getViewportRows(), 1);
	assert.equal(harness.pane.render(40).length, 1);
});

test("keyboard input delegates to the view and ctrl+s reaches onSave", () => {
	const harness = setup("hello");
	assert.equal(harness.pane.handleInput("x"), true);
	assert.equal(harness.store.getDocument(harness.doc.id)?.text, "xhello");
	assert.equal(harness.pane.handleInput("\x13"), true, "ctrl+s");
	assert.equal(harness.saves(), 1);
});

test("dirty, read-only and conflict markers are text, not color alone", () => {
	const harness = setup("hello");
	harness.pane.layout(8);
	let rows = plain(harness.pane.render(80));
	assert.doesNotMatch(rows[0] ?? "", /●/);
	assert.doesNotMatch(rows[7] ?? "", /●/);

	assert.ok(harness.store.insertText(harness.doc.id, "!").ok);
	rows = plain(harness.pane.render(80));
	assert.match(rows[0] ?? "", /●/, "dirty marker on the tab");
	assert.match(rows[7] ?? "", /●/, "dirty marker in the status row");

	// A structured conflict is disclosed with a literal `!` marker.
	assert.ok(harness.store.noteExternalChange(harness.doc.id, "rev-9").ok);
	rows = plain(harness.pane.render(80));
	assert.match(rows[0] ?? "", /!/);
	assert.match(rows[7] ?? "", /!/);

	// Read-only is disclosed in text.
	const readOnly = setup("sealed", { readOnly: true, uri: "file:///w/src/locked.ts" });
	readOnly.pane.layout(6);
	const locked = plain(readOnly.pane.render(80));
	assert.match(locked[0] ?? "", /\[ro\]/);
	assert.match(locked[5] ?? "", /\[ro\]/);
});

test("tab strip stays one stable row with a +N overflow indicator", () => {
	const store = new EditorStore();
	const tabModel = new TabModel({ isDirty: () => false });
	for (let index = 0; index < 7; index++) {
		const record = openDoc(store, `content ${index}`, {
			uri: `file:///w/very-long-directory-name/file-${index}.ts`,
		});
		tabModel.open(record.id);
	}
	let renders = 0;
	const view = new EditorView({
		store,
		view: store.getViewState(tabModel.activeDocumentId() as string) as NonNullable<
			ReturnType<EditorStore["getViewState"]>
		>,
		requestRender: () => {
			renders += 1;
		},
		clipboard: { async copy() { return true; }, async paste() { return null; } },
	});
	const pane = new EditorPane({ store, tabModel, view, requestRender: () => { renders += 1; } });
	pane.layout(6);
	const first = pane.render(30);
	const second = pane.render(30);
	assert.equal(first.length, 6);
	assert.equal(plain(first)[0], plain(second)[0], "the strip is stable across renders");
	assert.match(plain(first)[0] ?? "", /\+\d+/, "overflow reports the hidden tab count");
	assert.equal(plain(first).length, 6, "no unbounded multi-row wrapping");
	assert.ok(renders > 0);
});

test("duplicate basenames disambiguate with directory segments", () => {
	assert.deepEqual(
		disambiguatedTabLabels(["file:///w/one/a.ts", "file:///w/two/a.ts", "file:///w/b.ts"]),
		["one/a.ts", "two/a.ts", "b.ts"],
	);
	assert.deepEqual(
		disambiguatedTabLabels(["file:///x/y/z/a.ts", "file:///p/q/z/a.ts"]),
		["y/z/a.ts", "q/z/a.ts"],
		"deeper duplicates keep extending until unambiguous",
	);
	assert.deepEqual(disambiguatedTabLabels(["file:///w/plain.ts"]), ["plain.ts"]);
	assert.equal(uriDisplayPath("file:///w/My%20File.ts"), "/w/My File.ts");
});

test("clicking a tab selects it and its document; middle-click asks to close", () => {
	const harness = setup("first");
	const second = openDoc(harness.store, "second", { uri: "file:///w/src/other.ts" });
	harness.openTab(second);
	harness.pane.layout(6);
	const rows = plain(harness.pane.render(60));
	const firstRow = rows[0] ?? "";
	const x = firstRow.indexOf("[main.ts]");
	assert.ok(x >= 0, `tab labels present: ${firstRow}`);
	const click = harness.pane.handleMouse(mouseEvent({ type: "press", x: x + 1, y: 0, width: 60 }));
	assert.deepEqual(click, { handled: true, focus: true });
	assert.equal(harness.tabModel.activeDocumentId(), harness.doc.id);
	assert.equal(harness.view.getViewState()?.documentId, harness.doc.id);
	assert.deepEqual(harness.selected, [harness.doc.id]);
	assert.ok(harness.renders() > 0);

	const middle = harness.pane.handleMouse(
		mouseEvent({ type: "press", button: "middle", x: x + 1, y: 0, width: 60 }),
	);
	assert.equal(middle?.handled, true);
	assert.deepEqual(harness.closed, [harness.doc.id]);
	assert.equal(harness.tabModel.count(), 2, "the pane never closes a tab by itself");
});

test("viewport mouse events are translated into view-local coordinates", () => {
	const harness = setup("hello world");
	harness.pane.layout(8);
	harness.pane.render(60);
	// Row 2 is the first viewport row (strip 0, breadcrumb 1); gutter is 2 cells.
	const press = harness.pane.handleMouse(mouseEvent({ type: "press", x: 2 + 3, y: 2, width: 60 }));
	assert.equal(press?.capture, true);
	assert.equal(press?.focus, true);
	assert.equal(harness.store.getViewState(harness.doc.id)?.cursorOffset, 3);
	const drag = harness.pane.handleMouse(mouseEvent({ type: "drag", x: 2 + 8, y: 3, width: 60 }));
	assert.equal(drag?.handled, true);
	assert.equal(harness.store.getViewState(harness.doc.id)?.cursorOffset, 8);
	assert.equal(harness.store.getViewState(harness.doc.id)?.selectionAnchorOffset, 3);
	// Presses on the breadcrumb row or the status row are not the editor's.
	assert.equal(harness.pane.handleMouse(mouseEvent({ type: "press", x: 5, y: 1, width: 60 })), undefined);
	assert.equal(harness.pane.handleMouse(mouseEvent({ type: "press", x: 5, y: 7, width: 60 })), undefined);
});

test("wheel over the viewport scrolls the editor, not an outer pane", () => {
	const text = Array.from({ length: 40 }, (_, index) => `row ${index}`).join("\n");
	const harness = setup(text);
	harness.pane.layout(8); // 5 viewport rows
	harness.pane.render(80);
	const wheel = harness.pane.handleMouse(mouseEvent({ type: "wheel", x: 10, y: 3, wheelDelta: 3 }));
	assert.deepEqual(wheel, { handled: true });
	assert.equal(harness.view.getViewState()?.scrollLine, 3);
	// Wheel over the ticker/tab strip is not claimed by the pane.
	assert.equal(harness.pane.handleMouse(mouseEvent({ type: "wheel", x: 10, y: 0, wheelDelta: 3 })), undefined);
});

test("LSP status is text supplied by the controller, never invented", () => {
	const harness = setup("hello");
	harness.pane.layout(6);
	assert.doesNotMatch(plain(harness.pane.render(80))[5] ?? "", /ready|initializing|LSP/);
	harness.pane.setLspStatus("initializing");
	assert.match(plain(harness.pane.render(80))[5] ?? "", /initializing/);
	harness.pane.setLspStatus(undefined);
	assert.doesNotMatch(plain(harness.pane.render(80))[5] ?? "", /initializing/);
});

test("status row tracks the caret display position through the view", () => {
	const harness = setup("one\ntwo\tthree");
	harness.store.setSelection(harness.doc.id, { cursorOffset: 8 });
	harness.view.setDocument(harness.doc.id);
	harness.pane.layout(6);
	const status = plain(harness.pane.render(80))[5] ?? "";
	// "one\n" is 4 offsets; offset 8 starts "three", which sits at cell 4 after
	// the tab stop, so the 1-based display column is 5.
	assert.match(status, /Ln 2, Col 5/);
});

test("pane renders safely at degenerate widths and heights", () => {
	const harness = setup("hello 日本語 👩‍💻\tend");
	for (const rows of [1, 2, 3, 4, 23]) {
		harness.pane.layout(rows);
		for (const width of [0, 1, 2, 20, 40, 80, 120]) {
			let rendered: string[] = [];
			assert.doesNotThrow(() => {
				rendered = harness.pane.render(width);
			}, `render throws at ${width}x${rows}`);
			assert.equal(rendered.length, rows, `allocation ${rows} honored at width ${width}`);
			for (const row of rendered) {
				assert.ok(visibleWidth(row) <= width, `row exceeds ${width}: ${stripTerminalSequences(row)}`);
			}
		}
	}
});

test("pane boots as a real alt-screen root: focus, mouse dispatch and capture end to end", () => {
	const harness = setup("hello world");
	const second = openDoc(harness.store, "second file", { uri: "file:///w/src/other.ts" });
	harness.openTab(second);
	harness.tabModel.setActive(harness.doc.id);
	harness.view.setDocument(harness.doc.id);
	const term = mockTerminal(60, 12);
	const tui = new TuiAltScreen(term);
	tui.setLayoutRoot(harness.pane);
	harness.pane.layout(12);
	tui.start();
	tui.renderNow(true);
	try {
		const frame = plain(tui.getScreenLines());
		assert.equal(frame.length, 12, "the pane fills the terminal frame");
		assert.match(frame[0] ?? "", /\[main\.ts\]/);
		assert.match(frame[1] ?? "", /main\.ts/);
		assert.match(frame[2] ?? "", /hello world/);
		assert.match(frame[11] ?? "", /Ln 1, Col 1/);
		assert.equal(tui.getScreenLines().join("").includes(CURSOR_MARKER), false, "the marker is extracted by the renderer");
		assert.equal(tui.getScreenLines().join("").includes("\x1b[7m"), false, "no caret while the pane is unfocused");

		// Shell focus seam: focusing the pane focuses the view.
		tui.setFocus(harness.pane);
		assert.equal(harness.view.focused, true);
		tui.renderNow(true);
		assert.equal(tui.getScreenLines().join("").includes("\x1b[7m"), true, "focused caret is visible in the frame");

		// Real mouse dispatch: press at content column 3 of the first viewport row.
		const inject = term.onInput as (data: string) => void;
		inject(sgr(0, 2 + 3, 2));
		assert.equal(harness.store.getViewState(harness.doc.id)?.cursorOffset, 3);
		// Capture keeps the drag inside the pane even across a row change.
		inject(sgr(32, 2 + 8, 3));
		inject(sgr(0, 2 + 8, 3, true));
		assert.equal(harness.store.getViewState(harness.doc.id)?.cursorOffset, 8);
		assert.equal(harness.store.getViewState(harness.doc.id)?.selectionAnchorOffset, 3);

		// Tab-strip click through the same real dispatch path selects the tab.
		tui.renderNow(true);
		const strip = plain(tui.getScreenLines())[0] ?? "";
		const x = strip.indexOf("[other.ts]");
		assert.ok(x >= 0, `second tab visible: ${strip}`);
		inject(sgr(0, x + 1, 0));
		inject(sgr(0, x + 1, 0, true));
		assert.equal(harness.tabModel.activeDocumentId(), second.id);
		assert.equal(harness.view.getViewState()?.documentId, second.id);
		assert.deepEqual(harness.selected, [second.id]);
	} finally {
		tui.stop();
	}
});
