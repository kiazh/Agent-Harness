// T2 contract tests: pure workspace geometry (plan §7, §24A).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	adjustSplitRatio,
	clampSplitRatio,
	computeLayout,
	dividerRect,
	explorerColumnBudget,
} from "../src/workspace/layout.ts";
import { SPLIT_MINIMUM_COLUMNS, type LayoutInput } from "../src/workspace/state.ts";

const base: LayoutInput = {
	columns: 160,
	rows: 48,
	requestedMode: "split",
	splitRatio: 0.5,
	explorerRequested: false,
	preferredPane: "chat",
};

test("split at 160 columns allocates approximately 50/50 with a divider", () => {
	const r = computeLayout(base);
	assert.equal(r.effectiveMode, "split");
	assert.ok(r.chat && r.editor);
	assert.equal(r.chat.x, 0);
	assert.equal(r.editor.x, r.chat.columns + 1);
	assert.equal(r.chat.columns + 1 + r.editor.columns, 160);
	assert.ok(Math.abs(r.chat.columns - r.editor.columns) <= 2);
});

test("split is refused below the derived minimum and preserves the request", () => {
	const narrow = computeLayout({ ...base, columns: SPLIT_MINIMUM_COLUMNS - 1 });
	assert.equal(narrow.effectiveMode, "chat");
	assert.equal(narrow.reason, "narrow");
	assert.equal(narrow.chat?.columns, SPLIT_MINIMUM_COLUMNS - 1);
	assert.equal(narrow.editor, undefined);
	// The exact threshold column splits.
	const exact = computeLayout({ ...base, columns: SPLIT_MINIMUM_COLUMNS });
	assert.equal(exact.effectiveMode, "split");
});

test("narrow fallback prefers the focused pane", () => {
	const r = computeLayout({ ...base, columns: 60, preferredPane: "editor" });
	assert.equal(r.effectiveMode, "editor");
	assert.equal(r.editor?.columns, 60);
	assert.equal(r.chat, undefined);
});

test("short terminals fall back to a single pane with reason short", () => {
	const r = computeLayout({ ...base, rows: 6 });
	assert.equal(r.effectiveMode, "chat");
	assert.equal(r.reason, "short");
});

test("maximized modes allocate the full width and report requested", () => {
	const chat = computeLayout({ ...base, requestedMode: "chat" });
	assert.equal(chat.chat?.columns, 160);
	assert.equal(chat.editor, undefined);
	const editor = computeLayout({ ...base, requestedMode: "editor" });
	assert.equal(editor.editor?.columns, 160);
	assert.equal(editor.chat, undefined);
});

test("ratio clamps to 0.25–0.75 and steps are 5 points", () => {
	assert.equal(clampSplitRatio(0.1), 0.25);
	assert.equal(clampSplitRatio(0.9), 0.75);
	assert.equal(clampSplitRatio(Number.NaN), 0.5);
	assert.equal(adjustSplitRatio(0.5, 1), 0.55);
	assert.equal(adjustSplitRatio(0.75, 1), 0.75);
	assert.equal(adjustSplitRatio(0.5, -1), 0.45);
});

test("pane minima hold across the ratio range", () => {
	for (const ratio of [0.25, 0.5, 0.75]) {
		const r = computeLayout({ ...base, splitRatio: ratio, columns: SPLIT_MINIMUM_COLUMNS });
		assert.ok(r.chat && r.editor);
		assert.ok(r.chat.columns >= 40);
		assert.ok(r.editor.columns >= 48);
	}
});

test("explorer docks off the editor side when the editor keeps its minimum", () => {
	const r = computeLayout({ ...base, explorerRequested: true });
	assert.equal(r.explorerOverlay, false);
	assert.ok(r.explorer && r.editor);
	assert.equal(r.explorer.columns, 28);
	assert.equal(r.editor.x + r.editor.columns, r.explorer.x);
	assert.ok(r.editor.columns >= 48);
	assert.equal(r.chat!.columns + 1 + r.editor.columns + r.explorer.columns, 160);
});

test("explorer becomes an editor-local overlay when docking would starve the editor", () => {
	const r = computeLayout({ ...base, columns: SPLIT_MINIMUM_COLUMNS, explorerRequested: true });
	assert.equal(r.explorerOverlay, true);
	assert.equal(r.explorer, undefined);
	assert.ok(r.editor && r.editor.columns >= 48);
});

test("explorer budget never goes below the editor minimum", () => {
	assert.deepEqual(explorerColumnBudget(48), { docked: false, columns: 0 });
	assert.deepEqual(explorerColumnBudget(70), { docked: true, columns: 22 });
	assert.deepEqual(explorerColumnBudget(120, 36), { docked: true, columns: 36 });
	assert.deepEqual(explorerColumnBudget(120, 100), { docked: true, columns: 40 });
});

test("divider hitbox matches the gap between panes", () => {
	const r = computeLayout(base);
	const d = dividerRect(r);
	assert.ok(d);
	assert.equal(d.x, r.chat!.columns);
	assert.equal(d.columns, 1);
	assert.equal(dividerRect(computeLayout({ ...base, requestedMode: "chat" })), undefined);
});

test("degenerate widths never produce negative rectangles", () => {
	for (const columns of [0, 1, 2, 20, 40, 80]) {
		const r = computeLayout({ ...base, columns, explorerRequested: true });
		for (const pane of [r.chat, r.editor, r.explorer]) {
			if (pane) assert.ok(pane.columns >= 0 && pane.x >= 0);
		}
	}
});
