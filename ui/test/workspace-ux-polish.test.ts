// Closing-pass frame-level checks (WORKSPACE_UX_POLISH findings 1–7, 9, 10):
// the empty state, the root-relative breadcrumb, the status row without a
// repeated language, the explorer header hint, root-relative Problems rows
// with ellipsized messages, the 2-cell gutter rhythm and the Quick Open
// wording. All of these render the real components over a real EditorStore.
import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { EditorPane } from "../src/components/EditorPane.ts";
import { ProblemsPane } from "../src/components/ProblemsPane.ts";
import { EditorStore, type DocumentRecord } from "../src/editor/model.ts";
import { ProblemsModel, type ProblemRow } from "../src/editor/intelligence.ts";
import { EditorView, EMPTY_EDITOR_HINT } from "../src/editor/view.ts";
import { ExplorerModel, ExplorerPane } from "../src/editor/explorer.ts";
import type { WorkspaceListResult } from "../src/workspace/protocol.ts";

function openDoc(store: EditorStore, text: string, uri: string, languageId = "python"): DocumentRecord {
	const result = store.openDocument({ workspaceId: "ws", uri, text, languageId, revision: "rev-1", readOnly: false });
	assert.ok(result.ok);
	return result.document;
}

function paneWith(store: EditorStore, documentId: string | null, tabs: string[]): EditorPane {
	const view = new EditorView({
		store,
		view: { documentId: documentId ?? "", cursorOffset: 0, scrollLine: 0, horizontalCellOffset: 0 },
		requestRender: () => {},
		clipboard: { async copy() { return true; }, async paste() { return null; } },
	});
	const tabModel = {
		list: () => tabs.map((id) => ({ documentId: id })),
		activeDocumentId: () => documentId,
		setActive: () => ({ ok: false }),
	} as unknown as ConstructorParameters<typeof EditorPane>[0]["tabModel"];
	return new EditorPane({ store, tabModel, view, requestRender: () => {} });
}

const plain = (rows: string[]): string[] => rows.map(stripTerminalSequences);

test("empty editor: one hint in the viewport, blank strip/breadcrumb/status (finding 1)", () => {
	const store = new EditorStore();
	const pane = paneWith(store, null, []);
	pane.layout(6);
	const rows = plain(pane.render(50));
	assert.equal(rows.length, 6);
	assert.equal(rows[0]?.trim(), "", "tab strip says nothing extra");
	assert.equal(rows[1]?.trim(), "", "breadcrumb says nothing extra");
	assert.equal(rows[2]?.trimEnd(), `   ${EMPTY_EDITOR_HINT}`, "the single hint is gutter-aligned at the first content row");
	assert.equal(rows[2]?.trimEnd(), `   No open files · Ctrl+P to open`);
	assert.equal(rows[5]?.trim(), "", "status row says nothing extra");
});

test("breadcrumb is root-relative with the language right-aligned (finding 2)", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "print('hi')\n", "file:///tmp/ws-fixture/src/config.py");
	const pane = paneWith(store, doc.id, [doc.id]);
	pane.layout(6);
	pane.setWorkspaceRoot("/tmp/ws-fixture");
	const rows = plain(pane.render(60));
	assert.match(rows[1] ?? "", /^src\/config\.py\s+Python$/, "path relative to the root; language right-aligned");
	assert.doesNotMatch(rows[1] ?? "", /tmp\/ws-fixture/, "the root prefix is stripped");
	// Outside the root: basename only, never an absolute path.
	const other = openDoc(store, "x\n", "file:///elsewhere/notes.md", "markdown");
	const outside = paneWith(store, other.id, [other.id]);
	outside.layout(6);
	outside.setWorkspaceRoot("/tmp/ws-fixture");
	assert.match(plain(outside.render(60))[1] ?? "", /^notes\.md\s+Markdown\s*$/, "basename left, language right");
	assert.doesNotMatch(plain(outside.render(60))[1] ?? "", /elsewhere/);
});

test("status row never repeats the language; the LSP state is right-aligned and drops first (finding 3)", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "x = 1\n", "file:///w/main.py");
	const pane = paneWith(store, doc.id, [doc.id]);
	pane.layout(6);
	pane.setLspStatus("ready");
	const wide = plain(pane.render(80))[5] ?? "";
	assert.match(wide, /^Ln 1, Col 1 · UTF-8 · LF · Python\s+ready\s*$/, `right-aligned state, no repetition: ${wide}`);
	assert.doesNotMatch(wide, /python ready/);
	// A pane too narrow for both drops the state segment (not the position/language).
	const narrow = plain(pane.render(38))[5] ?? "";
	assert.match(narrow, /Python/);
	assert.doesNotMatch(narrow, /ready/);
	pane.setLspStatus("starting…");
	assert.match(plain(pane.render(80))[5] ?? "", /starting…\s*$/);
	pane.setLspStatus("missing");
	assert.match(plain(pane.render(80))[5] ?? "", /missing\s*$/);
	assert.doesNotMatch(plain(pane.render(80))[5] ?? "", /missing missing/);
});

test("explorer header groups the title with the current hidden-entries hint (finding 5)", async () => {
	const list = async (): Promise<WorkspaceListResult> => ({
		entries: [
			{ name: "src", path: "src", kind: "directory" },
			{ name: "main.ts", path: "src/main.ts", kind: "file" },
		],
		nextCursor: null,
		exclusions: [".git", "node_modules", "dist", "build", ".venv"],
		truncated: false,
	});
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	const header = plain(pane.render(50))[0] ?? "";
	assert.match(header, /^workspace\s+h: show hidden\s*$/, `title left, hint right: ${header}`);
	assert.equal(visibleWidth(stripTerminalSequences(header)), 50, "the row fills the pane width");
	await model.setHidden(true);
	assert.match(plain(pane.render(50))[0] ?? "", /^workspace\s+h: hide hidden\s*$/);
	// Files drop the leading glyph (indent only); directories keep ▸/▾.
	const rows = plain(pane.render(50));
	assert.ok(rows.some((row) => /^❯ ▸ src\s*$|^❯ ▸ src /u.test(row) || row.includes("▸ src")), "directory keeps its glyph");
	assert.ok(rows.some((row) => row.includes("main.ts") && !row.includes("· main.ts")), "files render without ·");
	// The exclusion footer shortens to two names plus a count.
	assert.match(rows.join("\n"), /excluded: \.git, node_modules, \+3/);
});

test("problems rows are root-relative with ellipsized messages and aligned glyphs (finding 4)", () => {
	const model = new ProblemsModel();
	model.push({
		uri: "file:///tmp/ws-fixture/src/main.py",
		version: 1,
		source: "pyright",
		diagnostics: [
			{
				range: { start: { line: 2, character: 6 }, end: { line: 2, character: 10 } },
				severity: 1,
				message: 'Argument of type "Literal[12345678901234567890]" cannot be assigned to parameter "x"',
				source: "Pyright",
				code: "reportArgumentType",
			},
		],
	});
	const pane = new ProblemsPane({ model, rowBudget: 3 });
	pane.setWorkspaceRoot("/tmp/ws-fixture");
	const rows = plain(pane.render(50));
	assert.match(rows[0] ?? "", /✗ 1 · ! 0 · i 0/, "counts header unchanged");
	const row = rows[1] ?? "";
	assert.match(row, /^ ?▶ src\/main\.py:3:7 ✗ /, `root-relative location and aligned glyph: ${row}`);
	assert.ok(row.includes("…"), "the message is ellipsized");
	assert.ok(row.includes("(Pyright)"), "the producer label survives the ellipsis");
	assert.ok(visibleWidth(row) <= 50, "never exceeds the pane width");
	assert.doesNotMatch(row, /tmp\/ws-fixture/, "no absolute path");
});

test("gutter right-padding is 2 cells and the caret column math agrees (finding 9)", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "hello\nworld\n", "file:///w/a.py");
	const pane = paneWith(store, doc.id, [doc.id]);
	pane.layout(6);
	const rows = plain(pane.render(40));
	assert.match(rows[2] ?? "", /^1 {2}hello/, "one digit + two padding cells before the content");
	store.setSelection(doc.id, { cursorOffset: 5 });
	const view = new EditorView({
		store,
		view: { documentId: doc.id, cursorOffset: 5, scrollLine: 0, horizontalCellOffset: 0 },
		requestRender: () => {},
		clipboard: { async copy() { return true; }, async paste() { return null; } },
	});
	assert.deepEqual(view.cursorDisplayPosition(), { line: 1, column: 6 });
});

test("problems section rule spans the pane width (finding 9)", () => {
	const store = new EditorStore();
	const doc = openDoc(store, "hello\n", "file:///w/a.py");
	const pane = paneWith(store, doc.id, [doc.id]);
	const model = new ProblemsModel();
	model.push({
		uri: "file:///w/a.py",
		version: 1,
		source: "pyright",
		diagnostics: [{ range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } }, severity: 1, message: "boom" }],
	});
	pane.setProblems(new ProblemsPane({ model, rowBudget: 3 }));
	pane.layout(8);
	const rows = plain(pane.render(24));
	const ruleIndex = rows.findIndex((row) => row.trimEnd() === "─".repeat(24));
	assert.equal(ruleIndex, 4, `the rule sits directly above the section: ${rows.map((r) => r.trimEnd()).join(" | ")}`);
	assert.match(rows[ruleIndex + 1] ?? "", /✗ 1/);
});

test("ProblemsModel dedupes visually identical rows across producers (finding 4)", () => {
	const model = new ProblemsModel();
	const diagnostic = {
		range: { start: { line: 3, character: 7 }, end: { line: 3, character: 11 } },
		severity: 1 as const,
		message: 'Argument of type "Literal[1]" cannot be assigned',
		source: "Pyright",
	};
	model.push({ uri: "file:///w/main.py", version: 1, source: "pyright", diagnostics: [diagnostic] });
	model.push({ uri: "file:///w/main.py", version: 1, source: "Pyright", diagnostics: [diagnostic] });
	assert.equal(model.total(), 2, "both producer sets are retained");
	const rows: ProblemRow[] = model.rows();
	assert.equal(rows.length, 1, "the display projection collapses the duplicate");
	assert.deepEqual(model.counts(), { error: 1, warning: 0, info: 0, hint: 0, total: 1 });
	// Distinct content still renders distinct rows.
	model.push({
		uri: "file:///w/main.py",
		version: 1,
		source: "Pyright",
		diagnostics: [{ ...diagnostic, message: "a different diagnostic" }],
	});
	assert.equal(model.rows().length, 2);
	assert.equal(model.counts().total, 2);
});
