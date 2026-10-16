// Assistant prose in a quiet square frame. The box is content-aware (plan §6):
// width follows the measured Markdown rows (compact content-fit -> medium ->
// full pane width) and height is the real rendered row count, never guessed.
// Streaming deltas extend the block; only its one-cell heading marker
// animates while text is arriving.

import { Markdown, type Component } from "@earendil-works/pi-tui";
import { ACTIVITY_INTERVAL_MS, type ActivitySource } from "../design/motion.ts";
import {
	chooseResponseGeometry,
	contentPrefersWide,
	renderAssistantFrame,
	type ResponseGeometry,
	type ResponseMeasure,
	type ResponseWidthClass,
} from "../design/responseLayout.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { BLOCK_GAP } from "../design/tokens.ts";
import { markdownTheme } from "../theme.ts";
import { repairFill, theme } from "../theme.ts";

export class AssistantTurn implements Component {
	private readonly md: Markdown;
	private content: string;
	/** Sanitized projection of `content`; measurement only ever sees this. */
	private display: string;
	/** Bumps on every append/setText; keys the geometry cache. */
	private revision = 0;
	private cachedGeometry: { revision: number; paneWidth: number; geometry: ResponseGeometry } | undefined;
	/** Last class at the same pane width, for monotonic stream growth. */
	private previousClass: { widthClass: ResponseWidthClass; paneWidth: number } | undefined;
	private readonly activity: ActivitySource | undefined;
	private streaming = false;
	private startedAt = 0;

	constructor(text = "", activity?: ActivitySource) {
		this.content = text;
		this.activity = activity;
		// Untrusted content path: assistant prose can echo repo files, web
		// pages, model output. Sanitize the ASSEMBLED content (not per-delta)
		// so a control sequence split across deltas cannot evade the filter
		// (wave-3 hotfix; same sanitizer as UserTurn/ToolTurn/ApprovalCard).
		// Measurement and rendering both read this sanitized projection.
		this.display = sanitizeDisplay(text);
		this.md = new Markdown(this.display, 0, 0, markdownTheme, { color: theme.text });
	}

	setStreaming(active: boolean): void {
		if (this.streaming === active) return;
		this.streaming = active;
		if (active) this.startedAt = Date.now();
	}

	append(delta: string): void {
		this.content += delta;
		this.display = sanitizeDisplay(this.content);
		this.md.setText(this.display);
		this.revision++;
	}

	/** Reconcile to canonical final text (replaces incompatible streamed content). */
	setText(text: string): void {
		this.content = text;
		this.display = sanitizeDisplay(this.content);
		this.md.setText(this.display);
		this.revision++;
		// The canonical text is a new sizing basis: stream growth history ends.
		this.previousClass = undefined;
	}

	getText(): string {
		return this.content;
	}

	/** Re-resolve theme colors (render is lazy; text was never baked). */
	refreshSkin(): void {
		this.invalidate();
	}

	/**
	 * Chosen geometry for the current content at an allocated pane width.
	 * Cached by (content revision, pane width); the monotonic `previous` class
	 * resets when the pane width changes or canonical text is set.
	 */
	responseGeometry(paneWidth: number): ResponseGeometry {
		const width = Math.max(0, Math.floor(paneWidth));
		const cached = this.cachedGeometry;
		if (cached && cached.revision === this.revision && cached.paneWidth === width) {
			return cached.geometry;
		}
		// A real pane-width change abandons the previous allocation's growth.
		if (this.previousClass && this.previousClass.paneWidth !== width) this.previousClass = undefined;
		const geometry = chooseResponseGeometry(width, this.measure(), this.previousClass);
		this.previousClass = { widthClass: geometry.widthClass, paneWidth: width };
		this.cachedGeometry = { revision: this.revision, paneWidth: width, geometry };
		return geometry;
	}

	render(width: number): string[] {
		const geometry = this.responseGeometry(width);
		if (geometry.span <= 0) return [...BLOCK_GAP];
		const rows = this.md.render(geometry.contentWidth);
		const frame = this.streaming
			? this.activity?.frame(["·", "•", "●", "•"], ACTIVITY_INTERVAL_MS, this.startedAt) ?? "·"
			: " ";
		return [
			...BLOCK_GAP,
			...renderAssistantFrame(rows, geometry, theme.accent(frame), {
				border: theme.border,
				accentBold: theme.accentBold,
				fill: (line) => repairFill("assistantBg", theme.assistantBg(line)),
				dim: theme.dim,
			}),
		];
	}

	/** Measurement hook backed by the real pi-tui Markdown renderer. */
	private measure(): ResponseMeasure {
		return {
			renderRows: (contentWidth: number) => this.md.render(Math.max(1, Math.floor(contentWidth))),
			prefersWide: () => contentPrefersWide(this.display),
		};
	}

	invalidate(): void {
		this.md.invalidate();
	}
}
