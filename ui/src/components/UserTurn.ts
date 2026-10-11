// Plain user turn: dim `>` prefix, normal text, no box. Multiline content is
// preserved; continuation wraps beneath the text column.

import { truncateToWidth, type Component } from "@earendil-works/pi-tui";
import { BLOCK_GAP } from "../design/tokens.ts";
import { sanitizeDisplay, wrapDisplay } from "../design/text.ts";
import { theme } from "../theme.ts";

export class UserTurn implements Component {
	private readonly raw: string;

	constructor(text: string) {
		this.raw = text;
	}

	/** Re-resolve theme colors (render is lazy; text was never baked). */
	refreshSkin(): void {
		this.invalidate();
	}

	render(width: number): string[] {
		const total = Math.max(0, width);
		const inner = Math.max(1, width - 2);
		const lines = sanitizeDisplay(this.raw)
			.split("\n")
			.flatMap((line) => wrapDisplay(line, inner));
		const out = lines.map((line, i) =>
			i === 0 ? `${theme.dim(">")} ${truncateToWidth(line, inner)}` : `  ${truncateToWidth(line, inner)}`,
		);
		const body = out.length ? out : [`${theme.dim(">")} `];
		return [...BLOCK_GAP, ...body.map((line) => truncateToWidth(line, total))];
	}

	invalidate(): void {}
}
