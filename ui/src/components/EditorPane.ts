// Editor pane chrome (plan §2 mockup, §9 tabs, §23): tab strip, breadcrumb,
// viewport and local status row around an EditorView.
//
// Layout contract: components receive only width, never height, so the future
// shell calls `layout(rows)` with the pane's allocated row count. The pane
// gives the view `rows - 3` viewport rows (tab strip + breadcrumb + status
// row are the fixed chrome). `render(width)` always returns exactly the
// allocated row count, so a shell can trust the pane's height. Tests call
// `layout(rows)` explicitly; the default allocation is 23 rows (the view's
// default 20-row viewport plus chrome).
//
// The pane never mutates document text and never forces tab closes: tab
// selection goes through TabModel, and a dirty close decision belongs to the
// controller via `onCloseTab`. Tab-strip keybindings are deferred to the T3
// command router; keyboard input currently delegates to the view (ctrl+s is
// forwarded to `onSave` when the pane owns focus, mirroring the ADR's
// `editor.save` binding as a fallback under the future command registry).
//
// States are text, never color-only (plan §23): dirty `●`, read-only `[ro]`,
// conflict `!`, plus the LSP status text supplied by the controller through
// `setLspStatus` (no invented states).

import {
	Key,
	matchesKey,
	visibleWidth,
	type Component,
	type Focusable,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { truncateDisplay } from "../design/text.ts";
import { selectListTheme, theme } from "../design/theme.ts";
import type { DocumentRecord } from "../editor/model.ts";
import type { EditorStore } from "../editor/model.ts";
import type { TabModel } from "../editor/tabs.ts";
import type { EditorView } from "../editor/view.ts";
import type { PopupKeyAction } from "./IntelligencePopups.ts";

/** Rows owned by the pane chrome: tab strip, breadcrumb, status row. */
export const EDITOR_PANE_CHROME_ROWS = 3;
/** Chrome rows above the viewport (tab strip + breadcrumb). */
export const EDITOR_PANE_HEADER_ROWS = 2;
/** Default pane allocation before the shell calls `layout(rows)`. */
export const EDITOR_PANE_DEFAULT_ROWS = 23;
/** Default row budget (header included) for the problems slot. */
export const EDITOR_PANE_PROBLEMS_DEFAULT_ROWS = 6;

export interface EditorPaneOptions {
	store: EditorStore;
	tabModel: TabModel;
	view: EditorView;
	requestRender(): void;
	onSelectTab?(documentId: string): void;
	onCloseTab?(documentId: string): void;
	onSave?(): void;
}

/**
 * Callbacks the app wires so the pane can interpret a mounted popup's
 * semantic key actions. Hooks are optional: a popup whose action has no hook
 * is still consumed (the popup is open and owns the key).
 */
export interface IntelligenceHooks {
	/** Enter/Tab on an open popup: accept the current item. */
	accept?(): void;
	/** Esc on an open popup: dismiss it. */
	dismiss?(): void;
	/** Arrow movement inside the popup list. */
	move?(direction: 1 | -1): void;
}

/** Minimal contract a mounted popup exposes for keyboard routing. */
export interface IntelligenceKeySource {
	handleInput(data: string): PopupKeyAction;
}

/** Optional row-budget seam for the bottom problems slot. */
export interface ProblemsBudgetComponent {
	setRowBudget(rows: number): void;
}

interface TabChip {
	documentId: string;
	label: string;
	dirty: boolean;
	readOnly: boolean;
	conflict: boolean;
}

interface TabHitArea {
	documentId: string;
	start: number;
	end: number;
}

/** Scheme-stripped, percent-decoded display path for a document URI. */
export function uriDisplayPath(uri: string): string {
	let path = uri;
	const scheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.exec(path);
	if (scheme) path = path.slice(scheme[0].length);
	try {
		path = decodeURIComponent(path);
	} catch {
		// Malformed percent-encoding: keep the raw path rather than throwing.
	}
	return path;
}

function pathSegments(path: string): string[] {
	return path.split("/").filter((segment) => segment.length > 0);
}

/**
 * Basenames, extended with parent directory segments until duplicate
 * basenames are unambiguous (plan §9: relative-path disambiguation). Chips
 * that cannot be separated (identical URIs, which the store's dedup key
 * prevents) keep their full path.
 */
export function disambiguatedTabLabels(uris: string[]): string[] {
	const segments = uris.map((uri) => pathSegments(uriDisplayPath(uri)));
	const depths = segments.map(() => 1);
	const labelAt = (index: number): string => {
		const parts = segments[index] ?? [];
		if (parts.length === 0) return uris[index] ?? "";
		const depth = Math.min(depths[index] ?? 1, parts.length);
		return parts.slice(parts.length - depth).join("/");
	};
	for (let round = 0; round < 8; round++) {
		const labels = segments.map((_, index) => labelAt(index));
		const counts = new Map<string, number>();
		for (const label of labels) counts.set(label, (counts.get(label) ?? 0) + 1);
		let grew = false;
		for (let index = 0; index < segments.length; index++) {
			const label = labels[index] ?? "";
			const ambiguous = (counts.get(label) ?? 0) > 1;
			const depth = depths[index] ?? 1;
			if (ambiguous && depth < (segments[index]?.length ?? 0)) {
				depths[index] = depth + 1;
				grew = true;
			}
		}
		if (!grew) break;
	}
	return segments.map((_, index) => labelAt(index));
}

function chipText(chip: TabChip): string {
	const markers: string[] = [];
	if (chip.dirty) markers.push("●");
	if (chip.conflict) markers.push("!");
	if (chip.readOnly) markers.push("[ro]");
	return markers.length > 0 ? `[${chip.label} ${markers.join(" ")}]` : `[${chip.label}]`;
}

function clampRows(rows: number): number {
	if (!Number.isFinite(rows)) return EDITOR_PANE_DEFAULT_ROWS;
	return Math.max(1, Math.floor(rows));
}

function normalizeWidth(width: number): number {
	if (!Number.isFinite(width)) return 0;
	return Math.max(0, Math.floor(width));
}

function padRow(row: string, width: number): string {
	const visible = visibleWidth(row);
	if (visible >= width) return row;
	return row + " ".repeat(width - visible);
}

/**
 * Pane chrome around one EditorView: one-row tab strip, breadcrumb, the
 * virtualized viewport and a local status row.
 *
 * The pane is the focusable component in the layout tree (the view itself is
 * not mounted there); `focused` is a proxy that drives the view's caret and
 * hardware-cursor marker, so `tui.setFocus(pane)` is the focus seam the shell
 * uses.
 */
export class EditorPane implements Component, Focusable {
	private readonly store: EditorStore;
	private readonly tabModel: TabModel;
	private readonly view: EditorView;
	private readonly requestRenderCallback: () => void;
	private readonly onSelectTabCallback: ((documentId: string) => void) | undefined;
	private readonly onCloseTabCallback: ((documentId: string) => void) | undefined;
	private readonly onSaveCallback: (() => void) | undefined;
	private lspStatus: string | undefined;
	private allocatedRows = EDITOR_PANE_DEFAULT_ROWS;
	private viewportRows: number;
	private lastRenderedWidth = 0;
	private intelligencePopup: Component | undefined;
	private intelligenceHooks: IntelligenceHooks | undefined;
	private problemsComponent: Component | undefined;
	private problemsRowBudget = EDITOR_PANE_PROBLEMS_DEFAULT_ROWS;
	/** Popup/problems rows measured during the last render. */
	private lastIntelligenceRows = 0;
	private lastProblemsRows = 0;

	/** Focusable proxy: TUI focus on the pane is the view's caret focus. */
	get focused(): boolean {
		return this.view.focused;
	}

	set focused(value: boolean) {
		this.view.focused = value;
		this.requestRenderCallback();
	}

	constructor(options: EditorPaneOptions) {
		this.store = options.store;
		this.tabModel = options.tabModel;
		this.view = options.view;
		this.requestRenderCallback = options.requestRender;
		this.onSelectTabCallback = options.onSelectTab;
		this.onCloseTabCallback = options.onCloseTab;
		this.onSaveCallback = options.onSave;
		this.viewportRows = Math.max(1, this.allocatedRows - EDITOR_PANE_CHROME_ROWS);
	}

	/**
	 * Shell layout call: allocate `rows` rows to the pane. The viewport gets
	 * every row that is not fixed chrome (at least 1); `render()` returns
	 * exactly this allocation.
	 */
	layout(rows: number): void {
		this.allocatedRows = clampRows(rows);
		this.applyViewportAllocation();
		this.requestRenderCallback();
	}

	/** Local LSP status text ("" or undefined hides the segment). Text only. */
	setLspStatus(text: string | undefined): void {
		const next = text && text.length > 0 ? text : undefined;
		if (next === this.lspStatus) return;
		this.lspStatus = next;
		this.requestRenderCallback();
	}

	/**
	 * Mount (or unmount) the intelligence popup slot. The popup renders
	 * directly below the viewport, above the status row; its measured height
	 * is honored from the first render and never shrinks the viewport below
	 * one row. `hooks` connect the popup's semantic key actions to the
	 * machine behind it (see {@link handleIntelligenceKey}).
	 */
	setIntelligence(popup?: Component, hooks?: IntelligenceHooks): void {
		this.intelligencePopup = popup;
		this.intelligenceHooks = popup ? hooks : undefined;
		if (!popup) this.lastIntelligenceRows = 0;
		this.requestRenderCallback();
	}

	/**
	 * Mount (or unmount) the bottom problems section with a fixed row budget
	 * (header included). The budget is shared with the popup and viewport, and
	 * the component's `setRowBudget` seam is fed when it exists.
	 */
	setProblems(component?: Component, rows?: number): void {
		this.problemsComponent = component;
		this.problemsRowBudget =
			rows === undefined
				? EDITOR_PANE_PROBLEMS_DEFAULT_ROWS
				: Math.max(0, Number.isFinite(rows) ? Math.floor(rows) : EDITOR_PANE_PROBLEMS_DEFAULT_ROWS);
		if (!component) this.lastProblemsRows = 0;
		if (component) this.feedProblemsBudget(component);
		this.requestRenderCallback();
	}

	/**
	 * Plan §8 priority contract, called by the app focus router BEFORE the
	 * view sees the key: while a popup is mounted and open, Enter accepts and
	 * Esc closes it (consumed); everything the popup does not claim falls
	 * through to the editor unchanged. With no popup every key falls through.
	 */
	handleIntelligenceKey(data: string): "consumed" | "fallthrough" {
		const popup = this.intelligencePopup;
		if (!popup) return "fallthrough";
		const source = popup as Partial<IntelligenceKeySource>;
		const handler = source.handleInput;
		if (typeof handler !== "function") return "fallthrough";
		const action = handler.call(popup, data);
		switch (action.kind) {
			case "accept":
				this.intelligenceHooks?.accept?.();
				return "consumed";
			case "close":
				this.intelligenceHooks?.dismiss?.();
				return "consumed";
			case "next":
				this.intelligenceHooks?.move?.(1);
				return "consumed";
			case "prev":
				this.intelligenceHooks?.move?.(-1);
				return "consumed";
			case "insert-text":
				return "fallthrough";
			default:
				return "fallthrough";
		}
	}

	/** Viewport rows currently allocated to the editor view. */
	getViewportRows(): number {
		return this.viewportRows;
	}

	handleInput(data: string): boolean {
		if (matchesKey(data, Key.ctrl("s")) && this.onSaveCallback) {
			this.onSaveCallback();
			return true;
		}
		return this.view.handleInput(data);
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		const width =
			Number.isFinite(event.width) && event.width > 0
				? Math.floor(event.width)
				: this.lastRenderedWidth;
		if (event.type === "wheel") {
			if (!this.inViewport(event.y)) return undefined;
			return this.view.handleMouse({
				...event,
				y: event.y - EDITOR_PANE_HEADER_ROWS,
				width,
				height: this.viewportRows,
			});
		}
		if (event.type === "press" && Math.floor(event.y) === 0) {
			const x = Math.floor(event.x);
			const hit = this.tabStrip(width).hitAreas.find((area) => x >= area.start && x < area.end);
			if (!hit) return undefined;
			if (event.button === "middle") {
				this.onCloseTabCallback?.(hit.documentId);
				return { handled: true, focus: true };
			}
			if (event.button === "left") {
				this.selectTab(hit.documentId);
				return { handled: true, focus: true };
			}
			return undefined;
		}
		// Slot bands (below the viewport, above/around the status row): popups
		// and the problems section receive their own local coordinates. Drag and
		// release keep flowing to the view once a viewport gesture captured the
		// pointer; the slots only claim fresh (press/click/move) events.
		const y = Math.floor(event.y);
		const pointerGesture = event.type === "drag" || event.type === "release";
		const popupTop = EDITOR_PANE_HEADER_ROWS + this.viewportRows;
		if (!pointerGesture && this.intelligencePopup && this.lastIntelligenceRows > 0) {
			if (y >= popupTop && y < popupTop + this.lastIntelligenceRows) {
				return this.intelligencePopup.handleMouse?.({
					...event,
					y: y - popupTop,
					width,
					height: this.lastIntelligenceRows,
				});
			}
		}
		const problemsTop = popupTop + this.lastIntelligenceRows + 1;
		if (!pointerGesture && this.problemsComponent && this.lastProblemsRows > 0) {
			if (y >= problemsTop && y < problemsTop + this.lastProblemsRows) {
				return this.problemsComponent.handleMouse?.({
					...event,
					y: y - problemsTop,
					width,
					height: this.lastProblemsRows,
				});
			}
		}
		if (event.type === "drag" || event.type === "release" || this.inViewport(event.y)) {
			// Viewport-local coordinates: chrome rows sit above the view.
			return this.view.handleMouse({
				...event,
				y: event.y - EDITOR_PANE_HEADER_ROWS,
				width,
				height: this.viewportRows,
			});
		}
		return undefined;
	}

	render(width: number): string[] {
		const safeWidth = normalizeWidth(width);
		this.lastRenderedWidth = safeWidth;
		const strip = this.tabStrip(safeWidth);

		// The popup and problems slots share one budget with the viewport: at
		// least one viewport row always survives, and the popup wins contested
		// rows because it carries the focused interaction.
		const slotBudget = Math.max(0, this.allocatedRows - EDITOR_PANE_CHROME_ROWS - 1);
		const popupLines = this.intelligencePopup ? this.intelligencePopup.render(safeWidth) : [];
		const popupShown = popupLines.slice(0, slotBudget);
		const problemsRows = this.problemsComponent
			? Math.min(this.problemsRowBudget, Math.max(0, slotBudget - popupShown.length))
			: 0;
		const viewportRows = Math.max(
			1,
			this.allocatedRows - EDITOR_PANE_CHROME_ROWS - popupShown.length - problemsRows,
		);
		this.lastIntelligenceRows = popupShown.length;
		this.lastProblemsRows = problemsRows;
		this.viewportRows = viewportRows;
		this.view.setViewportRows(viewportRows);

		const viewLines = this.view.render(safeWidth);
		while (viewLines.length < viewportRows) viewLines.push(" ".repeat(safeWidth));
		const problemLines = problemsRows > 0 ? this.renderProblems(safeWidth, problemsRows) : [];
		const lines = [
			strip.text,
			this.breadcrumb(safeWidth),
			...viewLines.slice(0, viewportRows),
			...popupShown,
			this.statusRow(),
			...problemLines,
		];
		return lines
			.slice(0, this.allocatedRows)
			.map((line) => padRow(truncateDisplay(line, safeWidth), safeWidth));
	}

	invalidate(): void {
		this.view.invalidate();
	}

	// ------------------------------------------------------------- internals

	/** Recomputed viewport from the last measured slot heights. */
	private applyViewportAllocation(): void {
		const slotBudget = Math.max(0, this.allocatedRows - EDITOR_PANE_CHROME_ROWS - 1);
		const popupRows = Math.min(this.lastIntelligenceRows, slotBudget);
		const problemsRows = this.problemsComponent
			? Math.min(this.problemsRowBudget, Math.max(0, slotBudget - popupRows))
			: 0;
		this.viewportRows = Math.max(
			1,
			this.allocatedRows - EDITOR_PANE_CHROME_ROWS - popupRows - problemsRows,
		);
		this.view.setViewportRows(this.viewportRows);
	}

	private feedProblemsBudget(component: Component): void {
		const target = component as Partial<ProblemsBudgetComponent>;
		if (typeof target.setRowBudget === "function") target.setRowBudget(this.problemsRowBudget);
	}

	/** Render the bottom section to exactly its reserved rows. */
	private renderProblems(width: number, rows: number): string[] {
		const component = this.problemsComponent;
		if (!component || rows <= 0) return [];
		const rendered = component.render(width);
		const out = rendered.slice(0, rows).map((line) => truncateDisplay(line, width));
		while (out.length < rows) out.push(" ".repeat(width));
		return out;
	}

	private activeRecord(): DocumentRecord | undefined {
		const documentId = this.tabModel.activeDocumentId();
		return documentId ? this.store.getDocument(documentId) : undefined;
	}

	private inViewport(y: number): boolean {
		return y >= EDITOR_PANE_HEADER_ROWS && y < EDITOR_PANE_HEADER_ROWS + this.viewportRows;
	}

	private selectTab(documentId: string): void {
		const result = this.tabModel.setActive(documentId);
		if (!result.ok) return;
		this.view.setDocument(documentId);
		this.onSelectTabCallback?.(documentId);
		this.requestRenderCallback();
	}

	private tabChips(): TabChip[] {
		const tabs = this.tabModel.list();
		const uris = tabs.map((tab) => this.store.getDocument(tab.documentId)?.uri ?? tab.documentId);
		const labels = disambiguatedTabLabels(uris);
		return tabs.map((tab, index) => {
			const record = this.store.getDocument(tab.documentId);
			return {
				documentId: tab.documentId,
				label: labels[index] ?? tab.documentId,
				dirty: record?.dirty ?? false,
				readOnly: record?.readOnly ?? false,
				conflict: record?.conflict !== undefined,
			};
		});
	}

	/**
	 * One stable strip row: active chip always shown; a deterministic
	 * contiguous window around it grows until width is exhausted, and the
	 * hidden remainder is reported as a trailing `+N` indicator.
	 */
	private tabStrip(width: number): { text: string; hitAreas: TabHitArea[] } {
		const chips = this.tabChips();
		if (chips.length === 0) return { text: theme.dim("No open files"), hitAreas: [] };
		const active = this.tabModel.activeDocumentId();
		const styles = chips.map((chip) =>
			chip.documentId === active ? selectListTheme.selectedText(chipText(chip)) : theme.muted(chipText(chip)),
		);
		const widths = styles.map((style) => visibleWidth(style));
		const total = widths.reduce((sum, chipWidth) => sum + chipWidth, 0) + Math.max(0, chips.length - 1);
		const hitAreas: TabHitArea[] = [];
		let lo = 0;
		let hi = chips.length - 1;
		if (total > width) {
			let activeIndex = chips.findIndex((chip) => chip.documentId === active);
			if (activeIndex < 0) activeIndex = 0;
			const hiddenSuffix = (hidden: number): string => theme.dim(`+${hidden}`);
			const windowWidth = (from: number, to: number): number => {
				let sum = 0;
				for (let index = from; index <= to; index++) sum += widths[index] ?? 0;
				sum += to - from; // one separator between adjacent visible chips
				const hidden = chips.length - (to - from + 1);
				if (hidden > 0) sum += 1 + visibleWidth(hiddenSuffix(hidden));
				return sum;
			};
			lo = activeIndex;
			hi = activeIndex;
			for (;;) {
				if (hi + 1 < chips.length && windowWidth(lo, hi + 1) <= width) {
					hi += 1;
				} else if (lo - 1 >= 0 && windowWidth(lo - 1, hi) <= width) {
					lo -= 1;
				} else {
					break;
				}
			}
		}
		let text = "";
		for (let index = lo; index <= hi; index++) {
			if (index > lo) text += " ";
			const start = visibleWidth(text);
			const style = styles[index] ?? "";
			text += style;
			hitAreas.push({ documentId: chips[index]?.documentId ?? "", start, end: start + (widths[index] ?? 0) });
		}
		const hidden = chips.length - (hi - lo + 1);
		if (hidden > 0) text += ` ${theme.dim(`+${hidden}`)}`;
		return { text, hitAreas };
	}

	private breadcrumb(width: number): string {
		const record = this.activeRecord();
		if (!record) return theme.dim("No file open");
		const language = theme.dim(record.languageId);
		const languageWidth = visibleWidth(language);
		const pathBudget = Math.max(0, width - languageWidth - 2);
		const left = theme.muted(truncateDisplay(uriDisplayPath(record.uri), pathBudget));
		const gap = Math.max(1, width - visibleWidth(left) - languageWidth);
		return `${left}${" ".repeat(gap)}${language}`;
	}

	private statusRow(): string {
		const record = this.activeRecord();
		if (!record) return theme.dim("No open document");
		const position = this.view.cursorDisplayPosition();
		const parts = [
			theme.text(`Ln ${position.line}, Col ${position.column}`),
			theme.dim("UTF-8"),
			theme.dim(record.lineEnding === "mixed" ? "mixed endings" : record.lineEnding),
			theme.dim(record.languageId),
		];
		if (this.lspStatus) parts.push(theme.dim(this.lspStatus));
		if (record.dirty) parts.push(theme.warning("●"));
		if (record.readOnly) parts.push(theme.dim("[ro]"));
		if (record.conflict) parts.push(theme.error("!"));
		return parts.join(theme.dim(" · "));
	}
}
