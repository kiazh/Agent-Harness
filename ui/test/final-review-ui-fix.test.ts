// Final-review UI fix lane. Evidence for
// docs/verification/proofs/final_review_2026-10-15/line_ui.md:
//   P3-1 OutputViewer header sanitizes an untrusted provider tool name/id;
//   P3-2 default/mono `dim` tokens clear WCAG AA and stay below `muted`;
//   Q-3  OAuthProgress "c" cancel branch returns.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { OutputStore, OutputViewer } from "../src/components/OutputViewer.ts";
import { OAuthProgress } from "../src/components/OAuthProgress.ts";
import { skinBase } from "../src/design/theme.ts";

test("P3-1 retained-output header sanitizes a hostile tool name and id", () => {
	const toolId = "tool\x1b[2Jid";
	const hostileName = "evil\x1b[2J\x1b]0;pwn\x07\nname";
	const store = new OutputStore();
	store.put("s", toolId, hostileName, "body", false);
	const viewer = new OutputViewer(store.get("s", toolId)!);
	const rows = viewer.render(80);
	const header = rows[0]!;
	// No ESC/CSI/OSC and no raw newline may reach the terminal from the header.
	assert.ok(!header.includes("\x1b[2J"), "screen-clear CSI stripped");
	assert.ok(!header.includes("\x1b]0;"), "OSC title stripped");
	assert.ok(!rows.some((row) => row.includes("\n")), "no rendered row carries a raw newline");
	const shown = stripTerminalSequences(header);
	assert.ok(shown.includes("evil name"), "visible name survives sanitization, flattened");
	assert.ok(shown.includes("(tool)"), "sanitized tool-id prefix renders");
	for (const row of rows) assert.ok(visibleWidth(row) <= 80, "header stays inside the width");
});

test("P3-1 viewer header keeps a well-behaved name verbatim (no behavior drift)", () => {
	const store = new OutputStore();
	store.put("s", "abcdef0123456789", "read_file", "ok", false);
	const viewer = new OutputViewer(store.get("s", "abcdef0123456789")!);
	const header = stripTerminalSequences(viewer.render(80)[0]!);
	assert.match(header, /Output: read_file \(abcdef01\)/);
});

test("P3-2 default and mono dim clear WCAG AA and stay below muted", () => {
	const luminance = (hex: string): number => {
		const [r, g, b] = hex.slice(1).match(/../g)!.map((pair) => {
			const channel = Number.parseInt(pair, 16) / 255;
			return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
		});
		return r! * 0.2126 + g! * 0.7152 + b! * 0.0722;
	};
	const contrast = (a: string, b: string): number => {
		const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
		return (values[0]! + 0.05) / (values[1]! + 0.05);
	};
	for (const name of ["default", "mono"]) {
		const base = skinBase(name)!;
		const dim = contrast(base.neutral, base.dim);
		assert.ok(dim >= 4.5, `${name}.dim contrast ${dim.toFixed(2)}:1 is below 4.5:1`);
		assert.ok(dim < contrast(base.neutral, base.muted), `${name}.dim must stay below muted`);
	}
});

test("Q-3 OAuthProgress 'c' cancels exactly once", () => {
	let cancelled = 0;
	const progress = new OAuthProgress("t", "s", { onOpen: () => {}, onCancel: () => { cancelled++; } });
	progress.handleInput("c");
	assert.equal(cancelled, 1, "the c key cancels");
	assert.equal(progress.isCancelled(), true);
	// Already cancelled: further input is ignored (no second cancel).
	progress.handleInput("c");
	progress.handleInput("\x1b[B");
	assert.equal(cancelled, 1);
});
