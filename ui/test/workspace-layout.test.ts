// T2 contract tests: pure workspace geometry (plan §7, §24A) with the
// far-right explorer grip and the chatSide split swap.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	adjustSplitRatio,
	chatIsRight,
	clampSplitRatio,
	computeLayout,
	dividerRect,
	explorerColumnBudget,
	explorerTreeRowBudget,
} from "../src/workspace/layout.ts";
import { LAYOUT_BUDGETS, SPLIT_MINIMUM_COLUMNS, type LayoutInput } from "../src/workspace/state.ts";

const base: LayoutInput = {
	columns: 160,
	rows: 48,
	requestedMode: "split",
	splitRatio: 0.5,
	explorerRequested: false,
	preferredPane: "chat",
};

test("split at 160 columns allocates approximately 50/50 with a divider and grip", () => {
	const r = computeLayout(base);
	assert.equal(r.effectiveMode, "split");
	assert.ok(r.chat && r.editor);
	assert.equal(r.chat.x, 0);
	assert.equal(r.editor.x, r.chat.columns + 1);
	assert.ok(r.grip, "the far-right grip column is reserved");
	assert.equal(r.grip.columns, LAYOUT_BUDGETS.gripColumns);
	assert.equal(r.grip.x, 160 - LAYOUT_BUDGETS.gripColumns);
	assert.equal(r.chat.columns + 1 + r.editor.columns + r.grip.columns, 160, "every column is accounted for");
	assert.ok(Math.abs(r.chat.columns - r.editor.columns) <= 2);
});

test("split is refused below the derived minimum and preserves the request", () => {
	const narrow = computeLayout({ ...base, columns: SPLIT_MINIMUM_COLUMNS - 1 });
	assert.equal(narrow.effectiveMode, "chat");
	assert.equal(narrow.reason, "narrow");
	assert.equal(narrow.chat?.columns, SPLIT_MINIMUM_COLUMNS - 1);
	assert.equal(narrow.editor, undefined);
	assert.equal(narrow.grip, undefined, "the grip exists only while the editor is visible");
	// The exact threshold column splits.
	const exact = computeLayout({ ...base, columns: SPLIT_MINIMUM_COLUMNS });
	assert.equal(exact.effectiveMode, "split");
	assert.ok(exact.chat && exact.editor && exact.grip);
	assert.ok(exact.chat.columns >= LAYOUT_BUDGETS.minChatColumns);
	assert.ok(exact.editor.columns >= LAYOUT_BUDGETS.minEditorColumns);
	assert.equal(exact.chat.columns + 1 + exact.editor.columns + exact.grip.columns, SPLIT_MINIMUM_COLUMNS);
});

test("narrow fallback prefers the focused pane and keeps the grip beside the editor", () => {
	const r = computeLayout({ ...base, columns: 60, preferredPane: "editor" });
	assert.equal(r.effectiveMode, "editor");
	assert.equal(r.editor?.columns, 60 - LAYOUT_BUDGETS.gripColumns);
	assert.equal(r.chat, undefined);
	assert.equal(r.grip?.x, 60 - LAYOUT_BUDGETS.gripColumns);
	// The chat fallback has no grip (no editor column to ground).
	const chat = computeLayout({ ...base, columns: 60, preferredPane: "chat" });
	assert.equal(chat.effectiveMode, "chat");
	assert.equal(chat.chat?.columns, 60);
	assert.equal(chat.grip, undefined);
});

test("short terminals fall back to a single pane with reason short", () => {
	const r = computeLayout({ ...base, rows: 6 });
	assert.equal(r.effectiveMode, "chat");
	assert.equal(r.reason, "short");
});

test("maximized modes allocate the width minus the grip and report requested", () => {
	const chat = computeLayout({ ...base, requestedMode: "chat" });
	assert.equal(chat.chat?.columns, 160);
	assert.equal(chat.editor, undefined);
	assert.equal(chat.grip, undefined);
	const editor = computeLayout({ ...base, requestedMode: "editor" });
	assert.equal(editor.editor?.columns, 160 - LAYOUT_BUDGETS.gripColumns);
	assert.equal(editor.chat, undefined);
	assert.equal(editor.grip?.x, 160 - LAYOUT_BUDGETS.gripColumns);
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

test("chatSide right mirrors the split and keeps both panes at their minima", () => {
	const left = computeLayout(base);
	const right = computeLayout({ ...base, chatSide: "right" });
	assert.equal(right.effectiveMode, "split");
	assert.ok(right.chat && right.editor && right.grip && left.chat && left.editor);
	assert.equal(right.editor.x, 0, "the editor leads on a right-side chat");
	assert.equal(right.chat.x, right.editor.columns + 1, "the divider separates editor and chat");
	assert.equal(right.chat.x + right.chat.columns + right.grip.columns, 160);
	assert.equal(chatIsRight(right), true);
	assert.equal(chatIsRight(left), false);
	assert.equal(right.chat.columns, left.chat.columns, "a side swap never changes the split halves");
	assert.equal(right.editor.columns, left.editor.columns);
	assert.ok(right.chat.columns >= 40 && right.editor.columns >= 48);
});

test("a right-side chat + explorer keeps the drawer at the far right, right of chat", () => {
	const r = computeLayout({ ...base, chatSide: "right", explorerRequested: true });
	assert.ok(r.chat && r.editor && r.explorer && r.grip);
	assert.equal(r.explorer.x + r.explorer.columns + r.grip.columns, 160, "drawer docks at the far right edge");
	assert.ok(r.explorer.x >= r.chat.x + r.chat.columns, "the drawer sits right of the chat column");
	assert.ok(r.chat.columns >= 40 && r.editor.columns >= 48);
});

test("explorer docks off the editor side when the editor keeps its minimum", () => {
	const r = computeLayout({ ...base, explorerRequested: true });
	assert.equal(r.explorerOverlay, false);
	assert.ok(r.explorer && r.editor && r.grip);
	assert.equal(r.explorer.columns, 28);
	assert.equal(r.editor.x + r.editor.columns, r.explorer.x);
	assert.ok(r.editor.columns >= 48);
	assert.equal(r.explorer.x + r.explorer.columns + r.grip.columns, 160);
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

test("divider hitbox matches the gap between panes on either side", () => {
	const left = computeLayout(base);
	assert.ok(left.chat && left.editor);
	const leftDivider = dividerRect(left);
	assert.ok(leftDivider);
	assert.equal(leftDivider.x, left.chat.columns);
	assert.equal(leftDivider.columns, 1);
	// Mirrored split: the divider still sits between the panes.
	const right = computeLayout({ ...base, chatSide: "right" });
	assert.ok(right.chat && right.editor);
	const rightDivider = dividerRect(right);
	assert.ok(rightDivider);
	assert.equal(rightDivider.x, right.editor.columns);
	assert.equal(rightDivider.x + 1, right.chat.x);
	assert.equal(dividerRect(computeLayout({ ...base, requestedMode: "chat" })), undefined);
});

test("degenerate widths never produce negative rectangles", () => {
	for (const columns of [0, 1, 2, 20, 40, 80]) {
		for (const chatSide of ["left", "right"] as const) {
			const r = computeLayout({ ...base, columns, explorerRequested: true, chatSide });
			for (const pane of [r.chat, r.editor, r.explorer, r.grip]) {
				if (pane) assert.ok(pane.columns >= 0 && pane.x >= 0);
			}
			if (r.chat && r.editor) {
				assert.ok(r.chat.x + r.chat.columns <= columns);
				assert.ok(r.editor.x + r.editor.columns <= columns);
			}
		}
	}
});

test("the docked explorer's tree budget is a bounded panel, never the whole page", () => {
	// A tall pane area is capped so a long tree cannot paint a full-page surface
	// rectangle; the tree scrolls with `+N more` disclosure instead.
	assert.equal(
		explorerTreeRowBudget(40),
		LAYOUT_BUDGETS.explorerMaxTreeRows,
		"a tall page caps the painted panel well below the page height",
	);
	assert.ok(
		LAYOUT_BUDGETS.explorerMaxTreeRows < 40,
		"the cap is strictly less than a typical panes area",
	);
	// A short page keeps its available rows; tiny budgets keep the marker floor.
	assert.equal(explorerTreeRowBudget(10), 10, "a short page uses its spare rows");
	assert.equal(explorerTreeRowBudget(2), 3, "the marker floor is 3 rows");
	assert.equal(explorerTreeRowBudget(0), 3, "degenerate areas never go below the floor");
	assert.equal(explorerTreeRowBudget(-5), 3, "negative areas never go below the floor");
});
