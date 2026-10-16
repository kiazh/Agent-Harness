// T4a contract tests: the tab model (plan §9, §24B).
// Preview/pinned/dirty rules, close-neighbor selection and overflow eviction.
import { test } from "node:test";
import assert from "node:assert/strict";
import { TabModel } from "../src/editor/tabs.ts";

function model(dirtyIds: string[] = []): TabModel {
	const dirty = new Set(dirtyIds);
	return new TabModel({ isDirty: (documentId: string) => dirty.has(documentId) });
}

test("opening tabs appends persistent tabs and focuses the newest", () => {
	const tabs = model();
	const first = tabs.open("a");
	assert.ok(first.ok);
	assert.equal(first.created, true);
	assert.deepEqual(first.tab, { documentId: "a", pinned: false, preview: false });
	assert.equal(first.activeDocumentId, "a");
	const second = tabs.open("b");
	assert.ok(second.ok);
	assert.equal(second.created, true);
	assert.equal(tabs.count(), 2);
	assert.equal(tabs.activeDocumentId(), "b");
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["a", "b"],
	);
});

test("reopening an existing document focuses its tab without duplicating", () => {
	const tabs = model();
	assert.ok(tabs.open("a").ok);
	assert.ok(tabs.open("b").ok);
	const again = tabs.open("a");
	assert.ok(again.ok);
	assert.equal(again.created, false);
	assert.equal(again.replacedDocumentId, undefined);
	assert.equal(again.convertedDocumentId, undefined);
	assert.equal(tabs.count(), 2);
	assert.equal(tabs.activeDocumentId(), "a");
	// Reopening does not silently promote an existing preview.
	assert.ok(tabs.open("p", { preview: true }).ok);
	const reopenPreview = tabs.open("p");
	assert.ok(reopenPreview.ok);
	assert.equal(reopenPreview.tab.preview, true);
});

test("a second clean preview replaces the previous preview in place", () => {
	const tabs = model();
	assert.ok(tabs.open("a").ok);
	const p1 = tabs.open("p1", { preview: true });
	assert.ok(p1.ok);
	assert.equal(p1.tab.preview, true);
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["a", "p1"],
	);

	const p2 = tabs.open("p2", { preview: true });
	assert.ok(p2.ok);
	assert.equal(p2.created, true);
	assert.equal(p2.replacedDocumentId, "p1");
	assert.equal(p2.convertedDocumentId, undefined);
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["a", "p2"], // replaced in the previous preview's slot
	);
	assert.equal(tabs.activeDocumentId(), "p2");
	assert.equal(tabs.has("p1"), false);

	// The replacement chain continues for subsequent previews.
	const p3 = tabs.open("p3", { preview: true });
	assert.ok(p3.ok);
	assert.equal(p3.replacedDocumentId, "p2");
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["a", "p3"],
	);
});

test("a dirty preview is preserved and promoted instead of replaced", () => {
	const dirty = model(["p1"]);
	assert.ok(dirty.open("a").ok);
	assert.ok(dirty.open("p1", { preview: true }).ok);

	const opened = dirty.open("p2", { preview: true });
	assert.ok(opened.ok);
	assert.equal(opened.replacedDocumentId, undefined);
	assert.equal(opened.convertedDocumentId, "p1");
	assert.deepEqual(
		dirty.list().map((tab) => `${tab.documentId}${tab.preview ? ":preview" : ""}`),
		["a", "p1", "p2:preview"],
	);
	assert.equal(dirty.has("p1"), true); // the dirty buffer was never discarded
	assert.equal(dirty.activeDocumentId(), "p2");
});

test("editing or pinning a preview converts it to persistent", () => {
	const tabs = model();
	assert.ok(tabs.open("p", { preview: true }).ok);
	const edited = tabs.noteEdited("p");
	assert.equal(edited.converted, true);
	assert.equal(tabs.list()[0]?.preview, false);
	assert.equal(tabs.noteEdited("p").converted, false); // already persistent
	assert.equal(tabs.noteEdited("not-open").converted, false); // lenient no-op

	assert.ok(tabs.open("q", { preview: true }).ok);
	const pinned = tabs.pin("q");
	assert.ok(pinned.ok);
	assert.equal(pinned.tab.pinned, true);
	assert.equal(pinned.tab.preview, false);
	const pinnedAgain = tabs.pin("q");
	assert.ok(pinnedAgain.ok);
	assert.equal(pinnedAgain.tab.pinned, true);
	const refusal = tabs.pin("not-open");
	assert.equal(refusal.ok, false);
	if (!refusal.ok) assert.equal(refusal.error.code, "not-open");

	// After promotion, a new preview no longer threatens the old tab.
	const next = tabs.open("r", { preview: true });
	assert.ok(next.ok);
	assert.equal(next.convertedDocumentId, undefined);
	assert.equal(next.replacedDocumentId, undefined);
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["p", "q", "r"],
	);
});

test("setActive focuses an open tab and refuses unknown ones", () => {
	const tabs = model();
	assert.ok(tabs.open("a").ok);
	assert.ok(tabs.open("b").ok);
	const focused = tabs.setActive("a");
	assert.ok(focused.ok);
	assert.equal(focused.activeDocumentId, "a");
	assert.equal(tabs.activeDocumentId(), "a");
	const refusal = tabs.setActive("missing");
	assert.equal(refusal.ok, false);
	if (!refusal.ok) assert.equal(refusal.error.code, "not-open");
	assert.equal(tabs.activeDocumentId(), "a");
});

test("closing a clean tab returns the next neighbor, else the previous", () => {
	const tabs = model();
	assert.ok(tabs.open("a").ok);
	assert.ok(tabs.open("b").ok);
	assert.ok(tabs.open("c").ok);
	tabs.setActive("b");
	const closedMiddle = tabs.close("b");
	assert.ok(closedMiddle.ok);
	assert.equal(closedMiddle.closed, "b");
	assert.equal(closedMiddle.activeDocumentId, "c"); // next sibling

	const closedLast = tabs.close("c");
	assert.ok(closedLast.ok);
	assert.equal(closedLast.activeDocumentId, "a"); // no next -> previous

	const closedOnly = tabs.close("a");
	assert.ok(closedOnly.ok);
	assert.equal(closedOnly.activeDocumentId, null);
	assert.equal(tabs.count(), 0);
});

test("closing a dirty tab needs a decision; cancel is a no-op and force discards", () => {
	const tabs = model(["dirty"]);
	assert.ok(tabs.open("dirty").ok);
	const before = tabs.list();
	const needs = tabs.close("dirty");
	assert.equal(needs.ok, false);
	if (!needs.ok && needs.needsDecision) {
		assert.equal(needs.documentId, "dirty");
		assert.deepEqual(needs.options, ["save", "discard", "cancel"]);
	} else {
		assert.fail("expected a needs-decision result");
	}
	// Cancel: nothing changed.
	assert.deepEqual(tabs.list(), before);
	assert.equal(tabs.has("dirty"), true);
	assert.equal(tabs.activeDocumentId(), "dirty");

	// Save path: once the document is clean, the ordinary close succeeds.
	const clean = model();
	assert.ok(clean.open("doc").ok);
	const saved = clean.close("doc");
	assert.ok(saved.ok);
	assert.equal(saved.closed, "doc");

	// Discard path: force removes it even while dirty.
	const force = tabs.close("dirty", { force: true });
	assert.ok(force.ok);
	assert.equal(force.closed, "dirty");
	assert.equal(force.activeDocumentId, null);
	assert.equal(tabs.has("dirty"), false);

	const refused = tabs.close("missing");
	assert.equal(refused.ok, false);
	if (!refused.ok && !refused.needsDecision) assert.equal(refused.error.code, "not-open");
});

test("closing a non-active tab keeps the active tab", () => {
	const tabs = model();
	assert.ok(tabs.open("a").ok);
	assert.ok(tabs.open("b").ok);
	assert.ok(tabs.open("c").ok);
	tabs.setActive("b");
	const closed = tabs.close("a");
	assert.ok(closed.ok);
	assert.equal(closed.closed, "a");
	assert.equal(closed.activeDocumentId, "b");
	assert.equal(tabs.activeDocumentId(), "b");
});

test("overflow keeps the active and pinned tabs and evicts the oldest clean non-active", () => {
	const tabs = model();
	for (const id of ["a", "b", "c", "d"]) assert.ok(tabs.open(id).ok);
	tabs.pin("b");
	const result = tabs.enforceOverflow(2);
	assert.ok(result.ok);
	assert.deepEqual(result.evicted, ["a", "c"]);
	assert.equal(result.satisfied, true);
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["b", "d"], // active d and pinned b survive
	);
	assert.equal(tabs.activeDocumentId(), "d");

	// The active tab is never evicted even when it is the oldest.
	const activeOldest = model();
	assert.ok(activeOldest.open("x1").ok);
	assert.ok(activeOldest.open("x2").ok);
	assert.ok(activeOldest.open("x3").ok);
	activeOldest.setActive("x1");
	const trimmed = activeOldest.enforceOverflow(1);
	assert.ok(trimmed.ok);
	assert.deepEqual(trimmed.evicted, ["x2", "x3"]);
	assert.equal(trimmed.satisfied, true);
	assert.deepEqual(
		activeOldest.list().map((tab) => tab.documentId),
		["x1"],
	);
});

test("overflow never evicts dirty tabs and reports when it cannot satisfy the limit", () => {
	const tabs = model(["c"]);
	for (const id of ["a", "b", "c", "d"]) assert.ok(tabs.open(id).ok);
	tabs.setActive("a");
	tabs.pin("b");
	const result = tabs.enforceOverflow(1);
	assert.ok(result.ok);
	assert.deepEqual(result.evicted, ["d"]); // the only clean, non-active, unpinned tab
	assert.equal(result.satisfied, false); // cannot reach the limit without dropping dirty/pinned/active
	assert.deepEqual(
		tabs.list().map((tab) => tab.documentId),
		["a", "b", "c"],
	);
	assert.equal(tabs.activeDocumentId(), "a");
	// A satisfied limit is a no-op with an empty report.
	const noop = tabs.enforceOverflow(10);
	assert.ok(noop.ok);
	assert.deepEqual(noop.evicted, []);
	assert.equal(noop.satisfied, true);
});

test("list returns copies so callers cannot corrupt the model", () => {
	const tabs = model();
	assert.ok(tabs.open("a").ok);
	const snapshot = tabs.list();
	const target = snapshot[0];
	assert.ok(target);
	target.pinned = true;
	target.preview = true;
	assert.deepEqual(tabs.list(), [{ documentId: "a", pinned: false, preview: false }]);
	snapshot.pop();
	assert.equal(tabs.count(), 1);
});
