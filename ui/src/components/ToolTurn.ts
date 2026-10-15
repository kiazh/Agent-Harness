// Compact honest tool turn: marker + name + muted args header, bounded
// result/diff preview, explicit outcome text. Neutral frame; critical outcome
// stays visible at narrow widths. Arguments are display-only.

import { truncateToWidth, type Component } from "@earendil-works/pi-tui";
import { ICONS, TOOL_MARKERS } from "../design/icons.ts";
import { ACTIVITY_INTERVAL_MS, type ActivitySource } from "../design/motion.ts";
import { LIMITS } from "../design/tokens.ts";
import { sanitizeDisplay, shapePreview } from "../design/text.ts";
import { theme } from "../theme.ts";
import { panelContentWidth, renderToolPanel } from "./ToolPanel.ts";
import { styleDiffLine } from "./DiffBlock.ts";

export type ToolOutcome =
	| "running"
	| "awaiting_approval"
	| "success"
	| "denied"
	| "error"
	| "interrupted"
	| "outcome_unknown"
	| "historical";

export interface ToolResultView {
	/** Sanitized, bounded retained content. */
	text: string;
	backendTruncated: boolean;
	locallyTruncated: boolean;
}

function markerFor(outcome: ToolOutcome): string {
	switch (outcome) {
		case "running":
			return TOOL_MARKERS.running;
		case "awaiting_approval":
			return "○";
		case "success":
			return TOOL_MARKERS.success;
		case "denied":
			// A policy denial is not a success and not an execution error:
			// its own neutral marker (⊘, muted) keeps the three distinct.
			return "⊘";
		case "error":
			return TOOL_MARKERS.error;
		case "interrupted":
			return TOOL_MARKERS.interrupted;
		case "outcome_unknown":
			return TOOL_MARKERS.unknown;
		case "historical":
			return "○";
	}
}

export class ToolTurn implements Component {
	private readonly name: string;
	private readonly args: string;
	private readonly activity: ActivitySource;
	private readonly startedAt: number;
	private result: ToolResultView = { text: "", backendTruncated: false, locallyTruncated: false };
	private outcome: ToolOutcome = "running";
	private paused: "stopping" | "cleanup_pending" | undefined;

	constructor(name: string, args: Record<string, unknown>, activity: ActivitySource) {
		this.name = sanitizeDisplay(name);
		this.args = sanitizeDisplay(Object.entries(args).map(([key, value]) =>
			["path", "command"].includes(key) && typeof value === "string" ? value : formatToolArgs({ [key]: value }),
		).join(", "));
		this.activity = activity;
		this.startedAt = Date.now();
	}

	settle(result: ToolResultView, outcome: ToolOutcome): void {
		this.result = result;
		this.outcome = outcome;
	}

	setMotionPaused(phase: "stopping" | "cleanup_pending"): void {
		this.paused = phase;
	}

	/** Re-resolve theme colors (render is lazy; nothing was baked). */
	refreshSkin(): void {
		this.invalidate();
	}

	render(width: number): string[] {
		const total = Math.max(0, width);
		const inner = Math.max(1, panelContentWidth(width) - 2);
		const marker = this.outcome === "running" && this.paused
			? theme.dim(ICONS.interrupted)
			: this.outcome === "running"
			? theme.accent(this.activity.frame([...ICONS.toolActiveFrames], ACTIVITY_INTERVAL_MS, this.startedAt))
			: styleMarker(this.outcome, markerFor(this.outcome));
		// Sanitize keeps newlines, so a multi-line name/args header must become
		// separate physical rows; a newline inside one row breaks the panel.
		const header = `${marker} ${this.name}${this.args ? ` ${theme.muted(this.args)}` : ""}`;
		const lines = header.split("\n").map((row) => truncateToWidth(row, panelContentWidth(width)));
		const indent = "  ";
		if (this.outcome === "running") {
			lines.push(`${indent}${theme.dim(this.paused ? `${this.paused === "stopping" ? "stopping" : "cleanup pending"}; outcome pending` : "running")}`);
		} else if (this.outcome === "awaiting_approval") {
			lines.push(`${indent}${theme.dim("awaiting approval")}`);
		} else if (this.outcome === "denied") {
			lines.push(`${indent}${theme.muted("denied; not executed")}`);
		} else if (this.outcome === "interrupted") {
			lines.push(`${indent}${theme.warning("interrupted; outcome unknown")}`);
		} else if (this.outcome === "outcome_unknown") {
			lines.push(`${indent}${theme.muted("completion not reported")}`);
		} else if (this.outcome === "historical") {
			lines.push(`${indent}${theme.dim("recorded output")}`);
		}
		if (this.result.text) {
			const shaped = shapePreview(this.result.text, inner, LIMITS.previewLinesDefault);
			for (const line of shaped.shown) lines.push(`${indent}${styleDiffLine(line)}`);
			if (shaped.omitted > 0) {
				lines.push(`${indent}${theme.dim(`… ${shaped.omitted} more line${shaped.omitted === 1 ? "" : "s"}`)}`);
			}
		}
		if (this.result.backendTruncated && this.outcome !== "running") {
			lines.push(`${indent}${theme.dim("server output truncated")}`);
		}
		return ["", ...renderToolPanel(lines, total)];
	}

	invalidate(): void {}
}

function styleMarker(outcome: ToolOutcome, mark: string): string {
	switch (outcome) {
		case "success":
			return theme.success(mark);
		case "denied":
			return theme.muted(mark);
		case "error":
			return theme.error(mark);
		case "interrupted":
			return theme.warning(mark);
		case "outcome_unknown":
			return theme.muted(mark);
		case "historical":
			return theme.dim(mark);
		default:
			return theme.dim(mark);
	}
}

/** Compact single-line argument summary (display-only, never reconstructed). */
export function formatToolArgs(args: Record<string, unknown>): string {
	return Object.entries(args)
		.map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
		.join(", ");
}
