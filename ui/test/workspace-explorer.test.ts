// T6 explorer tests (plan §12): lazy tree over an injected listing function,
// stable selection identity, explicit text states, paging, keyboard/mouse
// parity, focused-only key handling and width safety.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { ExplorerModel, ExplorerPane, type ExplorerListFn } from "../src/editor/explorer.ts";
import type { WorkspaceListEntry, WorkspaceListResult } from "../src/workspace/protocol.ts";

function entry(
	path: string,
	kind: "file" | "directory",
	extra: Partial<WorkspaceListEntry> = {},
): WorkspaceListEntry {
	return { name: path.split("/").at(-1) ?? path, path, kind, ...extra };
}

function page(
	entries: WorkspaceListEntry[],
	extra: Partial<WorkspaceListResult> = {},
): WorkspaceListResult {
	return { entries, nextCursor: null, exclusions: [], truncated: false, ...extra };
}

interface FakeCall {
	path: string;
	cursor: string | undefined;
	hidden: boolean;
}

type FakeSource = (
	cursor: string | undefined,
	hidden: boolean,
) => WorkspaceListResult | Promise<WorkspaceListResult>;

function recordingList(table: Record<string, FakeSource>): { list: ExplorerListFn; calls: FakeCall[] } {
	const calls: FakeCall[] = [];
	const list: ExplorerListFn = async (path, cursor, options) => {
		const hidden = options?.includeHidden === true;
		calls.push({ path, cursor, hidden });
		const source = table[path];
		if (!source) throw new Error(`no fake listing for ${JSON.stringify(path)}`);
		return await source(cursor, hidden);
	};
	return { list, calls };
}

/** The fake workspace: directories, exclusions, paging, denials, symlinks. */
function standardTree(): Record<string, FakeSource> {
	return {
		"": (_cursor, hidden) =>
			page(
				[
					entry("docs", "directory"),
					entry("src", "directory"),
					entry("node_modules", "directory", { excluded: true }),
					entry("README.md", "file", { sizeBytes: 12 }),
					...(hidden ? [entry(".env", "file")] : []),
				],
				{ exclusions: [".git", "node_modules"] },
			),
		docs: () => page([entry("docs/guide.md", "file")]),
		src: () =>
			page([
				entry("src/empty", "directory"),
				entry("src/paged", "directory"),
				entry("src/denied", "directory"),
				entry("src/main.ts", "file"),
			]),
		"src/empty": () => page([]),
		"src/denied": () =>
			page([], { refused: { reason: "denied", message: "permission denied" } }),
		"src/paged": (cursor) =>
			cursor === undefined
				? page(
					[
						entry("src/paged/a.ts", "file"),
						entry("src/paged/b.ts", "file"),
						entry("src/paged/c.ts", "file"),
					],
					{ nextCursor: "3", truncated: true },
				)
				: page([entry("src/paged/d.ts", "file"), entry("src/paged/e.ts", "file")]),
	};
}

function plainLines(pane: ExplorerPane, width = 28): string[] {
	return pane.render(width).map((line) => stripTerminalSequences(line));
}

function plain(pane: ExplorerPane, width = 28): string {
	return plainLines(pane, width).join("\n");
}

function rowOf(pane: ExplorerPane, needle: string, width = 28): number {
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
		width: 28,
		height: 20,
		shift: false,
		alt: false,
		ctrl: false,
		clickCount,
	};
}

function settle(): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, 0));
}

// ─── model: laziness, expansion, selection identity ─────────────────────────

test("root listing loads lazily and only expanded directories are requested", async () => {
	const { list, calls } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	assert.equal(calls.length, 0, "no request before load");
	assert.match(plain(pane), /^workspace/, "header renders the root label");
	await model.load();
	assert.deepEqual(calls.map((call) => call.path), [""]);
	assert.deepEqual(
		model.visibleNodes().map((node) => node.id),
		["docs", "src", "node_modules", "README.md"],
		"backend order is preserved (no client re-sorting)",
	);
	assert.equal(model.selectedId(), "docs", "first visible entry is the initial selection");
	await model.expand("src");
	assert.deepEqual(calls.map((call) => call.path), ["", "src"]);
	assert.match(plain(pane), /main\.ts/);
	assert.match(plain(pane), /empty/, "unexpanded children are not fetched");
	assert.ok(!calls.some((call) => call.path === "src/empty"), "src/empty was never requested");
});

test("expansion, collapse and selection identity survive re-render", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	await model.load();
	await model.expand("src");
	model.select("src/main.ts");
	const selectedRow = (): string | undefined =>
		plainLines(new ExplorerPane(model)).find((line) => line.includes("main.ts"));
	assert.ok(selectedRow()?.startsWith("❯ "), "selection marker sits on the selected entry");
	// Expanding a directory above the selection shifts row indexes; identity holds.
	await model.expand("docs");
	assert.equal(model.selectedId(), "src/main.ts");
	assert.ok(selectedRow()?.startsWith("❯ "), "the same entry stays selected by path, not row index");
	// Collapsing the containing directory returns selection to that directory.
	model.collapse("src");
	assert.equal(model.selectedId(), "src");
	assert.ok(!plain(new ExplorerPane(model)).includes("main.ts"));
	// Re-expanding does not refetch the still-loaded directory.
	assert.equal(model.directory("src")?.state, "loaded");
});

test("collapsing a directory while a child is selected returns to the directory row", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	await model.load();
	await model.expand("src");
	model.select("src/main.ts");
	model.collapse("src");
	assert.equal(model.selectedId(), "src");
	const line = plainLines(new ExplorerPane(model)).find((l) => l.includes("src"));
	assert.ok(line?.startsWith("❯ "), "the collapsed directory row carries the selection");
});

// ─── paging ─────────────────────────────────────────────────────────────────

test("pagination loads the next page on demand and keeps backend order", async () => {
	const { list, calls } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	await model.expand("src");
	await model.expand("src/paged");
	const pagedIds = (): string[] =>
		model.visibleNodes()
			.filter((node) => node.kind === "entry" && node.id.startsWith("src/paged/"))
			.map((node) => node.id);
	assert.deepEqual(pagedIds(), ["src/paged/a.ts", "src/paged/b.ts", "src/paged/c.ts"]);
	assert.ok(
		model.visibleNodes().some((node) => node.kind === "load-more" && node.parentPath === "src/paged"),
		"a truncated page exposes a selectable load-more row",
	);
	assert.match(plain(pane), /load more/);
	assert.match(plain(pane), /more entries/, "footer carries the paging affordance");
	await model.loadMore("src/paged");
	assert.deepEqual(calls.at(-1), { path: "src/paged", cursor: "3", hidden: false });
	assert.deepEqual(pagedIds(), [
		"src/paged/a.ts",
		"src/paged/b.ts",
		"src/paged/c.ts",
		"src/paged/d.ts",
		"src/paged/e.ts",
	]);
	assert.ok(
		!model.visibleNodes().some((node) => node.kind === "load-more"),
		"an exhausted page drops the load-more row",
	);
	assert.doesNotMatch(plain(pane), /more entries/);
});

// ─── distinct text states ───────────────────────────────────────────────────

test("directory states render distinct text: loading, empty, denied", async () => {
	let release = (): void => {};
	const table = standardTree();
	const loading = new Promise<WorkspaceListResult>((resolve) => {
		release = () => resolve(page([entry("src/deferred.ts", "file")]));
	});
	table["src"] = () => loading;
	const { list } = recordingList(table);
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	const expansion = model.expand("src");
	await settle();
	assert.match(plain(pane), /…/, "a pending listing renders the loading ellipsis");
	release();
	await expansion;
	assert.doesNotMatch(plain(pane), /…/);
	assert.match(plain(pane), /deferred\.ts/);

	const { list: list2 } = recordingList(standardTree());
	const model2 = new ExplorerModel(list2);
	const pane2 = new ExplorerPane(model2);
	await model2.load();
	await model2.expand("src");
	await model2.expand("src/empty");
	assert.match(plain(pane2), /\(empty\)/);
	await model2.expand("src/denied");
	assert.match(plain(pane2), /\(denied\)/, "a refused listing is disclosed as text");
	assert.equal(model2.directory("src/denied")?.state, "error");
});

test("an empty root renders (empty); a refused root renders (denied)", async () => {
	const empty = recordingList({ "": () => page([]) });
	const emptyModel = new ExplorerModel(empty.list);
	await emptyModel.load();
	assert.match(plain(new ExplorerPane(emptyModel)), /\(empty\)/);

	const refused = recordingList({
		"": () => page([], { refused: { reason: "denied", message: "root not readable" } }),
	});
	const refusedModel = new ExplorerModel(refused.list);
	await refusedModel.load();
	assert.match(plain(new ExplorerPane(refusedModel)), /\(denied\)/);
});

test("excluded entries and the exclusion policy are disclosed in text", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	const excludedRow = plainLines(pane).find((line) => line.includes("node_modules"));
	assert.ok(excludedRow?.includes("(excluded)"), "policy exclusion is a visible marker, not a silent hide");
	assert.match(plain(pane), /excluded:/);
	assert.match(plain(pane), /\.git/);
	assert.match(plain(pane), /node_modules/);
});

test("a thrown listing failure renders (error) and can be retried", async () => {
	let flakyCalls = 0;
	const list: ExplorerListFn = async (path) => {
		if (path === "") return page([entry("flaky", "directory")]);
		if (path === "flaky") {
			flakyCalls += 1;
			if (flakyCalls === 1) throw new Error("rpc dropped");
			return page([entry("flaky/ok.ts", "file")]);
		}
		throw new Error(`unexpected listing for ${path}`);
	};
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	await model.expand("flaky");
	assert.match(plain(pane), /\(error\)/, "a transport failure is disclosed, never a silent empty");
	await model.reload("flaky");
	assert.match(plain(pane), /ok\.ts/, "retry recovers");
});

// ─── hidden toggle ──────────────────────────────────────────────────────────

test("hidden toggle re-requests listings with the flag and updates the affordance", async () => {
	const { list, calls } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	await model.expand("src");
	assert.doesNotMatch(plain(pane), /\.env/);
	assert.match(plain(pane), /h: show hidden/, "the hint states the current mode (hidden entries excluded)");
	await model.setHidden(true);
	assert.equal(model.hidden, true);
	assert.equal(calls.at(-1)?.hidden, true, "the root is re-requested with includeHidden");
	assert.ok(
		calls.filter((call) => call.path === "src").at(-1)?.hidden === true,
		"expanded directories are re-requested too",
	);
	assert.match(plain(pane), /\.env/, "hidden entries appear after the toggle");
	assert.match(plain(pane), /h: hide hidden/);
	await model.toggleHidden();
	assert.equal(model.hidden, false);
	assert.doesNotMatch(plain(pane), /\.env/);
});

test("hidden toggle preserves selection identity when the entry survives", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	await model.load();
	await model.expand("src");
	model.select("src/main.ts");
	await model.setHidden(true);
	assert.equal(model.selectedId(), "src/main.ts", "selection survives a listing refresh");
});

// ─── symlinks ───────────────────────────────────────────────────────────────

test("symlink entries render a marker; outside links stay non-expandable", async () => {
	const calls: string[] = [];
	const list: ExplorerListFn = async (path) => {
		calls.push(path);
		if (path === "escape") throw new Error("must not list an outside symlink");
		if (path === "") {
			return page([
				entry("inroot-link", "directory", { symlink: true }),
				entry("escape", "file", { symlink: true }),
				entry("real.txt", "file"),
			]);
		}
		return page([entry("inroot-link/child.ts", "file")]);
	};
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	const inroot = plainLines(pane).find((line) => line.includes("inroot-link"));
	const escape = plainLines(pane).find((line) => line.includes("escape"));
	assert.ok(inroot?.includes("@"), "directory symlink carries the marker");
	assert.ok(escape?.includes("@"), "file symlink carries the marker");
	model.select("escape");
	await model.moveInto();
	assert.deepEqual(calls, [""], "a non-directory symlink is never expanded");
	await model.expand("inroot-link");
	assert.match(plain(pane), /child\.ts/, "backend-approved directory symlinks expand");
});

// ─── keyboard and mouse mapping ─────────────────────────────────────────────

test("unfocused keys are inert; focused keys move, expand, collapse and preview", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	const previews: string[] = [];
	pane.onPreviewFile = (item) => previews.push(item.path);
	await model.load();
	await model.expand("src");
	model.select("src/main.ts");

	pane.focused = false;
	pane.handleInput("\x1b[B");
	assert.equal(model.selectedId(), "src/main.ts", "unfocused input never moves the selection");
	pane.handleInput("h");
	assert.equal(model.hidden, false, "unfocused 'h' never toggles hidden");

	pane.focused = true;
	pane.handleInput("\x1b[B");
	assert.equal(model.selectedId(), "node_modules", "down moves among visible nodes");
	pane.handleInput("\x1b[A");
	assert.equal(model.selectedId(), "src/main.ts");
	pane.handleInput("\r");
	assert.deepEqual(previews, ["src/main.ts"], "enter previews the file (pinning is the controller's)");
	// Left on a file moves to its parent; left on an expanded directory collapses it.
	pane.handleInput("\x1b[D");
	assert.equal(model.selectedId(), "src");
	pane.handleInput("\x1b[D");
	assert.equal(model.directory("src")?.expanded, false, "left collapses an expanded directory");
	assert.equal(model.selectedId(), "src", "collapse keeps the directory selected");
	pane.handleInput("\x1b[C");
	await settle();
	assert.equal(model.directory("src")?.expanded, true, "right expands a collapsed directory");
	pane.handleInput("\x1b[C");
	await settle();
	assert.equal(model.selectedId(), "src/empty", "right then moves into the first child");
	pane.handleInput("h");
	await settle();
	assert.equal(model.hidden, true, "focused 'h' toggles hidden");
});

test("mouse: click selects and previews, double-click opens, glyph toggles", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	const previews: string[] = [];
	const opens: string[] = [];
	pane.onPreviewFile = (item) => previews.push(item.path);
	pane.onOpenFile = (item) => opens.push(item.path);
	await model.load();
	await model.expand("src");

	const mainRow = rowOf(pane, "main.ts");
	const press = pane.handleMouse(mouse("press", 10, mainRow));
	assert.deepEqual(press, { handled: true, focus: true }, "press claims focus");
	const single = pane.handleMouse(mouse("click", 10, mainRow));
	assert.equal(single?.handled, true);
	assert.equal(model.selectedId(), "src/main.ts");
	assert.deepEqual(previews, ["src/main.ts"], "single click previews");
	const double = pane.handleMouse(mouse("click", 10, mainRow, 2));
	assert.equal(double?.handled, true);
	assert.deepEqual(opens, ["src/main.ts"], "double click opens/pins");
	assert.equal(previews.length, 1, "an open does not also re-preview");

	// Clicking the disclosure cell of a directory row toggles without a preview.
	const docsRow = rowOf(pane, "docs");
	const toggled = pane.handleMouse(mouse("click", 2, docsRow));
	assert.equal(toggled?.handled, true);
	await settle();
	assert.equal(model.directory("docs")?.expanded, true, "glyph click toggles the directory");
	assert.deepEqual(previews, ["src/main.ts"], "directory clicks never preview");

	// The header's hidden affordance is a real control.
	const hiddenBefore = model.hidden;
	const headerToggle = pane.handleMouse(mouse("click", 11, 0));
	assert.equal(headerToggle?.handled, true);
	await settle();
	assert.equal(model.hidden, !hiddenBefore, "[h] click toggles hidden");
	// Non-left buttons are ignored.
	assert.equal(pane.handleMouse(mouse("click", 10, mainRow, 1, "right")), undefined);
});

// ─── width safety and row budgeting ─────────────────────────────────────────

test("widths 20/28/40/120 never throw, never overflow, and sanitize names", async () => {
	const hostile = `evil\u001b[2Jname${"x".repeat(120)}`;
	const list: ExplorerListFn = async () =>
		page([
			entry("src", "directory"),
			entry(hostile, "file"),
			entry(`line\nbreak.ts`, "file"),
		]);
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model);
	await model.load();
	for (const width of [20, 28, 40, 120]) {
		const lines = pane.render(width);
		assert.ok(lines.length > 0);
		for (const line of lines) {
			assert.ok(visibleWidth(line) <= width, `overwide line at ${width}`);
		}
		const text = plain(pane, width);
		assert.ok(!text.includes("\u001b]"), "control sequences never reach the pane text");
		assert.ok(!text.includes("\u001b[2J"), "control sequences never reach the pane text");
		assert.doesNotMatch(text, /\n\s*break\.ts/, "embedded newlines are collapsed to one row");
	}
	assert.match(plain(pane, 120), /evilname/, "the sanitized name survives at wide widths");
	for (const width of [0, 1, 2]) {
		assert.doesNotThrow(() => pane.render(width));
	}
});

test("rows beyond the row budget disclose +N more without losing the selection", async () => {
	const files = Array.from({ length: 30 }, (_, index) =>
		entry(`many/f${String(index).padStart(2, "0")}.ts`, "file"));
	const { list } = recordingList({
		"": () => page([entry("many", "directory")]),
		many: () => page(files),
	});
	const model = new ExplorerModel(list);
	const pane = new ExplorerPane(model, { rowBudget: 6 });
	await model.load();
	await model.expand("many");
	await settle();
	const top = plainLines(pane);
	assert.ok(top.some((line) => /\+\d+ more/.test(line)), "hidden loaded rows are counted");
	assert.match(plain(pane), /\+27 more/, "31 tree rows at a 6-row budget hide 27 below");
	model.select("many/f29.ts");
	const scrolled = plainLines(pane);
	assert.match(scrolled[1] ?? "", /^\+\d+ more\s*$/, "scrolling to the end discloses hidden rows above");
	assert.ok(scrolled.some((line) => line.includes("f29.ts")), "the selected row stays visible");
	pane.setRowBudget(undefined);
	assert.doesNotMatch(plain(pane), /\+\d+ more/, "no budget renders every loaded row");
});

test("selection clamps predictably when the selected entry disappears", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	await model.load();
	await model.setHidden(true);
	model.select(".env");
	await model.setHidden(false);
	assert.equal(
		model.selectedId(),
		"README.md",
		"the fallback lands on the closest visible neighbor, never silently elsewhere",
	);
});

test("reset drops expanded children from the previous workspace", async () => {
	const { list } = recordingList(standardTree());
	const model = new ExplorerModel(list);
	await model.load();
	await model.expand("src");
	assert.equal(model.directory("src")?.state, "loaded");

	model.reset();
	assert.equal(model.directory("src"), undefined);
	await model.load();
	assert.equal(
		model.visibleNodes().some((node) => node.id.startsWith("src/")),
		false,
		"old-root children must not survive a root switch",
	);
});

test("backend scan truncation survives without a next cursor", async () => {
	const { list } = recordingList({
		"": () => page([entry("a.ts", "file")], { nextCursor: null, truncated: true }),
	});
	const model = new ExplorerModel(list);
	await model.load();
	assert.equal(model.directory("")?.truncated, true);
});

test("a stale load-more finisher cannot clear a newer spinner", async () => {
	const pending: Array<(value: WorkspaceListResult) => void> = [];
	const list: ExplorerListFn = async (_path, cursor) => {
		if (cursor === undefined) {
			return page([entry("docs", "directory")], { nextCursor: "1" });
		}
		return await new Promise<WorkspaceListResult>((resolve) => pending.push(resolve));
	};
	const model = new ExplorerModel(list);
	await model.load();

	const first = model.loadMore("");
	assert.equal(model.directory("")?.loadingMore, true);
	await model.reload(""); // generation bump; invalidates first
	const second = model.loadMore("");
	assert.equal(model.directory("")?.loadingMore, true);

	pending[0]?.(page([entry("old.ts", "file")]));
	await first;
	assert.equal(
		model.directory("")?.loadingMore,
		true,
		"the stale finisher cleared the newer request's loading flag",
	);

	pending[1]?.(page([entry("new.ts", "file")]));
	await second;
	assert.equal(model.directory("")?.loadingMore, false);
});
