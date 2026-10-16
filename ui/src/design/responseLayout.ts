// Shared adaptive response measurement policy (plan §6).
// Pure decisions only: the caller supplies a real Markdown render callback and owns caches.
// Height is natural (rendered rows); width grows compact -> medium -> full.

import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { transcriptInset } from "./spacing.ts";

export type ResponseWidthClass = "compact" | "medium" | "full";

export interface ResponseSizingPolicy {
	/** Compact trial width C. */
	compactTrialWidth: number;
	/** Medium trial width fraction of available text cells (M = max(C, ceil(f * A))). */
	targetFraction: number;
	/** A compact class requires the rendered content to fit within this many rows. */
	compactMaxRows: number;
	/** A medium class requires the rendered content to fit within this many rows. */
	mediumMaxRows: number;
	/** Border columns for a framed box, ordinarily 2. */
	borderColumns: number;
	/** Total horizontal inside padding for a framed box. */
	paddingColumns: number;
	/** Below this frame budget (pane minus gutter) the frame degrades to frameless. */
	minFramedSpan: number;
	/** Minimum content width for a compact box (keeps the heading legible). */
	minCompactContent: number;
}

/** Centralized tunable defaults; capture-verified, not per-widget constants. */
export const DEFAULT_RESPONSE_POLICY: ResponseSizingPolicy = {
	compactTrialWidth: 56,
	targetFraction: 0.72,
	compactMaxRows: 4,
	mediumMaxRows: 12,
	borderColumns: 2,
	paddingColumns: 2,
	minFramedSpan: 16,
	minCompactContent: 12,
};

/** Content measurement hook backed by the real Markdown renderer. */
export interface ResponseMeasure {
	/** Rendered display rows at a given content width (post-sanitization). */
	renderRows(contentWidth: number): string[];
	/**
	 * True when the content contains a block that prefers broader presentation
	 * (multiline code fence, table). Compact is then skipped entirely.
	 */
	prefersWide?: () => boolean;
}

/** A GFM table delimiter row: dashes with optional colons, pipes and spaces. */
function isTableDelimiter(line: string): boolean {
	const trimmed = line.trim();
	return /^[|\s:-]+$/.test(trimmed) && trimmed.includes("-");
}

/**
 * Structural preference for a broader frame: a triple-backtick fence with at
 * least two body lines (closed, or still open while streaming) or a Markdown
 * table (a pipe row directly above a delimiter row). Pure shape heuristic:
 * a false positive only widens the box, never truncates text.
 */
export function contentPrefersWide(source: string): boolean {
	const lines = source.split("\n");
	for (let i = 0; i < lines.length; i++) {
		if (!/^\s*```/.test(lines[i] ?? "")) continue;
		let body = 0;
		while (i + 1 + body < lines.length && !/^\s*```\s*$/.test(lines[i + 1 + body] ?? "")) body++;
		if (body >= 2) return true;
		// Skip any closing fence so it is never re-read as a new opener.
		i += body + 1;
	}
	for (let i = 1; i < lines.length; i++) {
		if ((lines[i - 1] ?? "").includes("|") && isTableDelimiter(lines[i] ?? "")) return true;
	}
	return false;
}

export interface ResponseGeometry {
	/** Allocated chat-pane columns this decision was computed against. */
	paneWidth: number;
	widthClass: ResponseWidthClass;
	framed: boolean;
	/** Outside gutter before the frame. */
	indent: number;
	/** Frame span in cells (content + border + padding), never above the pane. */
	span: number;
	/** Text cells inside the frame. */
	contentWidth: number;
	/** Natural frame height once rendered at `contentWidth` (borders included). */
	frameRows: number;
}

/** Frame budget derived from an allocated pane width (never terminal.columns). */
export function assistantFrameBudget(
	paneWidth: number,
	policy: ResponseSizingPolicy = DEFAULT_RESPONSE_POLICY,
): { indent: number; frameBudget: number; available: number; framed: boolean } {
	const width = Math.max(0, Math.floor(paneWidth));
	const indent = transcriptInset(width);
	const frameBudget = Math.max(0, width - indent);
	const framed = frameBudget >= policy.minFramedSpan;
	const available = framed
		? Math.max(0, frameBudget - policy.borderColumns - policy.paddingColumns)
		: Math.max(0, frameBudget - 2);
	return { indent, frameBudget, available, framed };
}

function classRank(cls: ResponseWidthClass): number {
	return cls === "compact" ? 0 : cls === "medium" ? 1 : 2;
}

function widthForClass(
	cls: ResponseWidthClass,
	available: number,
	policy: ResponseSizingPolicy,
): number {
	if (cls === "compact") return Math.min(available, policy.compactTrialWidth);
	if (cls === "medium") {
		const c = Math.min(available, policy.compactTrialWidth);
		return Math.min(available, Math.max(c, Math.ceil(policy.targetFraction * available)));
	}
	return available;
}

function buildGeometry(
	paneWidth: number,
	indent: number,
	framed: boolean,
	widthClass: ResponseWidthClass,
	contentWidth: number,
	rows: string[],
	policy: ResponseSizingPolicy,
): ResponseGeometry {
	const span = framed
		? Math.min(paneWidth - indent, contentWidth + policy.borderColumns + policy.paddingColumns)
		: Math.min(paneWidth - indent, contentWidth + 2);
	return {
		paneWidth,
		widthClass,
		framed,
		indent,
		span: Math.max(0, span),
		contentWidth: Math.max(0, contentWidth),
		frameRows: framed ? rows.length + 2 : rows.length,
	};
}

/**
 * Deterministic sizing procedure (plan §6): measure at C, then M, else A.
 * `previous` enables monotonic class growth within a stable pane allocation so
 * streaming cannot oscillate compact -> medium -> compact between tokens.
 */
export function chooseResponseGeometry(
	paneWidth: number,
	measure: ResponseMeasure,
	previous?: { widthClass: ResponseWidthClass; paneWidth: number },
	policy: ResponseSizingPolicy = DEFAULT_RESPONSE_POLICY,
): ResponseGeometry {
	const width = Math.max(0, Math.floor(paneWidth));
	const { indent, framed, available } = assistantFrameBudget(width, policy);

	if (available <= 0) {
		return {
			paneWidth: width,
			widthClass: "compact",
			framed,
			indent,
			span: Math.max(0, Math.min(width - indent, 0)),
			contentWidth: 0,
			frameRows: 0,
		};
	}

	const prefersWide = measure.prefersWide?.() ?? false;

	// Monotonic growth outranks content-fit: a larger previous class in the
	// same allocation is kept, so the compact trial is skipped entirely.
	const pinned =
		previous !== undefined && previous.paneWidth === width && classRank(previous.widthClass) > 0;

	// Step 2-4: compact trial (skipped for wide-preferring content or a pinned
	// larger class).
	let computed: ResponseWidthClass = "full";
	if (!prefersWide && !pinned) {
		const c = widthForClass("compact", available, policy);
		const rowsAtC = measure.renderRows(c);
		if (rowsAtC.length <= policy.compactMaxRows) {
			// Content-fit: grow only to the widest rendered row, bounded by C.
			// The toolkit Markdown pads rows to the trial width, so measure the
			// visible ink (trailing padding excluded), not the padded cells.
			const widest = rowsAtC.reduce((max, row) => Math.max(max, visibleWidth(row.trimEnd())), 0);
			const contentWidth = Math.min(
				c,
				Math.max(policy.minCompactContent, widest),
				available,
			);
			const rows = measure.renderRows(contentWidth);
			return buildGeometry(width, indent, framed, "compact", contentWidth, rows, policy);
		}
	}
	// Step 5: medium trial.
	const m = widthForClass("medium", available, policy);
	const rowsAtM = measure.renderRows(m);
	if (rowsAtM.length <= policy.mediumMaxRows) computed = "medium";

	// Monotonic growth: never shrink the class inside the same allocation.
	const chosen =
		previous && previous.paneWidth === width && classRank(previous.widthClass) > classRank(computed)
			? previous.widthClass
			: computed;

	const contentWidth = widthForClass(chosen, available, policy);
	const rows = measure.renderRows(contentWidth);
	return buildGeometry(width, indent, framed, chosen, contentWidth, rows, policy);
}

/**
 * Render a framed assistant panel for pre-rendered rows at a chosen geometry.
 * Mirrors the established heading/body paint while honoring the explicit span.
 */
export function renderAssistantFrame(
	rows: string[],
	geometry: ResponseGeometry,
	marker: string,
	styles: {
		border(text: string): string;
		accentBold(text: string): string;
		fill(line: string): string;
		dim(text: string): string;
	},
): string[] {
	const { indent, span, framed, contentWidth } = geometry;
	if (span <= 0 || geometry.paneWidth <= 0) return [];
	const pad = " ".repeat(indent);
	if (!framed) {
		return rows.map((line, i) =>
			truncateToWidth(
				i === 0 ? `${styles.dim("●")} ${line}` : `  ${line}`,
				geometry.paneWidth,
			),
		);
	}
	const heading = `${styles.border("┌─ ")}${styles.accentBold("assistant")} ${marker}`;
	const rule = "─".repeat(Math.max(0, span - visibleWidth(heading) - 1));
	const body = rows.map((line) => {
		const clipped = truncateToWidth(line, contentWidth);
		const padded = clipped + " ".repeat(Math.max(0, contentWidth - visibleWidth(clipped)));
		return `${pad}${styles.border("│")}${styles.fill(` ${padded} `)}${styles.border("│")}`;
	});
	return [
		`${pad}${heading}${styles.border(`${rule}┐`)}`,
		...body,
		`${pad}${styles.border(`└${"─".repeat(Math.max(0, span - 2))}┘`)}`,
	];
}
