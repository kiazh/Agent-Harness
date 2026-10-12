// Transcript and chrome widgets built from pi-tui primitives.

import { homedir } from "node:os";
import {
	Box,
	type Component,
	CURSOR_MARKER,
	Editor,
    Key,
    matchesKey,
	type EditorOptions,
	type EditorTheme,
	ScrollView,
	type SelectItem,
	truncateToWidth,
	type TuiMouseEvent,
	visibleWidth,
	VStack,
	type TUI,
} from "@earendil-works/pi-tui";
import { ICONS } from "./design/icons.ts";
import { ACTIVITY_INTERVAL_MS, type AnimationCoordinator, type AnimationSubscription } from "./design/motion.ts";
import { sanitizeDisplay } from "./design/text.ts";
import { LIMITS } from "./design/tokens.ts";
import { transcriptInset } from "./design/spacing.ts";
import { Menu } from "./components/Menu.ts";
import { repairFill, theme } from "./theme.ts";

export { Footer } from "./components/Footer.ts";

/**
 * Coordinator-driven thinking indicator: fixed-width dot frames plus an
 * elapsed counter derived from timestamps at render. No private ticker;
 * the shared coordinator owns the single render timer. Implements the same
 * start/stop/setMessage shape the transcript expects from a spinner.
 */
export class ThinkingIndicator implements Component {
	private sub: AnimationSubscription | undefined;
	private startedAt = 0;
	private baseMessage = "Thinking…";
	private readonly activity: AnimationCoordinator;

	constructor(activity: AnimationCoordinator) {
		this.activity = activity;
	}

	start(): void {
		if (this.sub) return;
		this.startedAt = Date.now();
		this.sub = this.activity.subscribe("thinking", ACTIVITY_INTERVAL_MS);
	}

	stop(): void {
		this.sub?.stop();
		this.sub = undefined;
	}

	setMessage(message: string): void {
		if (message) this.baseMessage = message.replace(/\s+\d+s$/, "");
	}

	render(width: number): string[] {
		if (!this.sub) return [];
		const dots = this.activity.frame([...ICONS.toolActiveFrames], ACTIVITY_INTERVAL_MS, this.startedAt);
		const secs = Math.floor((Date.now() - this.startedAt) / 1000);
		const msg = secs > 0 ? `${this.baseMessage} ${secs}s` : this.baseMessage;
		const gutter = " ".repeat(transcriptInset(width));
		return [truncateToWidth(`${gutter}${theme.accent(dots)} ${theme.muted(msg)}`, width)];
	}

	invalidate(): void {}
}

/** Shorten a path with `~`, Codex session-header style. */
function shortCwd(cwd: string): string {
	const home = homedir();
	return home && cwd.startsWith(home) ? `~${cwd.slice(home.length)}` : cwd;
}

// Compact transcript widgets live in components/*. New code imports them
// directly; this facade re-exports the shared surface plus legacy helper
// names (diffStyle/formatArgs) so existing callers keep compiling.
export { AssistantTurn } from "./components/AssistantTurn.ts";
export { ApprovalCard } from "./components/ApprovalCard.ts";
export type { ApprovalDecision, ApprovalDetails } from "./components/ApprovalCard.ts";
export { DiffBlock, styleDiffLine } from "./components/DiffBlock.ts";
export { styleDiffLine as diffStyle } from "./components/DiffBlock.ts";
export { Notice } from "./components/Notice.ts";
export { OutputStore, OutputViewer } from "./components/OutputViewer.ts";
export type { OutputRecord, OutputViewerOptions } from "./components/OutputViewer.ts";
export { formatToolArgs as formatArgs, ToolTurn } from "./components/ToolTurn.ts";
export type { ToolOutcome, ToolResultView } from "./components/ToolTurn.ts";
export { UserTurn } from "./components/UserTurn.ts";


/** Actual session metadata supplied by the gateway. */
export interface SessionHeaderInfo {
	model: string;
	provider: string;
	cwd: string;
	branch: string;
}

/** One brand row with workspace and branch aligned right. */
export function header(_version: string, info?: SessionHeaderInfo): Component {
	return {
		render(width: number): string[] {
			const brand = `${theme.accent(">_")} ${theme.bold("AgentHarness")}`;
			const where = info?.cwd ? theme.dim(sanitizeDisplay(shortCwd(info.cwd) + (info.branch ? ` · ${info.branch}` : ""))) : "";
			const room = Math.max(0, width - visibleWidth(brand) - 3);
			const right = room >= 6 ? truncateToWidth(where, room) : "";
			return [truncateToWidth(`${brand}${" ".repeat(Math.max(0, width - visibleWidth(brand) - visibleWidth(right)))}${right}`, width)];
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
    onPaste?: (text: string) => boolean;
    onSubmitBlocked?: () => boolean;
    private attachmentPaste: string | undefined;

    override handleInput(data: string): void {
        if (matchesKey(data, Key.enter) && this.onSubmitBlocked?.()) return;
        if (this.attachmentPaste !== undefined || data.includes("\x1b[200~")) {
            if (this.attachmentPaste === undefined) {
                const start = data.indexOf("\x1b[200~");
                if (start > 0) super.handleInput(data.slice(0, start));
                this.attachmentPaste = "";
                data = data.slice(start + 6);
            }
            this.attachmentPaste += data;
            const end = this.attachmentPaste.indexOf("\x1b[201~");
            if (end < 0) return;
            const text = this.attachmentPaste.slice(0, end);
            const remaining = this.attachmentPaste.slice(end + 6);
            this.attachmentPaste = undefined;
            if (!this.onPaste?.(text)) super.handleInput(`\x1b[200~${text}\x1b[201~`);
            if (remaining) this.handleInput(remaining);
            return;
        }
        if (!this.onPaste?.(data)) super.handleInput(data);
    }

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
		const popupCommand = event.y < popupHeight && /^\/[\w-]*$/.test(this.getText().trim());
		const y = event.y < popupHeight ? event.y + textBlock : event.y - popupHeight;
		const result = super.handleMouse({ ...event, y });
		// Picker-first actions open as soon as the command is selected.
		if (popupCommand && !this.isShowingAutocomplete() && ["/delegate", "/delete"].includes(this.getText().trim())) {
			this.onSubmit?.(this.getText());
		}
		return result;
	}
}

/** Dim composer placeholder, Codex style (the Editor cannot render its own). */
export const COMPOSER_PLACEHOLDER = "Ask anything, or type / for commands.";

/**
 * Bound rendered editor rows around the real cursor instead of clipping the
 * tail: the cursor row stays visible with preference for trailing context.
 * Autocomplete popup rows (already moved above the input) are preserved.
 */
export function windowAroundCursor(rows: string[], budget: number, popupHeight = 0): string[] {
	const popup = popupHeight > 0 ? rows.slice(0, popupHeight) : [];
	const body = popupHeight > 0 ? rows.slice(popupHeight) : rows;
	if (body.length <= budget) return [...popup, ...body];
	const cap = Math.max(1, budget);
	const cursor = body.findIndex((line) => line.includes(CURSOR_MARKER) || line.includes("\x1b[7m"));
	const at = cursor < 0 ? body.length - 1 : cursor;
	const start = Math.max(0, Math.min(body.length - cap, at - cap + 1));
	return [...popup, ...body.slice(start, start + cap)];
}

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
	private maxRows: number = LIMITS.composerMaxRows;
	private degradedMode = false;

	constructor(tui: TUI, editorTheme: EditorTheme, options?: EditorOptions) {
		super(0, 0, theme.userBg);
		this.editor = new ComposerInput(tui, editorTheme, options);
		this.addChild(this.editor);
	}

	/** Viewport-derived editable-row budget (shell density, max 8). */
	setMaxRows(rows: number): void {
		this.maxRows = Math.max(1, rows);
	}

	/** Safe degraded mode: draft preserved, editing deferred until resize. */
	setDegradedMode(degraded: boolean): void {
		this.degradedMode = degraded;
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
		const bar = theme.accent(ICONS.composerBar);
		if (this.degradedMode) {
			return [`${bar} ${fill(theme.dim(truncateToWidth("Too small — resize to continue (draft kept)", inner)))}`].map(
				(line) => truncateToWidth(line, Math.max(0, width)),
			);
		}
		// NOTE: the editor renders raw (unfilled) lines here on purpose —
		// going through super.render would double-fill via the Box.
		let body: string[];
		if (this.editor.getText() === "") {
			const placeholder = fill(theme.dim(truncateToWidth(`> ${COMPOSER_PLACEHOLDER}`, inner)));
			body = this.maxRows >= 3 ? [fill(""), placeholder, fill("")] : [placeholder];
		} else {
			const rendered = this.editor.render(inner);
			const { renderedAutocompleteHeight: popupHeight } = this.editor as unknown as EditorPopupLayout;
			body = windowAroundCursor(rendered, this.maxRows, popupHeight).map((line) => fill(line));
		}
		const total = Math.max(0, width);
		return body.map((line) => truncateToWidth(`${bar} ${line}`, total));
	}

	override handleMouse(event: TuiMouseEvent): ReturnType<Box["handleMouse"]> {
		if (event.x < 2) return undefined;
		return super.handleMouse({ ...event, x: event.x - 2, width: Math.max(1, event.width - 2) });
	}
}

/**
 * A titled, filterable list shown as an overlay (e.g. the session picker).
 * Implemented over Menu: every picker owns a real filter buffer matched
 * against label, value and description. Kept for existing callers; new code
 * uses Menu directly.
 */
export class Picker implements Component {
	private readonly menu: Menu;

	constructor(title: string, items: SelectItem[], onSelect: (item: SelectItem) => void, onCancel: () => void, visibleRows = 10, hint = "enter select · esc back", tui?: TUI) {
		this.menu = new Menu(title, items, { visibleRows, hint, tui });
		this.menu.onSelect = onSelect;
		this.menu.onCancel = onCancel;
	}

	handleInput(data: string): void {
		this.menu.handleInput(data);
	}

	render(width: number): string[] {
		return this.menu.render(width);
	}

	invalidate(): void {
		this.menu.invalidate();
	}
}


export { Menu } from "./components/Menu.ts";
export type { MenuOptions } from "./components/Menu.ts";
export { SecretForm, SecretInput, TextForm } from "./components/Form.ts";
export type { SecretFormOptions, TextFormOptions } from "./components/Form.ts";
