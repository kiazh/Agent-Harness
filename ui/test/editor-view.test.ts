// T4b contract tests: the terminal editor viewport (plan §9/§10/§23).
// Unit-level, frame-level and input-sequence evidence for EditorView over a
// real EditorStore (the store is pure, so no gateway or terminal is needed).
import assert from "node:assert/strict";
import { test } from "node:test";
import { CURSOR_MARKER, stripTerminalSequences, visibleWidth, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { decodePrintableKey } from "@earendil-works/pi-tui/dist/keys.js";
import { theme } from "../src/design/theme.ts";
import { EditorStore, type DocumentRecord, type EditorViewState } from "../src/editor/model.ts";
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

function freshStore(text: string, options: OpenOptions = {}): { store: EditorStore; doc: DocumentRecord } {
	const store = new EditorStore();
	return { store, doc: openDoc(store, text, options) };
}

interface ViewHarness {
	view: EditorView;
	changes: string[];
	moves: () => number;
	renders: () => number;
	copied: string[];
	setPaste(text: string | null): void;
}

function makeView(
	store: EditorStore,
	documentId: string,
	options: { softWrap?: boolean; tabWidth?: number; viewState?: EditorViewState } = {},
): ViewHarness {
	const changes: string[] = [];
	let moves = 0;
	let renders = 0;
	const copied: string[] = [];
	let pasteText: string | null = null;
	const injected = options.viewState ?? store.getViewState(documentId);
	assert.ok(injected, "view state should exist");
	const view = new EditorView({
		store,
		view: injected,
		onChange: (id) => changes.push(id),
		onCursorMove: () => {
			moves += 1;
		},
		requestRender: () => {
			renders += 1;
		},
		clipboard: {
			async copy(text: string) {
				copied.push(text);
				return true;
			},
			async paste() {
				return pasteText;
			},
		},
		softWrap: options.softWrap,
		tabWidth: options.tabWidth,
	});
	return {
		view,
		changes,
		moves: () => moves,
		renders: () => renders,
		copied,
		setPaste: (text) => {
			pasteText = text;
		},
	};
}

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

function mouseEvent(partial: Partial<TuiMouseEvent> & { type: TuiMouseEvent["type"]; x: number; y: number }): TuiMouseEvent {
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

test("gutter numbers align and no row exceeds the width at 20/40/80/120", () => {
	const lines = Array.from({ length: 12 }, (_, index) => (index === 4 ? "日本語テスト行" : `line ${index}`));
	const { store, doc } = freshStore(lines.join("\n"));
	store.setSelection(doc.id, { cursorOffset: 0 });
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(12);
	for (const width of [20, 40, 80, 120]) {
		const rows = harness.view.render(width);
		assert.equal(rows.length, 12);
		for (const row of rows) {
			assert.ok(visibleWidth(row) <= width, `row exceeds ${width}: ${stripTerminalSequences(row)}`);
		}
		const stripped = plain(rows);
		for (const row of stripped) assert.match(row.slice(0, 3), /^\s*\d+ $/, "gutter is digits then a separator space");
		assert.equal(stripped[8]?.slice(0, 2), " 9", "single-digit numbers are right-aligned");
		assert.equal(stripped[9]?.slice(0, 2), "10", "double-digit numbers fill the gutter");
		assert.ok(stripped[4]?.includes("日本語テスト行"), "wide CJK content renders at the shared content column");
		assert.equal(stripped[4]?.indexOf("日"), 4, "content starts after the fixed gutter (digits + 2-cell padding)");
	}
});

test("line numbers are faint and the caret line's number is accented", () => {
	const { store, doc } = freshStore("first\nsecond\nthird");
	store.setSelection(doc.id, { cursorOffset: 8 }); // inside "second"
	const harness = makeView(store, doc.id);
	const rows = harness.view.render(40);
	assert.ok(rows[0]?.includes(theme.dim("1")), "inactive line numbers use faint ink");
	assert.ok(rows[1]?.includes(theme.accent("2")), "the active line number uses accent ink");
	assert.ok(rows[2]?.includes(theme.dim("3")));
});

test("a focused view renders exactly one cursor marker and the fake caret cell", () => {
	const { store, doc } = freshStore("hello");
	const harness = makeView(store, doc.id);
	const unfocused = harness.view.render(40).join("");
	assert.equal(unfocused.includes(CURSOR_MARKER), false, "no hardware-cursor marker while unfocused");
	assert.equal(unfocused.includes("\x1b[7m"), false, "no fake caret while unfocused");
	harness.view.focused = true;
	const focused = harness.view.render(40).join("");
	assert.equal(focused.split(CURSOR_MARKER).length - 1, 1, "exactly one cursor marker when focused");
	assert.ok(focused.includes("\x1b[7m"), "fake inverse-video caret cell is present");
	assert.equal(
		stripTerminalSequences(focused),
		stripTerminalSequences(unfocused),
		"caret styling and the marker never change visible text",
	);
	harness.view.focused = false;
	assert.equal(harness.view.render(40).join("").includes(CURSOR_MARKER), false);
});

test("typing goes through store transactions with one onChange per transaction", () => {
	const { store, doc } = freshStore("");
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("a"), true);
	assert.equal(harness.view.handleInput("b"), true);
	assert.equal(store.getDocument(doc.id)?.text, "ab");
	assert.equal(store.getDocument(doc.id)?.version, 2, "each keystroke is one transaction");
	assert.deepEqual(harness.changes, [doc.id, doc.id], "onChange fires once per transaction");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 2);
	assert.ok(harness.moves() >= 2);
	assert.ok(harness.renders() >= 2);
});

test("raw movement and edit escape sequences mutate exactly through the store", () => {
	const { store, doc } = freshStore("alpha beta");
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x1b[1;5C"), true, "ctrl+right");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 5, "word motion lands on the word boundary");
	assert.equal(harness.view.handleInput("\x1b[1;2D"), true, "shift+left starts a selection");
	assert.equal(store.getViewState(doc.id)?.selectionAnchorOffset, 5);
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 4);
	const versionBefore = store.getDocument(doc.id)?.version;
	assert.equal(harness.view.handleInput("\x1b[3~"), true, "delete");
	assert.equal(store.getDocument(doc.id)?.text, "alph beta", "delete removes the selected grapheme");
	assert.equal(store.getDocument(doc.id)?.version, (versionBefore ?? 0) + 1);
	assert.equal(harness.changes.length, 1);
	// Enter inserts the document's dominant ending.
	const crlf = freshStore("x\r\ny");
	crlf.store.setSelection(crlf.doc.id, { cursorOffset: 0 });
	const crlfHarness = makeView(crlf.store, crlf.doc.id);
	assert.equal(crlfHarness.view.handleInput("\r"), true, "enter");
	assert.equal(crlf.store.getDocument(crlf.doc.id)?.text, "\r\nx\r\ny");
});

test("ctrl+c copies only with a live selection; without one it stays unconsumed", () => {
	const { store, doc } = freshStore("xy");
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x03"), false, "ctrl+c without a selection is left for the app");
	assert.deepEqual(harness.copied, []);
	assert.equal(harness.view.handleInput("\x1b[1;2C"), true, "shift+right selects x");
	assert.equal(store.getViewState(doc.id)?.selectionAnchorOffset, 0);
	assert.equal(harness.view.handleInput("\x03"), true);
	assert.deepEqual(harness.copied, ["x"]);
	assert.equal(harness.view.handleInput("\x18"), true, "ctrl+x cuts the selection");
	assert.equal(store.getDocument(doc.id)?.text, "y");
	assert.deepEqual(harness.copied, ["x", "x"]);
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 0);
	// Without a selection again, cut is unconsumed too.
	assert.equal(harness.view.handleInput("\x18"), false);
	// Select-all covers the whole buffer and copies it.
	assert.equal(harness.view.handleInput("\x01"), true, "ctrl+a");
	assert.equal(store.getViewState(doc.id)?.selectionAnchorOffset, 0);
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 1);
	assert.equal(harness.view.handleInput("\x03"), true);
	assert.deepEqual(harness.copied.at(-1), "y");
});

test("tab inserts a literal tab and escape is a no-op", () => {
	const { store, doc } = freshStore("");
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x1b"), false, "escape stays unconsumed for later popups");
	assert.equal(store.getDocument(doc.id)?.text, "");
	assert.equal(harness.view.handleInput("\t"), true);
	assert.equal(store.getDocument(doc.id)?.text, "\t");
	assert.equal(harness.view.handleInput("\x1b[999~"), false, "unknown escape sequences are not inserted");
	assert.equal(store.getDocument(doc.id)?.text, "\t");
});

test("bracketed paste inserts verbatim as one transaction (tabs and CRLF kept)", () => {
	const { store, doc } = freshStore("");
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x1b[200~a\tb\r\nc\x1b[201~"), true);
	const record = store.getDocument(doc.id);
	// Store contract: insertions are verbatim; the store detects line-ending
	// metadata from the literal text and never normalizes bytes.
	assert.equal(record?.text, "a\tb\r\nc");
	assert.equal(record?.version, 1, "the whole paste is one transaction");
	assert.equal(record?.lineEnding, "CRLF");
	assert.deepEqual(harness.changes, [doc.id]);
	// Pasting CRLF into an LF document keeps both kinds in the buffer.
	const lf = freshStore("x\n");
	const lfHarness = makeView(lf.store, lf.doc.id);
	lfHarness.view.handleInput("\x1b[200~p\r\nq\x1b[201~");
	assert.equal(lf.store.getDocument(lf.doc.id)?.text, "p\r\nqx\n");
	assert.equal(lf.store.getDocument(lf.doc.id)?.lineEnding, "mixed");
});

test("ctrl+v reads the injected clipboard and inserts as one transaction", async () => {
	const { store, doc } = freshStore("x");
	store.setSelection(doc.id, { cursorOffset: 1 });
	const harness = makeView(store, doc.id);
	harness.setPaste("pasted\nline\t!");
	assert.equal(harness.view.handleInput("\x16"), true);
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(store.getDocument(doc.id)?.text, "xpasted\nline\t!");
	assert.equal(store.getDocument(doc.id)?.version, 1);
	assert.deepEqual(harness.changes, [doc.id]);
});

test("backspace over an emoji removes the whole grapheme", () => {
	const { store, doc } = freshStore("a👍");
	store.setSelection(doc.id, { cursorOffset: 3 });
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x7f"), true);
	assert.equal(store.getDocument(doc.id)?.text, "a");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 1);
});

test("mouse clicks map through the gutter and horizontal offset; drags select", () => {
	const { store, doc } = freshStore("hello world");
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(5);
	// One line -> gutter is one digit plus the 2-cell padding (3 cells).
	const press = harness.view.handleMouse(mouseEvent({ type: "press", x: 3 + 3, y: 0 }));
	assert.deepEqual(press, { capture: true, focus: true, handled: true });
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 3);
	const drag = harness.view.handleMouse(mouseEvent({ type: "drag", x: 3 + 8, y: 0 }));
	assert.equal(drag?.handled, true);
	assert.equal(drag?.capture, true);
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 8);
	assert.equal(store.getViewState(doc.id)?.selectionAnchorOffset, 3);
	const release = harness.view.handleMouse(mouseEvent({ type: "release", x: 3 + 8, y: 0 }));
	assert.equal(release?.handled, true);
	assert.equal(harness.view.handleMouse(mouseEvent({ type: "release", x: 3 + 8, y: 0 })), undefined);
	// Double-click selects the word under the pointer.
	const double = harness.view.handleMouse(mouseEvent({ type: "click", x: 3 + 8, y: 0, clickCount: 2 }));
	assert.equal(double?.handled, true);
	assert.equal(store.getViewState(doc.id)?.selectionAnchorOffset, 6);
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 11);
});

test("clicking the gutter focuses without moving the caret", () => {
	const { store, doc } = freshStore("hello");
	store.setSelection(doc.id, { cursorOffset: 3 });
	const harness = makeView(store, doc.id);
	assert.deepEqual(harness.view.handleMouse(mouseEvent({ type: "press", x: 0, y: 0 })), {
		focus: true,
		handled: true,
	});
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 3);
	// A lone click (no press delivered to the view) still claims focus.
	assert.deepEqual(harness.view.handleMouse(mouseEvent({ type: "click", x: 0, y: 0 })), {
		focus: true,
		handled: true,
	});
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 3, "gutter clicks never move the caret");
});

test("a lone viewport click positions the caret and claims focus (no press needed)", () => {
	const { store, doc } = freshStore("hello world");
	store.setSelection(doc.id, { cursorOffset: 0 });
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(5);
	// Gesture without a preceding press: the click alone must place the caret.
	const click = harness.view.handleMouse(mouseEvent({ type: "click", x: 3 + 7, y: 0 }));
	assert.deepEqual(click, { handled: true, focus: true });
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 7, "the click positions the caret");
	// A press-then-click sequence keeps the click idempotent and preserves focus.
	const press = harness.view.handleMouse(mouseEvent({ type: "press", x: 3 + 3, y: 0 }));
	assert.equal(press?.capture, true);
	const again = harness.view.handleMouse(mouseEvent({ type: "click", x: 3 + 3, y: 0 }));
	assert.deepEqual(again, { handled: true, focus: true });
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 3);
	// Clicking a read-only document still focuses (the store refuses movement,
	// but a click never breaks the pane's caret plumbing).
	const sealed = freshStore("sealed", { readOnly: true });
	const sealedHarness = makeView(sealed.store, sealed.doc.id);
	const roClick = sealedHarness.view.handleMouse(mouseEvent({ type: "click", x: 3 + 4, y: 0 }));
	assert.equal(roClick?.focus, true);
});

test("horizontal scrolling maps mouse columns through horizontalCellOffset", () => {
	const { store, doc } = freshStore("0123456789");
	store.setSelection(doc.id, { cursorOffset: 8 });
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(3);
	harness.view.render(12); // gutter 3 -> 9 visible content cells
	// Caret at cell 8 fits; move right to 9 and the view scrolls by one cell.
	harness.view.handleInput("\x1b[C");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 9);
	harness.view.render(12);
	assert.equal(harness.view.getViewState()?.horizontalCellOffset, 1);
	const rows = plain(harness.view.render(12));
	// The window starts at cell 1; the caret sits at cell 9 (end of the line).
	assert.equal(rows[0]?.slice(3, 12), "123456789");
	// Click at content column 0 now maps to document offset 1.
	harness.view.handleMouse(mouseEvent({ type: "press", x: 3, y: 0, width: 12 }));
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 1);
});

test("wheel scrolls the viewport and freezes caret following until the caret moves", () => {
	const text = Array.from({ length: 40 }, (_, index) => `row ${index}`).join("\n");
	const { store, doc } = freshStore(text);
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(10);
	harness.view.render(80);
	const scrolled = harness.view.handleMouse(mouseEvent({ type: "wheel", x: 5, y: 3, wheelDelta: 4 }));
	assert.deepEqual(scrolled, { handled: true });
	assert.equal(harness.view.getViewState()?.scrollLine, 4);
	harness.view.render(80);
	assert.equal(harness.view.getViewState()?.scrollLine, 4, "explicit scroll is preserved while the caret stays put");
	// Caret movement re-enables following and pulls the window back to the caret.
	assert.equal(harness.view.handleInput("\x1b[1;5F"), true, "ctrl+end moves the caret to the document end");
	harness.view.render(80);
	const afterMove = harness.view.getViewState();
	assert.equal(afterMove?.scrollLine, 30, "following the caret bounds the scroll to the last page");
	// Scrolling back to the top clamps at zero.
	harness.view.handleMouse(mouseEvent({ type: "wheel", x: 5, y: 3, wheelDelta: -500 }));
	assert.equal(harness.view.getViewState()?.scrollLine, 0);
});

test("resizing the allocated rows keeps the caret visible and the scroll bounded", () => {
	const text = Array.from({ length: 60 }, (_, index) => `row ${index}`).join("\n");
	const { store, doc } = freshStore(text);
	store.setSelection(doc.id, { cursorOffset: text.length });
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(40);
	const wide = harness.view.render(120);
	assert.equal(wide.length, 40);
	assert.equal(harness.view.getViewState()?.scrollLine, 20);
	harness.view.setViewportRows(10);
	const narrow = harness.view.render(80);
	assert.equal(narrow.length, 10, "the view renders exactly the allocated rows");
	assert.equal(harness.view.getViewState()?.scrollLine, 50);
	assert.ok(plain(narrow).some((row) => row.includes("row 59")), "cursor line stays visible after the resize");
});

test("setDocument retains per-document scroll state", () => {
	const store = new EditorStore();
	const longText = Array.from({ length: 30 }, (_, index) => `line ${index}`).join("\n");
	const a = openDoc(store, longText, { uri: "file:///w/a.ts" });
	const b = openDoc(store, "bee", { uri: "file:///w/b.ts" });
	const harness = makeView(store, a.id);
	harness.view.setViewportRows(5);
	harness.view.render(80);
	for (let press = 0; press < 3; press++) harness.view.handleInput("\x1b[6;5~"); // ctrl+pageDown
	harness.view.render(80);
	const scrolledA = harness.view.getViewState()?.scrollLine ?? 0;
	assert.ok(scrolledA > 0, "paging down scrolls the viewport");
	harness.view.setDocument(b.id);
	assert.equal(harness.view.getViewState()?.documentId, b.id);
	assert.equal(harness.view.getViewState()?.scrollLine, 0);
	harness.view.setDocument(a.id);
	assert.equal(harness.view.getViewState()?.scrollLine, scrolledA, "scroll state survives tab switches");
});

test("tabs render at tabWidth stops and mouse columns respect them", () => {
	const { store, doc } = freshStore("a\tb\nab");
	const harness = makeView(store, doc.id, { tabWidth: 4 });
	const rows = plain(harness.view.render(40));
	assert.equal(rows[0]?.slice(3).trimEnd(), "a   b");
	assert.equal(rows[1]?.slice(3).trimEnd(), "ab");
	// Cells 2 and 3 sit inside the tab's stop gap and stay before the tab offset.
	harness.view.handleMouse(mouseEvent({ type: "press", x: 3 + 2, y: 0 }));
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 1);
	harness.view.handleMouse(mouseEvent({ type: "press", x: 3 + 3, y: 0 }));
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 1);
	harness.view.handleMouse(mouseEvent({ type: "press", x: 3 + 4, y: 0 }));
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 2);
});

test("control characters render as placeholders and never leak as live sequences", () => {
	const raw = "ok\x1b[31mred\x00\x07\tend";
	const { store, doc } = freshStore(raw);
	const harness = makeView(store, doc.id);
	const stripped = plain(harness.view.render(80))[0] ?? "";
	assert.ok(stripped.includes("ok␛[31mred␀␇    end"), `placeholders expected, got: ${stripped}`);
	assert.equal(stripped.includes("\x1b"), false, "no raw ESC survives the projection");
	// The projection is display-only: stored and saved text are untouched.
	assert.equal(store.getDocument(doc.id)?.text, raw);
	const serialized = store.serializeForSave(doc.id);
	assert.ok(serialized.ok);
	assert.equal(serialized.text, raw);
	// C1 controls and Unicode line separators also render visibly.
	const weird = freshStore("a\x9bb\u2028c", { uri: "file:///w/weird.txt" });
	const weirdHarness = makeView(weird.store, weird.doc.id);
	const weirdRow = plain(weirdHarness.view.render(40))[0] ?? "";
	assert.equal(weirdRow.slice(3).trimEnd(), "a\uFFFDb\uFFFDc");
});

test("soft wrap renders every visual row and keeps the caret visible", () => {
	const { store, doc } = freshStore("abcdefghij");
	const harness = makeView(store, doc.id, { softWrap: true });
	harness.view.setViewportRows(6);
	const rows = plain(harness.view.render(8)); // gutter 3 -> 5 content cells per row
	assert.equal(rows.length, 6);
	assert.equal(rows[0]?.slice(3).trimEnd(), "abcde");
	assert.equal(rows[1]?.slice(3).trimEnd(), "fghij");
	assert.equal(rows[1]?.slice(0, 2), "1 ", "both visual rows carry the same line number");
	for (const row of rows) assert.ok(visibleWidth(row) <= 8);
});

test("undo and redo restore text and selection through the store", () => {
	const { store, doc } = freshStore("");
	const harness = makeView(store, doc.id);
	harness.view.handleInput("h");
	harness.view.handleInput("i");
	assert.equal(store.getDocument(doc.id)?.version, 2);
	assert.equal(harness.view.handleInput("\x1a"), true, "ctrl+z undoes");
	assert.equal(store.getDocument(doc.id)?.text, "", "the typing run undoes as one group");
	assert.equal(store.getDocument(doc.id)?.version, 3);
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 0);
	assert.equal(harness.view.handleInput("\x1b[122;6u"), true, "ctrl+shift+z (Kitty) redoes");
	assert.equal(store.getDocument(doc.id)?.text, "hi");
	assert.equal(harness.view.handleInput("\x1a"), true);
	assert.equal(store.getDocument(doc.id)?.text, "");
	assert.equal(harness.view.handleInput("\x19"), true, "ctrl+y redoes");
	assert.equal(store.getDocument(doc.id)?.text, "hi");
	assert.deepEqual(harness.changes, [doc.id, doc.id, doc.id, doc.id, doc.id, doc.id], "undo and redo are transactions too");
});

test("ctrl+shift+k deletes the current line; alt+up/down move lines", () => {
	const { store, doc } = freshStore("one\ntwo\nthree");
	store.setSelection(doc.id, { cursorOffset: 5 });
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x1b[107;6u"), true, "ctrl+shift+k (Kitty form)");
	assert.equal(store.getDocument(doc.id)?.text, "one\nthree");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 4);
	assert.equal(harness.view.handleInput("\x1b[1;3A"), true, "alt+up moves the line up");
	assert.equal(store.getDocument(doc.id)?.text, "three\none");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 0);
	assert.equal(harness.view.handleInput("\x1b[1;3B"), true, "alt+down moves it back");
	assert.equal(store.getDocument(doc.id)?.text, "one\nthree");
	assert.equal(store.getViewState(doc.id)?.cursorOffset, 4);
});

test("read-only documents consume edits without mutating text", () => {
	const { store, doc } = freshStore("sealed", { readOnly: true });
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("x"), true, "the editor still owns the key");
	assert.equal(store.getDocument(doc.id)?.text, "sealed");
	assert.equal(store.getDocument(doc.id)?.version, 0);
	assert.deepEqual(harness.changes, []);
});

test("view state updates flow through getViewState for persistence", () => {
	const { store, doc } = freshStore("hello world");
	const harness = makeView(store, doc.id);
	harness.view.handleInput("\x1b[1;5C"); // ctrl+right: cursor at the end of "hello" (5)
	harness.view.handleInput("\x1b[1;2C"); // shift+right extends over the space
	const state = harness.view.getViewState();
	assert.ok(state);
	assert.equal(state.documentId, doc.id);
	assert.equal(state.cursorOffset, 6);
	assert.equal(state.selectionAnchorOffset, 5);
	const copy = harness.view.getViewState();
	assert.ok(copy);
	copy.cursorOffset = 999;
	assert.equal(harness.view.getViewState()?.cursorOffset, 6, "callers cannot corrupt the view's state");
});

test("setView restores an explicit scroll and caret state", () => {
	const text = Array.from({ length: 40 }, (_, index) => `line ${index}`).join("\n");
	const { store, doc } = freshStore(text);
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(10);
	harness.view.setView({
		documentId: doc.id,
		cursorOffset: text.length,
		scrollLine: 30,
		horizontalCellOffset: 0,
	});
	harness.view.focused = true;
	const rows = plain(harness.view.render(80));
	assert.equal(harness.view.getViewState()?.scrollLine, 30, "the injected scroll is honored while the caret is visible");
	assert.ok(rows.some((row) => row.includes("line 39")));
	assert.equal(harness.view.render(80).join("").split(CURSOR_MARKER).length - 1, 1);
});

test("delete line on the trailing empty line removes the previous terminator", () => {
	const { store, doc } = freshStore("a\n");
	store.setSelection(doc.id, { cursorOffset: 2 });
	const harness = makeView(store, doc.id);
	assert.equal(harness.view.handleInput("\x1b[107;6u"), true);
	assert.equal(store.getDocument(doc.id)?.text, "a");
});

test("render never throws or emits overwide rows at degenerate widths", () => {
	const { store, doc } = freshStore("hello 日本語 👩‍💻\tend");
	const harness = makeView(store, doc.id);
	harness.view.setViewportRows(4);
	for (const width of [0, 1, 2, 20, 40, 80, 120]) {
		let rows: string[] = [];
		assert.doesNotThrow(() => {
			rows = harness.view.render(width);
		}, `render throws at width ${width}`);
		assert.equal(rows.length, 4);
		for (const row of rows) {
			assert.ok(visibleWidth(row) <= width, `row exceeds ${width}: ${stripTerminalSequences(row)}`);
		}
	}
});

test("toolkit parity: decodePrintableKey and CURSOR_MARKER contracts are pinned", () => {
	// EditorView deep-imports decodePrintableKey (pi-tui 1.0.0 root export set
	// does not include it). If a toolkit bump moves it, this fails loudly.
	assert.equal(decodePrintableKey("a"), undefined, "legacy printable keys are not CSI-u");
	assert.equal(decodePrintableKey("\x1b[97u"), "a", "Kitty CSI-u plain key");
	assert.equal(CURSOR_MARKER, "\x1b_pi:c\x07");
});
