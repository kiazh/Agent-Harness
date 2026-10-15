// R4-022 + R4-023 (round-4): honesty pins for streamed tool outcomes and for
// untrusted text reaching the terminal through Menu/Picker/ToolTurn/ApprovalCard.
/* eslint-disable no-control-regex -- terminal control sequences are the subject matter here. */
//
// These are fake-terminal tests: components are rendered through the real
// TuiAltScreen onto a no-op terminal, and the captured screen rows must carry
// no live control bytes beyond the renderer's own SGR styling. Embedded \n in
// untrusted content must become separate physical rows, never a break inside
// one screen row.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	Container,
	stripTerminalSequences,
	TuiAltScreen,
	type Component,
	type Terminal,
} from "@earendil-works/pi-tui";
import { ApprovalCard } from "../src/components/ApprovalCard.ts";
import { Menu, type MenuSectionItem } from "../src/components/Menu.ts";
import { Picker, ToolTurn } from "../src/components.ts";
import type { ActivitySource } from "../src/design/motion.ts";
import type { GatewayEvent } from "../src/protocol.ts";
import { Transcript } from "../src/transcript.ts";

class FakeSpinner implements Component {
	running = false;
	start(): void {
		this.running = true;
	}
	stop(): void {
		this.running = false;
	}
	render(): string[] {
		return ["(spinner)"];
	}
	invalidate(): void {}
}

const ev = (e: Record<string, unknown>) => ({ sessionId: "s", turnId: "t", ...e }) as GatewayEvent;
const staticActivity: ActivitySource = { frame: (frames) => frames[0] ?? "" };

function plain(component: Component, width = 70): string {
	return component.render(width).map(stripTerminalSequences).join("\n");
}

/** Run one component through the real alt-screen renderer on a no-op terminal. */
function fakeScreen(component: Component, columns = 90, rows = 24): string[] {
	const listeners: Array<(data: string) => void> = [];
	const term: Terminal & { columns: number; rows: number } = {
		columns,
		rows,
		kittyProtocolActive: false,
		start(onInput) {
			listeners.push(onInput);
		},
		stop() {},
		async drainInput() {},
		write() {},
		moveBy() {},
		hideCursor() {},
		showCursor() {},
		clearLine() {},
		clearFromCursor() {},
		clearScreen() {},
		setTitle() {},
		setProgress() {},
	};
	const tui = new TuiAltScreen(term);
	tui.addChild(component);
	tui.start();
	tui.renderNow(true);
	return tui.getScreenLines();
}

// Renderer-owned SGR (colors, backgrounds, reset) and the alt-screen
// renderer's own empty OSC-8 hyperlink close are trusted styling; every other
// control byte -- including \n inside a single row -- is a leak. A hostile
// OSC-8 payload keeps its non-empty URL body and is still caught.
const TRUSTED_SEQUENCES_RE = /\x1b\[[0-9;]*m|\x1b\]8;;\x07/g;
const RAW_CONTROL_RE = /[\x00-\x1f\x7f-\u009f\u2028\u2029]/;

function assertRowsClean(rows: string[], where: string): void {
	for (const row of rows) {
		const stripped = row.replace(TRUSTED_SEQUENCES_RE, "");
		assert.doesNotMatch(
			stripped,
			RAW_CONTROL_RE,
			`${where}: control byte reached a rendered row: ${JSON.stringify(row)}`,
		);
	}
}

// ─── hostile payload corpus ──────────────────────────────────────────────────

const ESC = "\x1b";
const OSC_TITLE = `${ESC}]0;evil title\x07`;
const OSC52 = `${ESC}]52;c;Y2xpcGJvYXJk\x07`;
const CLEAR = `${ESC}[2J${ESC}[1;1H`;
const C1_OSC = "\u009d0;c1-osc\u009c";
const SEP = "\u2028";
const BIDI = "\u202e";
const HOSTILE = `${OSC52}${CLEAR}${C1_OSC}${SEP}sweep${OSC_TITLE}\tTabbed${BIDI}`;

// ─── R4-022: denied outcomes render as denied, never as success ──────────────

test("R4-022: a streamed denied tool renders the denied state, not the success check", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(ev({ type: "tool.start", id: "d1", name: "terminal", args: { command: "rm -rf /tmp/x" } }));
	transcript.apply(
		ev({
			type: "tool.complete",
			id: "d1",
			name: "terminal",
			result: "Permission denied: approval denied. The action was NOT executed.",
			isError: false,
			outcome: "denied",
		}),
	);
	const out = plain(view);
	assert.match(out, /⊘ terminal/);
	assert.match(out, /denied; not executed/);
	assert.doesNotMatch(out, /✔ terminal/);
	assert.doesNotMatch(out, /✖ terminal/);
	assertRowsClean(view.render(70), "denied tool card");
});

test("R4-022: typed outcomes keep a consistent mapping (paused, failed, unknown, legacy)", () => {
	const cases: Array<{ outcome?: string; isError: boolean; marker: string; status?: RegExp }> = [
		{ outcome: "paused", isError: false, marker: "○ terminal", status: /awaiting approval/ },
		{ outcome: "failed", isError: true, marker: "✖ terminal" },
		{ outcome: "outcome_unknown", isError: false, marker: "? terminal", status: /completion not reported/ },
		{ isError: false, marker: "✔ terminal" },
		{ isError: true, marker: "✖ terminal" },
	];
	for (const { outcome, isError, marker, status } of cases) {
		const view = new Container();
		const transcript = new Transcript(view, new FakeSpinner());
		transcript.apply(ev({ type: "tool.start", id: "t1", name: "terminal", args: {} }));
		transcript.apply(
			ev({
				type: "tool.complete",
				id: "t1",
				name: "terminal",
				result: "Permission denied: nope. The action was NOT executed.",
				isError,
				...(outcome === undefined ? {} : { outcome }),
			}),
		);
		const out = plain(view);
		assert.ok(out.includes(marker), `outcome=${String(outcome)} isError=${isError}: ${out}`);
		if (status) assert.match(out, status);
		if (outcome !== undefined) {
			assert.doesNotMatch(out, /✔ terminal/, `outcome=${outcome} must not render success`);
		}
	}
});

// ─── R4-023: Menu/Picker sanitize payloads and split newline rows ────────────

test("R4-023: Menu sanitizes hostile payloads and splits embedded newlines into rows", () => {
	const menu = new Menu(`Ti${OSC_TITLE}tle`, [
		{ value: "v1", label: `one\ntwo` },
		{ value: "v2", label: `label ${HOSTILE}`, description: `desc ${HOSTILE}` },
	]);
	const rows = menu.render(80);

	assertRowsClean(rows, "Menu");
	// Title, filter, 2 label rows, 1 hostile item row, hint.
	assert.equal(rows.length, 6, JSON.stringify(rows.map(stripTerminalSequences)));
	const clean = rows.map((row) => stripTerminalSequences(row).trimEnd());
	assert.match(clean[0]!, /^Title \(2\/2\)/);
	assert.match(clean[2]!, /❯ one/);
	assert.equal(clean[3], "two");
	assert.match(clean[4]!, /label /);
	assert.ok(!rows.some((row) => row.includes("\n")), "no row may embed a newline");
	assert.ok(!plain(menu).includes("evil title"), "OSC payload text must not survive");
	assert.ok(!plain(menu).includes("\t"), "tabs are expanded, never emitted raw");

	// Description newlines split too (they attach to the last label row).
	const described = new Menu("T", [{ value: "v", label: "l", description: "d1\nd2" }]).render(80);
	assertRowsClean(described, "Menu/description");
	assert.equal(described.length, 1 + 1 + 2 + 1);
	const descRows = described.map((row) => stripTerminalSequences(row).trimEnd());
	assert.match(descRows[2]!, /^❯ l {2}d1$/);
	assert.equal(descRows[3], "d2");

	// Section headers split and stay dim chrome rows.
	const sectionItem: MenuSectionItem = {
		value: "__section__::s",
		label: `sec ${OSC_TITLE}head\nsec-tail`,
		section: true,
	};
	const section = new Menu("T", [sectionItem, { value: "v", label: "value" }]).render(80);
	assertRowsClean(section, "Menu/section");
	// title + filter + 2 section rows + item + hint
	assert.equal(section.length, 1 + 1 + 2 + 1 + 1, JSON.stringify(section.map(stripTerminalSequences)));
});

test("R4-023: Picker inherits Menu sanitization for titles, labels and descriptions", () => {
	const picker = new Picker(
		`Pick ${OSC_TITLE}`,
		[
			{ value: "a", label: `alpha\nbeta`, description: `d ${OSC52}` },
			{ value: "b", label: `b ${CLEAR}`, description: "plain" },
		],
		() => {},
		() => {},
	);
	const rows = picker.render(80);
	assertRowsClean(rows, "Picker");
	assert.equal(rows.length, 1 + 1 + 2 + 1 + 1, JSON.stringify(rows.map(stripTerminalSequences)));
	const clean = rows.map((row) => stripTerminalSequences(row));
	assert.match(clean[0]!, /^Pick/);
	assert.ok(clean.some((row) => row.includes("alpha")));
	assert.ok(clean.some((row) => row.trimEnd().startsWith("beta")), JSON.stringify(clean));
	assert.ok(!plain(picker).includes("Y2xpcGJvYXJk"), "OSC-52 payload text must not survive");
});

// ─── R4-023: single-row headers split embedded newlines ──────────────────────

test("R4-023: ToolTurn header splits newline-laden names/args into physical rows", () => {
	const hostileTurn = new ToolTurn(`name ${OSC_TITLE}head\nname-tail`, { path: `p ${OSC52}\np2` }, staticActivity);
	hostileTurn.settle({ text: "", backendTruncated: false, locallyTruncated: false }, "denied");
	const rows = hostileTurn.render(70);
	assertRowsClean(rows, "ToolTurn");
	assert.ok(!rows.some((row) => row.includes("\n")), "no row may embed a newline");
	const clean = rows.map((row) => stripTerminalSequences(row).trimEnd());
	assert.ok(clean.some((row) => row.includes("name head")), JSON.stringify(clean));
	assert.ok(clean.some((row) => row.includes("name-tail p")), JSON.stringify(clean));
	assert.ok(clean.some((row) => row.includes("p2")), JSON.stringify(clean));

	// Exact row count: margin + panel top + 2 header rows + denied status + bottom.
	const exact = new ToolTurn("n1\nn2", {}, staticActivity);
	exact.settle({ text: "", backendTruncated: false, locallyTruncated: false }, "denied");
	const exactRows = exact.render(70).map((row) => stripTerminalSequences(row).trimEnd());
	assert.equal(exactRows.length, 6, JSON.stringify(exactRows));
	assert.ok(exactRows[2]!.includes("⊘ n1"), exactRows[2]);
	assert.ok(exactRows[3]!.includes("n2"), exactRows[3]);
	assert.ok(exactRows[4]!.includes("denied; not executed"), exactRows[4]);
});

test("R4-023: ApprovalCard dock title splits newline-laden tool/target fields", () => {
	const card = new ApprovalCard(
		{
			requestId: `req ${OSC_TITLE}`,
			sessionId: "12345678-aaaa-bbbb-cccc-1234567890ab",
			originTurnId: "t1",
			operation: "process.exec",
			tool: `x ${OSC52}\nInjected`,
			target: `y ${CLEAR}\nTarget2`,
			argv: [],
			cwd: "/tmp",
			backend: "host",
			capabilities: [],
		},
		{ docked: true },
	);
	const rows = card.render(80);
	assertRowsClean(rows, "ApprovalCard dock");
	assert.ok(!rows.some((row) => row.includes("\n")), "no row may embed a newline");
	const clean = rows.map((row) => stripTerminalSequences(row).trimEnd());
	// border + 3 title rows (two embedded newlines) + 3 choices + hint
	assert.equal(clean.length, 8, JSON.stringify(clean));
	assert.match(clean[0]!, /^─/);
	assert.match(clean[1]!, /^Allow x {2}/);
	assert.match(clean[2]!, /^Injected · y$/);
	assert.match(clean[3]!, /^Target2\?$/);
	assert.match(clean[6]!, /Deny/);

	// A single-line title keeps the exact previous row shape (no width-math drift).
	const single = new ApprovalCard(
		{
			requestId: "r",
			sessionId: "12345678-aaaa-bbbb-cccc-1234567890ab",
			operation: "process.exec",
			tool: "git",
			target: "status",
			argv: [],
			cwd: "/tmp",
			backend: "host",
			capabilities: [],
		},
		{ docked: true },
	);
	const singleRows = single.render(80);
	assertRowsClean(singleRows, "ApprovalCard single-line");
	assert.equal(singleRows.length, 6);
	assert.match(stripTerminalSequences(singleRows[1]!), /^Allow git · status\?/);
});

// ─── fake-terminal capture: the same components through the alt-screen ───────

test("R4-023: hostile payloads render clean through the real alt-screen terminal", () => {
	const menuRows = fakeScreen(
		new Menu(`T ${OSC_TITLE}`, [{ value: "v", label: `plain\n${HOSTILE}label`, description: "d" }]),
	);
	assertRowsClean(menuRows, "alt-screen Menu");
	assert.ok(
		menuRows.some((row) => stripTerminalSequences(row).includes("label")),
		"visible label text survives sanitization",
	);
	assert.ok(
		!menuRows.some((row) => stripTerminalSequences(row).includes("evil title")),
		"OSC title payload text never reaches the screen",
	);

	const pickerRows = fakeScreen(
		new Picker(`P ${OSC52}`, [{ value: "a", label: `a\nb` }], () => {}, () => {}),
	);
	assertRowsClean(pickerRows, "alt-screen Picker");

	const toolCard = new ToolTurn(`run ${OSC52}`, { path: `x\ny` }, staticActivity);
	toolCard.settle({ text: "", backendTruncated: false, locallyTruncated: false }, "denied");
	const toolRows = fakeScreen(toolCard);
	assertRowsClean(toolRows, "alt-screen ToolTurn");

	const approvalRows = fakeScreen(
		new ApprovalCard(
			{
				requestId: `r ${OSC_TITLE}`,
				sessionId: "12345678-aaaa-bbbb-cccc-1234567890ab",
				operation: "process.exec",
				tool: `git ${C1_OSC}`,
				target: "status",
				argv: [],
				cwd: "/tmp",
				backend: "host",
				capabilities: [],
			},
			{ docked: true },
		),
	);
	assertRowsClean(approvalRows, "alt-screen ApprovalCard");
});
