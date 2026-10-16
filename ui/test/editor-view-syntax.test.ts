// EditorView syntax wiring (closing pass, UX-B → EditorView): spans are
// applied through `applySyntax` on the raw-line projection, after the gutter,
// and never break selection fill, the inverse caret cell, CURSOR_MARKER
// column math, horizontal scrolling, tab expansion or the control-character
// projection. Highlighting is opt-in (`setSyntaxEnabled`), so every other
// view test keeps byte-identical output.
import assert from "node:assert/strict";
import { test } from "node:test";
import { CURSOR_MARKER, stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { theme } from "../src/design/theme.ts";
import { EditorStore, type DocumentRecord } from "../src/editor/model.ts";
import { EditorView } from "../src/editor/view.ts";

function openDoc(store: EditorStore, text: string, languageId: string, uri = "file:///w/f"): DocumentRecord {
	const result = store.openDocument({
		workspaceId: "ws",
		uri,
		text,
		languageId,
		revision: "rev-1",
		readOnly: false,
	});
	assert.ok(result.ok, "document opens");
	return result.document;
}

function makeView(store: EditorStore, documentId: string): EditorView {
	const injected = store.getViewState(documentId);
	assert.ok(injected);
	return new EditorView({
		store,
		view: injected,
		requestRender: () => {},
		clipboard: {
			async copy() {
				return true;
			},
			async paste() {
				return null;
			},
		},
	});
}

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

test("a python line renders keyword/string/comment/number SGR distinctly with intact text", () => {
	const store = new EditorStore();
	const doc = openDoc(store, 'def greet(name: str, n=42):  # say hi\n    return "ok"\n', "python", "file:///w/main.py");
	const view = makeView(store, doc.id);
	view.setSyntaxEnabled(true);
	view.setViewportRows(4);
	const rows = view.render(80);
	const first = rows[0] ?? "";
	assert.ok(first.includes(theme.accent("def")), `keyword styled: ${first}`);
	assert.ok(first.includes(theme.info("str")), "builtin type styled");
	assert.ok(first.includes(theme.warning("42")), "number styled");
	assert.ok(first.includes(theme.dim("# say hi")), "comment styled");
	assert.ok(first.includes("\x1b["), "ANSI styling present");
	assert.equal(plain(rows)[0]?.trimEnd(), "1  def greet(name: str, n=42):  # say hi", "visible text intact with the gutter");
	const second = rows[1] ?? "";
	assert.ok(second.includes(theme.success('"ok"')), "string styled");
	assert.ok(second.includes(theme.accent("return")), "keyword on the continuation line");
});

test("a multi-line state carries across lines (triple-quoted string) and invalidates from the edit", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "x = 1\n'''\nstill inside\nafter = 2\n", "python", "file:///w/t.py");
	const view = makeView(store, doc.id);
	view.setSyntaxEnabled(true);
	view.setViewportRows(6);
	const rows = plain(view.render(60));
	assert.equal(rows[2]?.trimEnd(), "3  still inside", "gutter + content text");
	// Line 3 ("still inside") is inside the triple-quoted string → success ink.
	let styled = view.render(60);
	assert.ok(styled[2]?.includes(theme.success("still inside")), `continuation line carries string state: ${styled[2]}`);

	// Edit line 0 to open a NEW string before the old one: the cached states
	// from the edited line onward must be recomputed (no stale prefix reuse).
	store.setSelection(doc.id, { cursorOffset: 0 });
	view.handleInput("'''");
	styled = view.render(60);
	assert.ok(styled[0]?.includes(theme.success("'''x = 1")), `edit invalidated the cache: ${styled[0]}`);
	// Line 1 (`'''`) now CLOSES the new string, so line 2 is no longer inside
	// one — exactly the recompute this test exists for (a stale cache would
	// still paint it as string ink).
	assert.ok(styled[1]?.includes(theme.success("'''")), "the closing line participates in the new state");
	assert.ok(!styled[2]?.includes(theme.success("still inside")), `line 3 left the string state: ${styled[2]}`);
	assert.equal(plain(view.render(60)).length, 6);
});

test("width invariants hold at 20/40/80/120 with CJK and tabs; toggling off is byte-identical", () => {
	const text = "def 日\t本(a):\n\treturn len(日 本)\nstrings = \"ok\"  # mixed \t 日本語 with tabs\n";
	const store = new EditorStore();
	const doc = openDoc(store, text, "python", "file:///w/wide.py");
	store.setSelection(doc.id, { cursorOffset: 5 });
	const view = makeView(store, doc.id);
	view.setViewportRows(5);
	const off80 = view.render(80);
	assert.ok(off80.every((row) => !row.includes(theme.success("ok"))), "off by default");
	for (const width of [20, 40, 80, 120]) {
		view.setSyntaxEnabled(false);
		const off = view.render(width);
		view.setSyntaxEnabled(true);
		const on = view.render(width);
		for (const row of on) {
			assert.ok(visibleWidth(row) <= width, `row exceeds ${width}: ${stripTerminalSequences(row)}`);
		}
		assert.ok(on.some((row) => row.includes("\x1b[")), `styling present at ${width}`);
		assert.deepEqual(plain(on), plain(off), `visible text identical at ${width}`);
	}
	view.setSyntaxEnabled(false);
	assert.deepEqual(view.render(80), off80, "toggle off restores the exact previous frame");
});

test("the caret column, its marker and the inverse cell survive highlighting", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "const value = 日本語;\n", "typescript", "file:///w/a.ts");
	const view = makeView(store, doc.id);
	view.setViewportRows(3);
	view.focused = true;
	// Place the caret after the keyword, through the view's own state seam.
	view.setView({ documentId: doc.id, cursorOffset: 6, scrollLine: 0, horizontalCellOffset: 0 });
	const offColumn = view.cursorDisplayPosition();
	const offRows = view.render(60);
	view.setSyntaxEnabled(true);
	view.setView({ documentId: doc.id, cursorOffset: 6, scrollLine: 0, horizontalCellOffset: 0 });
	const onColumn = view.cursorDisplayPosition();
	assert.deepEqual(onColumn, offColumn, "caret display column unchanged by highlighting");
	const rows = view.render(60);
	assert.equal(rows.join("").split(CURSOR_MARKER).length - 1, 1, "exactly one cursor marker");
	assert.ok(rows[0]?.includes("\x1b[7m"), "the inverse caret cell is present");
	assert.equal(plain(rows)[0], plain(offRows)[0], "visible text unchanged under the caret");
	assert.ok(rows[0]?.includes(theme.accent("const")), "keyword styling next to the caret still applies");
});

test("horizontal scrolling slices styled runs without shifting columns", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "let s = \"\t日本語日本語日本語\";\n", "typescript", "file:///w/h.ts");
	const view = makeView(store, doc.id);
	view.setViewportRows(3);
	view.setSyntaxEnabled(true);
	// Park the caret at the line end so the window scrolls right.
	view.setView({ documentId: doc.id, cursorOffset: doc.text.indexOf("\n"), scrollLine: 0, horizontalCellOffset: 0 });
	const scrolled = view.render(24);
	const state = view.getViewState();
	assert.ok((state?.horizontalCellOffset ?? 0) > 0, "scrolled horizontally");
	for (const row of scrolled) assert.ok(visibleWidth(row) <= 24, `row exceeds 24: ${stripTerminalSequences(row)}`);
	assert.ok(plain(scrolled)[0]?.includes(";"), "the line tail is visible after scrolling");
});

test("a wide window over a long file stays correct after typing at the top", () => {
	const lines = Array.from({ length: 4000 }, (_, index) => `print("line ${index}")  # ${index}`);
	lines[0] = "x = '''";
	lines[1] = "inside";
	lines[2] = "'''";
	const store = new EditorStore();
	const doc = openDoc(store, lines.join("\n"), "python", "file:///w/big.py");
	const view = makeView(store, doc.id);
	view.setViewportRows(6);
	view.setSyntaxEnabled(true);
	const first = view.render(60);
	assert.ok(first[1]?.includes(theme.success("inside")), "state computed through the visible window");
	// Type at the top: the cached prefix above the edit is reused, the state
	// from the edited line is recomputed.
	store.setSelection(doc.id, { cursorOffset: 0 });
	view.handleInput("#");
	const rows = view.render(60);
	assert.ok(rows[0]?.includes(theme.dim("#x = '''")), "the edited line re-highlights as a comment");
	// The comment closed the previously-open string; the state change must
	// flow down (stale cached states would keep line 2 painted as a string).
	assert.ok(!rows[1]?.includes(theme.success("inside")), `line 2 left the string state: ${rows[1]}`);
	assert.ok(rows[2]?.includes(theme.success("'''")), "line 3 opens a fresh (unterminated) string");
});
