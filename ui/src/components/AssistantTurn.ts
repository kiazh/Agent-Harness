// Plain assistant turn: dim/static `●` prefix with immediate Markdown
// rendering, no card background or border. Streaming deltas extend the block.

import { Markdown, truncateToWidth, type Component } from "@earendil-works/pi-tui";
import { BLOCK_GAP } from "../design/tokens.ts";
import { transcriptInset } from "../design/spacing.ts";
import { markdownTheme } from "../theme.ts";
import { theme } from "../theme.ts";

export class AssistantTurn implements Component {
	private readonly md: Markdown;
	private content: string;

	constructor(text = "") {
		this.content = text;
		this.md = new Markdown(text, 0, 0, markdownTheme, { color: theme.text });
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
		const total = Math.max(0, width);
		const gutter = " ".repeat(transcriptInset(width));
		const inner = Math.max(1, Math.min(110, width - gutter.length - 2));
		const rows = this.md.render(inner);
		const out = rows.map((line, i) =>
			i === 0 ? `${theme.dim("●")} ${truncateToWidth(line, inner)}` : `  ${truncateToWidth(line, inner)}`,
		);
		return [...BLOCK_GAP, ...out.map((line) => truncateToWidth(gutter + line, total))];
	}

	invalidate(): void {
		this.md.invalidate();
	}
}
