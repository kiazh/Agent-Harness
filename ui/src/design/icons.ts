// Central icon/marker registry. No color here; theme.ts applies styling.
// Every state keeps a text marker so meaning survives monochrome skins.

export const ICONS = {
	userPrefix: "›",
	assistantPrefix: "●",
	assistantIdle: "●",
	toolRunning: "●",
	toolRunningDouble: "●●",
	toolActiveFrames: ["·", "•", "●", "•"] as const,
	success: "✔",
	successAlt: "✓",
	error: "✖",
	errorAlt: "✗",
	interrupted: "○",
	unknown: "?",
	noticeSuccess: "✓",
	noticeWarning: "!",
	noticeError: "✗",
	idleSquare: "□",
	growthFrames: ["▪"] as const,
	growthStatic: "▪",
	composerBar: "│",
	selectedMarker: "❯",
	selectedMarkerAscii: ">",
	separatorDot: " · ",
	ellipsisMore: "…",
	diffHunk: "@@",
	secretMask: "•",
} as const;

export type IconName = keyof typeof ICONS;

/** ASCII fallbacks when glyph/color support requires it. No emoji. */
export const ASCII_FALLBACKS: Record<string, string> = {
	"✔": "ok",
	"✓": "ok",
	"✖": "err",
	"✗": "err",
	"○": "-",
	"·": ".",
	"•": "*",
	"●": "*",
	"●●": "**",
	"▪": "#",
	"■": "#",
	"▣": "#",
	"□": "-",
	"❯": ">",
	"›": ">",
	"┃": "|",
	"…": "...",
};

/** Resolve an icon to its ASCII fallback when `useAscii` is true. */
export function iconFor(mark: string, useAscii: boolean): string {
	if (!useAscii) return mark;
	return ASCII_FALLBACKS[mark] ?? mark;
}

/** Tool header markers by outcome (section 6.5). */
export const TOOL_MARKERS = {
	running: ICONS.toolRunning,
	success: ICONS.success,
	error: ICONS.error,
	interrupted: ICONS.interrupted,
	unknown: ICONS.unknown,
} as const;

export type ToolMarkerKind = keyof typeof TOOL_MARKERS;
