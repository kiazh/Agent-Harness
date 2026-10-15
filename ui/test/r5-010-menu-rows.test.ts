// R5-F (finding 10, RED-first): Menu physical-row mapping.
//
// Rendering expands embedded newlines into physical rows, but mouse
// hit-testing indexes items (one row per item) and the viewport budget
// counts items, not physical rows. Session titles accept embedded
// newlines, so in the deletion picker (host.pickMany → multi-select
// Menu, ui/src/app.ts) clicking the second physical row of a two-line
// title checks the NEXT session — which is then deleted on confirm.
// The same skew applies to multi-line titles (the `y - 2` origin assumes
// a one-row title + filter) and to the row budget.
//
// Fake-terminal harness: real Menu render rows establish the physical
// layout, then component-local mouse events (zero-based, y=0 is the
// first title row) click exact physical rows.
import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { Menu } from "../src/components/Menu.ts";

const WIDTH = 80;

function click(x: number, y: number, width = WIDTH): TuiMouseEvent {
	return {
		type: "click", button: "left", x, y,
		screenX: x, screenY: y, width, height: 24,
		shift: false, alt: false, ctrl: false,
	};
}

const FIRST = "11111111-aaaa-bbbb-cccc-111111111111";
const OTHER = "22222222-aaaa-bbbb-cccc-222222222222";

function clean(menu: Menu, width = WIDTH): string[] {
	return menu.render(width).map((row) => stripTerminalSequences(row));
}

test("R5-010: clicking the second physical row of a two-line entry checks that entry", () => {
	// Deletion-picker shape from ui/src/features/sessions.ts sessionItems:
	// session-id values with title labels. The first title wraps two rows.
	const menu = new Menu("Select sessions to delete", [
		{ value: FIRST, label: "first\nsecond-line" },
		{ value: OTHER, label: "other" },
	], { multiSelect: true });
	const movedTo: string[] = [];
	menu.onSelectionChange = (item) => void movedTo.push(item.value);

	const rows = clean(menu);
	assert.match(rows[2]!, /first/, "row 2 carries the entry's first line");
	assert.equal(rows[3]!.trim(), "second-line", "row 3 is the entry's second physical row");
	assert.match(rows[4]!, /other/, "row 4 carries the next session");

	menu.handleMouse(click(2, 3));

	assert.ok(!movedTo.includes(OTHER), `click on "second-line" must not select the next session (moved to ${JSON.stringify(movedTo)})`);
	const checked = clean(menu).filter((row) => row.includes("[x]"));
	assert.equal(checked.length, 1, `exactly one session checked, got: ${JSON.stringify(clean(menu))}`);
	assert.match(checked[0]!, /first/, "the two-line entry itself is checked");
	assert.doesNotMatch(checked[0]!, /other/);
});

test("R5-010: the row budget counts physical rows, not items", () => {
	const menu = new Menu("T", [
		{ value: "a", label: "a1\na2" },
		{ value: "b", label: "b" },
		{ value: "c", label: "c" },
	], { multiSelect: true, visibleRows: 2 });
	const frame = clean(menu).join("\n");
	assert.match(frame, /a1/, "the two-row entry fits the 2-row budget");
	assert.doesNotMatch(frame, /\bb\b/, "no room left: the next item is outside the budget");
});

test("R5-010: hit-testing accounts for multi-line titles", () => {
	const menu = new Menu("line one\nline two", [
		{ value: "x", label: "Xray" },
		{ value: "y", label: "Yankee" },
	], { multiSelect: true });
	const movedTo: string[] = [];
	menu.onSelectionChange = (item) => void movedTo.push(item.value);
	const rows = clean(menu);
	assert.match(rows[0]!, /line one/);
	assert.match(rows[1]!, /line two/);
	assert.match(rows[3]!, /Xray/, "items start after 2 title rows + the filter row");

	menu.handleMouse(click(2, 3));

	assert.ok(!movedTo.includes("y"), `click on "Xray" must not select "Yankee" (moved to ${JSON.stringify(movedTo)})`);
	const checked = clean(menu).filter((row) => row.includes("[x]"));
	assert.equal(checked.length, 1);
	assert.match(checked[0]!, /Xray/);
});

test("R5-010: a description newline belongs to its own entry", () => {
	const menu = new Menu("T", [
		{ value: "a", label: "l", description: "d1\nd2" },
		{ value: "b", label: "m" },
	], { multiSelect: true });
	const movedTo: string[] = [];
	menu.onSelectionChange = (item) => void movedTo.push(item.value);
	const rows = clean(menu);
	assert.match(rows[2]!, /d1/, "row 2 carries the entry plus the description head");
	assert.equal(rows[3]!.trim(), "d2", "row 3 is the description's second physical row");

	menu.handleMouse(click(2, 3));

	assert.ok(!movedTo.includes("b"), `click on "d2" must not select the next entry (moved to ${JSON.stringify(movedTo)})`);
	const checked = clean(menu).filter((row) => row.includes("[x]"));
	assert.equal(checked.length, 1);
	assert.match(checked[0]!, /\[x\] l/);
});
