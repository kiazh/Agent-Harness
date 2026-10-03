// AgentHarness color palette and the themes handed to pi-tui components.
// Colors are defined in OKHSL so equal saturation reads as equally vivid
// across hues; escape codes are computed once at startup.

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

const palette = {
	text: okhslColor(250, 0.04, 0.88),
	muted: okhslColor(250, 0.07, 0.64),
	dim: okhslColor(250, 0.07, 0.48),
	accent: okhslColor(205, 0.8, 0.72),
	secondary: okhslColor(285, 0.62, 0.7),
	success: okhslColor(150, 0.65, 0.7),
	warning: okhslColor(80, 0.85, 0.76),
	error: okhslColor(25, 0.8, 0.67),
	border: okhslColor(215, 0.35, 0.5),
	userBg: okhslColor(225, 0.32, 0.22),
	toolBg: okhslColor(250, 0.08, 0.2),
	toolOkBg: okhslColor(155, 0.32, 0.19),
	toolErrBg: okhslColor(25, 0.42, 0.2),
} satisfies Record<string, Color>;

export type PaletteName = keyof typeof palette;

const mode = getTerminalColorMode();

function fg(name: PaletteName, attrs: TextAttributes = {}): (text: string) => string {
	const code = foregroundAnsi(palette[name], mode);
	return (text) => styleTextWithAnsi(text, code, undefined, attrs);
}

function bg(name: PaletteName): (text: string) => string {
	const code = backgroundAnsi(palette[name], mode);
	return (text) => styleTextWithAnsi(text, undefined, code, {});
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
