// Neutral transcript frame shared by tool output and approval evidence.
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { assistantFrameBudget } from "../design/responseLayout.ts";
import { panelInset } from "../design/spacing.ts";
import { repairFill, theme } from "../theme.ts";
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

/**
 * Content cells available to an assistant box at an allocated pane width.
 * Legacy seam: assistant sizing now lives in `design/responseLayout.ts`
 * (plan §6) and no longer caps at a fixed 110 columns.
 */
export function assistantPanelContentWidth(width: number): number {
	return assistantFrameBudget(width).available;
}
