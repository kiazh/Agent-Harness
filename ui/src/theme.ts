// AgentHarness skins: named palettes plus header ASCII art.
// Colors are defined in OKHSL so equal saturation reads as equally vivid
// across hues; escape codes are computed once at startup.
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
	okhslColor,
	styleTextWithAnsi,
	type TextAttributes,
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
	| "border"
	| "userBg"
	| "assistantBg"
	| "toolBg"
	| "toolOkBg"
	| "toolErrBg";

type Hsl = [hue: number, saturation: number, lightness: number];

interface SkinDef {
	description: string;
	art: string[];
	colors: Record<PaletteName, Hsl>;
}

const SKINS: Record<string, SkinDef> = {
	default: {
		description: "Balanced blue-grey (ships with AgentHarness)",
		art: ["  ◆──────◆", " │ AGENT  │", " │ HARNESS│", "  ◆──────◆"],
		colors: {
			text: [250, 0.04, 0.88],
			muted: [250, 0.07, 0.64],
			dim: [250, 0.07, 0.48],
			accent: [205, 0.8, 0.72],
			secondary: [285, 0.62, 0.7],
			success: [150, 0.65, 0.7],
			warning: [80, 0.85, 0.76],
			error: [25, 0.8, 0.67],
			border: [215, 0.35, 0.5],
			userBg: [225, 0.32, 0.22],
			assistantBg: [222, 0.25, 0.16],
			toolBg: [250, 0.08, 0.2],
			toolOkBg: [155, 0.32, 0.19],
			toolErrBg: [25, 0.42, 0.2],
		},
	},
	midnight: {
		description: "Deep navy blues for late-night sessions",
		art: ["   .--.  ", "  ( moon)", "   `--'  ", "  *  . * "],
		colors: {
			text: [230, 0.05, 0.9],
			muted: [230, 0.1, 0.62],
			dim: [230, 0.1, 0.45],
			accent: [215, 0.9, 0.7],
			secondary: [265, 0.7, 0.72],
			success: [160, 0.6, 0.65],
			warning: [60, 0.8, 0.7],
			error: [10, 0.8, 0.65],
			border: [220, 0.5, 0.45],
			userBg: [222, 0.45, 0.2],
			assistantBg: [225, 0.35, 0.13],
			toolBg: [230, 0.15, 0.16],
			toolOkBg: [160, 0.35, 0.16],
			toolErrBg: [10, 0.45, 0.17],
		},
	},
	forest: {
		description: "Greens and moss, low glare",
		art: ["    ▲    ", "   ▲▲▲   ", "  ▲▲▲▲▲  ", "    │    "],
		colors: {
			text: [120, 0.04, 0.88],
			muted: [120, 0.08, 0.62],
			dim: [120, 0.08, 0.46],
			accent: [145, 0.7, 0.65],
			secondary: [85, 0.7, 0.65],
			success: [145, 0.7, 0.7],
			warning: [60, 0.8, 0.7],
			error: [15, 0.8, 0.65],
			border: [150, 0.4, 0.42],
			userBg: [150, 0.35, 0.18],
			assistantBg: [150, 0.3, 0.12],
			toolBg: [140, 0.12, 0.15],
			toolOkBg: [145, 0.4, 0.16],
			toolErrBg: [15, 0.45, 0.17],
		},
	},
	sunset: {
		description: "Warm ambers and dusk magenta",
		art: ["  \\ | / ", "  --●-- ", "  / | \\ ", "   dusk  "],
		colors: {
			text: [40, 0.05, 0.89],
			muted: [35, 0.1, 0.63],
			dim: [35, 0.1, 0.47],
			accent: [35, 0.9, 0.68],
			secondary: [320, 0.7, 0.7],
			success: [150, 0.6, 0.68],
			warning: [55, 0.9, 0.72],
			error: [5, 0.85, 0.66],
			border: [30, 0.5, 0.48],
			userBg: [30, 0.4, 0.2],
			assistantBg: [25, 0.35, 0.14],
			toolBg: [30, 0.12, 0.16],
			toolOkBg: [150, 0.35, 0.16],
			toolErrBg: [5, 0.45, 0.18],
		},
	},
	grape: {
		description: "Purples and neon pink",
		art: ["  ● ● ● ", "   ● ●  ", "  ● ● ● ", "   vine  "],
		colors: {
			text: [290, 0.05, 0.89],
			muted: [290, 0.1, 0.64],
			dim: [290, 0.1, 0.47],
			accent: [290, 0.75, 0.72],
			secondary: [330, 0.7, 0.7],
			success: [160, 0.6, 0.68],
			warning: [60, 0.85, 0.72],
			error: [10, 0.8, 0.66],
			border: [285, 0.45, 0.5],
			userBg: [285, 0.4, 0.2],
			assistantBg: [285, 0.35, 0.14],
			toolBg: [290, 0.14, 0.16],
			toolOkBg: [160, 0.35, 0.16],
			toolErrBg: [10, 0.45, 0.17],
		},
	},
	mono: {
		description: "High-contrast greys, no hue (accessible)",
		art: ["  ┌───┐  ", "  │ A │  ", "  │ H │  ", "  └───┘  "],
		colors: {
			text: [250, 0.02, 0.9],
			muted: [250, 0.02, 0.66],
			dim: [250, 0.02, 0.5],
			accent: [250, 0.02, 0.96],
			secondary: [250, 0.02, 0.78],
			success: [250, 0.02, 0.86],
			warning: [250, 0.02, 0.82],
			error: [250, 0.06, 0.72],
			border: [250, 0.02, 0.45],
			userBg: [250, 0.03, 0.22],
			assistantBg: [250, 0.03, 0.15],
			toolBg: [250, 0.02, 0.18],
			toolOkBg: [250, 0.03, 0.2],
			toolErrBg: [250, 0.05, 0.2],
		},
	},
};

export const SKIN_NAMES: string[] = Object.keys(SKINS);

const skinColors: Record<string, Record<PaletteName, Color>> = Object.fromEntries(
	Object.entries(SKINS).map(([name, def]) => [
		name,
		Object.fromEntries(
			Object.entries(def.colors).map(([slot, [h, s, l]]) => [slot, okhslColor(h, s, l)]),
		) as Record<PaletteName, Color>,
	]),
);

let currentSkin = "default";

function colors(): Record<PaletteName, Color> {
	return skinColors[currentSkin] ?? skinColors["default"]!;
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
	selectedPrefix: fg("accent"),
	selectedText: fg("accent", { bold: true }),
	description: fg("muted"),
	scrollInfo: fg("dim"),
	noMatch: fg("dim"),
};

export const editorTheme: EditorTheme = {
	borderColor: fg("border"),
	selectList: selectListTheme,
};
