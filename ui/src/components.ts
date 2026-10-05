// Transcript and chrome widgets built from pi-tui primitives.

import { homedir } from "node:os";
import {
	Box,
	type Component,
	Editor,
	type EditorOptions,
	type EditorTheme,
	Markdown,
	ScrollView,
	SelectList,
	type SelectItem,
	Text,
	truncateToWidth,
	type TuiMouseEvent,
	visibleWidth,
	VStack,
	wrapTextWithAnsi,
	type TUI,
} from "@earendil-works/pi-tui";
import type { NoticeKind } from "./features/index.ts";
import { isSelectionRow, markdownTheme, repairFill, selectionBar, selectListTheme, theme } from "./theme.ts";

/** Shorten a path with `~`, Codex session-header style. */
function shortCwd(cwd: string): string {
	const home = homedir();
	return home && cwd.startsWith(home) ? `~${cwd.slice(home.length)}` : cwd;
}

/**
 * A user turn, Codex-style: a plain transcript echo, accent marker plus
 * default-foreground text — no box, the words speak for themselves.
 */
export class UserMessage extends Text {
	constructor(text: string) {
		super(`${theme.accentBold("›")} ${theme.text(text)}`, 1, 0);
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
		// Repair the fill: model output can carry nested background resets
		// (e.g. pasted ANSI), which would otherwise leak terminal black.
		return this.box.render(width).map((line) => repairFill("assistantBg", line));
	}

	invalidate(): void {
		this.box.invalidate();
	}
}

/** A one-paragraph note: help text, command output, errors, status. */
export class Notice extends Text {
	constructor(text: string, kind: NoticeKind = "info") {
		const style = {
			info: theme.info,
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

/**
 * Codex-style diff coloring for tool output: additions green, deletions red,
 * hunk headers info — everything else stays dim.
 */
export function diffStyle(line: string): string {
	if (/^diff --git /.test(line)) return theme.bold(line);
	if (/^(\+\+\+|---) /.test(line)) return theme.bold(theme.dim(line));
	if (/^@@/.test(line)) return theme.info(line);
	if (/^\+[^+]/.test(line) || line === "+") return theme.success(line);
	if (/^-[^-]/.test(line) || line === "-") return theme.error(line);
	return theme.dim(line);
}

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
		const bgSlot = this.state === "error" ? "toolErrBg" : this.state === "success" ? "toolOkBg" : "toolBg";
		const bgFn = this.state === "error" ? theme.toolErrBg : this.state === "success" ? theme.toolOkBg : theme.toolBg;
		const icon =
			this.state === "error" ? theme.error("✗") : this.state === "success" ? theme.success("✓") : theme.accent("●");
		const inner = Math.max(1, width - 2);

		// Codex exec-cell titles: Running while live, Ran/Failed once settled.
		const stateWord = this.state === "error" ? "Failed" : this.state === "success" ? "Ran" : "Running";
		const header = `${icon} ${theme.bold(stateWord)} ${theme.bold(this.name)}${this.args ? ` ${theme.muted(this.args)}` : ""}`;
		const lines = [truncateToWidth(header, inner)];

		if (this.result) {
			const wrapped = this.result
				.split("\n")
				.flatMap((line) => wrapTextWithAnsi(line, inner))
				.filter((line, i, all) => line !== "" || i < all.length - 1);
			for (const line of wrapped.slice(0, TOOL_PREVIEW_LINES)) lines.push(diffStyle(line));
			if (wrapped.length > TOOL_PREVIEW_LINES) {
				lines.push(theme.dim(`… ${wrapped.length - TOOL_PREVIEW_LINES} more lines`));
			}
		}

		// Repair the fill: tool output can carry nested background resets
		// (e.g. colored command output), which would leak terminal black.
		return lines.map((line) =>
			repairFill(bgSlot, bgFn(` ${line}${" ".repeat(Math.max(0, inner - visibleWidth(line)))} `)),
		);
	}

	invalidate(): void {}
}

/**
 * Bottom status, Codex style: two dim lines, no rules. First the
 * `model · dir · branch` status line (plus tokens and a busy/offline
 * marker), then the key hints.
 */
export class Footer implements Component {
	cwd = "";
	branch = "";
	model = "";
	tokens = 0;
	status: "ready" | "working" | "offline" = "ready";

	render(width: number): string[] {
		const sep = theme.dim(" · ");
		const segments: string[] = [];
		if (this.model) segments.push(theme.accent(this.model));
		if (this.cwd) segments.push(theme.dim(shortCwd(this.cwd)));
		if (this.branch) segments.push(theme.secondary(this.branch));
		segments.push(theme.dim(`${this.tokens.toLocaleString("en-US")} tokens`));
		if (this.status === "working") segments.push(theme.accent("● working"));
		else if (this.status === "offline") segments.push(theme.error("● offline"));
		const statusLine = truncateToWidth(segments.join(sep), width);
		const hints = truncateToWidth(theme.dim("? for shortcuts · / for commands"), width);
		return [statusLine, hints];
	}

	invalidate(): void {}
}

/**
 * Startup banner: skin ASCII art beside the title with a divider rule below,
 * opencode-masthead style. Rendered at the exact width so the rule spans it.
 */
export interface SessionHeaderInfo {
	model: string;
	provider: string;
	cwd: string;
	branch: string;
}

/**
 * Minimal session header, Codex style: one `>_` brand line plus a dim
 * directory line. No art, no hints, no rules — those live in the logo
 * and the footer.
 */
export function header(version: string, info?: SessionHeaderInfo): Component {
	return {
		render(width: number): string[] {
			const lines = [`${theme.accent(">_")} ${theme.bold("AgentHarness")} ${theme.dim(`(v${version})`)}`];
			const where = info?.cwd ? shortCwd(info.cwd) + (info.branch ? ` (${info.branch})` : "") : "";
			if (where) lines.push(`   ${theme.dim(where)}`);
			return lines.map((line) => truncateToWidth(line, width));
		},
		invalidate(): void {},
	};
}

/**
 * Codex-style composer input: the pi-tui Editor with its rules replaced by
 * filled rows, so the input reads as one solid block instead of ruled lines.
 * Scroll position survives via a dim `↑ N more` marker.
 */
export class ComposerInput extends Editor {
	override renderTopBorder(_width: number, hiddenLineCount: number): string {
		if (hiddenLineCount > 0) return theme.dim(`↑ ${hiddenLineCount} more`);
		return "";
	}

	override renderBottomBorder(_width: number, hiddenLineCount: number): string {
		if (hiddenLineCount > 0) return theme.dim(`↓ ${hiddenLineCount} more`);
		return "";
	}
}

/** Dim composer placeholder, Codex style (the Editor cannot render its own). */
export const COMPOSER_PLACEHOLDER = "Ask AgentHarness to do anything";

/**
 * The app's fullscreen layout, Codex style: the transcript scrolls in the
 * flexible area while the composer and footer stay pinned to the bottom.
 * The fixed chrome must never shrink — all overflow belongs to the scroll
 * area (opencode's composer is flexShrink=0 too).
 */
export type LayoutRoot = VStack;

export function createLayout(transcript: Component, composer: Component, footer: Component): LayoutRoot {
	return new VStack([
		{ component: new ScrollView(transcript, { follow: "end" }), grow: 1 },
		{ component: composer, shrink: 0 },
		{ component: footer, shrink: 0 },
	]);
}

/**
 * Codex-style composer: a filled input block with a left accent bar. Wraps
 * the Editor in a background box — focus and input stay on the inner
 * editor. While the editor is empty, a dim placeholder fills the same rows
 * so the block never jumps in height.
 */
export class ComposerBox extends Box {
	readonly editor: ComposerInput;

	constructor(tui: TUI, editorTheme: EditorTheme, options?: EditorOptions) {
		super(0, 0, theme.userBg);
		this.editor = new ComposerInput(tui, editorTheme, options);
		this.addChild(this.editor);
	}

	override render(width: number): string[] {
		const inner = Math.max(1, width - 2);
		// Fill via repairFill, not the naive Box fill: editor rows can carry
		// nested background resets (selection pill), which would otherwise
		// leak terminal black into the padding. Render the editor directly
		// so the fill width stays exact.
		const fill = (line: string): string => {
			const padded = `${line}${" ".repeat(Math.max(0, inner - visibleWidth(line)))}`;
			return repairFill("userBg", theme.userBg(padded));
		};
		const bar = theme.accent("┃");
		// NOTE: the editor renders raw (unfilled) lines here on purpose —
		// going through super.render would double-fill via the Box.
		const body =
			this.editor.getText() !== ""
				? this.editor.render(inner).map((line) => fill(line))
				: [
						fill(theme.dim(truncateToWidth(` ${COMPOSER_PLACEHOLDER}`, inner))),
						fill(""),
						fill(""),
					];
		return body.map((line) => `${bar} ${line}`);
	}

	override handleMouse(event: TuiMouseEvent): ReturnType<Box["handleMouse"]> {
		if (event.x < 2) return undefined;
		return super.handleMouse({ ...event, x: event.x - 2, width: Math.max(1, event.width - 2) });
	}
}

/**
 * A titled, filterable list shown as an overlay (e.g. the session picker).
 * Codex menu style: borderless, docked above the composer, selected row as
 * a full-width accent bar, lowercase hints.
 */
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
		const content = Math.max(1, width);
		const pad = (line: string): string =>
			`${line}${" ".repeat(Math.max(0, content - visibleWidth(line)))}`;
		const title = truncateToWidth(theme.dim(this.title), content);
		const hint = truncateToWidth(theme.dim("enter select · esc back"), content);
		const rows = this.list.render(content).map((line) =>
			isSelectionRow(line) ? selectionBar(line, content) : pad(line),
		);
		return [title, ...rows, hint];
	}

	invalidate(): void {
		this.list.invalidate();
	}
}
