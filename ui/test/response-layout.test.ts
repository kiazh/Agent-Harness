// T1 contract tests: adaptive assistant response boxes (plan §6).
// Width is content-aware (compact content-fit -> medium -> full pane width),
// height is the actual rendered Markdown row count, and streaming growth is
// monotonic within a stable pane allocation.
import assert from "node:assert/strict";
import { test } from "node:test";
import { Markdown, stripTerminalSequences, visibleWidth, type Component } from "@earendil-works/pi-tui";
import { AssistantTurn } from "../src/components/AssistantTurn.ts";
import {
	DEFAULT_RESPONSE_POLICY,
	assistantFrameBudget,
	chooseResponseGeometry,
	type ResponseWidthClass,
} from "../src/design/responseLayout.ts";
import { markdownTheme, theme } from "../src/theme.ts";

const SHORT = "hello **world**";
const LONG = Array.from({ length: 24 }, (_, i) => `Paragraph ${i}: ${"detail ".repeat(6)}`).join("\n\n");
// 12 source lines of 58 cells: 24 rendered rows at width 56, 12 at width 111.
const MID = Array.from({ length: 12 }, (_, i) => `entry ${i}: ${"x".repeat(50)}`).join("\n");
const FENCE = "```ts\nconst a = 1;\nconst b = 2;\nconsole.log(a + b);\n```";
const TABLE = "| name | value |\n| --- | --- |\n| alpha | 1 |";

function plainLines(component: Component, width: number): string[] {
	return component.render(width).map((line) => stripTerminalSequences(line));
}

function classRank(cls: ResponseWidthClass): number {
	return cls === "compact" ? 0 : cls === "medium" ? 1 : 2;
}

test("a short reply renders a compact content-fit frame", () => {
	const turn = new AssistantTurn(SHORT);
	const budget = assistantFrameBudget(160);
	const geometry = turn.responseGeometry(160);
	assert.equal(geometry.widthClass, "compact");
	assert.ok(geometry.span < 80, `compact span ${geometry.span} stays small`);
	assert.ok(geometry.contentWidth < budget.available, "compact boxes are content-fit, not pane-wide");
	assert.ok(geometry.contentWidth >= DEFAULT_RESPONSE_POLICY.minCompactContent);
	assert.equal(
		geometry.span,
		geometry.contentWidth + DEFAULT_RESPONSE_POLICY.borderColumns + DEFAULT_RESPONSE_POLICY.paddingColumns,
	);
	assert.equal(geometry.frameRows, 3, "one content row plus two borders");
	const lines = plainLines(turn, 160);
	assert.equal(lines.length, 1 + geometry.frameRows, "BLOCK_GAP + heading + content + footer");
	assert.match(lines[1] ?? "", /┌─ assistant/);
	assert.match(lines.at(-1) ?? "", /└─+┘/);
});

test("long replies grow to the full usable chat width with natural height", () => {
	const turn = new AssistantTurn(LONG);
	const budget = assistantFrameBudget(160);
	const geometry = turn.responseGeometry(160);
	assert.equal(geometry.widthClass, "full");
	assert.equal(geometry.contentWidth, budget.available, "full boxes use every available text cell");
	assert.equal(geometry.span, budget.frameBudget, "the frame spans the whole allocation");
	assert.ok(geometry.frameRows > DEFAULT_RESPONSE_POLICY.mediumMaxRows + 2);
	// Height is the real rendered Markdown row count plus two borders.
	const reference = new Markdown(LONG, 0, 0, markdownTheme, { color: theme.text });
	assert.equal(geometry.frameRows, reference.render(budget.available).length + 2);
	const lines = plainLines(turn, 160);
	assert.equal(lines.length, 1 + geometry.frameRows, "every rendered row is emitted");
	assert.equal(lines.slice(2, -1).length, geometry.frameRows - 2, "no row is truncated away");
	for (const line of lines) assert.ok(visibleWidth(line) <= 160);
});

test("every emitted row fits widths 0,1,2,20,40,80,120,160,240 and the frame boundary", () => {
	const widths = [0, 1, 2, 20, 40, 80, 120, 160, 240, 3, 8, 15, 16];
	for (const text of ["", SHORT, LONG, MID, FENCE, TABLE]) {
		const turn = new AssistantTurn(text);
		for (const width of widths) {
			let lines: string[] = [];
			assert.doesNotThrow(() => {
				lines = turn.render(width);
			}, `render threw at width ${width}`);
			for (const line of lines) {
				assert.ok(
					visibleWidth(line) <= width,
					`width ${width} exceeded: ${JSON.stringify(stripTerminalSequences(line))}`,
				);
			}
		}
	}
});

test("streaming growth is monotonic and the final text stays exact", () => {
	const turn = new AssistantTurn("");
	const tokens = ["Short opener.", "\n\n"];
	for (let i = 0; i < 140; i++) tokens.push(`segment-${i} `);
	const seen: ResponseWidthClass[] = [];
	for (const token of tokens) {
		turn.append(token);
		const cls = turn.responseGeometry(160).widthClass;
		const previous = seen.at(-1);
		if (previous) {
			assert.ok(classRank(cls) >= classRank(previous), `class must not oscillate: ${previous} -> ${cls}`);
		}
		seen.push(cls);
	}
	assert.equal(seen[0], "compact", "short prefixes start compact");
	assert.ok(seen.includes("medium"), "the stream passes through the medium class");
	assert.equal(seen.at(-1), "full", "a long stream reaches the full width");
	assert.equal(turn.getText(), tokens.join(""), "appended text is exact");
	turn.setText("Short again.");
	assert.equal(turn.getText(), "Short again.", "canonical text replaces the streamed text exactly");
	assert.equal(
		turn.responseGeometry(160).widthClass,
		"compact",
		"setText resets the stream growth history",
	);
});

test("a pinned larger class survives a later remeasure that would fit compact", () => {
	let tall = true;
	const measure = {
		renderRows: (contentWidth: number) =>
			Array.from({ length: tall ? 20 : 1 }, () => "x".repeat(Math.min(contentWidth, 40))),
	};
	const first = chooseResponseGeometry(160, measure);
	assert.equal(first.widthClass, "full");
	tall = false;
	const second = chooseResponseGeometry(160, measure, { widthClass: first.widthClass, paneWidth: 160 });
	assert.equal(second.widthClass, "full", "no oscillation back to compact within one allocation");
});

test("code fences and tables prefer broader frames over compact", () => {
	const fence = new AssistantTurn(FENCE).responseGeometry(160);
	assert.notEqual(fence.widthClass, "compact", "a multiline fence is never squeezed compact");
	const table = new AssistantTurn(TABLE).responseGeometry(160);
	assert.notEqual(table.widthClass, "compact", "a table row is never squeezed compact");
	const singleLineFence = new AssistantTurn("```js\nconst x = 1;\n```").responseGeometry(160);
	assert.equal(singleLineFence.widthClass, "compact", "a one-line fence does not force a wide frame");
	const inline = new AssistantTurn("Use `npm run check` here.").responseGeometry(160);
	assert.equal(inline.widthClass, "compact");
});

test("width follows measured rows, not byte counts", () => {
	const wrapped = "x".repeat(200);
	const broken = `${"a\n".repeat(99)}aa`;
	assert.equal(wrapped.length, broken.length, "same character count, different rendered shape");
	const compact = new AssistantTurn(wrapped).responseGeometry(160);
	const full = new AssistantTurn(broken).responseGeometry(160);
	assert.equal(compact.widthClass, "compact");
	assert.equal(full.widthClass, "full");
	assert.equal(full.contentWidth, assistantFrameBudget(160).available);
});

test("a wide chat pane uses the newly available width instead of a stale cap", () => {
	const turn = new AssistantTurn(LONG);
	const at160 = turn.responseGeometry(160);
	const at240 = turn.responseGeometry(240);
	assert.equal(at160.contentWidth, assistantFrameBudget(160).available);
	assert.equal(at240.contentWidth, assistantFrameBudget(240).available);
	assert.ok(at240.contentWidth > at160.contentWidth, "growing the pane grows the box");
	assert.ok(at240.contentWidth > 110, "no stale 110-column cap survives");
	for (const line of turn.render(240)) assert.ok(visibleWidth(line) <= 240);
});

test("a real pane resize recomputes; resizing back restores the full width", () => {
	const longTurn = new AssistantTurn(LONG);
	assert.equal(longTurn.responseGeometry(160).contentWidth, assistantFrameBudget(160).available);
	assert.equal(longTurn.responseGeometry(80).contentWidth, assistantFrameBudget(80).available);
	assert.equal(
		longTurn.responseGeometry(160).contentWidth,
		assistantFrameBudget(160).available,
		"no stale narrow cap after resizing back",
	);

	const midTurn = new AssistantTurn(MID);
	assert.equal(midTurn.responseGeometry(160).widthClass, "medium");
	assert.equal(midTurn.responseGeometry(80).widthClass, "full", "a narrower allocation reflows into more rows");
	assert.equal(midTurn.responseGeometry(160).widthClass, "medium", "the class re-derives per allocation");
});

test("geometry is cached by content revision and pane width", () => {
	const turn = new AssistantTurn(SHORT);
	const first = turn.responseGeometry(160);
	assert.equal(turn.responseGeometry(160), first, "same revision and pane reuse the cached decision");
	assert.notEqual(turn.responseGeometry(80), first, "a pane change recomputes");
	turn.append(" more words");
	assert.notEqual(turn.responseGeometry(160), first, "appended content invalidates the cache");
});

test("the legacy assistant content width delegates to the frame budget (no 110 cap)", async () => {
	const { assistantPanelContentWidth } = await import("../src/components/ToolPanel.ts");
	assert.equal(assistantPanelContentWidth(240), assistantFrameBudget(240).available);
	assert.ok(assistantPanelContentWidth(240) > 110, "the old fixed cap is gone");
	assert.equal(assistantPanelContentWidth(0), 0);
});

test("refreshSkin re-resolves the frame without changing text or layout", async () => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const before = getSkin();
	try {
		const turn = new AssistantTurn(SHORT);
		const geometry = turn.responseGeometry(160);
		const first = plainLines(turn, 160);
		setSkin("tokyonight");
		turn.refreshSkin();
		assert.deepEqual(plainLines(turn, 160), first, "text and layout survive the skin switch");
		assert.equal(turn.responseGeometry(160), geometry, "geometry cache survives a re-skin");
	} finally {
		setSkin(before);
	}
});

test("sanitization runs before measurement: hostile control text stays inert", () => {
	const url = `https://example.com/${"x".repeat(300)}`;
	const hostile = `\x1b]8;;${url}\x07link\x1b]8;;\x07 and \x1b[2Jcleared\x1b[0m`;
	const turn = new AssistantTurn(hostile);
	const geometry = turn.responseGeometry(160);
	assert.equal(geometry.widthClass, "compact", "the long OSC payload is not measured as visible width");
	const joined = turn.render(160).join("\n");
	assert.ok(!joined.includes("\x1b]"), "OSC introducers never reach the frame");
	assert.ok(!joined.includes("\x1b[2J"), "unsafe CSI is stripped before rendering");
	assert.match(stripTerminalSequences(joined), /link/);
	assert.match(stripTerminalSequences(joined), /cleared/);
});
