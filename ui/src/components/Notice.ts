// Concise notice: 1–2 lines with a text marker plus semantic color, so
// state survives monochrome skins. Explicit reports use a viewer instead.

import { Text, truncateToWidth } from "@earendil-works/pi-tui";
import type { NoticeKind } from "../features/types.ts";
import { BLOCK_GAP } from "../design/tokens.ts";
import { transcriptInset } from "../design/spacing.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { theme } from "../theme.ts";

export class Notice extends Text {
	private readonly raw: string;
	private readonly kind: NoticeKind;

	constructor(text: string, kind: NoticeKind = "info") {
		super("", 0, 0);
		this.raw = text;
		this.kind = kind;
		this.refreshSkin();
	}

	/** Re-resolve theme colors (skins switch live; text was baked at construction). */
	refreshSkin(): void {
		// Untrusted content path (command output, recalled transcripts, MCP
		// errors): strip terminal controls before display — same sanitizer
		// UserTurn/ToolTurn/ApprovalCard already use (wave-3 hotfix).
		const safe = sanitizeDisplay(this.raw);
		const marked =
			this.kind === "success"
				? `✓ ${safe}`
				: this.kind === "warning"
					? `! ${safe}`
					: this.kind === "error"
						? `✗ ${safe}`
						: safe;
		const style = {
			info: theme.info,
			plain: theme.text,
			success: theme.success,
			warning: theme.warning,
			error: theme.error,
		}[this.kind];
		this.setText(style(marked));
	}

	override render(width: number): string[] {
		const total = Math.max(0, width);
		const gutter = " ".repeat(transcriptInset(width));
		return [...BLOCK_GAP, ...super.render(Math.max(1, width - gutter.length)).map((line) => truncateToWidth(gutter + line, total))];
	}
}
