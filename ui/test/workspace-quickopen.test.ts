// T6b Quick Open UI tests (plan §12): debounced provider calls with
// request-sequence stale-drop, local as-you-type subsequence filtering,
// bounded rendering with exact +N more disclosure, distinct empty/error/
// stale states, honest footer disclosure, keyboard/mouse parity and width
// safety (20/40/80/120).

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth, type TuiMouseEvent } from "@earendil-works/pi-tui";
import {
	QUICK_OPEN_QUERY_MAX,
	QuickOpenPane,
	subsequenceMatches,
	type QuickOpenProvider,
} from "../src/editor/quickOpen.ts";
import type {
	WorkspaceQuickOpenEntry,
	WorkspaceQuickOpenResult,
} from "../src/workspace/protocol.ts";

function hit(path: string): WorkspaceQuickOpenEntry {
	const parts = path.split("/");
	const name = parts.at(-1) ?? path;
	return { path, name, directory: parts.length > 1 ? parts.slice(0, -1).join("/") : "" };
}

function answer(
	entries: WorkspaceQuickOpenEntry[],
	extra: Partial<WorkspaceQuickOpenResult> = {},
): WorkspaceQuickOpenResult {
	return {
		entries,
		total: entries.length,
		truncated: false,
		reasons: [],
		indexing: false,
		scanned: entries.length,
		stale: false,
		workspaceEpoch: 1,
		exclusions: [],
		...extra,
	};
}

function plainLines(pane: QuickOpenPane, width = 60): string[] {
	return pane.render(width).map((line) => stripTerminalSequences(line));
}

function plain(pane: QuickOpenPane, width = 60): string {
	return plainLines(pane, width).join("\n");
}

function rowOf(pane: QuickOpenPane, needle: string, width = 60): number {
	return plainLines(pane, width).findIndex((line) => line.includes(needle));
}

function mouse(
	type: "press" | "click",
	x: number,
	y: number,
	clickCount = 1,
	button: "left" | "right" = "left",
): TuiMouseEvent {
	return {
		type,
		button,
		x,
		y,
		screenX: x,
		screenY: y,
		width: 60,
		height: 20,
		shift: false,
		alt: false,
		ctrl: false,
		clickCount,
	};
}

function delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function fixedProvider(entries: WorkspaceQuickOpenEntry[]): {
	provider: QuickOpenProvider;
	queries: string[];
} {
	const queries: string[] = [];
	const provider: QuickOpenProvider = async (query) => {
		queries.push(query);
		return answer(entries);
	};
	return { provider, queries };
}

// ─── provider plumbing: debounce, stale-drop, local filtering ────────────────

test("activate requests the empty query immediately and renders provider order", async () => {
	const { provider, queries } = fixedProvider([hit("src/b.ts"), hit("src/a.ts")]);
	const pane = new QuickOpenPane(provider);
	pane.activate();
	assert.deepEqual(queries, [""], "the empty query is sent without waiting for debounce");
	await delay(0);
	const lines = plainLines(pane);
	assert.deepEqual(pane.visibleEntries().map((entry) => entry.path), ["src/b.ts", "src/a.ts"]);
	assert.ok(lines[0]?.includes("Go to file:"), "the overlay header renders the title (finding 10)");
	assert.ok(lines.some((line) => line.includes("b.ts")));
	assert.ok(lines.some((line) => line.includes("2 results")), "the footer counts provider rows");
});

test("typing debounces provider calls and only the settled query is sent", async () => {
	const { provider, queries } = fixedProvider([hit("src/main.ts")]);
	const pane = new QuickOpenPane(provider, { debounceMs: 15 });
	pane.activate();
	for (const char of ["m", "a", "i", "n"]) pane.handleInput(char);
	assert.deepEqual(queries, [""], "no provider call while the user is still typing");
	await delay(40);
	assert.deepEqual(queries, ["", "main"], "one debounced call for the settled query");
	assert.equal(pane.getQuery(), "main");
});

test("local subsequence filter applies as-you-type over the last provider result", async () => {
	const entries = [hit("src/main.ts"), hit("docs/manual.md"), hit("src/other.md"), hit("notes.txt")];
	const { provider, queries } = fixedProvider(entries);
	const pane = new QuickOpenPane(provider, { debounceMs: 500 });
	pane.activate();
	await delay(0);
	assert.equal(pane.visibleEntries().length, 4);

	pane.handleInput("m");
	pane.handleInput("n");
	assert.deepEqual(
		pane.visibleEntries().map((entry) => entry.path),
		["src/main.ts", "docs/manual.md"],
		"the subsequence filter drops non-matching basenames immediately",
	);
	assert.deepEqual(queries, [""], "filtering never bypasses the debounce");

	pane.handleInput("z");
	pane.handleInput("z");
	assert.deepEqual(pane.visibleEntries(), []);
	assert.match(plain(pane), /\(no matches\)/);
});

test("an older slow response can never replace a newer one (request sequence)", async () => {
	const waiters = new Map<string, (value: WorkspaceQuickOpenResult) => void>();
	const calls: string[] = [];
	const provider: QuickOpenProvider = (query) => {
		calls.push(query);
		return new Promise((resolve) => waiters.set(query, resolve));
	};
	const pane = new QuickOpenPane(provider, { debounceMs: 5 });
	pane.activate(); // query "" is requested immediately and stays pending
	pane.handleInput("x"); // supersedes it; the "x" request fires after the debounce
	await delay(15);
	assert.deepEqual(calls, ["", "x"]);

	waiters.get("x")!(answer([hit("x-new.ts")]));
	await delay(0);
	assert.deepEqual(pane.visibleEntries().map((entry) => entry.path), ["x-new.ts"]);

	waiters.get("")!(answer([hit("old.ts")])); // the slower, older answer
	await delay(0);
	assert.deepEqual(
		pane.visibleEntries().map((entry) => entry.path),
		["x-new.ts"],
		"a superseded response is dropped, not merged",
	);
	assert.equal(
		pane.getView().entries.some((entry) => entry.name === "old.ts"),
		false,
		"the stale answer never reaches the list",
	);
});

test("selection identity survives a provider refresh", async () => {
	let call = 0;
	const provider: QuickOpenProvider = async () => {
		call += 1;
		return call === 1
			? answer([hit("a.ts"), hit("b.ts"), hit("c.ts")])
			: answer([hit("c.ts"), hit("b.ts"), hit("a.ts")]);
	};
	const pane = new QuickOpenPane(provider, { debounceMs: 5 });
	pane.activate();
	await delay(10);
	pane.handleInput("\x1b[B"); // select b.ts
	assert.equal(pane.selectedEntry()?.path, "b.ts");
	pane.handleInput("b");
	pane.handleInput("\x7f"); // back to "" → a fresh provider request
	await delay(25);
	assert.deepEqual(
		pane.visibleEntries().map((entry) => entry.path),
		["c.ts", "b.ts", "a.ts"],
		"the refreshed provider order replaced the list",
	);
	assert.equal(pane.selectedEntry()?.path, "b.ts", "selection follows the path, not the row index");
});

// ─── states and disclosure ──────────────────────────────────────────────────
test("empty states are distinct: searching, no files, no matches, error, refused, stale", async () => {
	let release = (): void => {};
	const provider: QuickOpenProvider = () =>
		new Promise((resolve) => {
			release = () => resolve(answer([]));
		});
	const pane = new QuickOpenPane(provider);
	pane.activate();
	assert.match(plain(pane), /\(searching…\)/, "an in-flight initial request is disclosed");
	release();
	await delay(0);
	assert.match(plain(pane), /\(no files\)/, "an empty index is distinct from an empty query");

	const failing = new QuickOpenPane(async () => {
		throw new Error("rpc dropped");
	});
	failing.activate();
	await delay(0);
	assert.match(plain(failing), /\(error: rpc dropped\)/, "a thrown failure is disclosed");

	const refused = new QuickOpenPane(async () =>
		answer([], { refused: { reason: "denied", message: "workspace is not open" } }),
	);
	refused.activate();
	await delay(0);
	assert.match(plain(refused), /\(error: workspace is not open\)/);

	const stale = new QuickOpenPane(async () => answer([], { stale: true, reasons: ["stale-epoch"] }));
	stale.activate();
	await delay(0);
	assert.match(plain(stale), /stale — workspace changed/, "a discarded stale answer is named");
	assert.match(plain(stale), /stale/, "the footer repeats the stale disclosure");
});

test("footer always discloses indexing, truncation, scanned progress and exclusions", async () => {
	const provider: QuickOpenProvider = async () =>
		answer([hit("src/a.ts")], {
			total: 250,
			truncated: true,
			reasons: ["result-limit", "file-cap"],
			indexing: true,
			scanned: 4_000,
			exclusions: [".git", "node_modules"],
		});
	const pane = new QuickOpenPane(provider);
	pane.activate();
	await delay(0);
	const footer = plainLines(pane, 200).at(-1) ?? "";
	assert.ok(footer.includes("250 results"), "the honest (index-relative) count is shown");
	assert.ok(footer.includes("indexing"), "indexing is never hidden");
	assert.ok(footer.includes("4000 scanned"), "progress is disclosed");
	assert.ok(footer.includes("truncated"), "truncation is never hidden");
	assert.ok(footer.includes("result-limit") && footer.includes("file-cap"), "bounds are named");
	assert.ok(footer.includes("excluded:") && footer.includes("node_modules"), "policy is disclosed");
});

// ─── keyboard and mouse ─────────────────────────────────────────────────────
test("keyboard: up/down clamp, enter opens, backspace edits, esc closes", async () => {
	const { provider } = fixedProvider([hit("a.ts"), hit("b/c.ts"), hit("d.ts")]);
	const pane = new QuickOpenPane(provider, { debounceMs: 5 });
	const opened: string[] = [];
	let closes = 0;
	pane.onOpen = (entry) => opened.push(entry.path);
	pane.onClose = () => {
		closes += 1;
	};
	pane.activate();
	await delay(10);
	assert.equal(pane.selectedEntry()?.path, "a.ts");

	pane.handleInput("\x1b[B");
	assert.equal(pane.selectedEntry()?.path, "b/c.ts");
	pane.handleInput("\x1b[B");
	pane.handleInput("\x1b[B");
	assert.equal(pane.selectedEntry()?.path, "d.ts", "down clamps at the last row");
	pane.handleInput("\x1b[A");
	assert.equal(pane.selectedEntry()?.path, "b/c.ts");

	pane.handleInput("\r");
	assert.deepEqual(opened, ["b/c.ts"], "enter opens the selected entry");
	pane.handleInput("\x1b[A");
	pane.handleInput("\r");
	assert.deepEqual(opened, ["b/c.ts", "a.ts"]);

	pane.handleInput("m");
	pane.handleInput("a");
	assert.equal(pane.getQuery(), "ma");
	pane.handleInput("\x7f");
	assert.equal(pane.getQuery(), "m", "backspace edits the query");

	pane.handleInput("\x1b");
	assert.equal(closes, 1, "escape closes the overlay");
	assert.equal(pane.focused, false);
	pane.handleInput("\x1b[B");
	assert.equal(pane.selectedEntry(), null, "keys after dismissal are inert");
});

test("the query buffer is capped at the protocol bound", async () => {
	const { provider } = fixedProvider([hit("a.ts")]);
	const pane = new QuickOpenPane(provider, { debounceMs: 500 });
	pane.activate();
	for (let index = 0; index < QUICK_OPEN_QUERY_MAX + 25; index++) pane.handleInput("x");
	assert.equal(pane.getQuery().length, QUICK_OPEN_QUERY_MAX);
});

test("unfocused keys are inert", () => {
	const { provider, queries } = fixedProvider([hit("a.ts")]);
	const pane = new QuickOpenPane(provider);
	pane.handleInput("x");
	pane.handleInput("\x1b[B");
	pane.handleInput("\r");
	assert.equal(pane.getQuery(), "");
	assert.equal(pane.selectedEntry(), null);
	assert.deepEqual(queries, [], "no provider traffic from an unfocused pane");
});

test("mouse: press claims focus, click selects, double-click opens", async () => {
	const { provider } = fixedProvider([hit("src/a.ts"), hit("src/b.ts"), hit("src/c.ts")]);
	const pane = new QuickOpenPane(provider, { debounceMs: 5 });
	const opened: string[] = [];
	pane.onOpen = (entry) => opened.push(entry.path);
	pane.activate();
	await delay(10);

	const row = rowOf(pane, "b.ts");
	const press = pane.handleMouse(mouse("press", 10, row));
	assert.deepEqual(press, { handled: true, focus: true }, "press claims focus without selecting");
	assert.equal(pane.selectedEntry()?.path, "src/a.ts");

	const click = pane.handleMouse(mouse("click", 10, row));
	assert.equal(click?.handled, true);
	assert.equal(pane.selectedEntry()?.path, "src/b.ts", "a single click selects");

	const doubleClick = pane.handleMouse(mouse("click", 10, row, 2));
	assert.equal(doubleClick?.handled, true);
	assert.deepEqual(opened, ["src/b.ts"], "a double click opens");

	const header = pane.handleMouse(mouse("click", 3, 0));
	assert.equal(header?.handled, true);
	assert.deepEqual(opened, ["src/b.ts"], "chrome rows never open");

	assert.equal(pane.handleMouse(mouse("click", 10, row, 1, "right")), undefined, "right button ignored");
	assert.equal(pane.handleMouse(mouse("click", 10, 999)), undefined, "clicks outside the pane are ignored");
});

// ─── bounds, width safety, sanitization ─────────────────────────────────────
test("visible rows are bounded with exact +N more disclosure and follow the selection", async () => {
	const entries = Array.from({ length: 12 }, (_value, index) => hit(`src/f${index}.ts`));
	const { provider } = fixedProvider(entries);
	const pane = new QuickOpenPane(provider, { maxVisible: 5, debounceMs: 5 });
	pane.activate();
	await delay(10);

	const entryRows = (): string[] =>
		plainLines(pane).filter((line) => /f\d+\.ts/.test(line));
	assert.equal(entryRows().length, 3, "the row budget bounds visible entries");
	assert.ok(plain(pane).includes("+9 more"), "rows below the window are disclosed exactly");

	for (let index = 0; index < 11; index++) pane.handleInput("\x1b[B");
	assert.equal(pane.selectedEntry()?.path, "src/f11.ts");
	assert.ok(plain(pane).includes("+9 more"), "the same budget now hides rows above");
	assert.ok(entryRows()[0]?.includes("f9.ts"), "the window scrolled to keep the selection visible");
	assert.equal(entryRows().length, 3);
});

test("widths 20/40/80/120 never overflow and hostile names are sanitized", async () => {
	const hostile = hit("src/\u001b[2Jevil\u0007name.ts");
	const unbroken = hit(`deep/${"x".repeat(120)}.ts`);
	const multiline = hit("src/line\nbreak.ts");
	const { provider } = fixedProvider([hostile, unbroken, multiline]);
	const pane = new QuickOpenPane(provider, { maxVisible: 3, debounceMs: 5 });
	pane.activate();
	await delay(10);

	for (const width of [20, 40, 80, 120]) {
		const lines = pane.render(width);
		assert.ok(lines.length > 0);
		for (const line of lines) {
			assert.ok(
				visibleWidth(line) <= width,
				`line wider than ${width}: ${JSON.stringify(stripTerminalSequences(line))}`,
			);
		}
		const text = lines.map((line) => stripTerminalSequences(line)).join("\n");
		assert.ok(!text.includes("\u001b"), "no escape byte survives sanitization");
		assert.ok(!text.includes("\u0007"), "no C0 control survives sanitization");
		assert.ok(text.includes("evilname.ts"), "the sanitized name is still readable");
		assert.ok(text.includes("line break.ts"), "an embedded newline collapses to one row");
	}
});

test("subsequenceMatches is case-insensitive and empty queries match everything", () => {
	assert.equal(subsequenceMatches("Main.ts", ""), true);
	assert.equal(subsequenceMatches("Main.ts", "main"), true);
	assert.equal(subsequenceMatches("main.ts", "MN"), true, "subsequence across case");
	assert.equal(subsequenceMatches("main.ts", "mts"), true);
	assert.equal(subsequenceMatches("main.ts", "mtz"), false);
	assert.equal(subsequenceMatches("other.md", "mn"), false);
});

test("local quick-open filter uses casefold-compatible matching", () => {
	assert.equal(subsequenceMatches("straße.ts", "strasse"), true);
	assert.equal(subsequenceMatches("STRASSE.ts", "strasse"), true);
});
