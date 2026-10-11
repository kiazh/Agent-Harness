// Bounded diff preview: hunk headers in info/accent, additions in success,
// deletions in danger, file metadata as headers (never additions/deletions).
// Leading whitespace is preserved; control sequences never survive shaping.

import { truncateToWidth, type Component } from "@earendil-works/pi-tui";
import { LIMITS } from "../design/tokens.ts";
import { classifyDiffLine, shapePreview } from "../design/text.ts";
import { theme } from "../theme.ts";

/** Style one diff line (pure; classification lives in design/text.ts). */
export function styleDiffLine(line: string): string {
	switch (classifyDiffLine(line)) {
		case "diff-header":
			return theme.bold(line);
		case "file-meta":
			return theme.bold(theme.dim(line));
		case "hunk":
			return theme.info(line);
		case "add":
			return theme.success(line);
		case "del":
			return theme.error(line);
		case "context":
			return theme.dim(line);
	}
}

export class DiffBlock implements Component {
	private readonly lines: string[];
	private readonly maxLines: number;

	constructor(lines: string[], maxLines: number = LIMITS.previewLinesDefault) {
		this.lines = lines;
		this.maxLines = maxLines;
	}

	render(width: number): string[] {
		const shaped = shapePreview(this.lines.join("\n"), Math.max(1, width), this.maxLines);
		const out = shaped.shown.map((line) => truncateToWidth(styleDiffLine(line), width));
		if (shaped.omitted > 0) {
			out.push(truncateToWidth(theme.dim(`… ${shaped.omitted} more line${shaped.omitted === 1 ? "" : "s"}`), width));
		}
		return out;
	}

	invalidate(): void {}
}
