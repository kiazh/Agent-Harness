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
		description: "Codex-style restrained chrome — default fg, magenta brand accents",
		art: ["  ┌─────┐", "  │ >_  │", "  │ AH  │", "  └─────┘"],
		base: {
			text: "#e8e8e8",
			muted: "#8a8a8a",
			dim: "#5c5c5c",
			accent: "#56b6c2",
			secondary: "#c678dd",
			success: "#98c379",
			warning: "#e5c07b",
			error: "#e06c75",
			info: "#56b6c2",
			neutral: "#0d0d0d",
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
};

export const SKIN_NAMES: string[] = Object.keys(SKINS);

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
		return [name, full];
	}),
);

const skinNeutral: Record<string, Color> = Object.fromEntries(
	Object.entries(SKINS).map(([name, def]) => [name, parseColor(def.base.neutral)]),
);

let currentSkin = "default";

function colors(): Record<PaletteName, Color> {
	return skinColors[currentSkin] ?? skinColors["default"]!;
}

function background(): Color {
	return skinNeutral[currentSkin] ?? skinNeutral["default"]!;
}

/** Selected-row fill, Codex style: accent bg with background fg. */
function selectedRow(text: string): string {
	return styleTextWithAnsi(text, foregroundAnsi(background(), mode), backgroundAnsi(colors().accent, mode), {
		bold: true,
	});
}

const BG_OPEN = "\x1b[48;2;";
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
	let out = line;
	for (;;) {
		const start = out.indexOf(BG_OPEN);
		if (start === -1) break;
		const end = out.indexOf("m", start);
		if (end === -1) break;
		out = out.slice(0, start) + out.slice(end + 1);
	}
	return out.split(BG_CLOSE).join("");
}

/** True when a rendered line carries the selection background. */
export function isSelectionRow(line: string): boolean {
	return line.includes(BG_OPEN);
}

/**
 * Extend a selected row to a full-width bar: strip the partial background,
 * pad out, and repaint the whole row. Unselected rows stay unfilled.
 */
export function selectionBar(line: string, width: number): string {
	const stripped = stripBackground(line);
	const padded = `${stripped}${" ".repeat(Math.max(0, width - visibleWidth(line)))}`;
	return styleTextWithAnsi(padded, foregroundAnsi(background(), mode), backgroundAnsi(colors().accent, mode), {
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
