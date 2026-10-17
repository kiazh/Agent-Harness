// Intelligence popups (plan §23, T9 UI): presentational completion and hover
// components. The coordination lives in `editor/intelligence.ts`; these classes
// only render a snapshot and translate input/pointer events into semantics.
//
// Contracts:
// - `CompletionPopup.handleInput` NEVER mutates machine state. It returns a
//   semantic action (`accept`/`next`/`prev`/`close`/`insert-text`/`unhandled`)
//   and the parent owns the interpretation (consume or fall through). Mouse
//   interaction is self-contained: press selects, click reports the clicked
//   row (`onSelect`) and then accepts through the `onAction` callback, hover
//   only highlights.
// - Everything renders bounded: at most 8 item rows (plus a `+N more`
//   disclosure), and every line is truncated to the viewport width.
// - Selection is text, never color-only: the selected row starts with `▶`.
// - Hover text is untrusted: `HoverPopup` renders through `wrapDisplay`
//   (which sanitizes) and never interprets links or markup.

import {
	Key,
	matchesKey,
	styleTextWithAnsi,
	type Component,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { normalizeWidth, sanitizeDisplay, truncateDisplay, wrapDisplay } from "../design/text.ts";
import { selectionBar, theme } from "../design/theme.ts";

/** Semantic result of one popup key press; the parent decides what to do. */
export type PopupKeyAction =
	| { kind: "accept" }
	| { kind: "next" }
	| { kind: "prev" }
	| { kind: "close" }
	| { kind: "insert-text"; text: string }
	| { kind: "unhandled" };

function clampIndex(index: number, count: number): number {
	if (count <= 0) return 0;
	if (!Number.isFinite(index)) return 0;
	return Math.max(0, Math.min(count - 1, Math.floor(index)));
}

/** Printable text only: escapes, controls and C1 bytes never become insert input. */
function printableInput(data: string): string | undefined {
	if (data.length === 0) return undefined;
	if (data.includes("\x1b") || data.includes("\x9b")) return undefined;
	for (const ch of data) {
		const code = ch.charCodeAt(0);
		if (code < 32 || code === 0x7f || (code >= 0x80 && code <= 0x9f)) return undefined;
	}
	return data;
}

// ---------------------------------------------------------------- completion

export interface CompletionPopupItem {
	label: string;
	kind?: number;
	detail?: string;
}

export interface CompletionPopupModel {
	items: readonly CompletionPopupItem[];
	selectedIndex: number;
	omitted: number;
}

/**
 * Read-only view over a completion machine (e.g. `CompletionSession`), so the
 * popup can snapshot without importing the machine module.
 */
export interface CompletionListSource {
	items(): readonly CompletionPopupItem[];
	selectedIndex(): number;
	omitted(): number;
}

/** Snapshot a machine view into a popup model. */
export function completionPopupModelFrom(source: CompletionListSource): CompletionPopupModel {
	return {
		items: source.items().map((item) => ({ label: item.label, kind: item.kind, detail: item.detail })),
		selectedIndex: source.selectedIndex(),
		omitted: source.omitted(),
	};
}

/** Visible item rows (the `+N more` disclosure may consume one). */
export const COMPLETION_POPUP_VISIBLE_ROWS = 8;

/** LSP CompletionItemKind glyphs; a text marker always accompanies a label. */
export const COMPLETION_KIND_GLYPHS: Readonly<Record<number, string>> = {
	1: "t", // Text
	2: "m", // Method
	3: "f", // Function
	4: "c", // Constructor
	5: "▪", // Field
	6: "v", // Variable
	7: "C", // Class
	8: "I", // Interface
	9: "M", // Module
	10: "p", // Property
	11: "u", // Unit
	12: "V", // Value
	13: "E", // Enum
	14: "k", // Keyword
	15: "s", // Snippet
	16: "◆", // Color
	17: "F", // File
	18: "r", // Reference
	19: "D", // Folder
	20: "e", // EnumMember
	21: "K", // Constant
	22: "S", // Struct
	23: "!", // Event
	24: "+", // Operator
	25: "T", // TypeParameter
};

export function completionKindGlyph(kind: number | undefined): string {
	if (kind === undefined) return "·";
	return COMPLETION_KIND_GLYPHS[kind] ?? "·";
}

/**
 * Bounded completion list. The parent owns the keyboard contract; this
 * component owns presentation, its own row window, and pointer semantics.
 */
export class CompletionPopup implements Component {
	/** Mouse-click acceptance sink; the parent wires it to the machine. */
	onAction: ((action: PopupKeyAction) => void) | undefined;

	private readonly maxVisibleRows: number;
	/** Clicked-index sink: lets the parent's machine selection follow the click. */
	private readonly onSelectCallback: ((index: number) => void) | undefined;
	private modelValue: CompletionPopupModel | undefined;
	private hoverIndex: number | undefined;

	constructor(options: { maxVisibleRows?: number; onSelect?: (index: number) => void } = {}) {
		this.maxVisibleRows = Math.max(1, options.maxVisibleRows ?? COMPLETION_POPUP_VISIBLE_ROWS);
		this.onSelectCallback = options.onSelect;
	}

	setModel(model: CompletionPopupModel | undefined): void {
		if (!model || model.items.length === 0) {
			this.modelValue = undefined;
			this.hoverIndex = undefined;
			return;
		}
		this.modelValue = {
			items: model.items,
			selectedIndex: clampIndex(model.selectedIndex, model.items.length),
			omitted: Number.isFinite(model.omitted) ? Math.max(0, Math.floor(model.omitted)) : 0,
		};
		this.hoverIndex = undefined;
	}

	isOpen(): boolean {
		return this.modelValue !== undefined && this.modelValue.items.length > 0;
	}

	selectedIndex(): number {
		return this.modelValue?.selectedIndex ?? 0;
	}

	selectIndex(index: number): number {
		const model = this.modelValue;
		if (!model) return 0;
		model.selectedIndex = clampIndex(index, model.items.length);
		return model.selectedIndex;
	}

	moveSelection(delta: number): number {
		const model = this.modelValue;
		if (!model) return 0;
		model.selectedIndex = clampIndex(model.selectedIndex + delta, model.items.length);
		return model.selectedIndex;
	}

	/**
	 * Pure key mapping: no state changes. When the list is closed every key is
	 * `unhandled` so Enter can safely reach the editor.
	 */
	handleInput(data: string): PopupKeyAction {
		if (!this.isOpen()) return { kind: "unhandled" };
		if (matchesKey(data, Key.enter) || matchesKey(data, Key.tab)) return { kind: "accept" };
		if (matchesKey(data, Key.up)) return { kind: "prev" };
		if (matchesKey(data, Key.down)) return { kind: "next" };
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) return { kind: "close" };
		const text = printableInput(data);
		if (text !== undefined) return { kind: "insert-text", text };
		return { kind: "unhandled" };
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		const model = this.modelValue;
		if (!model || model.items.length === 0) return undefined;
		const geometry = this.geometry();
		const y = Number.isFinite(event.y) ? Math.floor(event.y) : -1;
		const index = y >= 0 && y < geometry.end - geometry.start ? geometry.start + y : -1;
		const overItem = index >= 0 && index < model.items.length;
		if (event.type === "move") {
			const hover = overItem ? index : undefined;
			if (hover === this.hoverIndex) return undefined;
			this.hoverIndex = hover;
			return { handled: true, render: true };
		}
		if (!overItem || event.button !== "left") return undefined;
		if (event.type === "press") {
			this.selectIndex(index);
			return { handled: true, focus: true };
		}
		// Completed left clicks only (a press falls through to the renderer's
		// click synthesis, mirroring the explorer grip): the parent hears the
		// clicked index first so its own selection matches the row the user
		// clicked, then accepts.
		if (event.type === "click") {
			this.selectIndex(index);
			this.onSelectCallback?.(index);
			this.onAction?.({ kind: "accept" });
			return { handled: true, focus: true };
		}
		return undefined;
	}

	render(width: number): string[] {
		const model = this.modelValue;
		if (!model || model.items.length === 0) return [];
		const content = normalizeWidth(width);
		const geometry = this.geometry();
		const lines: string[] = [];
		for (let index = geometry.start; index < geometry.end; index++) {
			const item = model.items[index];
			if (!item) continue;
			const selected = index === geometry.selected;
			const marker = selected ? "▶" : " ";
			const glyph = completionKindGlyph(item.kind);
			const label = sanitizeDisplay(item.label);
			const detail = item.detail ? `  ${sanitizeDisplay(item.detail)}` : "";
			let row = truncateDisplay(`${marker} ${glyph} ${label}${detail}`, content);
			if (selected) {
				row = selectionBar(row, content);
			} else if (index === this.hoverIndex) {
				row = styleTextWithAnsi(row, undefined, undefined, { underline: true });
			}
			lines.push(row);
		}
		if (geometry.footer) {
			lines.push(truncateDisplay(theme.dim(`+${geometry.omitted} more`), content));
		}
		return lines;
	}

	invalidate(): void {}

	// ------------------------------------------------------------- internals

	/** The bounded row window: selection kept visible, footer when hidden. */
	private geometry(): { start: number; end: number; selected: number; footer: boolean; omitted: number } {
		const model = this.modelValue as CompletionPopupModel;
		const total = model.items.length;
		const selected = clampIndex(model.selectedIndex, total);
		const footer = model.omitted > 0 || total > this.maxVisibleRows;
		const itemRows = Math.max(1, footer ? this.maxVisibleRows - 1 : this.maxVisibleRows);
		const start = Math.max(0, Math.min(selected < itemRows ? 0 : selected - itemRows + 1, Math.max(0, total - itemRows)));
		const end = Math.min(total, start + itemRows);
		return { start, end, selected, footer, omitted: total - (end - start) + model.omitted };
	}
}

// --------------------------------------------------------------------- hover

export const HOVER_POPUP_MAX_WIDTH = 80;
export const HOVER_POPUP_MAX_ROWS = 15;

/**
 * Sanitized hover text box, clamped to 80 columns and 15 rows. It is never a
 * focus target ("dims when not focused"): `focused` only selects the paint
 * style, and links/markup are rendered as plain text by construction.
 */
export class HoverPopup implements Component {
	focused = false;

	private text: string | undefined;

	setText(text: string | undefined): void {
		this.text = text !== undefined && text.length > 0 ? text : undefined;
	}

	getText(): string | undefined {
		return this.text;
	}

	isEmpty(): boolean {
		return this.text === undefined;
	}

	/**
	 * Hover never accepts: Escape/ctrl+c request dismissal, everything else
	 * falls through to the editor (which keeps typing in the same rhythm).
	 */
	handleInput(data: string): PopupKeyAction {
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) return { kind: "close" };
		return { kind: "unhandled" };
	}

	render(width: number): string[] {
		const text = this.text;
		if (text === undefined) return [];
		const content = normalizeWidth(width);
		const wrapWidth = Math.max(1, Math.min(content, HOVER_POPUP_MAX_WIDTH));
		const rows = wrapDisplay(text, wrapWidth).slice(0, HOVER_POPUP_MAX_ROWS);
		const paint = this.focused ? theme.text : theme.dim;
		return rows.map((row) => paint(row));
	}

	invalidate(): void {}
}
