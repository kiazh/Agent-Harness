// Neutral transcript frame shared by tool output and approval evidence.
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { repairFill, theme } from "../theme.ts";
import { panelInset, transcriptInset } from "../design/spacing.ts";
export const TOOL_PANEL_MAX_WIDTH = 104;
const indentFor = panelInset;
export const panelContentWidth = (width: number): number => Math.max(1, Math.min(TOOL_PANEL_MAX_WIDTH, width - indentFor(width)) - 4);
export function renderToolPanel(lines: string[], width: number): string[] {
 if (width <= 0) return [];
 const indent = " ".repeat(indentFor(width));
 const span = Math.max(0, Math.min(TOOL_PANEL_MAX_WIDTH, width - indent.length));
 if (span < 6) return lines.map(line => truncateToWidth(line, width));
 const inner = span - 4;
 const rule = "─".repeat(span - 2);
 const body = lines.map(line => {
  const clipped = truncateToWidth(line, inner);
  const padded = clipped + " ".repeat(Math.max(0, inner - visibleWidth(clipped)));
  return `${indent}${theme.border("│")}${repairFill("toolBg", theme.toolBg(` ${padded} `))}${theme.border("│")}`;
 });
 return [`${indent}${theme.border(`┌${rule}┐`)}`, ...body, `${indent}${theme.border(`└${rule}┘`)}`];
}

const ASSISTANT_PANEL_MAX_WIDTH = 110;

export function assistantPanelContentWidth(width: number): number {
	const span = Math.max(0, Math.min(ASSISTANT_PANEL_MAX_WIDTH, width - transcriptInset(width)));
	return Math.max(1, span - (span < 16 ? 2 : 4));
}

/** A quiet square frame for prose, with a one-cell activity marker in its heading. */
export function renderAssistantPanel(lines: string[], width: number, marker: string): string[] {
	if (width <= 0) return [];
	const indent = " ".repeat(transcriptInset(width));
	const span = Math.max(0, Math.min(ASSISTANT_PANEL_MAX_WIDTH, width - indent.length));
	if (span < 16) return lines.map((line, i) => truncateToWidth(`${i === 0 ? theme.dim("●") : " "} ${line}`, width));
	const inner = span - 4;
	const heading = `${theme.border("┌─ ")}${theme.accentBold("assistant")} ${marker}`;
	const rule = "─".repeat(Math.max(0, span - visibleWidth(heading) - 1));
	const body = lines.map((line) => {
		const clipped = truncateToWidth(line, inner);
		const padded = clipped + " ".repeat(Math.max(0, inner - visibleWidth(clipped)));
		return `${indent}${theme.border("│")}${repairFill("assistantBg", theme.assistantBg(` ${padded} `))}${theme.border("│")}`;
	});
	return [
		`${indent}${heading}${theme.border(`${rule}┐`)}`,
		...body,
		`${indent}${theme.border(`└${"─".repeat(span - 2)}┘`)}`,
	];
}
