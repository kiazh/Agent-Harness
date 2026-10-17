// Compatibility facade: the canonical theme registry lives in design/theme.ts.
// Existing callers keep importing from "./theme.ts"; new code imports from
// "./design/theme.ts", "./design/tokens.ts", "./design/icons.ts" or
// "./design/text.ts" directly. Do not add a second registry here.

export {
	REQUIRED_THEME_IDS,
	SKIN_NAMES,
	editorTheme,
	getSkin,
	isOpaqueBackground,
	isSelectionRow,
	isValidTheme,
	markdownTheme,
	repairFill,
	selectListTheme,
	selectionBar,
	semanticPalette,
	setOpaqueBackground,
	setSkin,
	styleComposerText,
	surfaceLine,
	skinArt,
	skinBase,
	skinDescription,
	theme,
} from "./design/theme.ts";
export type { PaletteName, SemanticPalette } from "./design/theme.ts";
