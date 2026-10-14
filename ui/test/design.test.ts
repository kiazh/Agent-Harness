import assert from "node:assert/strict";
/* eslint-disable no-control-regex -- terminal control sequences are the test fixtures here. */
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { ICONS, TOOL_MARKERS } from "../src/design/icons.ts";
import { LIMITS, SPACING_COMPACT, SPACING_NORMAL } from "../src/design/tokens.ts";
import {
	classifyDiffLine,
	countRenderedLines,
	hasNoTerminalControls,
	sanitizeDisplay,
	shapePreview,
	truncateDisplay,
	truncateUtf8ByBytes,
	wrapDisplay,
} from "../src/design/text.ts";
import {
	REQUIRED_THEME_IDS,
	SKIN_NAMES,
	getSkin,
	isValidTheme,
	semanticPalette,
	setSkin,
	skinBase,
} from "../src/design/theme.ts";

const LEGACY_IDS = [
	"default", "codex", "mono", "tokyonight", "catppuccin", "dracula", "gruvbox",
	"rosepine", "nord", "everforest", "matrix", "cobalt2", "ayu", "nightowl",
	"flexoki", "onedark", "monokai", "github", "solarized", "kanagawa", "palenight",
	"one-dark", "catppuccin-frappe", "catppuccin-macchiato",
];

test("theme_registry_preserves_all_saved_ids", () => {
	for (const id of LEGACY_IDS) {
		assert.ok(SKIN_NAMES.includes(id), `missing saved theme: ${id}`);
		assert.equal(isValidTheme(id), true);
	}
	for (const id of REQUIRED_THEME_IDS) {
		assert.ok(SKIN_NAMES.includes(id), `missing required theme: ${id}`);
	}
	for (const id of ["opencode", "light", "linen", "lagoon", "afterglow"]) {
		assert.ok(SKIN_NAMES.includes(id), `missing added theme: ${id}`);
	}
	for (const name of SKIN_NAMES) {
		const base = skinBase(name);
		assert.ok(base, `${name} has a palette`);
		const roles = [base.accent, base.secondary, base.success, base.warning, base.error, base.info].map((h) =>
			h.toLowerCase(),
		);
		assert.equal(new Set(roles).size, roles.length, `${name} reuses a color across roles`);
		const sem = semanticPalette(name);
		assert.ok(sem, `${name} has a semantic palette`);
		for (const [slot, value] of Object.entries(sem)) {
			assert.match(value, /^#[0-9a-fA-F]{6}$/, `${name}.${slot} is a hex color`);
		}
	}
});

test("new calm palettes keep text and selection accents readable", () => {
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
	for (const name of ["linen", "lagoon", "afterglow"]) {
		const base = skinBase(name);
		assert.ok(base);
		for (const role of ["text", "muted", "dim", "accent", "secondary", "success", "warning", "error", "info"] as const) {
			assert.ok(contrast(base.neutral, base[role]) >= 4.5, `${name} ${role} contrast`);
		}
	}
});

test("legacy theme facade still resolves", async () => {
	const legacy = await import("../src/theme.ts");
	assert.deepEqual([...legacy.SKIN_NAMES].sort(), [...SKIN_NAMES].sort());
	const before = getSkin();
	try {
		legacy.setSkin("codex");
		assert.equal(legacy.getSkin(), "codex");
		assert.equal(getSkin(), "codex");
	} finally {
		legacy.setSkin(before);
	}
});

test("characterization: skin switching round-trips every id", () => {
	const before = getSkin();
	try {
		for (const name of SKIN_NAMES) {
			setSkin(name);
			assert.equal(getSkin(), name);
		}
		assert.throws(() => setSkin("nope"), /Unknown skin/);
	} finally {
		setSkin(before);
	}
});

test("safe_text_never_emits_terminal_controls", () => {
	const payloads = [
		"\x1b]0;evil title\x07visible",
		"\x1b]52;c;Y2xpcGJvYXJk\x07clipboard",
		"\x1b[2J\x1b[1;1Hcleared",
		"\x1b[3Jscrollback wiped",
		"\x1b]8;;http://evil.example\x07click me\x1b]8;;\x07",
		"line1\rline2",
		"ab\x08cd",
		"nul\x00byte",
		"\x1bMreverse index",
		"\x1b(0line drawing",
		"csi\x9b2Jc1",
		"quota \u0085 sep \u2028 para \u2029 end",
		"\x1b[38;5;123mindexed fg\x1b[0m",
		"\x1b[48;2;1;2;3mbg fill\x1b[49m",
	];
	for (const payload of payloads) {
		const safe = sanitizeDisplay(payload);
		assert.equal(hasNoTerminalControls(safe), true, `controls survive: ${JSON.stringify(safe)}`);
		assert.ok(!safe.includes("\r"), "CR never survives");
		assert.ok(!safe.includes("\x07"), "BEL never survives");
		assert.ok(!/\x1b\][\s\S]/.test(safe), "OSC never survives");
	}
	// Safe SGR color survives and stays composable.
	const red = sanitizeDisplay("\x1b[31mred\x1b[0m plain");
	assert.ok(red.includes("\x1b[31m"), "safe SGR foreground is preserved");
	assert.ok(hasNoTerminalControls(red), "safe SGR is accepted as control-free");
	// Cursor-movement CSI is stripped even when mixed with safe color.
	const mixed = sanitizeDisplay("\x1b[31mred\x1b[2K\x1b[1G plain");
	assert.ok(!mixed.includes("[2K") && !mixed.includes("[1G"), "cursor/clipping CSI stripped");
	assert.ok(mixed.includes("red") && mixed.includes("plain"), "visible text kept");
});

test("C1 controls never survive sanitization or validation; safe SGR does", () => {
	// C1 OSC (0x9D) ... ST (0x9C), bare ST, and C1 DCS (0x90) payload forms.
	const inputs = ["A\u009d0;attack\u009cB", "A\u009cB", "A\u0090payload\u009cB"];
	for (const input of inputs) {
		const safe = sanitizeDisplay(input);
		assert.ok(!/[\u0080-\u009f]/.test(safe), `C1 survives sanitize: ${JSON.stringify(safe)}`);
		assert.equal(hasNoTerminalControls(input), false, `raw C1 accepted: ${JSON.stringify(input)}`);
		assert.equal(hasNoTerminalControls(safe), true, `sanitized C1 text rejected: ${JSON.stringify(safe)}`);
	}
	// Sequence payloads are removed whole, not left as visible garbage.
	assert.equal(sanitizeDisplay("A\u009d0;attack\u009cB"), "AB");
	assert.equal(sanitizeDisplay("A\u0090payload\u009cB"), "AB");
	assert.equal(sanitizeDisplay("A\u009cB"), "AB");
	// Allow-listed SGR formatting keeps working in both ESC and C1 CSI forms.
	for (const sgr of ["\x1b[31mred\x1b[0m", "\x9b[31mred\x9b[0m"]) {
		const safe = sanitizeDisplay(sgr);
		assert.equal(safe, sgr, `safe SGR preserved: ${JSON.stringify(sgr)}`);
		assert.equal(hasNoTerminalControls(safe), true, `safe SGR accepted: ${JSON.stringify(sgr)}`);
	}
	// Unsafe C1 CSI (clear screen) is stripped and can never validate.
	const cleared = sanitizeDisplay("x\x9b2Jy");
	assert.ok(!/[\u0080-\u009f]/.test(cleared), "unsafe C1 CSI survives");
	assert.equal(hasNoTerminalControls("x\x9b2Jy"), false, "raw 8-bit CSI accepted");
});

test("unicode_rendering_stays_inside_width", () => {
	const samples = [
		"日本語テスト plains",
		"👩‍💻🔥 emoji zwj",
		"é combining e\\u0301",
		"/very/long/unbroken/path/that/keeps/going/without/any/breaks/at/all/0123456789",
		"\x1b[32mgreen 日本語 👩‍💻\x1b[0m tail",
		"a\tb\tc tabs",
	];
	for (const width of [0, 1, 2, 20, 40, 80, 120]) {
		for (const sample of samples) {
			const truncated = truncateDisplay(sample, width);
			assert.ok(visibleWidth(truncated) <= Math.max(0, width), `truncate exceeds ${width}: ${JSON.stringify(sample)}`);
			// wrapDisplay must never throw, even at pathological widths. A single
			// wide grapheme (CJK width 2) cannot fit a 1-cell budget; pi-tui
			// emits it unbroken rather than corrupting it, so the width bound
			// below applies at width >= 2 only.
			let wrapped: string[];
			assert.doesNotThrow(() => {
				wrapped = wrapDisplay(sample, width);
			});
			if (width >= 2) {
				for (const line of wrapped!) {
					assert.ok(visibleWidth(line) <= Math.max(1, width), `wrap exceeds ${width}`);
				}
			}
			const preview = shapePreview(sample, width, LIMITS.previewLinesDefault);
			if (width >= 2) {
				for (const line of preview.shown) {
					assert.ok(visibleWidth(line) <= Math.max(1, width));
				}
			}
			assert.equal(preview.fullRenderedLines, countRenderedLines(sample, width));
		}
	}
	// Combining sequence stays in one grapheme cell budget.
	const combined = "é";
	assert.ok(visibleWidth(combined) <= 2, "combining mark does not explode width");
	assert.ok(stripTerminalSequences(truncateDisplay("日本語テスト", 4)).length > 0, "CJK truncates without throwing");
});

test("diff classification matches legacy rendering rules", () => {
	assert.equal(classifyDiffLine("diff --git a/b"), "diff-header");
	assert.equal(classifyDiffLine("+++ b/file"), "file-meta");
	assert.equal(classifyDiffLine("--- a/file"), "file-meta");
	assert.equal(classifyDiffLine("@@ -1,3 +1,4 @@"), "hunk");
	assert.equal(classifyDiffLine("+added"), "add");
	assert.equal(classifyDiffLine("+"), "add");
	assert.equal(classifyDiffLine("-removed"), "del");
	assert.equal(classifyDiffLine("-"), "del");
	assert.equal(classifyDiffLine(" context"), "context");
	assert.equal(classifyDiffLine("+++not-a-meta"), "context");
});

test("preview shaping counts wrapping and truncates utf8 safely", () => {
	const text = ["line1", "line2", "line3", "line4", "line5", "line6"].join("\n");
	const shaped = shapePreview(text, 80, LIMITS.previewLinesDefault);
	assert.equal(shaped.shown.length, 4);
	assert.equal(shaped.omitted, 2);
	assert.equal(shaped.fullRenderedLines, 6);
	const wide = "日本語テスト line that will wrap at tiny widths";
	const narrow = shapePreview(wide, 10, 2);
	assert.ok(narrow.shown.length <= 2);
	assert.equal(narrow.fullRenderedLines, countRenderedLines(wide, 10));
	const { text: prefix, truncated } = truncateUtf8ByBytes("a👩‍💻b", 4);
	assert.equal(truncated, true);
	assert.ok(prefix.length > 0 && prefix.length < "a👩‍💻b".length, "code point not split");
	assert.equal(truncateUtf8ByBytes("abc", 10).truncated, false);
});

test("tokens and icons stay centralized", () => {
	assert.equal(LIMITS.previewLinesDefault, 4);
	assert.equal(LIMITS.previewLinesCompact, 2);
	assert.equal(LIMITS.outputRetainedCount, 8);
	assert.equal(LIMITS.outputMaxBytesPer, 128 * 1024);
	assert.equal(LIMITS.outputMaxBytesTotal, 1024 * 1024);
	assert.equal(SPACING_NORMAL.blockGapRows, 1);
	assert.equal(SPACING_COMPACT.blockGapRows, 0);
	assert.equal(TOOL_MARKERS.success, ICONS.success);
	assert.equal(TOOL_MARKERS.error, ICONS.error);
	assert.equal(TOOL_MARKERS.unknown, ICONS.unknown);
});

function luminance(hex: string): number {
	const r = parseInt(hex.slice(1, 3), 16) / 255;
	const g = parseInt(hex.slice(3, 5), 16) / 255;
	const b = parseInt(hex.slice(5, 7), 16) / 255;
	const linear = (c: number): number => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
	return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

test("H01 required themes are readable with distinct semantic roles", async () => {
	const { REQUIRED_THEME_IDS, semanticPalette } = await import("../src/design/theme.ts");
	for (const id of REQUIRED_THEME_IDS) {
		const palette = semanticPalette(id);
		assert.ok(palette, `${id} resolves`);
		for (const [slot, value] of Object.entries(palette!)) {
			assert.match(value, /^#[0-9a-fA-F]{6}$/, `${id}.${slot} is a hex color`);
		}
		assert.notEqual(
			palette!.background.toLowerCase(),
			palette!.text.toLowerCase(),
			`${id} keeps text distinct from its background`,
		);
	}
	const light = semanticPalette("light")!;
	assert.ok(luminance(light.background) > 0.5, "light uses genuinely light surfaces");
	assert.ok(luminance(light.text) < 0.3, "light keeps dark readable text");
	for (const dark of ["codex", "opencode", "mono", "tokyonight"]) {
		assert.ok(luminance(semanticPalette(dark)!.background) < 0.3, `${dark} stays dark`);
	}
	const mono = semanticPalette("mono")!;
	for (const [slot, value] of Object.entries(mono)) {
		const r = value.slice(1, 3);
		const g = value.slice(3, 5);
		const b = value.slice(5, 7);
		assert.equal(`${r}${g}${b}`.toLowerCase(), `${r}${r}${r}`.toLowerCase(), `mono.${slot} carries no hue`);
	}
});
