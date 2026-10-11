/* eslint-disable no-control-regex -- inspecting actual native SGR colors. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { stripTerminalSequences } from "@earendil-works/pi-tui";

// Decode the actual emitted colors: a token-only assertion misses terminal
// quantization and fill resets, both of which can erase a dark frame visually.
function color(line: string, background = false): number[] {
	const match = line.match(new RegExp(`\\x1b\\[${background ? 48 : 38};(2|5);([0-9;]+)m`));
	assert.ok(match, "panel emits a color for its border and fill");
	const values = match[2]!.split(";").map(Number);
	if (match[1] === "2") return values;
	const index = values[0]!;
	if (index >= 232) return Array(3).fill(8 + (index - 232) * 10);
	assert.ok(index >= 16);
	const n = index - 16;
	const cube = [0, 95, 135, 175, 215, 255];
	return [cube[Math.floor(n / 36)]!, cube[Math.floor(n / 6) % 6]!, cube[n % 6]!];
}

function luminance(rgb: number[]): number {
	const linear = rgb.map(v => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
	return linear[0]! * 0.2126 + linear[1]! * 0.7152 + linear[2]! * 0.0722;
}

for (const trueColor of [true, false]) {
	test(`native output panels keep visible rectangular edges, fill, and margins (${trueColor ? "truecolor" : "256color"})`, () => {
		const child = spawnSync(process.execPath, ["--input-type=module", "-e", `
			import {referenceFixture} from "./test/support/reference.ts";
			const f = await referenceFixture();
			try { console.log(JSON.stringify(f.tui.getScreenLines())); }
			finally { await f.app.exit(); }
		`], { cwd: new URL("..", import.meta.url), env: { ...process.env, PI_TRUE_COLOR: trueColor ? "1" : "0", AH_UI_REDUCED_MOTION: "1" }, encoding: "utf8", timeout: 10000 });
		assert.equal(child.status, 0, child.stderr);
		const screen = JSON.parse(child.stdout) as string[];
		const plain = screen.map(stripTerminalSequences);
		const resultRow = plain.findIndex(row => row.includes("Homebrew fallback"));
		assert.ok(resultRow >= 0);
		const topRow = resultRow - 2;
		assert.match(plain[topRow]!, /^ {3}┌─+┐\s*$/);
		assert.match(plain[resultRow]!, /^ {3}│ {3}Example inspection.* │\s*$/);
		assert.match(plain[resultRow + 1]!, /^ {3}└─+┘\s*$/);
		assert.equal(plain[topRow - 1]?.trim(), "", "space outside the panel");
		const edge = luminance(color(screen[topRow]!));
		const fill = luminance(color(screen[resultRow]!, true));
		const backdrop = luminance([11, 11, 12]);
		assert.ok((edge + 0.05) / (fill + 0.05) >= 2.5, "rectangle remains visible against its inset surface");
		assert.ok((fill + 0.05) / (backdrop + 0.05) >= 1.1, "panel fill differs visibly from the transcript background");
		const evidence = plain.findIndex(row => row.includes("needs approval"));
		assert.match(plain[evidence - 1]!, /^ {3}┌─+┐\s*$/);
		assert.deepEqual(color(screen[evidence - 1]!), color(screen[topRow]!), "approval and tool frames share the same palette");
	});
}
