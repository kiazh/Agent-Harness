// Semantic design tokens: spacing, density, numeric bounds, palette types.
// No imports from App, features, or mutable conversation state.

/** Canonical semantic color slots (string hex values, not literal-locked). */
export type SemanticSlotName =
	| "background"
	| "surface"
	| "raisedSurface"
	| "border"
	| "text"
	| "muted"
	| "faint"
	| "accent"
	| "accentSoft"
	| "success"
	| "warning"
	| "danger"
	| "selection";

export type SemanticPalette = Record<SemanticSlotName, string>;

/** Legacy skin base slots (preserved for compatibility; see design/theme.ts). */
export type LegacyBaseSlot =
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

export type LegacyBase = Record<LegacyBaseSlot, string>;

/** Legacy style slots used by existing callers (theme.text, theme.toolBg, ...). */
export type LegacyPaletteName =
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

export interface Spacing {
	blockGapRows: number;
	chromeGapRows: number;
	composerPaddingX: number;
	menuPaddingX: number;
}

export const SPACING_NORMAL: Spacing = {
	blockGapRows: 1,
	chromeGapRows: 1,
	composerPaddingX: 1,
	menuPaddingX: 0,
};

export const SPACING_COMPACT: Spacing = {
	blockGapRows: 0,
	chromeGapRows: 0,
	composerPaddingX: 1,
	menuPaddingX: 0,
};

/** One blank terminal row between major transcript blocks (normal density). */
export const BLOCK_GAP: string[] = [""];

/** Central numeric bounds (sections 6.3/6.5/6.6). */
export const LIMITS = {
	previewLinesDefault: 4,
	previewLinesCompact: 2,
	toolPreviewLinesLegacy: 6,
	outputRetainedCount: 8,
	outputMaxBytesPer: 128 * 1024,
	outputMaxBytesTotal: 1024 * 1024,
	composerMaxRows: 8,
	composerMinRows: 1,
	pickerVisibleRowsDefault: 10,
	formMaxLengthPersona: 2000,
	maxCellWidth: 48,
} as const;

/** Viewport classes (section 6.3). Full interactive target is 40x9+. */
export const VIEWPORT = {
	fullMinWidth: 40,
	fullMinHeight: 9,
	degradedMinWidth: 20,
	degradedMinHeight: 6,
	supportedHeights: {
		fullFooterRows: 2,
		compactFooterRows: 1,
		chromeGapMinHeight: 16,
		reducedChromeMaxHeight: 15,
		compactMaxHeight: 8,
	},
} as const;

/** Starting codex semantic values from the spec (section 6.1). */
export const CODEX_SEMANTIC_START: SemanticPalette = {
	background: "#0b0b0c",
	surface: "#17171c",
	raisedSurface: "#16161a",
	border: "#606069",
	text: "#e4e4e7",
	muted: "#8b8b94",
	faint: "#5d5d66",
	accent: "#7aa2f7",
	accentSoft: "#3b4261",
	success: "#9ece6a",
	warning: "#e0af68",
	danger: "#f7768e",
	selection: "#2a3140",
};
