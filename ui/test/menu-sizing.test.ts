// Menu sizing: every Menu/Picker is bounded by a documented default viewport
// so no menu (e.g. the 53-skin theme picker) fills the page. The default is
// overridable per call site with an explicit numeric cap, and "full" opts out
// for legitimately huge views. Chrome (title + filter) stays at the top and
// the cancel/confirm hint stays pinned at the bottom.
import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, type TUI } from "@earendil-works/pi-tui";
import { DEFAULT_MENU_VISIBLE_ROWS, Menu } from "../src/components/Menu.ts";
import { Picker } from "../src/components.ts";

const CHROME_ROWS = 3; // title + filter + hint

function clean(component: { render(width: number): string[] }): string[] {
	return component.render(80).map((line) => stripTerminalSequences(line));
}

function items(count: number) {
	return Array.from({ length: count }, (_, i) => ({ value: `v${i}`, label: `Row ${i}` }));
}

test("a menu with 50+ items is bounded by the default row cap", () => {
	const menu = new Menu("Skins", items(53));
	const lines = clean(menu);
	assert.equal(
		lines.length,
		DEFAULT_MENU_VISIBLE_ROWS + CHROME_ROWS,
		`default cap + chrome, got:\n${lines.join("\n")}`,
	);
	assert.ok(lines.length < 24, "the menu no longer fills a 24-row page");
	assert.match(lines[0]!, /Skins/, "title stays on top");
	assert.match(lines.join("\n"), /Row 0\b/, "the first row is visible");
	assert.doesNotMatch(
		lines.join("\n"),
		new RegExp(`Row ${DEFAULT_MENU_VISIBLE_ROWS}\\b`),
		"items past the cap are scrolled out of view",
	);
});

test("menu chrome sits at the top, the hint is pinned at the bottom, items in between", () => {
	const menu = new Menu("Picker", items(53));
	const lines = clean(menu);
	assert.match(lines[0]!, /Picker/, "title is the first row");
	assert.match(lines[1]!, /Type to filter/, "filter row sits directly under the title");
	assert.equal(lines.at(-1)!.trim(), "enter select · esc back", "hint is the last row");
	const itemRows = lines.slice(2, -1);
	assert.equal(itemRows.length, DEFAULT_MENU_VISIBLE_ROWS, "exactly the budget of item rows");
	assert.ok(itemRows.every((row) => row.trim() !== ""), "no blank rows between chrome and items");
});

test("an explicit visibleRows cap overrides the default, still bounded by the live viewport", () => {
	const five = new Menu("Skins", items(53), { visibleRows: 5 });
	assert.equal(clean(five).length, 5 + CHROME_ROWS, "explicit cap wins over the default");

	const tui = { terminal: { rows: 24, columns: 80 } } as unknown as TUI;
	const tall = new Menu("Skins", items(53), { visibleRows: 40, tui });
	assert.equal(clean(tall).length, 20 + CHROME_ROWS, "live budget (24 - 4) clamps an oversized cap");
});

test('visibleRows: "full" opts out of the default cap for legitimately huge views', () => {
	const tui = { terminal: { rows: 50, columns: 80 } } as unknown as TUI;
	const menu = new Menu("Tall", items(60), { visibleRows: "full", tui });
	const lines = clean(menu);
	assert.equal(lines.length, 49, "title + filter + hint plus 46 rows (50 - chrome - one slack row)");
	assert.match(lines.join("\n"), /Row 45/);
	assert.doesNotMatch(lines.join("\n"), /Row 46\b/);
});

test("Picker inherits the bounded default and honours an explicit cap", () => {
	const picker = new Picker("Sessions", items(40), () => {}, () => {});
	const lines = clean(picker);
	assert.equal(lines.length, DEFAULT_MENU_VISIBLE_ROWS + CHROME_ROWS, "Picker wraps the Menu default");
	assert.equal(lines.at(-1)!.trim(), "enter select · esc back");

	const capped = new Picker("Sessions", items(40), () => {}, () => {}, 4);
	assert.equal(clean(capped).length, 4 + CHROME_ROWS, "an explicit cap still wins");
});

test("setVisibleRows keeps the menu bounded on resize", () => {
	const menu = new Menu("Skins", items(53));
	menu.setVisibleRows(3);
	assert.equal(clean(menu).length, 3 + CHROME_ROWS);
});
