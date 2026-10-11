// Transcript and chrome widgets built from pi-tui primitives.

import { homedir } from "node:os";
import {
	Box,
	type Component,
	CURSOR_MARKER,
	Editor,
	type EditorOptions,
	type EditorTheme,
	getKeybindings,
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
 * One blank terminal row between transcript blocks so filled response
 * boxes never touch corner-to-corner. Each block carries its own top
 * margin; the first block's margin also airs out the session header.
 */
const BLOCK_GAP: string[] = [""];

/**
 * A user turn, Codex-style: a plain transcript echo, accent marker plus
 * default-foreground text — no box, the words speak for themselves.
 */
export class UserMessage extends Text {
	private readonly raw: string;

	constructor(text: string) {
		super("", 1, 0);
		this.raw = text;
		this.refreshSkin();
	}

	/** Re-resolve theme colors (skins switch live; text was baked at construction). */
	refreshSkin(): void {
		this.setText(`${theme.accentBold("›")} ${theme.text(this.raw)}`);
	}

	override render(width: number): string[] {
		return [...BLOCK_GAP, ...super.render(width)];
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
		return [...BLOCK_GAP, ...this.box.render(width).map((line) => repairFill("assistantBg", line))];
	}

	invalidate(): void {
		this.box.invalidate();
	}
}

/**
 * A one-paragraph note: help text, command output, errors, status.
 * Success, warning, and error carry a text marker as well as color so
 * state survives monochrome skins and color-blind viewing (the same
 * marker convention as ToolCard and TextForm errors).
 */
export class Notice extends Text {
	private readonly raw: string;
	private readonly kind: NoticeKind;

	constructor(text: string, kind: NoticeKind = "info") {
		super("", 1, 0);
		this.raw = text;
		this.kind = kind;
		this.refreshSkin();
	}

	/** Re-resolve theme colors (skins switch live; text was baked at construction). */
	refreshSkin(): void {
		const marked =
			this.kind === "success"
				? `✓ ${this.raw}`
				: this.kind === "warning"
					? `! ${this.raw}`
					: this.kind === "error"
						? `✗ ${this.raw}`
						: this.raw;
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
		return [...BLOCK_GAP, ...super.render(width)];
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
		return [
			...BLOCK_GAP,
			...lines.map((line) =>
				repairFill(bgSlot, bgFn(` ${line}${" ".repeat(Math.max(0, inner - visibleWidth(line)))} `)),
			),
		];
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
	reducedMotion = /^(1|true)$/i.test(process.env.AH_UI_REDUCED_MOTION ?? "");
	private growthFrame = -1;
	private lastGrowthPulse = -Infinity;
	private growthTimer: NodeJS.Timeout | undefined;
	private static readonly GROWTH_FRAMES = ["▪", "■", "▣", "▪"];

	/** A bounded acknowledgment; frequent evidence never restarts or queues it. */
	pulseGrowth(requestRender: () => void): boolean {
		const now = Date.now();
		if (this.status === "offline" || now - this.lastGrowthPulse < 30_000) return false;
		this.lastGrowthPulse = now;
		this.growthFrame = 0;
		requestRender();
		if (this.reducedMotion) {
			this.growthTimer = setTimeout(() => {
				this.stopGrowth();
				requestRender();
			}, 800);
		} else {
			this.growthTimer = setInterval(() => {
				this.growthFrame++;
				if (this.growthFrame >= Footer.GROWTH_FRAMES.length) this.stopGrowth();
				requestRender();
			}, 200);
		}
		this.growthTimer.unref();
		return true;
	}

	private stopGrowth(): void {
		clearTimeout(this.growthTimer);
		clearInterval(this.growthTimer);
		this.growthTimer = undefined;
		this.growthFrame = -1;
	}

	/** Retire motion when the app exits, disconnects or changes visible session. */
	dispose(): void {
		this.stopGrowth();
		this.lastGrowthPulse = -Infinity;
	}

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
		const hints = width > 2 ? truncateToWidth(theme.dim("? for shortcuts · F1 help · / for commands"), width - 2) : "";
		const square = this.growthFrame < 0
			? theme.dim("□")
			: theme.accent(this.reducedMotion ? "▪" : Footer.GROWTH_FRAMES[this.growthFrame]!);
		const bottom = hints + " ".repeat(Math.max(0, width - visibleWidth(hints) - 1)) + square;
		return [statusLine, truncateToWidth(bottom, width)];
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

/** Private Editor layout fields the popup flip needs (read-only use). */
interface EditorPopupLayout {
	renderedAutocompleteHeight: number;
}

/**
 * Move trailing popup lines above the input block, Codex style. The Editor
 * renders `[top, text, bottom, ...popup]`; the composer shows
 * `[...popup, top, text, bottom]` so the `/` menu opens upward into the
 * transcript area instead of pushing past the footer.
 */
export function popAbove(lines: string[], popupHeight: number): string[] {
	if (!(popupHeight > 0) || popupHeight >= lines.length) return lines;
	return [...lines.slice(lines.length - popupHeight), ...lines.slice(0, lines.length - popupHeight)];
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

	override render(width: number): string[] {
		const lines = super.render(width);
		const { renderedAutocompleteHeight: popupHeight } = this as unknown as EditorPopupLayout;
		return popAbove(lines, popupHeight);
	}

	override handleMouse(event: TuiMouseEvent): ReturnType<Editor["handleMouse"]> {
		const { renderedAutocompleteHeight: popupHeight } = this as unknown as EditorPopupLayout;
		if (!(popupHeight > 0)) return super.handleMouse(event);
		// The popup is drawn above the input: map rows back to the
		// Editor-native coordinates before delegating.
		const total = this.render(event.width).length;
		const textBlock = total - popupHeight;
		if (textBlock <= 0) return super.handleMouse(event);
		const y = event.y < popupHeight ? event.y + textBlock : event.y - popupHeight;
		return super.handleMouse({ ...event, y });
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
	// One blank row between chrome sections so the last response box never
	// touches the composer corners, and the input block stands off the footer.
	return new VStack(
		[
			{ component: new ScrollView(transcript, { follow: "end" }), grow: 1 },
			{ component: composer, shrink: 0 },
			{ component: footer, shrink: 0 },
		],
		{ gap: 1 },
	);
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
						fill(""),
						fill(theme.dim(truncateToWidth(` ${COMPOSER_PLACEHOLDER}`, inner))),
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
	private readonly hint: string;

	constructor(title: string, items: SelectItem[], onSelect: (item: SelectItem) => void, onCancel: () => void, visibleRows = 10, hint = "enter select · esc back") {
		this.title = title;
		this.hint = hint;
		this.list = new SelectList(items, visibleRows, selectListTheme);
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
		const hint = truncateToWidth(theme.dim(this.hint), content);
		const rows = this.list.render(content).map((line) =>
			isSelectionRow(line) ? selectionBar(line, content) : pad(line),
		);
		return [title, ...rows, hint];
	}

	invalidate(): void {
		this.list.invalidate();
	}
}

export interface TextFormOptions {
	title: string;
	description?: string;
	initial?: string;
	placeholder?: string;
	example?: string;
	maxLength?: number;
}

/**
 * A titled multiline text form shown as an overlay (e.g. persona editing).
 * Codex menu style: borderless, docked above the composer, lowercase hints.
 * Uses the terminal Editor so Shift+Enter inserts a newline and Enter saves,
 * matching the composer. Ctrl+S also saves; Esc cancels. Errors carry a
 * text marker as well as color.
 */
export class TextForm implements Component {
	focused = false;
	readonly editor: Editor;
	onSubmit: ((value: string) => void) | undefined;
	onCancel: (() => void) | undefined;

	private readonly title: string;
	private readonly description?: string;
	private readonly placeholder?: string;
	private readonly example?: string;
	private readonly maxLength?: number;
	private readonly tui: TUI;
	private error = "";

	constructor(tui: TUI, editorTheme: EditorTheme, options: TextFormOptions) {
		this.tui = tui;
		this.title = options.title;
		this.description = options.description;
		this.placeholder = options.placeholder;
		this.example = options.example;
		this.maxLength = options.maxLength;
		this.editor = new Editor(tui, editorTheme, { paddingX: 1 });
		this.editor.setText(options.initial ?? "");
		this.editor.onSubmit = (value) => this.onSubmit?.(value);
		this.editor.onChange = () => {
			if (this.error) this.error = "";
			this.tui.requestRender();
		};
	}

	setError(message: string): void {
		this.error = message;
		this.tui.requestRender();
	}

	getValue(): string {
		return this.editor.getExpandedText();
	}

	handleInput(data: string): void {
		const kb = getKeybindings();
		if (kb.matches(data, "tui.select.cancel")) {
			this.onCancel?.();
			return;
		}
		// Explicit save shortcut alongside Enter (the Editor submits on Enter).
		if (data === "\x13") {
			this.onSubmit?.(this.getValue());
			return;
		}
		this.editor.focused = true;
		this.editor.handleInput(data);
	}

	render(width: number): string[] {
		const content = Math.max(1, width);
		// Match App's 70% sheet capped by its five-row bottom margin. Optional
		// guidance yields space to the cursor, count, errors, and controls.
		const rows = this.tui.terminal.rows;
		const height = Math.max(1, Math.min(Math.floor(rows * 70 / 100), rows - 5));
		const wrap = (text: string): string[] => text.split("\n").flatMap((line) => wrapTextWithAnsi(line, content));
		const value = this.getValue();
		// Reserve one editable row first, then controls and count. On the
		// shortest screens the title and editor borders yield to these tasks.
		const statusRows = Math.min(2, height - 1);
		const errorBudget = Math.max(0, Math.min(Math.max(1, height - 6), height - statusRows - 1));
		const errors = this.error ? wrap(this.error).slice(0, errorBudget) : [];
		const guidance = [
			...(this.description ? wrap(this.description) : []),
			...(!value && this.placeholder ? wrap(this.placeholder) : []),
			...(this.example ? wrap(`Example: ${this.example}`) : []),
		].slice(0, Math.max(0, height - 6 - errors.length));
		const out = [
			...(height - statusRows - errors.length >= 2 ? [truncateToWidth(theme.dim(this.title), content)] : []),
			...guidance.map(theme.dim),
		];
		this.editor.focused = this.focused;
		const editorRows = this.editor.render(content);
		const editorHeight = Math.max(1, height - out.length - errors.length - statusRows);
		if (editorRows.length > editorHeight) {
			// pi-tui's Editor has a minimum of five text rows; retain a window
			// around its cursor instead of letting the overlay clip its tail.
			const textRows = editorRows.slice(1, -1);
			const showBorders = editorHeight >= 3;
			const visible = editorHeight - (showBorders ? 2 : 0);
			const cursor = textRows.findIndex((line) => line.includes(CURSOR_MARKER) || line.includes("\x1b[7m"));
			const start = Math.max(0, Math.min(textRows.length - visible, cursor - visible + 1));
			if (showBorders) out.push(start > 0 ? theme.dim("↑ more") : editorRows[0]!);
			out.push(...textRows.slice(start, start + visible));
			if (showBorders) out.push(start + visible < textRows.length ? theme.dim("↓ more") : editorRows.at(-1)!);
		} else {
			out.push(...editorRows);
		}
		const count = Array.from(value).length;
		const countText = this.maxLength ? `${count} / ${this.maxLength} characters` : `${count} characters`;
		if (statusRows >= 2) out.push(truncateToWidth(theme.dim(countText), content));
		out.push(...errors.map((line) => truncateToWidth(theme.error(`✗ ${line}`), content)));
		const hints = content >= 56 ? "enter save · shift+enter newline · ctrl+s save · esc cancel" : "enter save · ctrl+s save · esc cancel";
		if (statusRows >= 1) out.push(truncateToWidth(theme.dim(hints), content));
		return out;
	}

	invalidate(): void {
		this.editor.invalidate();
	}
}
