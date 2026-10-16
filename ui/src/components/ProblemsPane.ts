// Problems pane (plan §23, T9 UI): a bounded, focusable diagnostics list fed
// by the ProblemsModel. Rendering is severity-marker-first (text, never
// color-only): `✗` error, `!` warning, `i` info, `~` hint, and the selected
// row carries a `▶` marker in addition to any styling.
//
// Row format: `path:line:col severity message (source)`, with unversioned
// producer entries labeled `[unversioned]`. Rows are windowed around the
// selection with a `+N more` disclosure; the header line always reports the
// full counts, so a bounded view never misrepresents what exists.

import {
	Key,
	matchesKey,
	type Component,
	type Focusable,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { displayWidth, sanitizeDisplay, truncateDisplay } from "../design/text.ts";
import { selectionBar, theme } from "../design/theme.ts";
import { uriDisplayPath } from "./EditorPane.ts";
import type { ProblemRow, ProblemsModel, SeverityCounts } from "../editor/intelligence.ts";

/** Total rows (header included) allocated to the pane by default. */
export const PROBLEMS_PANE_DEFAULT_ROWS = 6;

/** Text severity markers; every severity keeps meaning without color. */
export const SEVERITY_MARKERS: Readonly<Record<number, string>> = {
	1: "✗",
	2: "!",
	3: "i",
	4: "~",
};

/** Header counts, always in the same order: `✗ N · ! M · i K` (+ `~ H`). */
export function problemsHeaderText(counts: SeverityCounts): string {
	const parts = [`✗ ${counts.error}`, `! ${counts.warning}`, `i ${counts.info}`];
	if (counts.hint > 0) parts.push(`~ ${counts.hint}`);
	return parts.join(" · ");
}

/** One diagnostic row: `path:line:col severity message (source)`. */
export function formatProblemRow(row: ProblemRow): string {
	const where = `${uriDisplayPath(row.uri)}:${row.line + 1}:${row.character + 1}`;
	const marker = SEVERITY_MARKERS[row.severity] ?? "?";
	const message = sanitizeDisplay(row.message);
	const source = row.source !== undefined && row.source.length > 0 ? ` (${sanitizeDisplay(row.source)})` : "";
	const unversioned = row.version === null ? " [unversioned]" : "";
	return `${where} ${marker} ${message}${source}${unversioned}`;
}

function normalizeWidth(width: number): number {
	if (!Number.isFinite(width)) return 0;
	return Math.max(0, Math.floor(width));
}

function padRow(row: string, width: number): string {
	const visible = displayWidth(row);
	if (visible >= width) return row;
	return row + " ".repeat(width - visible);
}

export interface ProblemsPaneOptions {
	model: ProblemsModel;
	requestRender?: () => void;
	/** Enter on the selected row. */
	onOpen?: (row: ProblemRow) => void;
	/** Esc dismissal request; the pane never closes itself. */
	onClose?: () => void;
	/** Total row budget including the header; default 6. */
	rowBudget?: number;
}

/**
 * Bounded diagnostics list. `render(width)` returns exactly its row budget so
 * the parent pane can reserve the section without measuring it.
 */
export class ProblemsPane implements Component, Focusable {
	focused = false;
	/** App focus-router seam: invoked when a mouse event claims focus. */
	onFocusRequested?: () => void;

	private readonly model: ProblemsModel;
	private readonly requestRenderCallback: (() => void) | undefined;
	private readonly onOpenCallback: ((row: ProblemRow) => void) | undefined;
	private readonly onCloseCallback: (() => void) | undefined;
	private rowBudgetValue: number;
	private selectionValue = 0;
	/** Geometry from the last render, for mouse hit-testing. */
	private lastStart = 0;
	private lastShown = 0;

	constructor(options: ProblemsPaneOptions) {
		this.model = options.model;
		this.requestRenderCallback = options.requestRender;
		this.onOpenCallback = options.onOpen;
		this.onCloseCallback = options.onClose;
		this.rowBudgetValue = Math.max(
			1,
			Number.isFinite(options.rowBudget ?? PROBLEMS_PANE_DEFAULT_ROWS)
				? Math.floor(options.rowBudget ?? PROBLEMS_PANE_DEFAULT_ROWS)
				: PROBLEMS_PANE_DEFAULT_ROWS,
		);
	}

	/** Total row budget including the header line. */
	setRowBudget(rows: number): void {
		const next = Math.max(1, Number.isFinite(rows) ? Math.floor(rows) : PROBLEMS_PANE_DEFAULT_ROWS);
		if (next === this.rowBudgetValue) return;
		this.rowBudgetValue = next;
		this.requestRenderCallback?.();
	}

	getRowBudget(): number {
		return this.rowBudgetValue;
	}

	selectedIndex(): number {
		return this.selectionValue;
	}

	selectedRow(): ProblemRow | undefined {
		return this.model.rows()[this.selectionValue];
	}

	/** Move the selection by `delta` with clamping; returns the new index. */
	moveSelection(delta: number): number {
		const count = this.model.rows().length;
		const amount = Number.isFinite(delta) ? Math.floor(delta) : 0;
		this.selectionValue = count === 0 ? 0 : Math.max(0, Math.min(count - 1, this.selectionValue + amount));
		this.requestRenderCallback?.();
		return this.selectionValue;
	}

	handleInput(data: string): boolean {
		if (matchesKey(data, Key.escape)) {
			this.onCloseCallback?.();
			return true;
		}
		const count = this.model.rows().length;
		if (count === 0) return false;
		if (matchesKey(data, Key.up)) {
			this.moveSelection(-1);
			return true;
		}
		if (matchesKey(data, Key.down)) {
			this.moveSelection(1);
			return true;
		}
		if (matchesKey(data, Key.pageUp)) {
			this.moveSelection(-this.pageSize());
			return true;
		}
		if (matchesKey(data, Key.pageDown)) {
			this.moveSelection(this.pageSize());
			return true;
		}
		if (matchesKey(data, Key.home)) {
			this.moveSelection(-count);
			return true;
		}
		if (matchesKey(data, Key.end)) {
			this.moveSelection(count);
			return true;
		}
		if (matchesKey(data, Key.enter)) {
			const row = this.model.rows()[this.selectionValue];
			if (row) this.onOpenCallback?.(row);
			return true;
		}
		return false;
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		if (event.button !== "left") return undefined;
		const y = Number.isFinite(event.y) ? Math.floor(event.y) : -1;
		// Row 0 is the header; item rows start below it.
		if (y <= 0 || y > this.lastShown) return undefined;
		const index = this.lastStart + (y - 1);
		const row = this.model.rows()[index];
		if (!row) return undefined;
		this.onFocusRequested?.();
		if (event.type === "press") {
			this.selectionValue = index;
			this.requestRenderCallback?.();
			return { handled: true, focus: true };
		}
		if (event.type === "click") {
			this.selectionValue = index;
			this.onOpenCallback?.(row);
			return { handled: true, focus: true };
		}
		return undefined;
	}

	render(width: number): string[] {
		const content = normalizeWidth(width);
		const budget = Math.max(1, this.rowBudgetValue);
		const rows = this.model.rows();
		const counts = this.model.counts();
		const omittedTotal = this.model.omitted();
		this.selectionValue = rows.length === 0 ? 0 : Math.max(0, Math.min(rows.length - 1, this.selectionValue));

		const lines: string[] = [truncateDisplay(this.headerText(counts), content)];
		const bodyBudget = budget - 1;
		if (bodyBudget > 0) {
			if (rows.length === 0) {
				lines.push(truncateDisplay(theme.dim("No problems"), content));
			} else {
				const hiddenTotal = Math.max(0, rows.length - bodyBudget);
				const needFooter = hiddenTotal > 0 || omittedTotal > 0;
				let shown = Math.min(rows.length, bodyBudget);
				let footer = false;
				if (needFooter && bodyBudget >= 2) {
					shown = Math.min(rows.length, bodyBudget - 1);
					footer = true;
				} else if (needFooter) {
					// One body row and hidden content: the disclosure wins.
					shown = 0;
					footer = true;
				}
				let start = 0;
				if (shown > 0) {
					start = this.selectionValue < shown ? 0 : this.selectionValue - shown + 1;
					start = Math.max(0, Math.min(start, Math.max(0, rows.length - shown)));
				}
				this.lastStart = start;
				this.lastShown = shown;
				for (let index = start; index < start + shown; index++) {
					const row = rows[index];
					if (!row) continue;
					const selected = index === this.selectionValue;
					const marker = selected ? "▶" : " ";
					let line = truncateDisplay(`${marker} ${formatProblemRow(row)}`, content);
					if (selected) line = selectionBar(line, content);
					lines.push(line);
				}
				if (footer) {
					const hidden = rows.length - shown;
					lines.push(truncateDisplay(theme.dim(`+${hidden + omittedTotal} more`), content));
				}
			}
		}
		while (lines.length < budget) lines.push(" ".repeat(content));
		return lines.slice(0, budget).map((line) => padRow(line, content));
	}

	invalidate(): void {}

	// ------------------------------------------------------------- internals

	private headerText(counts: SeverityCounts): string {
		const zero = counts.total === 0;
		const part = (marker: string, value: number, paint: (text: string) => string): string =>
			zero ? theme.dim(`${marker} ${value}`) : paint(`${marker} ${value}`);
		const parts = [
			part("✗", counts.error, theme.error),
			part("!", counts.warning, theme.warning),
			part("i", counts.info, theme.info),
		];
		if (counts.hint > 0) parts.push(theme.muted(`~ ${counts.hint}`));
		return parts.join(theme.dim(" · "));
	}

	/** Body rows per page (the header and a possible footer row are chrome). */
	private pageSize(): number {
		return Math.max(1, this.rowBudgetValue - 2);
	}
}
