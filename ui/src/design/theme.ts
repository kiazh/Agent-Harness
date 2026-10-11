// AgentHarness skins: dark theme palettes ported from Codex's theme set,
// plus house skins. Foreground tokens (text through info) follow Codex's
// values; card backgrounds and borders are derived from each theme's
// background + ink with mixColors, the way Codex derives subtle surface
// elevations. The composer fill stays a neutral lift (Codex
// backgroundElement); selection rows paint accent bg with background fg.
//
// Style functions resolve the *current* skin lazily, so `setSkin()` recolors
// the whole UI without a restart. The choice persists via `config.theme`.

import {
	type Color,
	type EditorTheme,
	type MarkdownTheme,
	type SelectListTheme,
	backgroundAnsi,
	foregroundAnsi,
	getTerminalColorMode,
	mixColors,
	parseColor,
	styleTextWithAnsi,
	type TextAttributes,
	visibleWidth,
} from "@earendil-works/pi-tui";
import { CODEX_SEMANTIC_START, type SemanticPalette } from "./tokens.ts";

export type { SemanticPalette };

export type PaletteName =
	| "text"
	| "muted"
	| "dim"
	| "accent"
	| "secondary"
	| "success"
	| "warning"
	| "error"
	| "info"
	| "border"
	| "userBg"
	| "assistantBg"
	| "toolBg"
	| "toolOkBg"
	| "toolErrBg";

type BaseSlot =
	| "text"
	| "muted"
	| "dim"
	| "accent"
	| "secondary"
	| "success"
	| "warning"
	| "error"
	| "info"
	| "neutral";

interface SkinDef {
	description: string;
	art: string[];
	base: Record<BaseSlot, string>;
}

const SKINS: Record<string, SkinDef> = {
	default: {
		description: "opencode theme (dark) — the house default",
		art: ["  ◆──────◆", " │ AGENT  │", " │ HARNESS│", "  ◆──────◆"],
		base: {
			text: "#eeeeee",
			muted: "#808080",
			dim: "#5f5f5f",
			accent: "#fab283",
			secondary: "#9d7cd8",
			success: "#7fd88f",
			warning: "#f5a742",
			error: "#e06c75",
			info: "#56b6c2",
			neutral: "#0a0a0a",
		},
	},
	tokyonight: {
		description: "Tokyonight (dark)",
		art: ["   .--.  ", "  ( moon)", "   `--'  ", "  *  . * "],
		base: {
			text: "#c0caf5",
			muted: "#7a80a6",
			dim: "#565f89",
			accent: "#7aa2f7",
			secondary: "#bb9af7",
			success: "#9ece6a",
			warning: "#e0af68",
			error: "#f7768e",
			info: "#7dcfff",
			neutral: "#1a1b26",
		},
	},
	catppuccin: {
		description: "Catppuccin Mocha (dark)",
		art: ["  (=^.^=)", "  │mocha│", "  │latte│", "  └─────┘"],
		base: {
			text: "#cdd6f4",
			muted: "#7f849c",
			dim: "#585b70",
			accent: "#b4befe",
			secondary: "#f38ba8",
			success: "#a6d189",
			warning: "#f9e2af",
			error: "#e78284",
			info: "#89dceb",
			neutral: "#1e1e2e",
		},
	},
	dracula: {
		description: "Dracula (dark)",
		art: ["  ◇────◇", "  │DRAC │", "  │ ULA │", "  ◇────◇"],
		base: {
			text: "#f8f8f2",
			muted: "#a8abbf",
			dim: "#6272a4",
			accent: "#bd93f9",
			secondary: "#ff79c6",
			success: "#50fa7b",
			warning: "#ffb86c",
			error: "#ff5555",
			info: "#8be9fd",
			neutral: "#282a36",
		},
	},
	gruvbox: {
		description: "Gruvbox (dark)",
		art: ["  ┌─────┐", "  │GRUV │", "  │ BOX │", "  └─────┘"],
		base: {
			text: "#ebdbb2",
			muted: "#a89a85",
			dim: "#928374",
			accent: "#83a598",
			secondary: "#d3869b",
			success: "#b8bb26",
			warning: "#fabd2f",
			error: "#fb4934",
			info: "#8ec07c",
			neutral: "#282828",
		},
	},
	rosepine: {
		description: "Rosé Pine (dark)",
		art: ["  ♡────♡", "  │ROSE │", "  │ PINE│", "  ♡────♡"],
		base: {
			text: "#e0def4",
			muted: "#908caa",
			dim: "#6e6a86",
			accent: "#c4a7e7",
			secondary: "#ebbcba",
			success: "#9ccfd8",
			warning: "#f6c177",
			error: "#eb6f92",
			info: "#56949f",
			neutral: "#191724",
		},
	},
	nord: {
		description: "Nord (dark)",
		art: ["  ▲────▲", "  │NORD │", "  │FROST│", "  ▲────▲"],
		base: {
			text: "#e5e9f0",
			muted: "#7e8aa3",
			dim: "#616e88",
			accent: "#88c0d0",
			secondary: "#d57780",
			success: "#a3be8c",
			warning: "#ebcb8b",
			error: "#bf616a",
			info: "#81a1c1",
			neutral: "#2e3440",
		},
	},
	everforest: {
		description: "Everforest (dark)",
		art: ["    ▲    ", "   ▲▲▲   ", "  ▲▲▲▲▲  ", "    │    "],
		base: {
			text: "#d3c6aa",
			muted: "#7a8478",
			dim: "#585f57",
			accent: "#7fbbb3",
			secondary: "#d699b6",
			success: "#a7c080",
			warning: "#dbbc7f",
			error: "#e67e80",
			info: "#83c092",
			neutral: "#2d353b",
		},
	},
	matrix: {
		description: "Matrix (dark)",
		art: ["  0101010", "  MATRIX ", "  1010101", "  HACKER "],
		base: {
			text: "#62ff94",
			muted: "#8ca391",
			dim: "#3d4a40",
			accent: "#2eff6a",
			secondary: "#c770ff",
			success: "#62ff94",
			warning: "#e6ff57",
			error: "#ff5555",
			info: "#30b3ff",
			neutral: "#0a0e0a",
		},
	},
	codex: {
		description: "Codex-style quiet chrome — pale blue accents and neutral surfaces",
		art: ["  ┌─────┐", "  │ >_  │", "  │ AH  │", "  └─────┘"],
		base: {
			text: CODEX_SEMANTIC_START.text,
			muted: CODEX_SEMANTIC_START.muted,
			dim: CODEX_SEMANTIC_START.faint,
			accent: CODEX_SEMANTIC_START.accent,
			secondary: CODEX_SEMANTIC_START.accentSoft,
			success: CODEX_SEMANTIC_START.success,
			warning: CODEX_SEMANTIC_START.warning,
			error: CODEX_SEMANTIC_START.danger,
			info: "#7dcfff",
			neutral: CODEX_SEMANTIC_START.background,
		},
	},
	"catppuccin-frappe": {
		description: "Catppuccin Frappé (dark)",
		art: ["  ƒ────ƒ", "  │FRAP │", "  │PUCC │", "  ƒ────ƒ"],
		base: {
			text: "#c6d0f5",
			muted: "#b5bfe2",
			dim: "#949cb8",
			accent: "#8da4e2",
			secondary: "#f4b8e4",
			success: "#a6d189",
			warning: "#e5c890",
			error: "#e78284",
			info: "#81c8be",
			neutral: "#303446",
		},
	},
	"catppuccin-macchiato": {
		description: "Catppuccin Macchiato (dark)",
		art: ["  µ────µ", "  │MACC │", "  │HIATO│", "  µ────µ"],
		base: {
			text: "#cad3f5",
			muted: "#b8c0e0",
			dim: "#939ab7",
			accent: "#8aadf4",
			secondary: "#f5bde6",
			success: "#a6da95",
			warning: "#eed49f",
			error: "#ed8796",
			info: "#8bd5ca",
			neutral: "#24273a",
		},
	},
	cobalt2: {
		description: "Cobalt2 (dark)",
		art: ["  ◆────◆", "  │COBA │", "  │ LT2 │", "  ◆────◆"],
		base: {
			text: "#ffffff",
			muted: "#adb7c9",
			dim: "#2d5a7b",
			accent: "#0088ff",
			secondary: "#2affdf",
			success: "#9eff80",
			warning: "#ffc600",
			error: "#ff0088",
			info: "#ff9d00",
			neutral: "#193549",
		},
	},
	ayu: {
		description: "Ayu Dark",
		art: ["  ○────○", "  │AYU  │", "  │DARK │", "  ○────○"],
		base: {
			text: "#d6dae0",
			muted: "#8f96a3",
			dim: "#5a6673",
			accent: "#3fb7e3",
			secondary: "#f2856f",
			success: "#78d05c",
			warning: "#e4a75c",
			error: "#f58572",
			info: "#66c6f1",
			neutral: "#0f1419",
		},
	},
	nightowl: {
		description: "Night Owl (dark)",
		art: ["  ◑────◑", "  │NIGHT│", "  │ OWL │", "  ◑────◑"],
		base: {
			text: "#d6deeb",
			muted: "#8fa0ae",
			dim: "#637777",
			accent: "#82aaff",
			secondary: "#c792ea",
			success: "#c5e478",
			warning: "#ecc48d",
			error: "#ef5350",
			info: "#7fdbca",
			neutral: "#011627",
		},
	},
	"one-dark": {
		description: "One Dark",
		art: ["  ◇────◇", "  │ONE  │", "  │DARK │", "  ◇────◇"],
		base: {
			text: "#abb2bf",
			muted: "#828997",
			dim: "#5c6370",
			accent: "#61afef",
			secondary: "#c678dd",
			success: "#98c379",
			warning: "#e5c07b",
			error: "#e06c75",
			info: "#d19a66",
			neutral: "#282c34",
		},
	},
	flexoki: {
		description: "Flexoki Dark",
		art: ["  ≈────≈", "  │FLEX │", "  │ OKI │", "  ≈────≈"],
		base: {
			text: "#cecdc3",
			muted: "#6f6e69",
			dim: "#575652",
			accent: "#da702c",
			secondary: "#8b7ec8",
			success: "#879a39",
			warning: "#d0a215",
			error: "#d14d41",
			info: "#3aa99f",
			neutral: "#100f0f",
		},
	},
	mono: {
		description: "High-contrast greys, no hue (accessible) — house skin",
		art: ["  ┌───┐  ", "  │ A │  ", "  │ H │  ", "  └───┘  "],
		base: {
			text: "#e8e8e8",
			muted: "#a0a0a0",
			dim: "#6e6e6e",
			accent: "#ffffff",
			secondary: "#c0c0c0",
			success: "#d0d0d0",
			warning: "#b8b8b8",
			error: "#909090",
			info: "#c8c8c8",
			neutral: "#101010",
		},
	},
	onedark: {
		description: "One Dark (dark)",
		art: ["  ○────○", "  │ONE  │", "  │DARK │", "  ○────○"],
		base: {
			text: "#abb2bf",
			muted: "#7a8291",
			dim: "#5c6370",
			accent: "#61afef",
			secondary: "#c678dd",
			success: "#98c379",
			warning: "#e5c07b",
			error: "#e06c75",
			info: "#56b6c2",
			neutral: "#282c34",
		},
	},
	monokai: {
		description: "Monokai (dark)",
		art: ["  □────□", "  │MONO │", "  │ KAI │", "  □────□"],
		base: {
			text: "#f8f8f2",
			muted: "#9a9178",
			dim: "#75715e",
			accent: "#66d9ef",
			secondary: "#ae81ff",
			success: "#a6e22e",
			warning: "#e6db74",
			error: "#f92672",
			info: "#fd971f",
			neutral: "#272822",
		},
	},
	github: {
		description: "GitHub Dark",
		art: ["  ●────●", "  │HUB  │", "  │ GIT │", "  ●────●"],
		base: {
			text: "#e6edf3",
			muted: "#7d8590",
			dim: "#6e7681",
			accent: "#2f81f7",
			secondary: "#ab7df8",
			success: "#3fb950",
			warning: "#d29922",
			error: "#f85149",
			info: "#39c5cf",
			neutral: "#0d1117",
		},
	},
	solarized: {
		description: "Solarized Dark",
		art: ["  ◐────◐", "  │SOLAR│", "  │IZED │", "  ◐────◐"],
		base: {
			text: "#93a1a1",
			muted: "#839496",
			dim: "#586e75",
			accent: "#268bd2",
			secondary: "#d33682",
			success: "#859900",
			warning: "#b58900",
			error: "#dc322f",
			info: "#2aa198",
			neutral: "#002b36",
		},
	},
	kanagawa: {
		description: "Kanagawa Wave (dark)",
		art: ["  ≈────≈", "  │KANA │", "  │GAWA │", "  ≈────≈"],
		base: {
			text: "#dcd7ba",
			muted: "#72788e",
			dim: "#545864",
			accent: "#7e9cd8",
			secondary: "#957fb8",
			success: "#98bb6c",
			warning: "#e6c384",
			error: "#e46876",
			info: "#7fb4ca",
			neutral: "#1f1f28",
		},
	},
	palenight: {
		description: "Palenight (dark)",
		art: ["  ◑────◑", "  │PALE │", "  │NIGHT│", "  ◑────◑"],
		base: {
			text: "#a6accd",
			muted: "#676e95",
			dim: "#464b6a",
			accent: "#82aaff",
			secondary: "#c792ea",
			success: "#c3e88d",
			warning: "#ffcb6b",
			error: "#f07178",
			info: "#89ddff",
			neutral: "#292d3e",
		},
	},
	opencode: {
		description: "Opencode (dark) — calm blue/indigo accent",
		art: ["  ┌─────┐", "  │OPEN │", "  │CODE │", "  └─────┘"],
		base: {
			text: "#e4e4e7",
			muted: "#8b8b94",
			dim: "#5d5d66",
			accent: "#7aa2f7",
			secondary: "#9d7cd8",
			success: "#9ece6a",
			warning: "#e0af68",
			error: "#f7768e",
			info: "#7dcfff",
			neutral: "#0b0b0c",
		},
	},
	light: {
		description: "Light — dark text on neutral light surfaces",
		art: ["  ┌─────┐", "  │LIGH │", "  │  T  │", "  └─────┘"],
		base: {
			text: "#1a1a1e",
			muted: "#565660",
			dim: "#8a8a94",
			accent: "#2f5fd0",
			secondary: "#6a4fc7",
			success: "#1a7f37",
			warning: "#9a6700",
			error: "#cf222e",
			info: "#1a7f8a",
			neutral: "#f4f4f5",
		},
	},
};

export const SKIN_NAMES: string[] = Object.keys(SKINS);

/** Required theme IDs (legacy set + opencode + light). */
export const REQUIRED_THEME_IDS: string[] = [
	"codex",
	"opencode",
	"mono",
	"tokyonight",
	"dracula",
	"gruvbox",
	"nord",
	"catppuccin",
	"solarized",
	"light",
];

/** Every saved/legacy ID plus required IDs must resolve. */
export function isValidTheme(name: string): boolean {
	return SKIN_NAMES.includes(name);
}

/** Raw semantic hex palette for a skin (derived; no Color objects). */
export function semanticPalette(name: string): SemanticPalette | undefined {
	const def = SKINS[name];
	if (!def) return undefined;
	return {
		background: def.base.neutral,
		surface: def.base.neutral,
		raisedSurface: def.base.neutral,
		border: def.base.dim,
		text: def.base.text,
		muted: def.base.muted,
		faint: def.base.dim,
		accent: def.base.accent,
		accentSoft: def.base.accent,
		success: def.base.success,
		warning: def.base.warning,
		danger: def.base.error,
		selection: def.base.accent,
	};
}

/** Legacy style-slot mapping for compatibility callers (section 5.2). */
export const LEGACY_SLOT_TO_SEMANTIC: Record<PaletteName, keyof SemanticPalette | "secondary" | "info" | "surface"> = {
	text: "text",
	muted: "muted",
	dim: "faint",
	accent: "accent",
	secondary: "secondary",
	success: "success",
	warning: "warning",
	error: "danger",
	info: "info",
	border: "border",
	userBg: "surface",
	assistantBg: "surface",
	toolBg: "surface",
	toolOkBg: "success",
	toolErrBg: "danger",
};

const skinColors: Record<string, Record<PaletteName, Color>> = Object.fromEntries(
	Object.entries(SKINS).map(([name, def]) => {
		const neutral = parseColor(def.base.neutral);
		const text = parseColor(def.base.text);
		const full: Record<PaletteName, Color> = {
			text,
			muted: parseColor(def.base.muted),
			dim: parseColor(def.base.dim),
			accent: parseColor(def.base.accent),
			secondary: parseColor(def.base.secondary),
			success: parseColor(def.base.success),
			warning: parseColor(def.base.warning),
			error: parseColor(def.base.error),
			info: parseColor(def.base.info),
			border: mixColors(neutral, text, 0.28),
			userBg: mixColors(neutral, text, 0.1),
			assistantBg: mixColors(neutral, text, 0.06),
			toolBg: mixColors(neutral, text, 0.09),
			toolOkBg: mixColors(neutral, parseColor(def.base.success), 0.13),
			toolErrBg: mixColors(neutral, parseColor(def.base.error), 0.13),
		};
		if (name === "codex") {
			full.border = parseColor(CODEX_SEMANTIC_START.border);
			full.userBg = parseColor(CODEX_SEMANTIC_START.raisedSurface);
			full.toolBg = parseColor(CODEX_SEMANTIC_START.surface);
		}
		return [name, full];
	}),
);

const skinNeutral: Record<string, Color> = Object.fromEntries(
	Object.entries(SKINS).map(([name, def]) => [name, parseColor(def.base.neutral)]),
);

let currentSkin = "codex";

function colors(): Record<PaletteName, Color> {
	return skinColors[currentSkin] ?? skinColors["default"]!;
}

function background(): Color {
	return skinNeutral[currentSkin] ?? skinNeutral["default"]!;
}

/** Selected-row fill, Codex style: accent bg with background fg. */
function selectedRow(text: string): string {
	return styleTextWithAnsi(text, foregroundAnsi(currentSkin === "codex" ? colors().text : background(), mode), backgroundAnsi(currentSkin === "codex" ? parseColor(CODEX_SEMANTIC_START.selection) : colors().accent, mode), {
		bold: true,
	});
}

// ANSI background sequences necessarily contain the ESC control character.
// eslint-disable-next-line no-control-regex
const BG_OPEN = /\x1b\[48;(?:2;\d+;\d+;\d+|5;\d+)m/;
const BG_CLOSE = "\x1b[49m";
const FULL_RESET = "\x1b[0m";

/**
 * Repair a filled line whose content carries nested background resets
 * (e.g. a selection pill inside a filled box): without this, everything
 * after the nested reset falls back to terminal black. Re-asserts the
 * fill background after each reset so padding never leaks.
 */
export function repairFill(bg: PaletteName, filledLine: string): string {
	const open = backgroundAnsi(colors()[bg], mode);
	let body = filledLine;
	if (body.endsWith(BG_CLOSE)) body = body.slice(0, -BG_CLOSE.length);
	const repaired = body
		.split(BG_CLOSE)
		.join(`${BG_CLOSE}${open}`)
		.split(FULL_RESET)
		.join(`${FULL_RESET}${open}`);
	return `${open}${repaired}${BG_CLOSE}`;
}

/** Remove background color sequences while keeping foreground styling. */
function stripBackground(line: string): string {
	return line.split(BG_OPEN).join("").split(BG_CLOSE).join("");
}

/** True when a rendered line carries the selection background. */
export function isSelectionRow(line: string): boolean {
	return BG_OPEN.test(line);
}

/**
 * Extend a selected row to a full-width bar: strip the partial background,
 * pad out, and repaint the whole row. Unselected rows stay unfilled.
 */
export function selectionBar(line: string, width: number): string {
	const stripped = stripBackground(line);
	const padded = `${stripped}${" ".repeat(Math.max(0, width - visibleWidth(line)))}`;
	return styleTextWithAnsi(padded, foregroundAnsi(currentSkin === "codex" ? colors().text : background(), mode), backgroundAnsi(currentSkin === "codex" ? parseColor(CODEX_SEMANTIC_START.selection) : colors().accent, mode), {
		bold: true,
	});
}

/** Switch the active skin. Throws for unknown names. */
export function setSkin(name: string): void {
	if (!SKINS[name]) throw new Error(`Unknown skin "${name}". Available: ${SKIN_NAMES.join(", ")}`);
	currentSkin = name;
}

/** Currently active skin name. */
export function getSkin(): string {
	return currentSkin;
}

/** One-line description for a skin (for menus). */
export function skinDescription(name: string): string {
	return SKINS[name]?.description ?? "";
}

/** Raw hex palette for a skin (for tests and tooling). */
export function skinBase(name: string): Record<BaseSlot, string> | undefined {
	return SKINS[name]?.base;
}

/** ASCII art lines for a skin (defaults to the active one). */
export function skinArt(name: string = currentSkin): string[] {
	return SKINS[name]?.art ?? SKINS["default"]!.art;
}

const mode = getTerminalColorMode();

function fg(name: PaletteName, attrs: TextAttributes = {}): (text: string) => string {
	return (text) => styleTextWithAnsi(text, foregroundAnsi(colors()[name], mode), undefined, attrs);
}

function bg(name: PaletteName): (text: string) => string {
	return (text) => styleTextWithAnsi(text, undefined, backgroundAnsi(colors()[name], mode), {});
}

const plain = (attrs: TextAttributes) => (text: string) => styleTextWithAnsi(text, undefined, undefined, attrs);

export const theme = {
	text: fg("text"),
	bold: plain({ bold: true }),
	muted: fg("muted"),
	dim: fg("dim"),
	accent: fg("accent"),
	accentBold: fg("accent", { bold: true }),
	secondary: fg("secondary"),
	success: fg("success"),
	warning: fg("warning"),
	error: fg("error"),
	info: fg("info"),
	border: fg("border"),
	userBg: bg("userBg"),
	assistantBg: bg("assistantBg"),
	toolBg: bg("toolBg"),
	toolOkBg: bg("toolOkBg"),
	toolErrBg: bg("toolErrBg"),
};

export const markdownTheme: MarkdownTheme = {
	heading: fg("accent", { bold: true }),
	link: fg("accent", { underline: true }),
	linkUrl: fg("dim"),
	code: fg("secondary"),
	codeBlock: fg("text"),
	codeBlockBorder: fg("dim"),
	quote: fg("muted", { italic: true }),
	quoteBorder: fg("dim"),
	hr: fg("dim"),
	listBullet: fg("accent"),
	bold: plain({ bold: true }),
	italic: plain({ italic: true }),
	strikethrough: plain({ strikethrough: true }),
	underline: plain({ underline: true }),
};

export const selectListTheme: SelectListTheme = {
	selectedPrefix: selectedRow,
	selectedText: selectedRow,
	description: fg("muted"),
	scrollInfo: fg("dim"),
	noMatch: fg("dim"),
};

export const editorTheme: EditorTheme = {
	borderColor: fg("border"),
	selectList: selectListTheme,
};
