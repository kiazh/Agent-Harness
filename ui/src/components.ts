// Transcript and chrome widgets built from pi-tui primitives.

import {
	Box,
	type Component,
	Markdown,
	SelectList,
	type SelectItem,
	Text,
	truncateToWidth,
	visibleWidth,
	wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import type { NoticeKind } from "./features/index.ts";
import { markdownTheme, selectListTheme, skinArt, theme } from "./theme.ts";

/** A user turn: highlighted block with a marker. */
export class UserMessage extends Box {
	constructor(text: string) {
		super(1, 0, theme.userBg);
		this.addChild(new Text(`${theme.accentBold("›")} ${theme.text(text)}`, 0, 0));
	}
}

/**
 * An assistant turn rendered as Markdown inside a contrasting box
 * (opencode-style), so model output reads as a card against the background.
 * Text grows as deltas stream in.
 */
export class AssistantMessage implements Component {
	private readonly box: Box;
	private readonly md: Markdown;
	private content: string;

	constructor(text = "") {
		this.content = text;
		this.md = new Markdown(text, 1, 0, markdownTheme, { color: theme.text });
		this.box = new Box(1, 0, theme.assistantBg);
		this.box.addChild(this.md);
	}

	append(delta: string): void {
		this.content += delta;
		this.md.setText(this.content);
	}

	render(width: number): string[] {
		return this.box.render(width);
	}

	invalidate(): void {
		this.box.invalidate();
	}
}

/** A one-paragraph note: help text, command output, errors, status. */
export class Notice extends Text {
	constructor(text: string, kind: NoticeKind = "info") {
		const style = {
			info: theme.muted,
			plain: theme.text,
			success: theme.success,
			warning: theme.warning,
			error: theme.error,
		}[kind];
		super(style(text), 1, 0);
	}
}

export type ToolState = "running" | "success" | "error";

const TOOL_PREVIEW_LINES = 6;

/** Summarize tool arguments on one line, e.g. `path=a.py, limit=20`. */
export function formatArgs(args: Record<string, unknown>): string {
	return Object.entries(args)
		.map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
		.join(", ");
}

/** A tool call card: name + arguments, then a short preview of the result. */
export class ToolCard implements Component {
	private readonly name: string;
	private readonly args: string;
	private state: ToolState = "running";
	private result = "";

	constructor(name: string, args: Record<string, unknown>) {
		this.name = name;
		this.args = formatArgs(args);
	}

	complete(result: string, isError: boolean): void {
		this.result = result;
		this.state = isError ? "error" : "success";
	}

	getState(): ToolState {
		return this.state;
	}

	render(width: number): string[] {
		const bgFn = this.state === "error" ? theme.toolErrBg : this.state === "success" ? theme.toolOkBg : theme.toolBg;
		const icon =
			this.state === "error" ? theme.error("✗") : this.state === "success" ? theme.success("✓") : theme.accent("●");
		const inner = Math.max(1, width - 2);

		const header = `${icon} ${theme.bold(this.name)}${this.args ? ` ${theme.muted(this.args)}` : ""}`;
		const lines = [truncateToWidth(header, inner)];

		if (this.result) {
			const wrapped = this.result
				.split("\n")
				.flatMap((line) => wrapTextWithAnsi(line, inner))
				.filter((line, i, all) => line !== "" || i < all.length - 1);
			for (const line of wrapped.slice(0, TOOL_PREVIEW_LINES)) lines.push(theme.dim(line));
			if (wrapped.length > TOOL_PREVIEW_LINES) {
				lines.push(theme.dim(`… ${wrapped.length - TOOL_PREVIEW_LINES} more lines`));
			}
		}

		return lines.map((line) => bgFn(` ${line}${" ".repeat(Math.max(0, inner - visibleWidth(line)))} `));
	}

	invalidate(): void {}
}

/** One-line status bar: location on the left, model and token usage on the right. */
export class Footer implements Component {
	cwd = "";
	branch = "";
	model = "";
	session = "";
	tokens = 0;
	status: "ready" | "working" | "offline" = "ready";

	render(width: number): string[] {
		const place = this.branch ? `${this.cwd} (${this.branch})` : this.cwd;
		const where = this.session ? `${this.session} · ${place}` : place;
		const statusText =
			this.status === "working"
				? theme.accent("● working")
				: this.status === "offline"
					? theme.error("● offline")
					: theme.success("● ready");
		const right = `${theme.muted(this.model)}  ${theme.dim(`${this.tokens.toLocaleString("en-US")} tokens`)}  ${statusText}`;
		const rightWidth = visibleWidth(right);
		const leftMax = Math.max(0, width - rightWidth - 3);
		const left = leftMax > 0 ? truncateToWidth(theme.dim(where), leftMax) : "";
		const gap = Math.max(1, width - visibleWidth(left) - rightWidth - 2);
		const line = ` ${left}${" ".repeat(gap)}${right} `;
		return [visibleWidth(line) > width ? truncateToWidth(line, width) : line];
	}

	invalidate(): void {}
}

/** Startup banner: skin ASCII art beside the title (opencode-style masthead). */
export function header(version: string): Text {
	const art = skinArt();
	const right = [
		`${theme.accentBold("AgentHarness")} ${theme.dim(`v${version}`)}`,
		theme.muted("Enter to send · Shift+Enter newline · Esc to stop a reply · /help for commands · Ctrl+C to exit"),
	];
	const rows = Math.max(art.length, right.length);
	const lines: string[] = [];
	for (let i = 0; i < rows; i++) {
		const left = art[i] ? theme.accent(art[i]) : "";
		const pad = " ".repeat(Math.max(0, 16 - visibleWidth(art[i] ?? "")));
		lines.push(`${left}${pad}${right[i] ?? ""}`);
	}
	return new Text(lines.join("\n"), 1, 1);
}

/** A titled, filterable list shown as an overlay (e.g. the session picker). */
export class Picker implements Component {
	private readonly title: string;
	private readonly list: SelectList;

	constructor(title: string, items: SelectItem[], onSelect: (item: SelectItem) => void, onCancel: () => void) {
		this.title = title;
		this.list = new SelectList(items, 10, selectListTheme);
		this.list.onSelect = onSelect;
		this.list.onCancel = onCancel;
	}

	handleInput(data: string): void {
		this.list.handleInput(data);
	}

	render(width: number): string[] {
		const inner = Math.max(1, width - 2);
		const body = [theme.accentBold(this.title), ...this.list.render(inner), theme.dim("↑↓ move · Enter select · Esc close")];
		return body.map((line) => theme.toolBg(` ${line}${" ".repeat(Math.max(0, inner - visibleWidth(line)))} `));
	}

	invalidate(): void {
		this.list.invalidate();
	}
}
