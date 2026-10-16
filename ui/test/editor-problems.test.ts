// T9 UI contract tests: the Problems model (push/refresh, version rules,
// bounds, ordering) and the ProblemsPane + EditorPane bottom-slot behavior.
import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { EditorPane } from "../src/components/EditorPane.ts";
import { CompletionPopup } from "../src/components/IntelligencePopups.ts";
import { ProblemsPane, formatProblemRow } from "../src/components/ProblemsPane.ts";
import {
	PROBLEMS_TOTAL_CAP,
	ProblemsModel,
	type IntelligenceRequest,
} from "../src/editor/intelligence.ts";
import { EditorStore, type DocumentRecord } from "../src/editor/model.ts";
import { TabModel } from "../src/editor/tabs.ts";
import { EditorView } from "../src/editor/view.ts";
import type { WorkspaceIntelligenceResult } from "../src/workspace/protocol.ts";

const URI = "file:///w/src/main.ts";

function diagnostic(line: number, character: number, severity: number, message: string) {
	return {
		range: {
			start: { line, character },
			end: { line, character: character + 1 },
		},
		severity,
		message,
	};
}

function ok(result: unknown, partial: Partial<WorkspaceIntelligenceResult> = {}): WorkspaceIntelligenceResult {
	return {
		workspaceId: "ws",
		source: "editor-buffer",
		uri: URI,
		documentVersion: null,
		serverGeneration: 1,
		workspaceEpoch: 1,
		operation: "diagnostics",
		state: "ready",
		freshness: "versioned",
		truncated: false,
		result,
		...partial,
	};
}

function plain(lines: string[]): string[] {
	return lines.map((line) => stripTerminalSequences(line));
}

function open(store: EditorStore, text: string): DocumentRecord {
	const result = store.openDocument({
		workspaceId: "ws",
		uri: URI,
		text,
		languageId: "typescript",
		revision: "rev-1",
	});
	assert.ok(result.ok);
	return result.document;
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

// -------------------------------------------------------------------- the model

test("pushes replace per (uri, producer) and never merge with stale entries", () => {
	const model = new ProblemsModel();
	const first = model.push({
		uri: URI,
		version: 1,
		source: "ts",
		diagnostics: [diagnostic(0, 0, 1, "old error")],
	});
	assert.equal(first, 1);
	assert.equal(model.total(), 1);
	model.push({
		uri: URI,
		version: 2,
		source: "ts",
		diagnostics: [diagnostic(1, 2, 2, "new warning"), diagnostic(2, 0, 3, "note")],
	});
	assert.equal(model.total(), 2, "the producer's rows are replaced, not appended");
	assert.deepEqual(model.rows().map((row) => row.message), ["new warning", "note"]);

	// An older versioned push is dropped as stale.
	model.push({ uri: URI, version: 1, source: "ts", diagnostics: [diagnostic(0, 0, 1, "resurrected")] });
	assert.equal(model.stalePushes(), 1);
	assert.deepEqual(model.rows().map((row) => row.message), ["new warning", "note"]);

	// A different producer is independent.
	model.push({ uri: URI, version: 1, source: "py", diagnostics: [diagnostic(0, 0, 1, "python error")] });
	assert.equal(model.total(), 3);
	assert.deepEqual(model.producers(), ["py", "ts"]);

	// An empty push clears that producer only.
	model.push({ uri: URI, version: 3, source: "ts", diagnostics: [] });
	assert.deepEqual(model.rows().map((row) => row.message), ["python error"]);
	assert.equal(model.total(), 1);

	// Explicit clear: producer, then everything for the uri.
	assert.equal(model.clear(URI, "py"), true);
	assert.equal(model.total(), 0);
	assert.equal(model.clear(URI), true, "the remaining empty ts entry is dropped");
	assert.equal(model.clear(URI), false, "the uri entry is gone once cleared");
	assert.equal(model.clear("file:///nowhere"), false);
});

test("unversioned pushes replace and label; malformed entries are skipped", () => {
	const model = new ProblemsModel();
	model.push({ uri: URI, version: null, source: "ts", diagnostics: [diagnostic(0, 0, 1, "unversioned")] });
	assert.equal(model.rows()[0]?.version, null);
	model.push({
		uri: URI,
		version: 7,
		source: "ts",
		diagnostics: [diagnostic(0, 0, 1, "versioned now")],
	});
	assert.equal(model.rows()[0]?.version, 7, "a versioned push replaces the unversioned entry");
	model.push({ uri: URI, version: null, source: "ts", diagnostics: [diagnostic(0, 0, 1, "back to unknown")] });
	assert.equal(model.rows()[0]?.version, null);
	model.push({
		uri: URI,
		version: 9,
		source: "ts",
		diagnostics: [diagnostic(0, 0, 1, "kept"), { nope: true }, "garbage"],
	});
	assert.deepEqual(model.rows().map((row) => row.message), ["kept"]);
});

test("rows sort severity → uri → line and counts track each severity", () => {
	const model = new ProblemsModel();
	model.push({
		uri: "file:///w/b.ts",
		version: 1,
		source: "ts",
		diagnostics: [diagnostic(5, 0, 2, "warning b"), diagnostic(1, 0, 1, "error b")],
	});
	model.push({
		uri: "file:///w/a.ts",
		version: 1,
		source: "ts",
		diagnostics: [
			diagnostic(9, 0, 3, "info a"),
			diagnostic(0, 0, 1, "error a"),
			diagnostic(2, 0, 4, "hint a"),
		],
	});
	assert.deepEqual(
		model.rows().map((row) => `${row.uri.split("/").at(-1)}:${row.line}:${row.severity}`),
		["a.ts:0:1", "b.ts:1:1", "b.ts:5:2", "a.ts:9:3", "a.ts:2:4"],
	);
	assert.deepEqual(model.counts(), { error: 2, warning: 1, info: 1, hint: 1, total: 5 });
});

test("the total cap evicts the least-severe rows first and discloses them as omitted", () => {
	const model = new ProblemsModel();
	const hints = Array.from({ length: PROBLEMS_TOTAL_CAP + 5 }, (_, index) =>
		diagnostic(index, 0, 4, `hint ${index}`),
	);
	model.push({ uri: URI, version: 1, source: "ts", diagnostics: hints });
	assert.equal(model.total(), PROBLEMS_TOTAL_CAP);
	assert.equal(model.omitted(), 5);
	assert.equal(model.rows().length, PROBLEMS_TOTAL_CAP);
	// A severe diagnostic added later evicts another hint, never the error.
	model.push({ uri: URI, version: 1, source: "py", diagnostics: [diagnostic(0, 0, 1, "critical")] });
	assert.equal(model.total(), PROBLEMS_TOTAL_CAP);
	assert.equal(model.omitted(), 6);
	assert.equal(model.rows()[0]?.message, "critical", "errors survive the cap");

	// Replacing the producer re-delivers its diagnostics, so its share of the
	// cap disclosure resets instead of inflating forever.
	model.push({ uri: URI, version: 2, source: "ts", diagnostics: [diagnostic(0, 0, 2, "recomputed")] });
	assert.equal(model.omitted(), 0);
	assert.deepEqual(model.rows().map((row) => row.message), ["critical", "recomputed"]);
});

test("refresh replaces the producers the payload names, clears empties, and retains state on unavailable", async () => {
	const payloads: unknown[] = [
		{
			diagnostics: [
				{
					range: { start: { line: 3, character: 0 }, end: { line: 3, character: 1 } },
					severity: 1,
					message: "fresh ts error",
					source: "ts",
				},
			],
			omitted: 2,
			producers: ["ts", "py"],
		},
	];
	const request: IntelligenceRequest = async () => ok(payloads.shift());
	const model = new ProblemsModel({ request });
	model.push({ uri: URI, version: 1, source: "ts", diagnostics: [diagnostic(0, 0, 2, "old ts")] });
	model.push({ uri: URI, version: 1, source: "py", diagnostics: [diagnostic(0, 0, 2, "old py")] });
	const refreshed = await model.refresh(URI, 1);
	assert.deepEqual(refreshed, { ok: true, producers: 2, records: 1 });
	assert.deepEqual(model.rows().map((row) => row.message), ["fresh ts error"]);
	assert.equal(model.omitted(), 2, "the server's omitted count is disclosed");
	assert.equal(model.total(), 1);

	payloads.push(undefined); // value ignored below; replaced by a state override
	const stopped: IntelligenceRequest = async () => ok(null, { state: "stopped" });
	const retained = new ProblemsModel({ request: stopped });
	retained.push({ uri: URI, version: 1, source: "ts", diagnostics: [diagnostic(0, 0, 1, "stale but honest")] });
	const result = await retained.refresh(URI, 1);
	assert.deepEqual(result, { ok: false, reason: "unavailable", state: "stopped" });
	assert.equal(retained.total(), 1, "unavailable refreshes retain the last projection");
	assert.equal(retained.rows()[0]?.message, "stale but honest");

	const failing = new ProblemsModel({
		request: async () => {
			throw new Error("down");
		},
	});
	assert.deepEqual(await failing.refresh(URI), { ok: false, reason: "error" });
	assert.deepEqual(await new ProblemsModel().refresh(URI), { ok: false, reason: "no-request" });
});

// --------------------------------------------------------------------- the pane

function threeProblemModel(): ProblemsModel {
	const model = new ProblemsModel();
	model.push({
		uri: URI,
		version: 0,
		source: "ts",
		diagnostics: [
			{
				range: { start: { line: 2, character: 4 }, end: { line: 2, character: 5 } },
				severity: 1,
				message: "cannot find name 'x'",
				source: "ts",
			},
			{
				range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
				severity: 2,
				message: "unused variable",
				source: "ts",
			},
			{
				range: { start: { line: 1, character: 0 }, end: { line: 1, character: 1 } },
				severity: 3,
				message: "consider a doc comment",
				source: "ts",
			},
		],
	});
	return model;
}

test("problems pane renders counts, formatted rows and the empty state within its budget", () => {
	const pane = new ProblemsPane({ model: threeProblemModel(), rowBudget: 6 });
	const rows = plain(pane.render(80));
	assert.equal(rows.length, 6, "exactly the row budget");
	assert.match(rows[0] ?? "", /✗ 1 · ! 1 · i 1/);
	assert.match(rows[1] ?? "", /▶ \/w\/src\/main\.ts:3:5 ✗ cannot find name 'x' \(ts\)/);
	assert.match(rows[2] ?? "", /\/w\/src\/main\.ts:1:1 ! unused variable \(ts\)/);
	assert.match(rows[3] ?? "", /\/w\/src\/main\.ts:2:1 i consider a doc comment \(ts\)/);
	assert.equal((rows[4] ?? "").trim(), "");
	assert.equal((rows[5] ?? "").trim(), "");
	for (const width of [20, 40, 80, 120]) {
		for (const line of pane.render(width)) {
			assert.ok(visibleWidth(line) <= width, `row exceeds ${width}: ${stripTerminalSequences(line)}`);
		}
	}

	const empty = new ProblemsPane({ model: new ProblemsModel() });
	const emptyRows = plain(empty.render(40));
	assert.match(emptyRows[0] ?? "", /✗ 0 · ! 0 · i 0/);
	assert.match(emptyRows[1] ?? "", /No problems/);
});

test("problems pane windows rows with a +N disclosure and selects with the keyboard", () => {
	const model = new ProblemsModel();
	model.push({
		uri: URI,
		version: 1,
		source: "ts",
		diagnostics: Array.from({ length: 10 }, (_, index) => diagnostic(index, 0, 2, `warning ${index}`)),
	});
	const opened: string[] = [];
	let closed = 0;
	const pane = new ProblemsPane({
		model,
		rowBudget: 4,
		onOpen: (row) => opened.push(row.message),
		onClose: () => {
			closed += 1;
		},
	});
	const first = plain(pane.render(60));
	assert.equal(first.length, 4);
	assert.match(first[1] ?? "", /▶ /, "selection marker is text");
	assert.match(first[3] ?? "", /\+8 more/, "hidden rows are disclosed");
	assert.equal(pane.handleInput("\x1b[B"), true);
	assert.equal(pane.selectedIndex(), 1);
	assert.match(plain(pane.render(60))[2] ?? "", /▶ .*warning 1/);
	assert.equal(pane.handleInput("\x1b[F"), true, "end");
	assert.equal(pane.selectedIndex(), 9);
	assert.match(plain(pane.render(60)).join("\n"), /\+8 more/, "footer follows the window");
	assert.equal(pane.handleInput("\r"), true);
	assert.deepEqual(opened, ["warning 9"]);
	assert.equal(pane.handleInput("\x1b"), true, "esc asks the parent to close");
	assert.equal(closed, 1);
	assert.equal(pane.handleInput("x"), false);

	const mouseSelect: number[] = [];
	const mousePane = new ProblemsPane({ model, rowBudget: 4, onOpen: (row) => mouseSelect.push(row.line) });
	mousePane.render(60);
	const press = mousePane.handleMouse(mouseEvent({ type: "press", x: 0, y: 2, width: 60 }));
	assert.deepEqual(press, { handled: true, focus: true });
	assert.equal(mousePane.selectedIndex(), 1);
	mousePane.handleMouse(mouseEvent({ type: "click", x: 0, y: 2, width: 60 }));
	assert.deepEqual(mouseSelect, [1]);
	assert.equal(mousePane.handleMouse(mouseEvent({ type: "press", x: 0, y: 0, width: 60 })), undefined);
});

test("unversioned diagnostics are labeled in rows", () => {
	const model = new ProblemsModel();
	model.push({ uri: URI, version: null, source: "ts", diagnostics: [diagnostic(0, 0, 1, "mystery")] });
	const pane = new ProblemsPane({ model, rowBudget: 2 });
	const row = plain(pane.render(80))[1] ?? "";
	assert.match(row, /\[unversioned\]/);
	assert.equal(
		formatProblemRow(model.rows()[0]!),
		"/w/src/main.ts:1:1 ✗ mystery (ts) [unversioned]",
	);
});

// ------------------------------------------------------- EditorPane integration

interface PaneHarness {
	store: EditorStore;
	doc: DocumentRecord;
	pane: EditorPane;
}

function setupPane(text: string): PaneHarness {
	const store = new EditorStore();
	const doc = open(store, text);
	const tabModel = new TabModel({ isDirty: (documentId) => store.getDocument(documentId)?.dirty ?? false });
	tabModel.open(doc.id);
	const view = new EditorView({
		store,
		view: store.getViewState(doc.id) as NonNullable<ReturnType<EditorStore["getViewState"]>>,
		requestRender: () => {},
		clipboard: { async copy() { return true; }, async paste() { return null; } },
	});
	const pane = new EditorPane({ store, tabModel, view, requestRender: () => {} });
	return { store, doc, pane };
}

test("editor pane renders the problems section as a fixed bottom budget below the status row", () => {
	const harness = setupPane("hello");
	const problems = new ProblemsPane({ model: threeProblemModel() });
	harness.pane.setProblems(problems, 4);
	assert.equal(problems.getRowBudget(), 4, "the pane feeds the component's budget seam");
	harness.pane.layout(12);
	const rows = plain(harness.pane.render(60));
	assert.equal(rows.length, 12, "allocation is honored");
	assert.equal(harness.pane.getViewportRows(), 5, "12 - 3 chrome - 4 problems rows");
	assert.match(rows[2] ?? "", /1 hello/);
	assert.match(rows[7] ?? "", /Ln 1, Col 1/, "status row moves up");
	assert.match(rows[8] ?? "", /✗ 1 · ! 1 · i 1/);
	assert.match(rows[9] ?? "", /main\.ts:3:5 ✗/);
	assert.match(rows[10] ?? "", /main\.ts:1:1 !/);
	assert.match(rows[11] ?? "", /main\.ts:2:1 i/);

	harness.pane.setProblems(undefined);
	harness.pane.render(60);
	assert.equal(harness.pane.getViewportRows(), 9, "removing the section returns the rows");
});

test("the popup wins contested slot rows; problems never overdraw the allocation", () => {
	const harness = setupPane("hello");
	const model = threeProblemModel();
	const problems = new ProblemsPane({ model, rowBudget: 4 });
	const popup = new CompletionPopup();
	popup.setModel({
		items: Array.from({ length: 8 }, (_, index) => ({ label: `choice-${index}` })),
		selectedIndex: 0,
		omitted: 0,
	});
	harness.pane.setIntelligence(popup, {});
	harness.pane.setProblems(problems, 4);
	harness.pane.layout(12);
	const rows = plain(harness.pane.render(60));
	assert.equal(rows.length, 12);
	assert.equal(harness.pane.getViewportRows(), 1, "the popup takes the shared budget");
	assert.ok(!rows.some((line) => line.includes("✗ 1 ·")), "the problems section yields until rows free up");
	assert.match(rows[11] ?? "", /Ln 1, Col 1/);

	harness.pane.setIntelligence(undefined);
	const after = plain(harness.pane.render(60));
	assert.equal(after.length, 12);
	assert.match(after[8] ?? "", /✗ 1 · ! 1 · i 1/, "the problems section returns once the popup unmounts");
});

test("a pull replaces the set a push created (no duplicate rows from producer labels)", async () => {
	// CodeRabbit pass-1 finding, matches the live-frame duplicate: events key
	// by the backend producer ("pyright") while pulls keyed by the server's
	// display label ("Pyright"), so the same diagnostic appeared twice.
	const model = new ProblemsModel({
		request: async () =>
			ok({
				diagnostics: [
					{ ...diagnostic(2, 6, 1, 'Argument of type "Literal[123]" cannot be assigned'), source: "Pyright", producer: "pyright" },
				],
				omitted: 0,
				producers: [{ source: "pyright", version: 2, freshness: "versioned", count: 1 }],
			}),
	});
	model.push({
		uri: URI,
		version: 2,
		source: "pyright",
		diagnostics: [diagnostic(2, 6, 1, 'Argument of type "Literal[123]" cannot be assigned')],
	});
	assert.equal(model.rows().length, 1, "event push stored one row");
	await model.refresh(URI, 2);
	assert.equal(model.rows().length, 1, "refresh replaced the same producer's set, not added a duplicate");
	assert.deepEqual(model.producers(), ["pyright"], "one producer key survives");
});

test("legacy string producers and unattributed rows still map to one key", async () => {
	const model = new ProblemsModel({
		request: async () =>
			ok({
				diagnostics: [diagnostic(0, 0, 2, "unused import")],
				omitted: 0,
				producers: ["pyright"],
			}),
	});
	await model.refresh(URI, 1);
	const rows = model.rows();
	assert.equal(rows.length, 1);
	assert.equal(rows[0]?.producer, "pyright", "single-producer payload attributes unattributed rows");
});
