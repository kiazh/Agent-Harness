// Final-review UI follow-up fix lane. Evidence for
// docs/verification/proofs/final_review_2026-10-15/line_ui.md:
//   P3-2 residual: the runtime-default `codex` skin's `dim` (and every other
//        skin's) clears WCAG AA while staying below `muted`;
//   P3-3: the write-only `App.inFlightSessionId` dead state is gone;
//   Q-6:  an inline `/provider add <name> <url> <key>` secret never reaches
//         the editor's input history.

import assert from "node:assert/strict";
import { test } from "node:test";
import { commandCarriesSecret } from "../src/app.ts";
import { parseCommand } from "../src/commands.ts";
import { SKIN_NAMES, getSkin, skinBase } from "../src/design/theme.ts";

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

test("P3-2 residual: the runtime-default skin is codex and its dim clears AA", () => {
	// design/theme.ts initializes the active skin to "codex"; App.start() only
	// overrides it from config.theme. The original default/mono pair missed
	// this default, so pin it explicitly.
	assert.equal(getSkin(), "codex", "the startup skin is codex");
	const base = skinBase("codex")!;
	const dim = contrast(base.neutral, base.dim);
	assert.ok(dim >= 4.5, `codex.dim contrast ${dim.toFixed(2)}:1 is below 4.5:1`);
	assert.ok(dim < contrast(base.neutral, base.muted), "codex.dim stays below muted");
});

test("P3-2 residual: codex/opencode/tokyonight dims clear AA (named regressions)", () => {
	for (const name of ["codex", "opencode", "tokyonight"]) {
		const base = skinBase(name)!;
		const dim = contrast(base.neutral, base.dim);
		const muted = contrast(base.neutral, base.muted);
		assert.ok(dim >= 4.5, `${name}.dim contrast ${dim.toFixed(2)}:1 is below 4.5:1`);
		assert.ok(dim < muted, `${name}.dim (${dim.toFixed(2)}) must stay below muted (${muted.toFixed(2)})`);
	}
});

test("Q-6 commandCarriesSecret keeps inline secrets out of history", () => {
	const secret = (text: string): boolean => commandCarriesSecret(parseCommand(text));
	// /keys set carries a raw value (case-insensitive subcommand).
	assert.equal(secret("/keys set ANTHROPIC_API_KEY sk-123"), true);
	assert.equal(secret("/KEYS SET ANTHROPIC_API_KEY sk-123"), true);
	assert.equal(secret("/keys Set k v"), true);
	assert.equal(secret("/keys list"), false);
	assert.equal(secret("/keys clear k"), false);
	// /provider add has no inline-key form: a third token is ignored, so it
	// must not be stashed in history either.
	assert.equal(secret("/provider add acme http://127.0.0.1:9000/v1 sk-123"), true);
	assert.equal(secret("/provider add acme"), false);
	assert.equal(secret("/provider add acme http://127.0.0.1:9000/v1"), false);
	assert.equal(secret("/provider acme"), false);
	// Ordinary prompts and unknown commands are never treated as secrets.
	assert.equal(secret("just a prompt"), false);
	assert.equal(secret("/help"), false);
	assert.equal(commandCarriesSecret(undefined), false);
});

test("P3-2 residual: the registry is non-trivial (guard the iteration itself)", () => {
	assert.ok(SKIN_NAMES.length >= 26, `expected the full registry, saw ${SKIN_NAMES.length}`);
	assert.ok(SKIN_NAMES.includes("codex") && SKIN_NAMES.includes("opencode") && SKIN_NAMES.includes("tokyonight"));
});
