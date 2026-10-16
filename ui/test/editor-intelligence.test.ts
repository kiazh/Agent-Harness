// T9 UI contract tests: intelligence coordination (completion/hover/location/
// rename/symbol narrowing) and the popup components, plus the EditorPane
// keyboard pass-through contract (plan §8 priority: popup first).
import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth, type Component, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { EditorPane } from "../src/components/EditorPane.ts";
import { CompletionPopup, HoverPopup, completionPopupModelFrom } from "../src/components/IntelligencePopups.ts";
import {
	CompletionSession,
	HoverSession,
	LocationPicker,
	RenameSession,
	narrowCompletionPayload,
	narrowSymbolsPayload,
	prepareHoverText,
	snippetSyntaxPresent,
	type IntelligenceRequest,
} from "../src/editor/intelligence.ts";
import { EditorStore, type DocumentRecord } from "../src/editor/model.ts";
import { TabModel } from "../src/editor/tabs.ts";
import { EditorView } from "../src/editor/view.ts";
import type { LspRange, WorkspaceIntelligenceResult } from "../src/workspace/protocol.ts";

const URI = "file:///w/src/main.ts";

function open(store: EditorStore, text: string, uri: string = URI): DocumentRecord {
	const result = store.openDocument({
		workspaceId: "ws",
		uri,
		text,
		languageId: "typescript",
		revision: "rev-1",
	});
	assert.ok(result.ok, "document should open");
	return result.document;
}

function range(startLine: number, startCharacter: number, endLine: number, endCharacter: number): LspRange {
	return {
		start: { line: startLine, character: startCharacter },
		end: { line: endLine, character: endCharacter },
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
		operation: "completion",
		state: "ready",
		freshness: "versioned",
		truncated: false,
		result,
		...partial,
	};
}

interface Deferred<T> {
	promise: Promise<T>;
	resolve(value: T): void;
	reject(error?: unknown): void;
}

function deferred<T>(): Deferred<T> {
	let resolve!: (value: T) => void;
	let reject!: (error?: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function completionPayload(items: unknown[], extra: Record<string, unknown> = {}): unknown {
	return { isIncomplete: false, items, omitted: 0, ...extra };
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

// ----------------------------------------------------------------- completion

test("completion session walks idle→loading→visible and exposes a filtered, capped list", async () => {
	const store = new EditorStore();
	const doc = open(store, "const x = 1;");
	const pending = deferred<WorkspaceIntelligenceResult>();
	let captured: { operation: string; uri: string; version: number | null; position: unknown } | undefined;
	const request: IntelligenceRequest = async (operation, uri, version, position) => {
		captured = { operation, uri, version, position };
		return pending.promise;
	};
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 7 },
		prefix: "x",
	});
	assert.equal(session.state, "idle");
	const started = session.start();
	assert.equal(session.state, "loading");
	assert.deepEqual(captured, {
		operation: "completion",
		uri: URI,
		version: 0,
		position: { line: 0, character: 7 },
	});
	pending.resolve(ok(completionPayload([{ label: "x" }, { label: "xyz" }, { label: "other" }])));
	await started;
	assert.equal(session.state, "visible");
	assert.equal(session.capturedVersion, 0);
	assert.deepEqual(session.items().map((item) => item.label), ["x", "xyz"]);
	assert.equal(session.itemCount(), 2);
	assert.equal(session.selectedIndex(), 0);
	assert.equal(session.selectedItem()?.label, "x");
	session.moveSelection(5);
	assert.equal(session.selectedIndex(), 1, "selection clamps at the end");
	session.moveSelection(-5);
	assert.equal(session.selectedIndex(), 0, "selection clamps at the start");
	session.close("done");
	assert.equal(session.state, "closed");
	assert.equal(session.closedReason, "done");
});

test("a newer request logically cancels the previous one; late responses are dropped", async () => {
	const store = new EditorStore();
	const doc = open(store, "a");
	const first = deferred<WorkspaceIntelligenceResult>();
	const second = deferred<WorkspaceIntelligenceResult>();
	let calls = 0;
	const request: IntelligenceRequest = async () => {
		calls += 1;
		return calls === 1 ? first.promise : second.promise;
	};
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 1 },
	});
	const started = session.start();
	const rerun = session.rerun("ab");
	second.resolve(ok(completionPayload([{ label: "ab" }])));
	await rerun;
	assert.equal(session.state, "visible");
	assert.deepEqual(session.items().map((item) => item.label), ["ab"]);
	first.resolve(ok(completionPayload([{ label: "late" }])));
	await started;
	assert.equal(session.supersededDrops, 1, "the superseded response is dropped");
	assert.deepEqual(session.items().map((item) => item.label), ["ab"], "state keeps the newer response");
	assert.equal(session.lastDropReason, undefined, "supersede is not a display drop reason (acceptance still owns state)");
});

test("a response computed against another version is dropped as stale", async () => {
	const store = new EditorStore();
	const doc = open(store, "a");
	assert.ok(store.insertText(doc.id, "b").ok, "buffer moves to version 1");
	const request: IntelligenceRequest = async () =>
		ok(completionPayload([{ label: "b" }]), { documentVersion: 0 });
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 2 },
	});
	await session.start();
	assert.equal(session.state, "closed");
	assert.equal(session.staleDrops, 1);
	assert.equal(session.lastDropReason, "stale");
	assert.equal(session.itemCount(), 0, "stale items are never presented");
});

test("non-ready states and request errors close with honest reasons", async () => {
	const store = new EditorStore();
	const doc = open(store, "a");
	const stopped: IntelligenceRequest = async () =>
		ok(completionPayload([{ label: "x" }]), { state: "stopped" });
	const unavailable = new CompletionSession({
		store,
		request: stopped,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 0 },
	});
	await unavailable.start();
	assert.equal(unavailable.state, "closed");
	assert.equal(unavailable.lastDropReason, "unavailable");

	const failing: IntelligenceRequest = async () => {
		throw new Error("transport down");
	};
	const errored = new CompletionSession({
		store,
		request: failing,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 0 },
	});
	await errored.start();
	assert.equal(errored.state, "closed");
	assert.equal(errored.lastDropReason, "error");
});

test("prefix filtering uses filterText ?? label and preserves the selection when the item survives", async () => {
	const store = new EditorStore();
	const doc = open(store, "m");
	const request: IntelligenceRequest = async () =>
		ok(
			completionPayload([
				{ label: "map" },
				{ label: "mapper" },
				{ label: "max" },
				{ label: "display", filterText: "df" },
			]),
		);
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 1 },
	});
	await session.start();
	assert.equal(session.itemCount(), 4);
	session.setSelectedIndex(1);
	assert.equal(session.selectedItem()?.label, "mapper");
	session.setPrefix("map");
	assert.deepEqual(session.items().map((item) => item.label), ["map", "mapper"]);
	assert.equal(session.selectedItem()?.label, "mapper", "selection index follows the surviving item");
	session.setPrefix("mapp");
	assert.deepEqual(session.items().map((item) => item.label), ["mapper"]);
	assert.equal(session.selectedIndex(), 0, "clamped when the selected item is filtered out");
	session.setPrefix("df");
	assert.deepEqual(session.items().map((item) => item.label), ["display"], "filterText replaces the label for matching");
	session.setPrefix("map");
	assert.deepEqual(session.items().map((item) => item.label), ["map", "mapper"]);
	session.setPrefix("zzz");
	assert.equal(session.itemCount(), 0);
});

test("items are capped at 100 with an honest omitted count", async () => {
	const store = new EditorStore();
	const doc = open(store, "a");
	const items = Array.from({ length: 150 }, (_, index) => ({ label: `item-${String(index).padStart(3, "0")}` }));
	const request: IntelligenceRequest = async () => ok(completionPayload(items, { omitted: 5 }));
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 1 },
	});
	await session.start();
	assert.equal(session.receivedCount(), 150);
	assert.equal(session.filteredCount(), 150);
	assert.equal(session.itemCount(), 100);
	assert.equal(session.omitted(), 55, "50 hidden by the cap plus 5 server-omitted");
	assert.equal(session.items().at(0)?.label, "item-000");
	assert.equal(session.items().at(-1)?.label, "item-099");
});

test("accept applies the textEdit plus additional edits as ONE undo group", async () => {
	const store = new EditorStore();
	const doc = open(store, "const x = 1;");
	const request: IntelligenceRequest = async () =>
		ok(
			completionPayload([
				{
					label: "xyz",
					kind: 6,
					textEdit: { range: range(0, 6, 0, 7), newText: "xyz" },
					additionalTextEdits: [{ range: range(0, 0, 0, 0), newText: "// hint\n" }],
				},
			]),
		);
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 7 },
	});
	await session.start();
	const result = session.accept();
	assert.equal(result.ok, true);
	if (!result.ok) return;
	assert.equal(result.applied.length, 1);
	assert.equal(store.getDocument(doc.id)?.text, "// hint\nconst xyz = 1;");
	assert.equal(store.getDocument(doc.id)?.version, 1);
	assert.equal(store.undoDepth(doc.id), 1, "one transaction, one undo group");
	assert.equal(session.state, "closed");
	assert.ok(store.undo(doc.id).ok);
	assert.equal(store.getDocument(doc.id)?.text, "const x = 1;", "a single undo restores the pre-accept text");
});

test("accept inserts the label at the captured position when no textEdit exists", async () => {
	const store = new EditorStore();
	const doc = open(store, "ab");
	const request: IntelligenceRequest = async () => ok(completionPayload([{ label: "foo" }]));
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 2 },
		prefix: "foo",
	});
	await session.start();
	const result = session.accept();
	assert.equal(result.ok, true);
	assert.equal(store.getDocument(doc.id)?.text, "abfoo");
});

test("an InsertReplaceEdit is normalized to its insert range", async () => {
	const store = new EditorStore();
	const doc = open(store, "abcde");
	const request: IntelligenceRequest = async () =>
		ok(
			completionPayload([
				{
					label: "map",
					textEdit: { insert: range(0, 2, 0, 2), replace: range(0, 0, 0, 5), newText: "map" },
				},
			]),
		);
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 2 },
	});
	await session.start();
	assert.ok(session.selectedItem()?.replaceRange, "the replace side is preserved for policy");
	const result = session.accept();
	assert.equal(result.ok, true);
	assert.equal(store.getDocument(doc.id)?.text, "abmapcde", "only the insert range is applied");
});

test("snippet items are rejected honestly by default; literal opt-in never accepts tab stops", async () => {
	const store = new EditorStore();
	const doc = open(store, "ab");
	const snippetItems = [
		{ label: "fn", insertTextFormat: 2, textEdit: { range: range(0, 2, 0, 2), newText: "fn(${1:x})" } },
	];
	const request: IntelligenceRequest = async () => ok(completionPayload(snippetItems));
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 2 },
	});
	await session.start();
	const rejected = session.accept();
	assert.equal(rejected.ok, false);
	if (rejected.ok) return;
	assert.equal(rejected.reason, "snippet");
	assert.match(rejected.message, /snippet/i);
	assert.equal(store.getDocument(doc.id)?.text, "ab", "nothing is inserted");
	assert.equal(session.state, "visible", "the user can still pick another item");

	const literalStore = new EditorStore();
	const literalDoc = open(literalStore, "ab");
	const literalRequest: IntelligenceRequest = async () =>
		ok(
			completionPayload([
				{ label: "plain", insertTextFormat: 2, textEdit: { range: range(0, 2, 0, 2), newText: "plain()" } },
			]),
		);
	const literal = new CompletionSession({
		store: literalStore,
		request: literalRequest,
		uri: literalDoc.uri,
		documentId: literalDoc.id,
		position: { line: 0, character: 2 },
		allowLiteralSnippets: true,
	});
	await literal.start();
	const accepted = literal.accept();
	assert.equal(accepted.ok, true, "snippet-syntax-free text may be inserted literally");
	assert.equal(literalStore.getDocument(literalDoc.id)?.text, "abplain()");

	const tabStopStore = new EditorStore();
	const tabStopDoc = open(tabStopStore, "ab");
	const tabStopRequest: IntelligenceRequest = async () => ok(completionPayload(snippetItems));
	const tabStops = new CompletionSession({
		store: tabStopStore,
		request: tabStopRequest,
		uri: tabStopDoc.uri,
		documentId: tabStopDoc.id,
		position: { line: 0, character: 2 },
		allowLiteralSnippets: true,
	});
	await tabStops.start();
	const tabStopRejected = tabStops.accept();
	assert.equal(tabStopRejected.ok, false);
	if (!tabStopRejected.ok) assert.equal(tabStopRejected.reason, "snippet");
	assert.equal(tabStopStore.getDocument(tabStopDoc.id)?.text, "ab");
	assert.equal(snippetSyntaxPresent("fn(${1:x})"), true);
	assert.equal(snippetSyntaxPresent("plain()"), false);
});

test("accept validates the captured version: a moved buffer rejects as stale and closes", async () => {
	const store = new EditorStore();
	const doc = open(store, "ab");
	const request: IntelligenceRequest = async () =>
		ok(completionPayload([{ label: "foo", textEdit: { range: range(0, 2, 0, 2), newText: "foo" } }]), {
			documentVersion: 0,
		});
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 2 },
	});
	await session.start();
	assert.ok(store.insertText(doc.id, "!").ok, "the buffer moves after the completion arrived");
	const result = session.accept();
	assert.equal(result.ok, false);
	if (result.ok) return;
	assert.equal(result.reason, "stale");
	assert.equal(session.state, "closed");
	assert.equal(store.getDocument(doc.id)?.text, "!ab", "no edit was staged");
});

test("accept refuses malformed items and overlapping edit envelopes", async () => {
	const store = new EditorStore();
	const doc = open(store, "abc");
	const malformedRequest: IntelligenceRequest = async () =>
		ok(completionPayload([{ label: "x", textEdit: { range: null, newText: "x" } }]));
	const malformed = new CompletionSession({
		store,
		request: malformedRequest,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 0 },
	});
	await malformed.start();
	assert.equal(malformed.selectedItem()?.malformedTextEdit, true);
	const malformedResult = malformed.accept();
	assert.equal(malformedResult.ok, false);
	if (!malformedResult.ok) assert.equal(malformedResult.reason, "invalid-item");

	const overlapRequest: IntelligenceRequest = async () =>
		ok(
			completionPayload([
				{
					label: "x",
					textEdit: { range: range(0, 0, 0, 3), newText: "xyz" },
					additionalTextEdits: [{ range: range(0, 1, 0, 2), newText: "!" }],
				},
			]),
		);
	const overlap = new CompletionSession({
		store,
		request: overlapRequest,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 3 },
	});
	await overlap.start();
	const overlapResult = overlap.accept();
	assert.equal(overlapResult.ok, false);
	if (!overlapResult.ok) {
		assert.equal(overlapResult.reason, "refused");
		assert.deepEqual(overlapResult.refusals, [{ uri: URI, reason: "overlapping-edits" }]);
	}
	assert.equal(store.getDocument(doc.id)?.text, "abc");
});

// ----------------------------------------------------------------------- hover

test("hover preparation sanitizes untrusted content and caps lines", () => {
	const hostile = "\x1b[31mred\x1b[0m\x07\x1b]0;title\x07\u2028line";
	const prepared = prepareHoverText({ contents: hostile });
	assert.ok(prepared);
	if (!prepared) return;
	assert.ok(!prepared.text.includes("\x07"), "BEL is stripped");
	assert.ok(!prepared.text.includes("\x1b]"), "OSC title payload is stripped");
	assert.ok(!prepared.text.includes("\u2028"), "Unicode line separator is stripped");
	assert.ok(prepared.text.includes("red"), "printable text survives");

	const manyLines = prepareHoverText({ contents: Array.from({ length: 130 }, (_, i) => `line ${i}`).join("\n") });
	assert.ok(manyLines);
	if (!manyLines) return;
	assert.equal(manyLines.text.split("\n").length, 100);
	assert.equal(manyLines.omittedLines, 30);

	const longText = prepareHoverText({ contents: "x".repeat(30000) });
	assert.ok(longText);
	if (!longText) return;
	assert.equal(longText.charTruncated, true);
	assert.ok(longText.text.length <= 20000);

	assert.equal(prepareHoverText(undefined), undefined);
	assert.equal(prepareHoverText({ contents: 42 }), undefined);
});

test("hover session serves prepared text, ignores superseded responses and dismisses", async () => {
	const pending = deferred<WorkspaceIntelligenceResult>();
	const request: IntelligenceRequest = async () => pending.promise;
	const session = new HoverSession({
		request,
		uri: URI,
		version: 0,
		position: { line: 0, character: 1 },
	});
	const started = session.start();
	assert.equal(session.state, "loading");
	pending.resolve(ok({ contents: "docs for `x`\nsecond line", range: range(0, 0, 0, 1) }, { operation: "hover" }));
	await started;
	assert.equal(session.state, "visible");
	assert.equal(session.contents, "docs for `x`\nsecond line");
	assert.deepEqual(session.range, range(0, 0, 0, 1));
	assert.equal(session.omittedLines, 0);
	session.dismiss();
	assert.equal(session.state, "dismissed");
	assert.equal(session.contents, "docs for `x`\nsecond line", "dismissal never mutates the prepared text");

	const late = deferred<WorkspaceIntelligenceResult>();
	const lateSession = new HoverSession({
		request: async () => late.promise,
		uri: URI,
		position: { line: 0, character: 1 },
	});
	const lateStarted = lateSession.start();
	lateSession.dismiss();
	late.resolve(ok({ contents: "too late" }, { operation: "hover" }));
	await lateStarted;
	assert.notEqual(lateSession.contents, "too late", "a dismissed hover never resurrects");
	assert.equal(lateSession.state, "dismissed");
});

// ------------------------------------------------------------ location picker

test("location picker selects targets with strict next/prev bounds and confirm", () => {
	const picker = new LocationPicker([
		{ uri: "file:///w/a.ts", range: range(0, 0, 0, 1) },
		{ uri: "file:///w/b.ts", range: range(2, 3, 2, 4) },
		{ uri: "file:///w/c.ts", range: range(5, 0, 5, 1) },
		{ uri: 7, range: null },
	]);
	assert.equal(picker.size, 3);
	assert.equal(picker.omitted(), 1, "undecodable targets are disclosed, not dropped silently");
	assert.equal(picker.index(), 0);
	assert.equal(picker.previous()?.uri, "file:///w/a.ts", "previous at the first target stays put");
	assert.equal(picker.next()?.lineLabel, "3:4");
	assert.equal(picker.index(), 1);
	assert.equal(picker.next()?.lineLabel, "6:1");
	assert.equal(picker.next()?.lineLabel, "6:1", "next at the last target stays put");
	assert.equal(picker.index(), 2);
	assert.equal(picker.select(9), false);
	assert.equal(picker.index(), 2);
	assert.equal(picker.select(0), true);
	assert.equal(picker.confirm()?.uri, "file:///w/a.ts");
	assert.deepEqual(
		picker.targets().map((target) => target.uri),
		["file:///w/a.ts", "file:///w/b.ts", "file:///w/c.ts"],
	);
	const empty = new LocationPicker(undefined);
	assert.equal(empty.size, 0);
	assert.equal(empty.confirm(), undefined);
	assert.equal(empty.next(), undefined);
});

// ---------------------------------------------------------------------- rename

test("rename is honestly unavailable until the service allow-lists it", async () => {
	const store = new EditorStore();
	const doc = open(store, "let name = 1;");
	const session = new RenameSession({
		store,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 5 },
	});
	const started = await session.start();
	assert.equal(started.ok, false);
	if (started.ok) return;
	assert.equal(started.unavailable, true);
	assert.match(started.reason, /rename/i);
	assert.match(started.reason, /not .*offer|unavailable/i);
	assert.equal(session.state, "unavailable");
	assert.equal(store.getDocument(doc.id)?.version, 0, "nothing was touched");
});

test("rename with a payload previews then applies through version-checked staging", async () => {
	const store = new EditorStore();
	const doc = open(store, "let name = 1;");
	const payload = {
		label: "Rename",
		changes: [
			{
				uri: URI,
				version: 0,
				edits: [{ range: range(0, 4, 0, 8), newText: "title" }],
			},
		],
		refused: [],
	};
	const requestRename: (
		operation: "rename",
		uri: string,
		version: number | null,
		position?: { line: number; character: number },
	) => Promise<WorkspaceIntelligenceResult> = async () => ok({ edits: payload, unavailable: false }, { operation: "rename" });
	const session = new RenameSession({
		store,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 6 },
		requestRename,
	});
	const started = await session.start();
	assert.equal(started.ok, true);
	if (!started.ok) return;
	assert.equal(session.state, "ready");
	assert.equal(started.preview.totalEdits, 1);
	assert.equal(started.preview.files.length, 1);
	assert.equal(started.preview.files[0]?.documentId, doc.id);
	const declined = session.apply(false);
	assert.equal(declined.ok, false);
	if (!declined.ok) assert.equal(declined.reason, "not-confirmed");
	assert.equal(store.getDocument(doc.id)?.text, "let name = 1;");
	const applied = session.apply(true);
	assert.equal(applied.ok, true);
	assert.equal(store.getDocument(doc.id)?.text, "let title = 1;");
	assert.equal(session.state, "applied");

	// Version revalidation happens inside applyWorkspaceEdit: a payload built
	// against an old version refuses after the buffer moved on.
	const staleStore = new EditorStore();
	const staleDoc = open(staleStore, "let name = 1;");
	const staleSession = new RenameSession({
		store: staleStore,
		uri: staleDoc.uri,
		documentId: staleDoc.id,
		position: { line: 0, character: 6 },
		requestRename,
	});
	assert.equal((await staleSession.start()).ok, true);
	assert.ok(staleStore.insertText(staleDoc.id, " ").ok);
	const refused = staleSession.apply(true);
	assert.equal(refused.ok, false);
	if (!refused.ok) {
		assert.equal(refused.reason, "refused");
		assert.deepEqual(refused.refusals, [{ uri: URI, reason: "version-mismatch" }]);
	}
	assert.equal(staleSession.state, "refused");
});

// -------------------------------------------------------------- symbol/defensive

test("symbol narrowing flattens hierarchies and narrows garbage to empty", () => {
	const narrowed = narrowSymbolsPayload({
		hierarchical: [
			{
				name: "Widget",
				kind: 7,
				selectionRange: range(0, 6, 0, 12),
				children: [{ name: "render", kind: 6, selectionRange: range(2, 2, 2, 8) }],
			},
		],
		omitted: 3,
	});
	assert.deepEqual(
		narrowed.symbols.map((symbol) => [symbol.name, symbol.containerName ?? null]),
		[
			["Widget", null],
			["render", "Widget"],
		],
	);
	assert.equal(narrowed.omitted, 3);
	assert.deepEqual(narrowSymbolsPayload(null), { symbols: [], omitted: 0 });
	assert.deepEqual(narrowSymbolsPayload({ flat: [{ name: 5 }] }), { symbols: [], omitted: 1 });
	assert.deepEqual(narrowCompletionPayload("garbage"), { isIncomplete: false, items: [], omitted: 0 });
});

// ------------------------------------------------------------- completion popup

function popupModel(labels: string[]): { items: Array<{ label: string; kind?: number; detail?: string }>; selectedIndex: number; omitted: number } {
	return { items: labels.map((label) => ({ label })), selectedIndex: 0, omitted: 0 };
}

test("completion popup renders a bounded window with a non-color selection marker", () => {
	const popup = new CompletionPopup();
	const labels = Array.from({ length: 20 }, (_, index) => `item-${index}`);
	popup.setModel(popupModel(labels));
	for (const width of [20, 40, 80]) {
		const lines = popup.render(width);
		assert.ok(lines.length <= 8, `popup stays at 8 rows at width ${width}`);
		for (const line of lines) {
			assert.ok(visibleWidth(line) <= width, `row exceeds ${width}: ${stripTerminalSequences(line)}`);
		}
	}
	const first = plain(popup.render(40));
	assert.match(first[0] ?? "", /▶/, "selection keeps a text marker");
	assert.match(first[0] ?? "", /item-0/);
	assert.match(first.join("\n"), /\+13 more/, "hidden items are disclosed");
	popup.moveSelection(12);
	const scrolled = plain(popup.render(40));
	assert.match(scrolled.join("\n"), /item-12/);
	assert.match(scrolled.join("\n"), /▶/);
	assert.ok(scrolled.some((line) => line.includes("item-12") && line.includes("▶")), "selection stays visible");
	assert.ok(!scrolled.some((line) => /^\s*▶ .*item-0\b/.test(line)), "the window scrolled past the top");
});

test("completion popup shows kind glyphs, truncates detail, and reports omitted counts", () => {
	const popup = new CompletionPopup();
	popup.setModel({
		items: [{ label: "map", kind: 2, detail: "map<T, U>(items: T[], fn: (item: T) => U): U[]" }],
		selectedIndex: 0,
		omitted: 4,
	});
	const narrow = plain(popup.render(20));
	assert.equal(narrow.length, 2, "one item row plus the disclosure");
	assert.match(narrow[0] ?? "", /▶ m map/);
	assert.ok(visibleWidth(popup.render(20)[0] ?? "") <= 20);
	assert.match(narrow[1] ?? "", /\+4 more/);
});

test("completion popup key mapping is pure and semantic", () => {
	const popup = new CompletionPopup();
	assert.deepEqual(popup.handleInput("\r"), { kind: "unhandled" }, "closed lists claim nothing");
	popup.setModel(popupModel(["a", "b"]));
	assert.deepEqual(popup.handleInput("\r"), { kind: "accept" });
	assert.deepEqual(popup.handleInput("\t"), { kind: "accept" });
	assert.deepEqual(popup.handleInput("\x1b[B"), { kind: "next" });
	assert.deepEqual(popup.handleInput("\x1b[A"), { kind: "prev" });
	assert.deepEqual(popup.handleInput("\x1b"), { kind: "close" });
	assert.deepEqual(popup.handleInput("a"), { kind: "insert-text", text: "a" });
	assert.deepEqual(popup.handleInput("\x1b[6~"), { kind: "unhandled" });
	assert.equal(popup.selectedIndex(), 0, "handleInput never mutates the machine's selection");
	popup.moveSelection(1);
	assert.equal(popup.selectedIndex(), 1);
	assert.equal(popup.moveSelection(1), 1, "selection clamps");
});

test("completion popup mouse: press selects, hover highlights, click accepts", () => {
	const popup = new CompletionPopup();
	popup.setModel(popupModel(["a", "b", "c"]));
	const before = popup.render(30).join("\n");
	const press = popup.handleMouse(mouseEvent({ type: "press", x: 0, y: 1, width: 30 }));
	assert.deepEqual(press, { handled: true, focus: true });
	assert.equal(popup.selectedIndex(), 1);
	const hover = popup.handleMouse(mouseEvent({ type: "move", x: 0, y: 2, width: 30 }));
	assert.deepEqual(hover, { handled: true, render: true });
	assert.notEqual(popup.render(30).join("\n"), before, "hover has a visible treatment");
	const actions: string[] = [];
	popup.onAction = (action) => actions.push(action.kind);
	const click = popup.handleMouse(mouseEvent({ type: "click", x: 0, y: 2, width: 30 }));
	assert.deepEqual(click, { handled: true, focus: true });
	assert.deepEqual(actions, ["accept"], "click accepts through the parent callback");
	assert.equal(popup.selectedIndex(), 2);
	assert.equal(popup.handleMouse(mouseEvent({ type: "press", x: 0, y: 9, width: 30 })), undefined);
});

test("completion popup snapshots a session through the list-source view", async () => {
	const store = new EditorStore();
	const doc = open(store, "s");
	const request: IntelligenceRequest = async () => ok(completionPayload([{ label: "sort" }, { label: "sortBy" }], { omitted: 2 }));
	const session = new CompletionSession({
		store,
		request,
		uri: doc.uri,
		documentId: doc.id,
		position: { line: 0, character: 1 },
	});
	await session.start();
	session.setSelectedIndex(1);
	const model = completionPopupModelFrom(session);
	assert.deepEqual(model.items.map((item) => item.label), ["sort", "sortBy"]);
	assert.equal(model.selectedIndex, 1);
	assert.equal(model.omitted, 2);
});

// ------------------------------------------------------------------ hover popup

test("hover popup renders sanitized text bounded to 80 columns and 15 rows", () => {
	const popup = new HoverPopup();
	assert.deepEqual(popup.render(80), []);
	popup.setText("plain docs");
	assert.equal(popup.isEmpty(), false);
	for (const width of [20, 40, 80, 120]) {
		const lines = popup.render(width);
		assert.ok(lines.length > 0);
		for (const line of lines) {
			assert.ok(visibleWidth(line) <= Math.min(width, 80), `hover row exceeds bounds at ${width}`);
		}
	}
	const hostile = popup;
	hostile.setText("a\x1b[2Jb\x07c\x1b]8;;http://evil.example\x07link\x1b]8;;\x07");
	const flat = plain(hostile.render(80)).join("");
	assert.ok(!flat.includes("\x1b"), "no escape survives");
	assert.ok(!flat.includes("\x07"), "no BEL survives");
	assert.ok(flat.includes("link"), "link text renders as text only");
	const tall = new HoverPopup();
	tall.setText(Array.from({ length: 60 }, (_, i) => `row ${i}`).join("\n"));
	assert.equal(tall.render(80).length, 15, "rows clamp at 15");
	const unfocused = tall.render(80).join("\n");
	tall.focused = true;
	assert.notEqual(tall.render(80).join("\n"), unfocused, "focus changes the paint style (dim when unfocused)");
	assert.deepEqual(tall.handleInput("\x1b"), { kind: "close" });
	assert.deepEqual(tall.handleInput("\r"), { kind: "unhandled" }, "hover never accepts Enter");
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

test("editor pane renders the intelligence popup directly below the viewport, above the status row", () => {
	const harness = setupPane("hello");
	const popup = new CompletionPopup();
	popup.setModel(popupModel(["alpha", "beta", "gamma"]));
	harness.pane.setIntelligence(popup, {});
	harness.pane.layout(12);
	const rows = plain(harness.pane.render(60));
	assert.equal(rows.length, 12, "allocation is honored");
	assert.equal(harness.pane.getViewportRows(), 6, "12 - 3 chrome - 3 popup rows");
	assert.match(rows[2] ?? "", /1 hello/, "viewport starts at row 2");
	assert.match(rows[8] ?? "", /alpha/, "popup starts directly below the viewport");
	assert.match(rows[9] ?? "", /beta/);
	assert.match(rows[10] ?? "", /gamma/);
	assert.match(rows[11] ?? "", /Ln 1, Col 1/, "status row stays last");
});

test("Enter is consumed only while the popup is open (plan §8 priority)", () => {
	const harness = setupPane("hello");
	const popup = new CompletionPopup();
	let accepts = 0;
	const moves: number[] = [];
	harness.pane.setIntelligence(popup, { accept: () => { accepts += 1; }, move: (d) => moves.push(d) });
	assert.equal(harness.pane.handleIntelligenceKey("\r"), "fallthrough", "no popup content → the editor sees Enter");
	popup.setModel(popupModel(["alpha"]));
	assert.equal(harness.pane.handleIntelligenceKey("\r"), "consumed");
	assert.equal(accepts, 1);
	assert.equal(harness.pane.handleIntelligenceKey("\x1b[B"), "consumed");
	assert.deepEqual(moves, [1]);
	assert.equal(harness.pane.handleIntelligenceKey("a"), "fallthrough", "printable keys keep typing");
});

test("Esc closes the top popup first and never propagates to the editor", () => {
	const harness = setupPane("hello");
	const popup = new CompletionPopup();
	let dismissed = 0;
	harness.pane.setIntelligence(popup, {
		dismiss: () => {
			dismissed += 1;
			popup.setModel(undefined);
		},
	});
	popup.setModel(popupModel(["alpha"]));
	assert.equal(harness.pane.handleIntelligenceKey("\x1b"), "consumed");
	assert.equal(dismissed, 1);
	assert.equal(harness.pane.handleIntelligenceKey("\x1b"), "fallthrough", "a closed popup does not swallow Esc");
	harness.pane.setIntelligence(undefined);
	assert.equal(harness.pane.handleIntelligenceKey("\x1b"), "fallthrough");
});

test("pane mouse routing reaches the popup without moving the editor caret", () => {
	const harness = setupPane("hello world");
	const popup = new CompletionPopup();
	popup.setModel(popupModel(["alpha", "beta"]));
	harness.pane.setIntelligence(popup, {});
	harness.pane.layout(12);
	harness.pane.render(60);
	const popupTop = 2 + harness.pane.getViewportRows();
	const result = harness.pane.handleMouse(mouseEvent({ type: "press", x: 2, y: popupTop + 1, width: 60 }));
	assert.deepEqual(result, { handled: true, focus: true });
	assert.equal(popup.selectedIndex(), 1);
	assert.equal(harness.store.getViewState(harness.doc.id)?.cursorOffset, 0, "the caret never moved");
	// A viewport press captures the pointer: a drag crossing the popup band
	// still belongs to the editor gesture.
	harness.pane.handleMouse(mouseEvent({ type: "press", x: 2, y: 2, width: 60 }));
	const drag = harness.pane.handleMouse(mouseEvent({ type: "drag", x: 2 + 5, y: popupTop, width: 60 }));
	assert.equal(drag?.handled, true);
	assert.equal(harness.store.getViewState(harness.doc.id)?.cursorOffset, 5, "the drag moved the caret");
});

test("pane honors degenerate allocations with both slots mounted", () => {
	const harness = setupPane("hello");
	const popup = new CompletionPopup();
	popup.setModel(popupModel(["alpha", "beta", "gamma"]));
	const problemsStub: Component = { render: () => ["problem-row"], invalidate: () => {} };
	harness.pane.setIntelligence(popup, {});
	harness.pane.setProblems(problemsStub, 3);
	for (const allocated of [1, 2, 3, 4, 8, 23, 40]) {
		harness.pane.layout(allocated);
		for (const width of [0, 1, 2, 20, 40, 80]) {
			let rendered: string[] = [];
			assert.doesNotThrow(() => {
				rendered = harness.pane.render(width);
			}, `render throws at ${width}x${allocated} with slots mounted`);
			assert.equal(rendered.length, allocated, `allocation ${allocated} honored at width ${width}`);
			for (const row of rendered) {
				assert.ok(visibleWidth(row) <= width, `row exceeds ${width}: ${stripTerminalSequences(row)}`);
			}
		}
	}
});
