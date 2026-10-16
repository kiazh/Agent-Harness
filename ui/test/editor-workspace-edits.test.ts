// T9-logic contract tests: safe workspace-edit staging (plan §18).
// Refusal paths first: document resolution, versions, ranges, overlap; then
// the all-or-nothing synchronous apply, preview bounds, summaries, the
// formatting wrapper and defensive code-action narrowing.
import { test } from "node:test";
import assert from "node:assert/strict";
import { EditorStore, type DocumentRecord } from "../src/editor/model.ts";
import {
	WORKSPACE_EDIT_HUNK_LIMIT,
	WORKSPACE_EDIT_SUMMARY_LIMIT,
	applyWorkspaceEdit,
	codeActionsFromPayload,
	formatEditsForDocument,
	previewWorkspaceEdit,
	validateWorkspaceEdit,
	type WorkspaceEditApplyResult,
	type WorkspaceEditValidation,
} from "../src/editor/workspaceEdits.ts";
import type { LspTextEdit, WorkspaceEditChange, WorkspaceEditPayload } from "../src/workspace/protocol.ts";

function open(
	store: EditorStore,
	uri: string,
	text: string,
	options: { readOnly?: boolean } = {},
): DocumentRecord {
	const result = store.openDocument({
		workspaceId: "ws1",
		uri,
		text,
		languageId: "typescript",
		revision: "rev-1",
		readOnly: options.readOnly ?? false,
	});
	assert.ok(result.ok, `openDocument(${uri}) should succeed`);
	return result.document;
}

function doc(store: EditorStore, documentId: string): DocumentRecord {
	const record = store.getDocument(documentId);
	assert.ok(record, `document ${documentId} should exist`);
	return record;
}

function edit(
	startLine: number,
	startCharacter: number,
	endLine: number,
	endCharacter: number,
	newText: string,
): LspTextEdit {
	return {
		range: {
			start: { line: startLine, character: startCharacter },
			end: { line: endLine, character: endCharacter },
		},
		newText,
	};
}

function change(uri: string, version: number | null, edits: LspTextEdit[]): WorkspaceEditChange {
	return { uri, version, edits };
}

function payload(
	label: string,
	changes: WorkspaceEditChange[],
	refused: Array<{ uri: string; reason: string }> = [],
): WorkspaceEditPayload {
	return { label, changes, refused };
}

function expectRefusals(
	result: WorkspaceEditValidation | WorkspaceEditApplyResult,
	expected: Array<[string, string]>,
): void {
	assert.equal(result.ok, false, "expected a refusal result");
	if (result.ok) return;
	assert.deepEqual(
		result.refusals.map((refusal) => [refusal.uri, refusal.reason]),
		expected,
	);
}

test("validate refuses each resolution, version, read-only and range problem with a structured reason", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "hello");
	const readOnly = open(store, "file:///w/ro.ts", "locked", { readOnly: true });
	const lines = open(store, "file:///w/lines.ts", "a\nb\nc");

	const missing = validateWorkspaceEdit(
		store,
		payload("Rename", [change("file:///w/gone.ts", null, [edit(0, 0, 0, 0, "x")])]),
	);
	expectRefusals(missing, [["file:///w/gone.ts", "missing-document"]]);

	const stale = validateWorkspaceEdit(store, payload("Rename", [change(record.uri, 4, [edit(0, 0, 0, 1, "H")])]));
	expectRefusals(stale, [[record.uri, "version-mismatch"]]);

	const locked = validateWorkspaceEdit(store, payload("Format", [change(readOnly.uri, null, [edit(0, 0, 0, 1, "L")])]));
	expectRefusals(locked, [[readOnly.uri, "read-only"]]);

	// A decoded span whose start is after its end is refused.
	const reversed = validateWorkspaceEdit(store, payload("X", [change(lines.uri, null, [edit(2, 0, 0, 0, "x")])]));
	expectRefusals(reversed, [[lines.uri, "invalid-range"]]);

	const malformedRange = validateWorkspaceEdit(
		store,
		payload("X", [change(record.uri, null, [{ range: null, newText: "x" } as unknown as LspTextEdit])]),
	);
	expectRefusals(malformedRange, [[record.uri, "invalid-range"]]);

	const badText = validateWorkspaceEdit(
		store,
		payload("X", [
			change(record.uri, null, [
				{
					range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
					newText: 7,
				} as unknown as LspTextEdit,
			]),
		]),
	);
	expectRefusals(badText, [[record.uri, "invalid-range"]]);

	const badPosition = validateWorkspaceEdit(
		store,
		payload("X", [
			change(record.uri, null, [
				{
					range: { start: { line: -1, character: 0 }, end: { line: 0, character: 0 } },
					newText: "x",
				} as unknown as LspTextEdit,
			]),
		]),
	);
	expectRefusals(badPosition, [[record.uri, "invalid-range"]]);

	// Refusals collect across files, one per file, and never mutate anything.
	const multi = validateWorkspaceEdit(
		store,
		payload("X", [
			change("file:///w/gone.ts", null, [edit(0, 0, 0, 0, "x")]),
			change(record.uri, 9, [edit(0, 0, 0, 1, "H")]),
			change(readOnly.uri, null, [edit(0, 0, 0, 1, "L")]),
		]),
	);
	expectRefusals(multi, [
		["file:///w/gone.ts", "missing-document"],
		[record.uri, "version-mismatch"],
		[readOnly.uri, "read-only"],
	]);
	assert.equal(doc(store, record.id).text, "hello");
	assert.equal(doc(store, readOnly.id).text, "locked");
	assert.equal(store.undoDepth(record.id), 0);
});

test("validation decodes every range against one captured snapshot", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "a\r\nb\nc");
	const proposal = payload("X", [change(record.uri, null, [edit(1, 0, 1, 1, "B")])]);

	const validation = validateWorkspaceEdit(store, proposal);
	assert.equal(validation.ok, true);
	if (!validation.ok) return;
	assert.equal(validation.perDocument.length, 1);
	assert.equal(validation.perDocument[0].documentId, record.id);
	assert.equal(validation.perDocument[0].uri, record.uri);
	assert.equal(validation.perDocument[0].snapshot, "a\r\nb\nc");
	assert.deepEqual(validation.perDocument[0].edits, [
		{ startOffset: 3, endOffset: 4, newText: "B", order: 0 },
	]);

	const applied = applyWorkspaceEdit(store, proposal);
	assert.equal(applied.ok, true);
	assert.equal(doc(store, record.id).text, "a\r\nB\nc");
});

test("version mismatch lifecycle: stale after an edit, accepted after a re-request", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "let x = 1;");
	const proposal = payload("Rename", [change(record.uri, 0, [edit(0, 0, 0, 3, "var")])]);
	assert.equal(validateWorkspaceEdit(store, proposal).ok, true);

	store.setSelection(record.id, { cursorOffset: 10 });
	assert.ok(store.insertText(record.id, "!").ok);
	assert.equal(doc(store, record.id).version, 1);
	expectRefusals(validateWorkspaceEdit(store, proposal), [[record.uri, "version-mismatch"]]);

	const reissued = payload("Rename", [change(record.uri, 1, [edit(0, 0, 0, 3, "var")])]);
	const applied = applyWorkspaceEdit(store, reissued);
	assert.equal(applied.ok, true);
	if (applied.ok) {
		assert.equal(applied.applied.length, 1);
		assert.equal(applied.applied[0].versionAfter, 2);
	}
	assert.equal(doc(store, record.id).text, "var x = 1;!");
});

test("unversioned proposals validate against the current snapshot", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "one");
	store.setSelection(record.id, { cursorOffset: 3 });
	assert.ok(store.insertText(record.id, " two").ok);
	assert.equal(doc(store, record.id).version, 1);

	const proposal = payload("X", [change(record.uri, null, [edit(0, 0, 0, 3, "1")])]);
	assert.equal(validateWorkspaceEdit(store, proposal).ok, true);
	const applied = applyWorkspaceEdit(store, proposal);
	assert.equal(applied.ok, true);
	assert.equal(doc(store, record.id).text, "1 two");
	assert.equal(doc(store, record.id).version, 2);
});

test("overlap matrix mirrors the model: replaces, inserts and boundary cases", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "abcdef");
	const accepts = (...edits: LspTextEdit[]): boolean =>
		validateWorkspaceEdit(store, payload("X", [change(record.uri, null, edits)])).ok;

	// Two replacements sharing any span conflict.
	assert.equal(accepts(edit(0, 0, 0, 3, "A"), edit(0, 2, 0, 5, "B")), false);
	assert.equal(accepts(edit(0, 0, 0, 2, "A"), edit(0, 0, 0, 2, "B")), false);
	// An insert strictly inside a replaced span conflicts.
	assert.equal(accepts(edit(0, 0, 0, 3, "A"), edit(0, 2, 0, 2, "X")), false);
	assert.equal(accepts(edit(0, 2, 0, 2, "X"), edit(0, 0, 0, 3, "A")), false);
	// An insert exactly at a replaced range's start is ambiguous -> refused.
	assert.equal(accepts(edit(0, 2, 0, 2, "X"), edit(0, 2, 0, 5, "R")), false);
	assert.equal(accepts(edit(0, 2, 0, 5, "R"), edit(0, 2, 0, 2, "X")), false);
	// An insert exactly at its end, or outside it, is unambiguous.
	assert.equal(accepts(edit(0, 0, 0, 3, "A"), edit(0, 3, 0, 3, "X")), true);
	assert.equal(accepts(edit(0, 0, 0, 3, "A"), edit(0, 5, 0, 5, "X")), true);
	// Adjacent replacements do not touch.
	assert.equal(accepts(edit(0, 0, 0, 1, "A"), edit(0, 1, 0, 2, "B")), true);
	// Same-position zero-width inserts never conflict, at any count.
	assert.equal(accepts(edit(0, 4, 0, 4, "X"), edit(0, 4, 0, 4, "Y")), true);
	assert.equal(accepts(edit(0, 4, 0, 4, "X"), edit(0, 4, 0, 4, "Y"), edit(0, 4, 0, 4, "Z")), true);
	assert.equal(accepts(edit(0, 2, 0, 2, "X"), edit(0, 4, 0, 4, "Y")), true);
	// The overlap refusal names its reason.
	expectRefusals(
		validateWorkspaceEdit(
			store,
			payload("X", [change(record.uri, null, [edit(0, 0, 0, 2, "A"), edit(0, 1, 0, 3, "B")])]),
		),
		[[record.uri, "overlapping-edits"]],
	);
});

test("apply preserves payload array order for same-position inserts", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "abc");
	const applied = applyWorkspaceEdit(
		store,
		payload("Code Action", [
			change(record.uri, null, [edit(0, 3, 0, 3, "1"), edit(0, 3, 0, 3, "2"), edit(0, 3, 0, 3, "3")]),
		]),
	);
	assert.equal(applied.ok, true);
	// A naive reverse-offset apply would produce "abc321".
	assert.equal(doc(store, record.id).text, "abc123");

	const middle = open(store, "file:///w/b.ts", "hello");
	assert.equal(
		applyWorkspaceEdit(
			store,
			payload("X", [change(middle.uri, null, [edit(0, 2, 0, 2, "X"), edit(0, 2, 0, 2, "Y")])]),
		).ok,
		true,
	);
	assert.equal(doc(store, middle.id).text, "heXYllo");

	// Replacement followed by inserts exactly at its end: the descending-apply
	// semantics (ties in reverse-array order) equals the ascending composition.
	const mixed = open(store, "file:///w/c.ts", "hello");
	assert.equal(
		applyWorkspaceEdit(
			store,
			payload("X", [
				change(mixed.uri, null, [edit(0, 0, 0, 5, "Q"), edit(0, 5, 0, 5, "A"), edit(0, 5, 0, 5, "B")]),
			]),
		).ok,
		true,
	);
	assert.equal(doc(store, mixed.id).text, "QAB");

	// Ranges decode through UTF-16 positions, CRLF included.
	const crlf = open(store, "file:///w/crlf.ts", "a\r\nb\nc");
	assert.equal(
		applyWorkspaceEdit(store, payload("X", [change(crlf.uri, null, [edit(1, 0, 1, 1, "B")])])).ok,
		true,
	);
	assert.equal(doc(store, crlf.id).text, "a\r\nB\nc");
});

test("apply stages each document as exactly one transaction and marks it dirty", () => {
	const store = new EditorStore();
	const a = open(store, "file:///w/a.ts", "let a = 1;");
	const b = open(store, "file:///w/b.ts", "let b = 2;");
	const result = applyWorkspaceEdit(
		store,
		payload("Rename let", [
			change(a.uri, 0, [edit(0, 0, 0, 3, "const")]),
			change(b.uri, 0, [edit(0, 0, 0, 3, "const")]),
		]),
	);
	assert.equal(result.ok, true);
	if (!result.ok) return;
	assert.equal(result.applied.length, 2);
	assert.deepEqual(result.applied[0], {
		documentId: a.id,
		uri: a.uri,
		versionAfter: 1,
		transactionId: `${a.id}#v0`,
	});
	assert.equal(result.applied[1].documentId, b.id);
	assert.equal(result.applied[1].uri, b.uri);
	assert.equal(result.applied[1].versionAfter, 1);
	assert.equal(result.applied[1].transactionId, `${b.id}#v0`);
	assert.notEqual(result.applied[0].transactionId, result.applied[1].transactionId);
	assert.equal(doc(store, a.id).text, "const a = 1;");
	assert.equal(doc(store, b.id).text, "const b = 2;");
	// One version increment and one undo group per document: one transaction.
	assert.equal(doc(store, a.id).version, 1);
	assert.equal(doc(store, b.id).version, 1);
	assert.equal(doc(store, a.id).dirty, true);
	assert.equal(doc(store, b.id).dirty, true);
	assert.equal(store.undoDepth(a.id), 1);
	assert.equal(store.undoDepth(b.id), 1);
	const undone = store.undo(a.id);
	assert.equal(undone.ok, true);
	assert.equal(doc(store, a.id).text, "let a = 1;");
	assert.equal(doc(store, a.id).version, 2);
});

test("apply is all-or-nothing: one refused file leaves every other file untouched", () => {
	const store = new EditorStore();
	const first = open(store, "file:///w/first.ts", "one");
	const second = open(store, "file:///w/second.ts", "two");
	const readOnly = open(store, "file:///w/ro.ts", "locked", { readOnly: true });

	const stale = applyWorkspaceEdit(
		store,
		payload("X", [
			change(first.uri, null, [edit(0, 0, 0, 3, "first!")]),
			change(second.uri, 99, [edit(0, 0, 0, 3, "second!")]),
		]),
	);
	expectRefusals(stale, [[second.uri, "version-mismatch"]]);
	assert.equal(doc(store, first.id).text, "one");
	assert.equal(doc(store, first.id).version, 0);
	assert.equal(doc(store, first.id).dirty, false);
	assert.equal(store.undoDepth(first.id), 0);

	const missingFile = applyWorkspaceEdit(
		store,
		payload("X", [
			change(first.uri, null, [edit(0, 0, 0, 3, "first!")]),
			change("file:///w/gone.ts", null, [edit(0, 0, 0, 0, "x")]),
		]),
	);
	expectRefusals(missingFile, [["file:///w/gone.ts", "missing-document"]]);

	const locked = applyWorkspaceEdit(
		store,
		payload("X", [
			change(first.uri, null, [edit(0, 0, 0, 3, "first!")]),
			change(readOnly.uri, null, [edit(0, 0, 0, 1, "L")]),
		]),
	);
	expectRefusals(locked, [[readOnly.uri, "read-only"]]);

	const overlapping = applyWorkspaceEdit(
		store,
		payload("X", [
			change(first.uri, null, [edit(0, 0, 0, 3, "first!"), edit(0, 1, 0, 3, "overlap")]),
			change(second.uri, null, [edit(0, 0, 0, 3, "second!")]),
		]),
	);
	expectRefusals(overlapping, [[first.uri, "overlapping-edits"]]);

	assert.equal(doc(store, first.id).text, "one");
	assert.equal(doc(store, second.id).text, "two");
	assert.equal(doc(store, readOnly.id).text, "locked");
	assert.equal(store.undoDepth(first.id), 0);
	assert.equal(store.undoDepth(second.id), 0);
});

test("apply re-validates at the last synchronous moment before mutation", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "hello");
	const proposal = payload("Format", [change(record.uri, 0, [edit(0, 0, 0, 5, "HELLO")])]);
	assert.equal(validateWorkspaceEdit(store, proposal).ok, true);
	// The buffer changes between the caller's validation and the apply call.
	assert.ok(store.insertText(record.id, "!").ok);
	assert.equal(doc(store, record.id).text, "!hello");
	const result = applyWorkspaceEdit(store, proposal);
	expectRefusals(result, [[record.uri, "version-mismatch"]]);
	assert.equal(doc(store, record.id).text, "!hello");
	assert.equal(doc(store, record.id).version, 1);
});

test("duplicate URI groups merge into one document transaction", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "abc");
	const merged = validateWorkspaceEdit(
		store,
		payload("X", [
			change(record.uri, 0, [edit(0, 0, 0, 0, "1")]),
			change(record.uri, 0, [edit(0, 3, 0, 3, "2")]),
		]),
	);
	assert.equal(merged.ok, true);
	if (merged.ok) {
		assert.equal(merged.perDocument.length, 1);
		assert.equal(merged.perDocument[0].edits.length, 2);
	}
	const applied = applyWorkspaceEdit(
		store,
		payload("X", [
			change(record.uri, null, [edit(0, 0, 0, 0, "1")]),
			change(record.uri, null, [edit(0, 3, 0, 3, "2")]),
		]),
	);
	assert.equal(applied.ok, true);
	assert.equal(doc(store, record.id).text, "1abc2");
	assert.equal(store.undoDepth(record.id), 1); // merged: one transaction

	// Same-position inserts split across groups keep payload array order.
	const second = open(store, "file:///w/b.ts", "x");
	assert.equal(
		applyWorkspaceEdit(
			store,
			payload("X", [
				change(second.uri, null, [edit(0, 1, 0, 1, "A")]),
				change(second.uri, null, [edit(0, 1, 0, 1, "B")]),
			]),
		).ok,
		true,
	);
	assert.equal(doc(store, second.id).text, "xAB");

	// Overlap across groups is still detected; disagreeing versions refuse.
	expectRefusals(
		validateWorkspaceEdit(
			store,
			payload("X", [
				change(second.uri, null, [edit(0, 0, 0, 1, "R")]),
				change(second.uri, null, [edit(0, 0, 0, 0, "I")]),
			]),
		),
		[[second.uri, "overlapping-edits"]],
	);
	expectRefusals(
		validateWorkspaceEdit(
			store,
			payload("X", [
				change(second.uri, 0, [edit(0, 0, 0, 0, "A")]),
				change(second.uri, 7, [edit(0, 0, 0, 0, "B")]),
			]),
		),
		[[second.uri, "version-mismatch"]],
	);
});

test("preview bounds hunks per file, passes refused through and never mutates", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "abcdefghij");
	const edits: LspTextEdit[] = [];
	for (let index = 0; index < 25; index++) {
		edits.push(edit(0, 10, 0, 10, `e${index}`));
	}
	const preview = previewWorkspaceEdit(
		store,
		payload("Big", [change(record.uri, null, edits)], [{ uri: "file:///w/stale.ts", reason: "stale" }]),
	);
	assert.equal(preview.fileCount, 1);
	assert.equal(preview.totalEdits, 25);
	assert.equal(preview.files.length, 1);
	const file = preview.files[0];
	assert.equal(file.uri, record.uri);
	assert.equal(file.documentId, record.id);
	assert.equal(file.editCount, 25);
	assert.equal(file.hunks.length, WORKSPACE_EDIT_HUNK_LIMIT);
	assert.equal(file.omittedHunks, 25 - WORKSPACE_EDIT_HUNK_LIMIT);
	assert.equal(file.hunks[0].summary, '+ "e0"');
	assert.deepEqual(file.problems, []);
	assert.deepEqual(preview.refused, [{ uri: "file:///w/stale.ts", reason: "stale" }]);
	// Preview is pure: no document state changed.
	assert.equal(doc(store, record.id).text, "abcdefghij");
	assert.equal(doc(store, record.id).version, 0);
	assert.equal(doc(store, record.id).dirty, false);
	assert.equal(store.undoDepth(record.id), 0);

	// Long slices stay bounded per summary.
	const long = open(store, "file:///w/long.ts", "x");
	const longPreview = previewWorkspaceEdit(
		store,
		payload("Long", [change(long.uri, null, [edit(0, 0, 0, 1, "y".repeat(400))])]),
	);
	const longSummary = longPreview.files[0].hunks[0].summary;
	assert.ok(longSummary.length <= WORKSPACE_EDIT_SUMMARY_LIMIT);
	assert.ok(longSummary.endsWith('…"'));

	// A missing file reports a problem, still lists hunks and has no id.
	const missingPreview = previewWorkspaceEdit(
		store,
		payload("Mixed", [
			change(record.uri, null, [edit(0, 0, 0, 0, "x")]),
			change("file:///w/gone.ts", null, [
				edit(0, 0, 0, 0, "y"),
				edit(1, 0, 1, 2, "z"),
				edit(4, 0, 4, 1, ""),
			]),
		]),
	);
	assert.equal(missingPreview.fileCount, 2);
	assert.equal(missingPreview.files[1].documentId, undefined);
	assert.ok(missingPreview.files[1].problems.some((problem) => problem.includes("missing")));
	assert.equal(missingPreview.files[1].hunks.length, 3);
	assert.equal(missingPreview.files[1].hunks[0].summary, '@0:0 + "y"');
	assert.equal(missingPreview.files[1].hunks[1].summary, '@1:0 ~ "z"');
	assert.equal(missingPreview.files[1].hunks[2].summary, "@4:0 (delete)");

	// Refused files surface the reason as a problem, not a hard failure.
	const readOnly = open(store, "file:///w/ro.ts", "locked", { readOnly: true });
	const lockedPreview = previewWorkspaceEdit(
		store,
		payload("X", [change(readOnly.uri, null, [edit(0, 0, 0, 1, "L")])]),
	);
	assert.ok(lockedPreview.files[0].problems.some((problem) => problem.includes("read-only")));
});

test("hunk summaries derive from before/after slices", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "hello world");
	const preview = previewWorkspaceEdit(
		store,
		payload("X", [
			change(record.uri, null, [
				edit(0, 0, 0, 5, "HI"), // replace "hello" with "HI"
				edit(0, 6, 0, 6, "!"), // insert
				edit(0, 10, 0, 11, ""), // delete "d"
				edit(0, 6, 0, 6, ""), // no-op
			]),
		]),
	);
	const hunks = preview.files[0].hunks;
	assert.equal(hunks.length, 4);
	assert.equal(hunks[0].summary, '- "hello" + "HI"');
	assert.equal(hunks[1].summary, '+ "!"');
	assert.equal(hunks[2].summary, '- "d"');
	assert.equal(hunks[3].summary, "(no-op)");

	// Line terminators collapse to a visible escape so a hunk stays one row.
	const lines = open(store, "file:///w/lines.ts", "a\nb");
	const linePreview = previewWorkspaceEdit(
		store,
		payload("X", [change(lines.uri, null, [edit(1, 0, 1, 1, "\n")])]),
	);
	assert.equal(linePreview.files[0].hunks[0].summary, '- "b" + "\\n"');
});

test("formatEditsForDocument wraps formatting edits with the captured version", () => {
	const store = new EditorStore();
	const record = open(store, "file:///w/a.ts", "let  x");
	const edits: LspTextEdit[] = [edit(0, 3, 0, 5, " ")];
	const proposal = formatEditsForDocument(store, record.uri, edits);
	assert.equal(proposal.label, "Format Document");
	assert.equal(proposal.refused.length, 0);
	assert.equal(proposal.changes.length, 1);
	assert.equal(proposal.changes[0].uri, record.uri);
	assert.equal(proposal.changes[0].version, 0);
	assert.deepEqual(proposal.changes[0].edits, edits);

	// The wrapper clones ranges: mutating the source edits cannot reach it.
	edits[0]!.range.start.line = 9;
	assert.equal(proposal.changes[0].edits[0].range.start.line, 0);

	const applied = applyWorkspaceEdit(store, proposal);
	assert.equal(applied.ok, true);
	assert.equal(doc(store, record.id).text, "let x");
	assert.equal(doc(store, record.id).version, 1);

	// A later request captures the current version.
	assert.ok(store.insertText(record.id, "z").ok);
	assert.equal(doc(store, record.id).version, 2);
	const reissued = formatEditsForDocument(store, record.uri, [edit(0, 0, 0, 1, "L")]);
	assert.equal(reissued.changes[0].version, 2);

	// A missing document is reported, not invented.
	const missing = formatEditsForDocument(store, "file:///w/gone.ts", [edit(0, 0, 0, 0, "x")]);
	assert.deepEqual(missing.changes, []);
	assert.deepEqual(missing.refused, [{ uri: "file:///w/gone.ts", reason: "missing-document" }]);
});

test("codeActionsFromPayload narrows untrusted results without throwing", () => {
	const garbage: unknown[] = [
		null,
		undefined,
		42,
		"actions",
		{},
		{ actions: null },
		{ actions: {} },
		{ actions: [null, 7, "x", {}] },
	];
	for (const value of garbage) {
		assert.deepEqual(codeActionsFromPayload(value), { actions: [] });
	}

	const validEdits = payload("Organize Imports", [change("file:///w/a.ts", 0, [edit(0, 0, 0, 1, "a")])]);
	const narrowed = codeActionsFromPayload({
		actions: [
			{ title: "Organize Imports", kind: "source.organizeImports", edits: validEdits },
			{ title: "Build", command: { title: "Build", command: "workbench.action.build", arguments: [1] } },
			{ title: "Nothing here" },
			{ title: 42, edits: validEdits },
			{ title: "Broken edits", edits: { nope: true } },
			{ title: "Broken command", command: { command: "x" } },
			{ title: "Bad arguments", command: { title: "T", command: "c", arguments: "nope" } },
			{ title: "Both", edits: validEdits, command: { title: "T", command: "c" } },
		],
	});
	assert.deepEqual(
		narrowed.actions.map((action) => action.title),
		["Organize Imports", "Build", "Both"],
	);
	assert.equal(narrowed.actions[0]!.edits, validEdits);
	assert.equal(narrowed.actions[0]!.kind, "source.organizeImports");
	assert.deepEqual(narrowed.actions[1]!.command, {
		title: "Build",
		command: "workbench.action.build",
		arguments: [1],
	});
	assert.equal(narrowed.actions[1]!.edits, undefined);
	assert.ok(narrowed.actions[2]!.edits);
	assert.ok(narrowed.actions[2]!.command);
	assert.equal(narrowed.actions.length, 3);
});

test("empty and zero-edit proposals are validated no-ops", () => {
	const store = new EditorStore();
	const empty: WorkspaceEditPayload = { label: "Nothing", changes: [], refused: [] };
	const validation = validateWorkspaceEdit(store, empty);
	assert.equal(validation.ok, true);
	if (validation.ok) assert.deepEqual(validation.perDocument, []);
	const applied = applyWorkspaceEdit(store, empty);
	assert.equal(applied.ok, true);
	if (applied.ok) assert.deepEqual(applied.applied, []);
	assert.deepEqual(previewWorkspaceEdit(store, empty), { files: [], refused: [], totalEdits: 0, fileCount: 0 });

	const record = open(store, "file:///w/a.ts", "unchanged");
	const zero = payload("No edits", [change(record.uri, 0, [])]);
	const zeroValidation = validateWorkspaceEdit(store, zero);
	assert.equal(zeroValidation.ok, true);
	if (zeroValidation.ok) {
		assert.equal(zeroValidation.perDocument.length, 1);
		assert.deepEqual(zeroValidation.perDocument[0].edits, []);
	}
	const zeroApplied = applyWorkspaceEdit(store, zero);
	assert.equal(zeroApplied.ok, true);
	if (zeroApplied.ok) {
		assert.equal(zeroApplied.applied.length, 1);
		assert.equal(zeroApplied.applied[0].versionAfter, 0);
		assert.equal(zeroApplied.applied[0].transactionId, undefined);
	}
	assert.equal(doc(store, record.id).version, 0);
	assert.equal(doc(store, record.id).dirty, false);
	assert.equal(store.undoDepth(record.id), 0);
});
