// T4a contract tests: the editor document store (plan §9, §22, §24B).
// Pure model semantics: transactions, versions, undo/redo groups, save
// acknowledgement races, disk reload/conflict flows and text budgets.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	DEFAULT_EDITABLE_FILE_BUDGET_BYTES,
	DEFAULT_TOTAL_TEXT_BUDGET_BYTES,
	DEFAULT_UNDO_BUDGET_BYTES,
	EditorStore,
	detectLineEnding,
	dominantLineEnding,
	hashText,
	type DocumentRecord,
	type TextEdit,
} from "../src/editor/model.ts";

function expectRefusal(result: unknown, code: string): void {
	const refusal = result as { ok: boolean; error?: { code: string; message: string } };
	assert.equal(refusal.ok, false, "expected a structured refusal");
	assert.ok(refusal.error, "refusal must carry an error object");
	assert.equal(refusal.error.code, code);
	assert.ok(refusal.error.message.length > 0, "refusal must carry a message");
}

function open(
	store: EditorStore,
	uri: string,
	text: string,
	extra: Partial<{ revision: string | null; readOnly: boolean; languageId: string; workspaceId: string }> = {},
): DocumentRecord {
	const result = store.openDocument({
		workspaceId: extra.workspaceId ?? "ws1",
		uri,
		text,
		languageId: extra.languageId ?? "typescript",
		revision: extra.revision === undefined ? "rev-1" : extra.revision,
		readOnly: extra.readOnly ?? false,
	});
	assert.ok(result.ok, `openDocument(${uri}) should succeed`);
	return result.document;
}

function doc(store: EditorStore, documentId: string): DocumentRecord {
	const record = store.getDocument(documentId);
	assert.ok(record, `document ${documentId} should exist`);
	return record;
}

function edit(startOffset: number, endOffset: number, text: string): TextEdit {
	return { range: { startOffset, endOffset }, text };
}

function assertDirtyInvariant(store: EditorStore, documentId: string): void {
	const record = doc(store, documentId);
	const expected =
		record.savedTextHash === null ? record.text.length > 0 : hashText(record.text) !== record.savedTextHash;
	assert.equal(record.dirty, expected, "dirty must be derived from savedTextHash");
}

test("openDocument builds a record with detected line endings and saved identity", () => {
	const store = new EditorStore();
	const text = "let x = 1;\r\n";
	const record = open(store, "file:///w/a.ts", text, { revision: "rev-1" });
	assert.ok(record.id.length > 0);
	assert.equal(record.workspaceId, "ws1");
	assert.equal(record.uri, "file:///w/a.ts");
	assert.equal(record.languageId, "typescript");
	assert.equal(record.text, text);
	assert.equal(record.version, 0);
	assert.equal(record.savedRevision, "rev-1");
	assert.equal(record.savedTextHash, hashText(text));
	assert.equal(record.dirty, false);
	assert.equal(record.readOnly, false);
	assert.equal(record.lineEnding, "CRLF");
	assert.equal(record.encoding, "utf-8");
	assert.equal(record.conflict, undefined);
	assert.equal(store.totalTextBytes(), new TextEncoder().encode(text).length);
	assertDirtyInvariant(store, record.id);

	// No revision -> untitled semantics: non-empty text counts as unsaved work.
	const untitled = open(store, "file:///w/untitled.ts", "draft", { revision: null });
	assert.equal(untitled.savedRevision, null);
	assert.equal(untitled.savedTextHash, null);
	assert.equal(untitled.dirty, true);
	const blank = open(store, "file:///w/blank.ts", "", { revision: null });
	assert.equal(blank.dirty, false);
	// The caller-provided readOnly flag is honored verbatim.
	const huge = open(store, "file:///w/huge.ts", "x", { readOnly: true });
	assert.equal(huge.readOnly, true);
	assert.equal(detectLineEnding("no newline"), "LF");
	assert.equal(detectLineEnding("a\nb"), "LF");
	assert.equal(detectLineEnding("a\r\nb"), "CRLF");
	assert.equal(detectLineEnding("a\rb"), "CR");
	assert.equal(detectLineEnding("a\r\nb\nc"), "mixed");
	assert.equal(dominantLineEnding("a\r\nb\nc"), "CRLF"); // count tie -> earliest occurrence
	assert.equal(dominantLineEnding("a\nb\r\nc\r\n"), "CRLF");
	assert.equal(dominantLineEnding("a\rb\nc\n"), "LF");
});

test("openDocument dedupes by canonical URI and keeps duplicate basenames distinct", () => {
	const store = new EditorStore();
	const first = open(store, "file:///w/a.ts", "original");
	const again = store.openDocument({
		workspaceId: "ws1",
		uri: "file:///w/a.ts",
		text: "competing buffer",
		languageId: "typescript",
		revision: "rev-9",
	});
	assert.ok(again.ok);
	assert.equal(again.created, false);
	assert.equal(again.document.id, first.id);
	assert.equal(doc(store, first.id).text, "original");
	assert.equal(store.documents().length, 1);

	const one = open(store, "file:///w/one/a.ts", "one");
	const two = open(store, "file:///w/two/a.ts", "two");
	assert.notEqual(one.id, two.id);
	assert.equal(store.documents().length, 3);

	// Explicit unknown ids are refused rather than silently remapped.
	const duplicate = store.openDocument({
		workspaceId: "ws1",
		uri: "file:///w/other.ts",
		text: "x",
		languageId: "typescript",
		revision: "rev-1",
		id: first.id,
	});
	expectRefusal(duplicate, "duplicate-id");
});

test("total open text budget refuses new opens and never evicts dirty documents", () => {
	const store = new EditorStore({ totalTextBudgetBytes: 5 });
	const a = open(store, "file:///w/a.txt", "12345");
	assert.equal(store.totalTextBytes(), 5);
	const refused = store.openDocument({
		workspaceId: "ws1",
		uri: "file:///w/b.txt",
		text: "x",
		languageId: "plaintext",
		revision: "rev-1",
	});
	expectRefusal(refused, "budget-exceeded");
	assert.equal(store.documents().length, 1);

	// Make A dirty; a refused open must not evict it.
	const typed = store.insertText(a.id, "!");
	assert.ok(typed.ok);
	assert.equal(doc(store, a.id).dirty, true);
	const refusedAgain = store.openDocument({
		workspaceId: "ws1",
		uri: "file:///w/c.txt",
		text: "y",
		languageId: "plaintext",
		revision: "rev-1",
	});
	expectRefusal(refusedAgain, "budget-exceeded");
	assert.equal(doc(store, a.id).text, "!12345");
	assert.equal(store.totalTextBytes(), 6); // edits are not re-checked; opens are
	assert.ok(store.closeDocument(a.id));
	assert.equal(store.totalTextBytes(), 0);
	const now = store.openDocument({
		workspaceId: "ws1",
		uri: "file:///w/c.txt",
		text: "y",
		languageId: "plaintext",
		revision: "rev-1",
	});
	assert.ok(now.ok);

	// Budget counts UTF-8 bytes, not UTF-16 code units.
	const utf8Store = new EditorStore({ totalTextBudgetBytes: 5 });
	const cjk = open(utf8Store, "file:///w/cjk.txt", "漢"); // 3 UTF-8 bytes
	assert.equal(utf8Store.totalTextBytes(), 3);
	const two = open(utf8Store, "file:///w/two.txt", "ab"); // 2 bytes -> exactly at budget
	assert.ok(cjk.id !== two.id);
	expectRefusal(
		utf8Store.openDocument({
			workspaceId: "ws1",
			uri: "file:///w/over.txt",
			text: "c",
			languageId: "plaintext",
			revision: "rev-1",
		}),
		"budget-exceeded",
	);
});

test("read-only documents refuse edits with a structured result", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/big.bin", "large content", { readOnly: true });
	const before = { text: record.text, version: record.version };
	expectRefusal(store.applyEdit(record.id, [edit(0, 1, "X")]), "read-only");
	expectRefusal(store.insertText(record.id, "x"), "read-only");
	expectRefusal(store.backspace(record.id), "read-only");
	expectRefusal(store.deleteForward(record.id), "read-only");
	expectRefusal(store.newline(record.id), "read-only");
	expectRefusal(store.replaceSelection(record.id, "x"), "read-only");
	assert.equal(doc(store, record.id).text, before.text);
	assert.equal(doc(store, record.id).version, before.version);
	assert.equal(store.undoDepth(record.id), 0);
	assert.equal(store.serializeForSave(record.id).ok, true);

	// Read-only buffers still participate in disk-state flows.
	const reloaded = store.reloadFromDisk(record.id, "refreshed view", "rev-2");
	assert.ok(reloaded.ok);
	assert.equal(doc(store, record.id).text, "refreshed view");
	assert.equal(doc(store, record.id).savedRevision, "rev-2");
	assert.equal(store.undoDepth(record.id), 0);
});

test("applyEdit applies multiple edits as one transaction with one version increment", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "hello world");
	const result = store.applyEdit(record.id, [edit(0, 1, "H"), edit(6, 7, "W")]);
	assert.ok(result.ok);
	assert.equal(result.version, 1);
	assert.equal(doc(store, record.id).text, "Hello World");
	assert.equal(doc(store, record.id).dirty, true);
	const second = store.applyEdit(record.id, [edit(0, 5, "HELLO")]);
	assert.ok(second.ok);
	assert.equal(second.version, 2);
	assert.equal(doc(store, record.id).text, "HELLO World");
	assertDirtyInvariant(store, record.id);
});

test("applyEdit rejects invalid offsets and overlapping ranges without partial mutation", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "hello");
	const malformed = null as unknown as TextEdit;
	const bad: unknown[] = [
		store.applyEdit(record.id, [edit(0, 6, "x")]), // end past EOF
		store.applyEdit(record.id, [edit(4, 2, "x")]), // start > end
		store.applyEdit(record.id, [edit(-1, 0, "x")]), // negative
		store.applyEdit(record.id, [edit(0.5, 1, "x")]), // fractional
		store.applyEdit(record.id, [edit(0, 3, "X"), edit(2, 5, "Y")]), // overlap
		store.applyEdit(record.id, [edit(0, 5, "X"), edit(2, 2, "Y")]), // insert inside replace
		store.applyEdit(record.id, []), // empty transaction
		store.applyEdit(record.id, [{ range: { startOffset: 0, endOffset: 1 }, text: 5 as unknown as string }]),
		store.applyEdit(record.id, [malformed]),
	];
	for (const refused of bad) expectRefusal(refused, "invalid-edit");
	const current = doc(store, record.id);
	assert.equal(current.text, "hello");
	assert.equal(current.version, 0);
	assert.equal(current.dirty, false);
	assert.equal(store.undoDepth(record.id), 0);
	assert.equal(store.undoBytesTotal(), 0);
});

test("same-position inserts preserve caller order; insertion at a replacement start is rejected", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "hello");
	// Same-position inserts are valid and keep the order of the array.
	const inserts = store.applyEdit(record.id, [edit(5, 5, "A"), edit(5, 5, "B")]);
	assert.ok(inserts.ok);
	assert.equal(doc(store, record.id).text, "helloAB");
	const more = store.applyEdit(record.id, [edit(0, 0, "X"), edit(0, 0, "Y"), edit(0, 0, "Z")]);
	assert.ok(more.ok);
	assert.equal(doc(store, record.id).text, "XYZhelloAB");

	// An insertion at the end of a replaced range is unambiguous and valid.
	const adjacent = store.applyEdit(record.id, [edit(0, 3, "Q"), edit(3, 3, "R")]);
	assert.ok(adjacent.ok);
	assert.equal(doc(store, record.id).text, "QRhelloAB");

	// An insertion exactly at the start of a replacement is ambiguous -> refused,
	// regardless of the order it appears in the transaction.
	expectRefusal(store.applyEdit(record.id, [edit(2, 2, "Y"), edit(2, 5, "X")]), "invalid-edit");
	expectRefusal(store.applyEdit(record.id, [edit(2, 5, "X"), edit(2, 2, "Y")]), "invalid-edit");
	// Adjacent (non-touching) ranges are valid.
	const adjacent2 = store.applyEdit(record.id, [edit(0, 1, "S"), edit(1, 2, "T")]);
	assert.ok(adjacent2.ok);
	assert.equal(doc(store, record.id).text, "SThelloAB");
});

test("control characters round-trip unchanged through edits, undo and serialization", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/raw.txt", "", { revision: "rev-raw" });
	const raw = "\x00\x01\x1b[31mred\x1b[0m\x07\tend\r\nnext\x9b";
	const inserted = store.applyEdit(record.id, [edit(0, 0, raw)]);
	assert.ok(inserted.ok);
	assert.equal(doc(store, record.id).text, raw);
	assert.equal(doc(store, record.id).text.includes("\x1b[31m"), true);
	const serialized = store.serializeForSave(record.id);
	assert.ok(serialized.ok);
	assert.equal(serialized.text, raw);
	assert.equal(hashText(serialized.text), hashText(raw));

	const undone = store.undo(record.id);
	assert.ok(undone.ok);
	assert.equal(doc(store, record.id).text, "");
	const redone = store.redo(record.id);
	assert.ok(redone.ok);
	assert.equal(doc(store, record.id).text, raw);

	// A single control character is a single-character insert for grouping.
	const single = store.insertText(record.id, "\x1b");
	assert.ok(single.ok);
	assert.equal(doc(store, record.id).text, raw + "\x1b");
	const undoRun = store.undo(record.id);
	assert.ok(undoRun.ok);
	assert.equal(doc(store, record.id).text, raw);
});

test("insert/backspace/delete/newline helpers respect graphemes and dominant line endings", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/emoji.txt", "", { revision: "rev-1" });
	assert.ok(store.insertText(record.id, "a").ok);
	assert.ok(store.insertText(record.id, "👍").ok);
	assert.ok(store.insertText(record.id, "e\u0301").ok);
	assert.equal(doc(store, record.id).text, "a👍e\u0301");
	let view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 5);

	// Backspace deletes whole graphemes.
	assert.ok(store.backspace(record.id).ok);
	assert.equal(doc(store, record.id).text, "a👍");
	view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 3);
	assert.ok(store.backspace(record.id).ok);
	assert.equal(doc(store, record.id).text, "a");
	view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 1);
	const versionBefore = doc(store, record.id).version;
	assert.ok(store.deleteForward(record.id).ok); // nothing after the cursor
	assert.equal(doc(store, record.id).version, versionBefore);

	// Forward delete stops after a grapheme.
	assert.ok(store.insertText(record.id, "👍x").ok);
	store.setSelection(record.id, { cursorOffset: 1 });
	const beforeDelete = doc(store, record.id).version;
	assert.ok(store.deleteForward(record.id).ok); // deletes 👍 only
	assert.equal(doc(store, record.id).text, "ax");
	assert.equal(doc(store, record.id).version, beforeDelete + 1);
	assert.ok(store.deleteForward(record.id).ok);
	assert.equal(doc(store, record.id).text, "a");

	// Newline insertion uses the document's dominant ending.
	const lf = open(store, "file:///w/lf.txt", "a\n");
	store.setSelection(lf.id, { cursorOffset: 0 });
	assert.ok(store.newline(lf.id).ok);
	assert.equal(doc(store, lf.id).text, "\na\n");
	assert.equal(doc(store, lf.id).lineEnding, "LF");

	const crlf = open(store, "file:///w/crlf.txt", "x\r\ny");
	store.setSelection(crlf.id, { cursorOffset: 0 });
	assert.ok(store.newline(crlf.id).ok);
	assert.equal(doc(store, crlf.id).text, "\r\nx\r\ny");
	assert.equal(doc(store, crlf.id).lineEnding, "CRLF");

	const cr = open(store, "file:///w/cr.txt", "x\ry");
	store.setSelection(cr.id, { cursorOffset: 0 });
	assert.ok(store.newline(cr.id).ok);
	assert.equal(doc(store, cr.id).text, "\rx\ry");
	assert.equal(doc(store, cr.id).lineEnding, "CR");

	const mixed = open(store, "file:///w/mixed.txt", "a\r\nb\n");
	store.setSelection(mixed.id, { cursorOffset: 0 });
	assert.ok(store.newline(mixed.id).ok);
	assert.equal(doc(store, mixed.id).text, "\r\na\r\nb\n");
	assert.equal(doc(store, mixed.id).lineEnding, "mixed");

	const empty = open(store, "file:///w/empty.txt", "", { revision: null });
	assert.ok(store.newline(empty.id).ok);
	assert.equal(doc(store, empty.id).text, "\n");
	assert.equal(doc(store, empty.id).lineEnding, "LF");

	// Empty inserts are no-ops: no transaction, no version bump.
	const noopDoc = open(store, "file:///w/noop.txt", "stable");
	const noopVersion = doc(store, noopDoc.id).version;
	const noop = store.insertText(noopDoc.id, "");
	assert.ok(noop.ok);
	assert.equal(doc(store, noopDoc.id).version, noopVersion);
	assert.equal(store.undoDepth(noopDoc.id), 0);

	// An edit that introduces a new ending kind flips the detected metadata.
	assert.ok(store.applyEdit(lf.id, [edit(1, 1, "\r\n")]).ok);
	assert.equal(doc(store, lf.id).lineEnding, "mixed");
});

test("typing runs coalesce into one undo group; backspace runs coalesce", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "", { revision: "rev-1" });
	for (const ch of ["h", "i", "!"]) {
		const r = store.insertText(record.id, ch);
		assert.ok(r.ok);
	}
	assert.equal(doc(store, record.id).text, "hi!");
	assert.equal(store.undoDepth(record.id), 1);
	assert.equal(store.undoBytesTotal(), 0); // the run snapshot is the empty string
	const undoRun = store.undo(record.id);
	assert.ok(undoRun.ok);
	assert.equal(doc(store, record.id).text, "");
	const redoRun = store.redo(record.id);
	assert.ok(redoRun.ok);
	assert.equal(doc(store, record.id).text, "hi!");
	assert.equal(store.undoDepth(record.id), 1);

	// Backspace run: one undo restores the whole deleted run.
	const word = open(store, "file:///w/word.txt", "hello", { revision: "rev-1" });
	store.setSelection(word.id, { cursorOffset: 5 });
	assert.ok(store.backspace(word.id).ok);
	assert.ok(store.backspace(word.id).ok);
	assert.ok(store.backspace(word.id).ok);
	assert.equal(doc(store, word.id).text, "he");
	assert.equal(store.undoDepth(word.id), 1);
	const undoBackspaces = store.undo(word.id);
	assert.ok(undoBackspaces.ok);
	assert.equal(doc(store, word.id).text, "hello");
	const backspaceView = store.getViewState(word.id);
	assert.ok(backspaceView);
	assert.equal(backspaceView.cursorOffset, 5);
	assert.equal(store.undoDepth(word.id), 0);

	// A mixed sequence (insert then backspace) forms two groups.
	const mixed = open(store, "file:///w/mix.txt", "", { revision: "rev-1" });
	assert.ok(store.insertText(mixed.id, "a").ok);
	assert.ok(store.backspace(mixed.id).ok);
	assert.equal(store.undoDepth(mixed.id), 2); // insert run + backspace run
	assert.equal(doc(store, mixed.id).text, "");
});

test("atomic transactions break typing runs and form their own group", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "a", { revision: "rev-1" });
	store.setSelection(record.id, { cursorOffset: 1 });
	assert.ok(store.applyEdit(record.id, [edit(1, 1, "XY")]).ok); // atomic
	assert.ok(store.insertText(record.id, "b").ok);
	assert.equal(doc(store, record.id).text, "aXYb");
	assert.equal(store.undoDepth(record.id), 2);

	// A multi-edit transaction never coalesces, even when claimed as typing.
	const multi = open(store, "file:///w/multi.txt", "ab", { revision: "rev-1" });
	const claimed = store.applyEdit(multi.id, [edit(0, 0, "x"), edit(1, 1, "y")], { coalesce: "typing-insert" });
	assert.ok(claimed.ok);
	assert.equal(doc(store, multi.id).text, "xayb");
	assert.equal(store.undoDepth(multi.id), 1);

	// Cursor movement breaks the run: typing at a distance starts a new group.
	const moved = open(store, "file:///w/moved.txt", "", { revision: "rev-1" });
	assert.ok(store.insertText(moved.id, "a").ok);
	store.setSelection(moved.id, { cursorOffset: 0 });
	assert.ok(store.insertText(moved.id, "b").ok);
	assert.equal(doc(store, moved.id).text, "ba");
	assert.equal(store.undoDepth(moved.id), 2);
	const undoMoved = store.undo(moved.id);
	assert.ok(undoMoved.ok);
	assert.equal(doc(store, moved.id).text, "a");
});

test("undo/redo restore exact text and selection and increment version", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "hello", { revision: "rev-1" });
	store.setSelection(record.id, { cursorOffset: 5 });
	const typed = store.insertText(record.id, "!");
	assert.ok(typed.ok);
	assert.equal(doc(store, record.id).version, 1);
	let view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 6);

	const undone = store.undo(record.id);
	assert.ok(undone.ok);
	assert.equal(doc(store, record.id).text, "hello");
	assert.equal(doc(store, record.id).version, 2);
	view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 5);

	const redone = store.redo(record.id);
	assert.ok(redone.ok);
	assert.equal(doc(store, record.id).text, "hello!");
	assert.equal(doc(store, record.id).version, 3);
	view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 6);

	// Blocked when there is nothing to do.
	expectRefusal(store.redo(record.id), "no-redo");
	const fresh = open(store, "file:///w/fresh.txt", "clean");
	expectRefusal(store.undo(fresh.id), "no-undo");
	expectRefusal(store.redo(fresh.id), "no-redo");
	expectRefusal(store.undo("missing-doc"), "not-found");
});

test("redo is cleared by a new edit", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "", { revision: "rev-1" });
	assert.ok(store.insertText(record.id, "a").ok);
	assert.ok(store.undo(record.id).ok);
	assert.equal(store.undoDepth(record.id), 0);
	assert.ok(store.insertText(record.id, "b").ok);
	expectRefusal(store.redo(record.id), "no-redo");
	assert.equal(doc(store, record.id).text, "b");
});

test("undo budget trims oldest whole groups first and discloses trimming", () => {
	const store = new EditorStore({ undoBudgetBytes: 10 });
	const record = open(store, "file:///w/a.txt", "", { revision: "rev-1" });
	let offset = 0;
	for (const chunk of ["abc", "def", "ghi", "jkl"]) {
		assert.ok(store.applyEdit(record.id, [edit(offset, offset, chunk)]).ok);
		offset += chunk.length;
	}
	assert.equal(doc(store, record.id).text, "abcdefghijkl");
	// Snapshots: "" (0), "abc" (3), "abcdef" (6), "abcdefghi" (9) = 18 units.
	// Oldest first: 0, 3, 6 are trimmed; the newest stays.
	assert.equal(store.trimmedUndo(record.id), true);
	assert.equal(store.undoDepth(record.id), 1);
	assert.equal(store.undoBytesTotal(), 9);
	assert.ok(store.undoBytesTotal() <= 10);
	const undone = store.undo(record.id);
	assert.ok(undone.ok);
	assert.equal(doc(store, record.id).text, "abcdefghi");
	expectRefusal(store.undo(record.id), "no-undo");
	// Undo itself mutated the history through the same budget: the redo
	// snapshot exceeded the budget and was trimmed, which is disclosed truthfully.
	assert.ok(store.undoBytesTotal() <= 10);
	expectRefusal(store.redo(record.id), "no-redo");

	// Coalesced typing counts once against the budget: a 200-character run
	// stores a single empty-string snapshot.
	const typing = open(store, "file:///w/typing.txt", "", { revision: "rev-1" });
	for (let i = 0; i < 50; i++) assert.ok(store.insertText(typing.id, "x").ok);
	assert.equal(store.undoDepth(typing.id), 1);
	assert.ok(store.undoBytesTotal() <= 10);

	// Global trimming picks the globally oldest group across documents.
	const cross = new EditorStore({ undoBudgetBytes: 12 });
	const first = open(cross, "file:///w/first.txt", "", { revision: "rev-1" });
	const second = open(cross, "file:///w/second.txt", "", { revision: "rev-1" });
	assert.ok(cross.applyEdit(first.id, [edit(0, 0, "a".repeat(10))]).ok); // snapshot ""
	assert.ok(cross.applyEdit(first.id, [edit(10, 10, "b".repeat(10))]).ok); // snapshot 10 -> 10 total
	assert.ok(cross.applyEdit(second.id, [edit(0, 0, "c".repeat(10))]).ok); // snapshot "" -> 10 total
	assert.ok(cross.applyEdit(second.id, [edit(10, 10, "d".repeat(10))]).ok); // snapshot 10 -> 20 > 12
	// Oldest first: first's "" and first's 10-unit snapshot go; second keeps both.
	assert.equal(cross.trimmedUndo(first.id), true);
	assert.equal(cross.trimmedUndo(second.id), false);
	assert.equal(cross.undoDepth(first.id), 0);
	assert.equal(cross.undoDepth(second.id), 2);
	assert.equal(cross.undoBytesTotal(), 10);
	assertDirtyInvariant(cross, first.id);
});

test("acknowledgeSave clears dirty only for the matching version and hash", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "hello", { revision: "rev-1" });
	store.setSelection(record.id, { cursorOffset: 5 });
	assert.ok(store.insertText(record.id, "!").ok);
	assert.equal(doc(store, record.id).dirty, true);
	const begun = store.beginSave(record.id);
	assert.ok(begun.ok);
	assert.equal(begun.token.documentId, record.id);
	assert.equal(begun.token.version, 1);
	assert.equal(begun.token.text, "hello!");
	assert.equal(begun.token.savedRevision, "rev-1");

	const acked = store.acknowledgeSave(record.id, begun.token, "rev-2");
	assert.ok(acked.ok);
	assert.equal(acked.cleared, true);
	assert.equal(acked.ignored, false);
	assert.equal(doc(store, record.id).dirty, false);
	assert.equal(doc(store, record.id).savedRevision, "rev-2");
	assert.equal(doc(store, record.id).savedTextHash, hashText("hello!"));
	assertDirtyInvariant(store, record.id);

	// A token whose captured version no longer matches must not clear dirty.
	assert.ok(store.insertText(record.id, "?").ok);
	const stale = store.beginSave(record.id);
	assert.ok(stale.ok);
	assert.ok(store.insertText(record.id, "#").ok);
	const ackStale = store.acknowledgeSave(record.id, stale.token, "rev-3");
	assert.ok(ackStale.ok);
	assert.equal(ackStale.cleared, false);
	assert.equal(doc(store, record.id).dirty, true);

	expectRefusal(store.acknowledgeSave("missing", stale.token, "rev-4"), "not-found");
});

test("edits typed while a save is in flight keep the document dirty", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "", { revision: "rev-1" });
	assert.ok(store.insertText(record.id, "a").ok);
	const token = store.beginSave(record.id);
	assert.ok(token.ok);
	// The user keeps typing after the save began.
	assert.ok(store.insertText(record.id, "b").ok);
	assert.equal(doc(store, record.id).version, 2);
	// The older save acknowledges: the disk now holds "a" at rev-2, but our
	// buffer is "ab", so the document must stay dirty.
	const acked = store.acknowledgeSave(record.id, token.token, "rev-2");
	assert.ok(acked.ok);
	assert.equal(acked.cleared, false);
	assert.equal(doc(store, record.id).dirty, true);
	assert.equal(doc(store, record.id).text, "ab");
	assert.equal(doc(store, record.id).savedRevision, "rev-2");
	assert.equal(doc(store, record.id).savedTextHash, hashText("a"));
	assertDirtyInvariant(store, record.id);

	// The follow-up save captures the newer text and clears cleanly.
	const second = store.beginSave(record.id);
	assert.ok(second.ok);
	assert.equal(second.token.version, 2);
	assert.equal(second.token.text, "ab");
	const ackedSecond = store.acknowledgeSave(record.id, second.token, "rev-3");
	assert.ok(ackedSecond.ok);
	assert.equal(ackedSecond.cleared, true);
	assert.equal(doc(store, record.id).dirty, false);
	assert.equal(doc(store, record.id).savedRevision, "rev-3");
});

test("stale save acknowledgements are ignored", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "", { revision: "rev-1" });
	assert.ok(store.insertText(record.id, "a").ok);
	const first = store.beginSave(record.id);
	assert.ok(first.ok);
	assert.ok(store.insertText(record.id, "b").ok);
	const second = store.beginSave(record.id);
	assert.ok(second.ok);
	const ackedSecond = store.acknowledgeSave(record.id, second.token, "rev-2");
	assert.ok(ackedSecond.ok);
	assert.equal(ackedSecond.cleared, true);

	// An out-of-order acknowledgement of the older token must not regress the
	// saved identity or un-clean the newer state.
	const lateFirst = store.acknowledgeSave(record.id, first.token, "rev-1");
	assert.ok(lateFirst.ok);
	assert.equal(lateFirst.cleared, false);
	assert.equal(lateFirst.ignored, true);
	assert.equal(doc(store, record.id).savedRevision, "rev-2");
	assert.equal(doc(store, record.id).dirty, false);
	assert.equal(doc(store, record.id).text, "ab");
});

test("beginSave refuses read-only documents and captures the current state", () => {
	const store = new EditorStore();
	const readOnly = open(store, "file:///w/big.txt", "large", { readOnly: true });
	expectRefusal(store.beginSave(readOnly.id), "read-only");
	expectRefusal(store.beginSave("missing-doc"), "not-found");
	const openDoc = open(store, "file:///w/a.txt", "x", { revision: "rev-7" });
	const token = store.beginSave(openDoc.id);
	assert.ok(token.ok);
	assert.equal(token.token.text, "x");
	assert.equal(token.token.version, 0);
	assert.equal(token.token.savedRevision, "rev-7");
});

test("serializeForSave is byte-identical and never normalizes whitespace or final newline", () => {
	const store = new EditorStore();
	const text = "a\r\nb\n  \r\nlast line "; // mixed endings, trailing spaces, no final newline
	const record = open(store, "file:///w/a.txt", text, { revision: "rev-1" });
	const serialized = store.serializeForSave(record.id);
	assert.ok(serialized.ok);
	assert.equal(serialized.text, text);
	assert.equal(serialized.text.endsWith(" "), true);
	assert.equal(serialized.text.endsWith("\n"), false);

	// A begin/acknowledge save cycle with no edits changes nothing.
	const token = store.beginSave(record.id);
	assert.ok(token.ok);
	const acked = store.acknowledgeSave(record.id, token.token, "rev-2");
	assert.ok(acked.ok);
	const afterSave = store.serializeForSave(record.id);
	assert.ok(afterSave.ok);
	assert.equal(afterSave.text, text);
	assert.equal(doc(store, record.id).lineEnding, "mixed");
	expectRefusal(store.serializeForSave("missing-doc"), "not-found");
});

test("clean buffers reload from disk; dirty buffers conflict instead of losing edits", () => {
	const store = new EditorStore();
	const clean = open(store, "file:///w/clean.txt", "old", { revision: "rev-1" });
	const reloaded = store.reloadFromDisk(clean.id, "new\r\n", "rev-2");
	assert.ok(reloaded.ok);
	assert.equal(doc(store, clean.id).text, "new\r\n");
	assert.equal(doc(store, clean.id).version, 1);
	assert.equal(doc(store, clean.id).dirty, false);
	assert.equal(doc(store, clean.id).savedRevision, "rev-2");
	assert.equal(doc(store, clean.id).savedTextHash, hashText("new\r\n"));
	assert.equal(doc(store, clean.id).lineEnding, "CRLF");
	assert.equal(doc(store, clean.id).conflict, undefined);
	expectRefusal(store.undo(clean.id), "no-undo"); // history resets on reload
	assert.equal(store.totalTextBytes(), new TextEncoder().encode("new\r\n").length);

	// setDiskState is the same explicit disk-state API.
	const alias = open(store, "file:///w/alias.txt", "old", { revision: "rev-1" });
	const aliased = store.setDiskState(alias.id, "from disk", "rev-9");
	assert.ok(aliased.ok);
	assert.equal(doc(store, alias.id).text, "from disk");
	assert.equal(doc(store, alias.id).savedRevision, "rev-9");

	// Dirty buffers must not auto-reload: structured conflict, buffer intact.
	const dirty = open(store, "file:///w/dirty.txt", "mine", { revision: "rev-1" });
	assert.ok(store.insertText(dirty.id, "!").ok);
	const refused = store.reloadFromDisk(dirty.id, "theirs", "rev-5");
	expectRefusal(refused, "conflict");
	const conflictError = (refused as { error: { conflict?: { diskRevision: string; reason: string } } }).error;
	assert.equal(conflictError.conflict?.diskRevision, "rev-5");
	assert.equal(doc(store, dirty.id).text, "!mine");
	assert.equal(doc(store, dirty.id).dirty, true);
	assert.equal(doc(store, dirty.id).savedRevision, "rev-1");
	assert.equal(doc(store, dirty.id).conflict?.diskRevision, "rev-5");
	expectRefusal(store.reloadFromDisk("missing-doc", "x", "rev-1"), "not-found");
});

test("noteExternalChange marks clean documents for reload and conflicts with dirty ones", () => {
	const store = new EditorStore();
	const clean = open(store, "file:///w/clean.txt", "content", { revision: "rev-1" });
	const unchanged = store.noteExternalChange(clean.id, "rev-1");
	assert.ok(unchanged.ok);
	assert.equal(unchanged.outcome, "unchanged");
	assert.equal(store.hasPendingReload(clean.id), false);

	const pending = store.noteExternalChange(clean.id, "rev-2");
	assert.ok(pending.ok);
	assert.equal(pending.outcome, "reload-pending");
	assert.equal(store.hasPendingReload(clean.id), true);
	assert.equal(store.pendingReloadRevision(clean.id), "rev-2");
	assert.equal(doc(store, clean.id).text, "content");
	assert.equal(doc(store, clean.id).version, 0);
	assert.equal(doc(store, clean.id).conflict, undefined);

	const reloaded = store.reloadFromDisk(clean.id, "content v2", "rev-2");
	assert.ok(reloaded.ok);
	assert.equal(store.hasPendingReload(clean.id), false);
	assert.equal(store.pendingReloadRevision(clean.id), undefined);

	const conflict = open(store, "file:///w/dirty.txt", "mine", { revision: "rev-1" });
	assert.ok(store.insertText(conflict.id, "!").ok);
	const conflicted = store.noteExternalChange(conflict.id, "rev-6");
	assert.ok(conflicted.ok);
	assert.equal(conflicted.outcome, "conflict");
	assert.equal(doc(store, conflict.id).conflict?.diskRevision, "rev-6");
	assert.ok((doc(store, conflict.id).conflict?.reason.length ?? 0) > 0);
	assert.equal(doc(store, conflict.id).text, "!mine");
	assert.equal(store.hasPendingReload(conflict.id), false);
	expectRefusal(store.noteExternalChange("missing-doc", "rev-1"), "not-found");
});

test("conflict resolution helpers keep or reload explicitly", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "mine", { revision: "rev-1" });
	assert.ok(store.insertText(record.id, "!").ok);
	assert.ok(store.noteExternalChange(record.id, "rev-9").ok);
	assert.equal(doc(store, record.id).conflict?.diskRevision, "rev-9");

	// Keep: buffer and dirty flag survive; conflict is dismissed.
	const kept = store.resolveConflictKeep(record.id);
	assert.ok(kept.ok);
	assert.equal(doc(store, record.id).conflict, undefined);
	assert.equal(doc(store, record.id).text, "!mine");
	assert.equal(doc(store, record.id).dirty, true);
	expectRefusal(store.resolveConflictKeep(record.id), "no-conflict");
	expectRefusal(store.resolveConflictReload(record.id, "x", "rev-1"), "no-conflict");

	// Reload: adopt the disk text explicitly.
	assert.ok(store.noteExternalChange(record.id, "rev-10").ok);
	const reloadVersion = doc(store, record.id).version;
	const resolved = store.resolveConflictReload(record.id, "theirs", "rev-10");
	assert.ok(resolved.ok);
	assert.equal(doc(store, record.id).conflict, undefined);
	assert.equal(doc(store, record.id).text, "theirs");
	assert.equal(doc(store, record.id).dirty, false);
	assert.equal(doc(store, record.id).savedRevision, "rev-10");
	assert.equal(doc(store, record.id).version, reloadVersion + 1);
});

test("setSelection and setViewport clamp and persist view state", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "hello");
	const initial = store.getViewState(record.id);
	assert.ok(initial);
	assert.deepEqual(initial, {
		documentId: record.id,
		cursorOffset: 0,
		selectionAnchorOffset: undefined,
		scrollLine: 0,
		horizontalCellOffset: 0,
	});

	const selected = store.setSelection(record.id, { cursorOffset: 3, anchorOffset: 1 });
	assert.ok(selected.ok);
	assert.equal(selected.viewState.cursorOffset, 3);
	assert.equal(selected.viewState.selectionAnchorOffset, 1);
	const clamped = store.setSelection(record.id, { cursorOffset: -5, anchorOffset: 99 });
	assert.ok(clamped.ok);
	assert.equal(clamped.viewState.cursorOffset, 0);
	assert.equal(clamped.viewState.selectionAnchorOffset, 5);
	const collapsed = store.setSelection(record.id, { cursorOffset: 2 });
	assert.ok(collapsed.ok);
	assert.equal(collapsed.viewState.selectionAnchorOffset, undefined);

	const viewport = store.setViewport(record.id, { scrollLine: 10.9, horizontalCellOffset: -3, preferredColumn: 7 });
	assert.ok(viewport.ok);
	assert.equal(viewport.viewState.scrollLine, 10);
	assert.equal(viewport.viewState.horizontalCellOffset, 0);
	assert.equal(viewport.viewState.preferredColumn, 7);
	const cleared = store.setViewport(record.id, { preferredColumn: null });
	assert.ok(cleared.ok);
	assert.equal(cleared.viewState.preferredColumn, undefined);

	// Returned view states are copies: callers cannot corrupt the model.
	const copy = store.getViewState(record.id);
	assert.ok(copy);
	copy.cursorOffset = 999;
	assert.equal(store.getViewState(record.id)?.cursorOffset, 2);

	expectRefusal(store.setSelection("missing-doc", { cursorOffset: 0 }), "not-found");
	expectRefusal(store.setViewport("missing-doc", { scrollLine: 1 }), "not-found");
});

test("selection replacement is a normal edit and is undoable with its exact selection", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "hello world", { revision: "rev-1" });
	store.setSelection(record.id, { cursorOffset: 5, anchorOffset: 0 });
	const replaced = store.replaceSelection(record.id, "HI");
	assert.ok(replaced.ok);
	assert.equal(doc(store, record.id).text, "HI world");
	assert.equal(doc(store, record.id).version, 1);
	let view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 2);
	assert.equal(view.selectionAnchorOffset, undefined);

	const undone = store.undo(record.id);
	assert.ok(undone.ok);
	assert.equal(doc(store, record.id).text, "hello world");
	view = store.getViewState(record.id);
	assert.ok(view);
	assert.equal(view.cursorOffset, 5);
	assert.equal(view.selectionAnchorOffset, 0);

	// Replacing an empty selection behaves like inserting at the caret.
	store.setSelection(record.id, { cursorOffset: 2 });
	assert.ok(store.replaceSelection(record.id, "X").ok);
	assert.equal(doc(store, record.id).text, "heXllo world");
});

test("closeDocument frees budget and history", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.txt", "hello");
	assert.ok(store.insertText(record.id, "!").ok);
	assert.ok(store.undoBytesTotal() >= 0);
	assert.equal(store.closeDocument(record.id), true);
	assert.equal(store.getDocument(record.id), undefined);
	assert.equal(store.totalTextBytes(), 0);
	assert.equal(store.undoBytesTotal(), 0);
	assert.equal(store.undoDepth(record.id), 0);
	expectRefusal(store.applyEdit(record.id, [edit(0, 0, "x")]), "not-found");
	assert.equal(store.closeDocument(record.id), false);
});

test("budget constants match the plan's published limits", () => {
	assert.equal(DEFAULT_EDITABLE_FILE_BUDGET_BYTES, 1024 * 1024); // 1 MiB per file, caller applies readOnly
	assert.equal(DEFAULT_TOTAL_TEXT_BUDGET_BYTES, 16 * 1024 * 1024); // 16 MiB total open text
	assert.equal(DEFAULT_UNDO_BUDGET_BYTES, 32 * 1024 * 1024); // 32 MiB undo data
});
