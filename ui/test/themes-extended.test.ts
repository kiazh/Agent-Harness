// Extended skin registry (2026-10-15): 12 new skins layered on the prior 29.
// Guards the same invariants the design tests enforce — text/muted/dim clear
// WCAG AA on their own surface with `dim` below `muted`, every accent role
// clears AA and stays distinct, selected rows clear AA — and renders each skin
// through the existing ThemeBackground fake-terminal harness so the surface
// paint resolves per skin.

import assert from "node:assert/strict";
import { test } from "node:test";
import { backgroundAnsi, getTerminalColorMode, parseColor } from "@earendil-works/pi-tui";
import { ThemeBackground } from "../src/design/terminal.ts";
import {
	SKIN_NAMES,
	getSkin,
	isValidTheme,
	selectionFill,
	selectionInk,
	semanticPalette,
	setSkin,
	skinArt,
	skinBase,
	skinDescription,
} from "../src/design/theme.ts";

const NEW_SKINS = [
	"aurora",
	"ember",
	"forest",
	"glacier",
	"neon-grid",
	"paper",
	"sepia-crt",
	"blueprint",
	"rose-quartz",
	"high-contrast-dark",
	"high-contrast-light",
	"sunset-drive",
] as const;

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

test("new skins register with a description, art and a full hex palette", () => {
	for (const name of NEW_SKINS) {
		assert.ok(SKIN_NAMES.includes(name), `${name} is registered`);
		assert.equal(isValidTheme(name), true, `${name} resolves`);
		assert.ok(skinDescription(name).length > 0, `${name} has a description`);
		assert.ok(skinArt(name).length > 0, `${name} has art`);
		const sem = semanticPalette(name);
		assert.ok(sem, `${name} has a semantic palette`);
		for (const [slot, value] of Object.entries(sem)) {
			assert.match(value, /^#[0-9a-fA-F]{6}$/, `${name}.${slot} is a hex color`);
		}
	}
});

test("new skins keep text, muted and dim readable with dim below muted", () => {
	for (const name of NEW_SKINS) {
		const base = skinBase(name)!;
		const text = contrast(base.neutral, base.text);
		const muted = contrast(base.neutral, base.muted);
		const dim = contrast(base.neutral, base.dim);
		assert.ok(text >= 7, `${name}.text contrast ${text.toFixed(2)}:1 is below 7:1`);
		assert.ok(muted >= 4.5, `${name}.muted contrast ${muted.toFixed(2)}:1 is below 4.5:1`);
		assert.ok(dim >= 4.5, `${name}.dim contrast ${dim.toFixed(2)}:1 is below 4.5:1`);
		assert.ok(dim < muted, `${name}.dim (${dim.toFixed(2)}) must stay below muted (${muted.toFixed(2)})`);
	}
});

test("new skins keep every accent role >= 4.5:1 and distinct", () => {
	const roles = ["accent", "secondary", "success", "warning", "error", "info"] as const;
	for (const name of NEW_SKINS) {
		const base = skinBase(name)!;
		const values = roles.map((role) => base[role].toLowerCase());
		assert.equal(new Set(values).size, values.length, `${name} reuses a color across roles`);
		for (const role of roles) {
			const ratio = contrast(base.neutral, base[role]);
			assert.ok(ratio >= 4.5, `${name}.${role} contrast ${ratio.toFixed(2)}:1 is below 4.5:1`);
		}
	}
});

test("new skins keep selected rows >= 4.5:1", () => {
	for (const name of NEW_SKINS) {
		const ratio = contrast(selectionInk(name), selectionFill(name));
		assert.ok(ratio >= 4.5, `${name} selection row contrast ${ratio.toFixed(2)}:1`);
	}
});

test("new skins paint a unique background through the terminal harness", () => {
	const previous = getSkin();
	try {
		const seen = new Map<string, string>();
		for (const name of NEW_SKINS) {
			setSkin(name);
			assert.equal(getSkin(), name);
			const painter = new ThemeBackground();
			const opened = painter.paint("\x1b[?1049h\x1b[2J");
			// eslint-disable-next-line no-control-regex
			const bg = opened.match(/\x1b\[48;[^m]+m/)?.[0];
			const source = semanticPalette(name)!.background;
			const expected = backgroundAnsi(parseColor(source), getTerminalColorMode());
			assert.equal(bg, expected, `${name} paints its own surface`);
			// Distinctness is asserted on the source background: the rendered
			// ANSI code depends on the detected terminal color mode, and in a
			// lower color depth two surfaces can legitimately quantize to the
			// same code (CI did; truecolor never does for these 12).
			assert.ok(!seen.has(source), `${name} surface is distinct`);
			seen.set(source, name);
			// A nested fill reset inside the frame restores this skin's surface.
			const frame = painter.paint("\x1b[48;2;1;2;3m row \x1b[49m");
			assert.ok(frame.includes(expected), `${name} restores its surface after a nested reset`);
		}
		assert.equal(seen.size, NEW_SKINS.length);
	} finally {
		setSkin(previous);
	}
});

// The full registry intentionally keeps two alias families: `opencode` and
// `codex` share the neutral surface (#0b0b0c) as the brand/default pair, and
// `onedark` is the unhyphenated spelling of `one-dark` (#282c34) kept for
// saved theme ids. Those pairs are the only allowed surface collisions; every
// skin's full palette must otherwise be unique so an accidental duplicate
// cannot land via a future theme wave.
const ALLOWED_SURFACE_ALIASES: ReadonlyArray<readonly string[]> = [
	["codex", "opencode"],
	["one-dark", "onedark"],
];

test("registry keeps every full palette and every surface distinct (aliases explicit)", () => {
	const palettes = new Map<string, string>();
	const surfaces = new Map<string, string[]>();
	for (const name of SKIN_NAMES) {
		const base = skinBase(name)!;
		const palette = JSON.stringify(base);
		const clash = palettes.get(palette);
		assert.equal(clash, undefined, `${name} duplicates the full palette of ${clash}`);
		palettes.set(palette, name);
		const surface = base.neutral.toLowerCase();
		if (!surfaces.has(surface)) surfaces.set(surface, []);
		surfaces.get(surface)!.push(name);
	}
	for (const [surface, names] of surfaces) {
		if (names.length === 1) continue;
		const allowed = ALLOWED_SURFACE_ALIASES.some(
			(pair) => names.length === pair.length && names.every((name) => pair.includes(name)),
		);
		assert.ok(allowed, `surface ${surface} is shared by non-alias skins: ${names.join(", ")}`);
	}
});
