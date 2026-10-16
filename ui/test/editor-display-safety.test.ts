// Editor display-safety regressions: a document/producer URI whose name
// carries percent-encoded terminal control bytes must never reach the chrome
// (tab strip, breadcrumb, problems rows) as live escape sequences. The fix
// lives inside `uriDisplayPath`, so every consumer is covered.
/* eslint-disable no-control-regex -- terminal control sequences are the subject matter here. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences } from "@earendil-works/pi-tui";
import { EditorPane, uriDisplayPath } from "../src/components/EditorPane.ts";
import { ProblemsPane, formatProblemRow } from "../src/components/ProblemsPane.ts";
import { ProblemsModel } from "../src/editor/intelligence.ts";
import { EditorStore } from "../src/editor/model.ts";
import { TabModel } from "../src/editor/tabs.ts";
import { EditorView } from "../src/editor/view.ts";

/** OSC 52 clipboard write + BEL, percent-encoded, inside a file name. */
const EVIL_URI = "file:///w/evil%1B%5D52%3Bc%3BcHduZWQ%07.py";
/** The same name spelled without control bytes: after sanitization both agree. */
const CLEAN_URI = "file:///w/evil.py";

/**
 * Remove the SGR color sequences the theme emits (`\x1b[…m`, including the
 * extended-color forms `hasNoTerminalControls` rejects). What remains must be
 * plain text: any other ESC/BEL byte is an injection that survived.
 */
function stripSgr(text: string): string {
	return text.replace(/\x1b\[[0-9;]*m/g, "");
}

function setupPane(uri: string): EditorPane {
	const store = new EditorStore();
	const opened = store.openDocument({
		workspaceId: "ws",
		uri,
		text: "hello",
		languageId: "python",
		revision: "rev-1",
	});
	assert.ok(opened.ok, "document should open");
	const documentId = opened.document.id;
	const tabModel = new TabModel({ isDirty: () => false });
	tabModel.open(documentId);
	const view = new EditorView({
		store,
		view: store.getViewState(documentId) as NonNullable<ReturnType<EditorStore["getViewState"]>>,
		requestRender: () => {},
		clipboard: { async copy() { return true; }, async paste() { return null; } },
	});
	return new EditorPane({ store, tabModel, view, requestRender: () => {} });
}

test("percent-encoded control bytes in a document URI never reach the tab strip or breadcrumb", () => {
	assert.equal(uriDisplayPath(EVIL_URI), "/w/evil.py", "the OSC and BEL are stripped, not re-encoded");
	const pane = setupPane(EVIL_URI);
	const cleanPane = setupPane(CLEAN_URI);
	pane.layout(8);
	cleanPane.layout(8);
	const rows = pane.render(80);
	const cleanRows = cleanPane.render(80);
	assert.deepEqual(rows, cleanRows, "the hostile name renders exactly like its benign counterpart");

	const strip = rows[0] ?? "";
	const crumb = rows[1] ?? "";
	for (const [name, row] of [["tab strip", strip], ["breadcrumb", crumb]] as const) {
		const rest = stripSgr(row);
		assert.ok(!rest.includes("\x1b"), `${name} carries an unlisted escape: ${JSON.stringify(rest)}`);
		assert.ok(!rest.includes("\x07"), `${name} carries a BEL from the injected OSC`);
		assert.ok(!row.includes("]52;") && !row.includes("cHduZWQ"), `${name} leaks the OSC payload`);
	}
	// Visible text keeps the honest path with the control run removed.
	assert.match(stripTerminalSequences(strip), /\[evil\.py ✕\]/);
	assert.match(stripTerminalSequences(crumb), /\/w\/evil\.py/);
});

test("a problems row with a %1B URI renders no escape byte", () => {
	const model = new ProblemsModel();
	model.push({ uri: EVIL_URI, version: 1, source: "ts", diagnostics: [
		{ range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } }, severity: 1, message: "boom" },
	] });
	const row = model.rows()[0];
	assert.ok(row, "the diagnostic row exists");
	const formatted = formatProblemRow(row);
	assert.ok(!formatted.includes("\x1b"), `formatProblemRow leaks an escape: ${JSON.stringify(formatted)}`);
	assert.ok(!formatted.includes("\x07"), "no BEL survives");
	assert.equal(formatted, "/w/evil.py:1:1 ✗ boom (ts)");

	const pane = new ProblemsPane({ model, rowBudget: 3 });
	const lines = pane.render(80);
	for (const line of lines) {
		const rest = stripSgr(line);
		assert.ok(!rest.includes("\x1b") && !rest.includes("\x07"), `pane row leaks controls: ${JSON.stringify(rest)}`);
	}
	assert.match(stripTerminalSequences(lines[1] ?? ""), /▶ \/w\/evil\.py:1:1 ✗ boom \(ts\)/);
});

test("a raw control byte in a URI that sanitizes to an empty path never reaches the tab strip", () => {
	// `disambiguatedTabLabels` once fell back to the RAW URI when the decoded
	// path had no segments, bypassing uriDisplayPath (V1 verification finding).
	// A raw ESC (deliverable through JSON-RPC) could thus reach the tab strip.
	const pane = setupPane("file://\u001b");
	pane.layout(8);
	const rows = pane.render(80);
	const strip = rows[0] ?? "";
	const rest = stripSgr(strip);
	assert.ok(!rest.includes("\x1b"), `tab strip leaks a raw escape: ${JSON.stringify(rest)}`);
	assert.ok(!strip.includes("\x07"), "no BEL survives either");
	assert.ok(!strip.includes("file://"), "the raw URI is not painted verbatim");
});
