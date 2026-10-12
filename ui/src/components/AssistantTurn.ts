// Assistant prose in a quiet square frame. Streaming deltas extend the block;
// only its one-cell heading marker animates while text is arriving.

import { Markdown, type Component } from "@earendil-works/pi-tui";
import { BLOCK_GAP } from "../design/tokens.ts";
import { ACTIVITY_INTERVAL_MS, type ActivitySource } from "../design/motion.ts";
import { markdownTheme } from "../theme.ts";
import { theme } from "../theme.ts";
import { assistantPanelContentWidth, renderAssistantPanel } from "./ToolPanel.ts";

export class AssistantTurn implements Component {
	private readonly md: Markdown;
	private content: string;
	private readonly activity: ActivitySource | undefined;
	private streaming = false;
	private startedAt = 0;

	constructor(text = "", activity?: ActivitySource) {
		this.content = text;
		this.activity = activity;
		this.md = new Markdown(text, 0, 0, markdownTheme, { color: theme.text });
	}

	setStreaming(active: boolean): void {
		if (this.streaming === active) return;
		this.streaming = active;
		if (active) this.startedAt = Date.now();
	}

	append(delta: string): void {
		this.content += delta;
		this.md.setText(this.content);
	}

	/** Reconcile to canonical final text (replaces incompatible streamed content). */
	setText(text: string): void {
		this.content = text;
		this.md.setText(this.content);
	}

	getText(): string {
		return this.content;
	}

	/** Re-resolve theme colors (render is lazy; text was never baked). */
	refreshSkin(): void {
		this.invalidate();
	}

	render(width: number): string[] {
		const rows = this.md.render(assistantPanelContentWidth(width));
		const frame = this.streaming
			? this.activity?.frame(["·", "•", "●", "•"], ACTIVITY_INTERVAL_MS, this.startedAt) ?? "·"
			: " ";
		return [...BLOCK_GAP, ...renderAssistantPanel(rows, width, theme.accent(frame))];
	}

	invalidate(): void {
		this.md.invalidate();
	}
}
