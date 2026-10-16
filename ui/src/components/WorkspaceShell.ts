// Workspace composition (plan §7 + UX batch): the divider, the far-right
// explorer grip, and the retained editor/explorer columns that AppShell places
// beside the chat transcript. Every pane rectangle comes from computeLayout();
// this module never re-derives a split. The composer lives in the shell root
// as a full-width row (AppShell), never inside this row.
//
// Injected components are never recreated — recomposition only swaps stack
// wrappers around the same instances, so editor scroll/selection state and
// duplicated-scroll anchors survive mode and density changes.

import {
	type Component,
	type StackEntry,
	type TUI,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { theme } from "../design/theme.ts";
import { chatIsRight, clampSplitRatio, computeLayout } from "../workspace/layout.ts";
import { LAYOUT_BUDGETS, type LayoutInput, type LayoutResult, type Rect } from "../workspace/state.ts";

export interface WorkspaceShellOptions {
	/**
	 * New persisted split ratio computed from a divider drag (or a divider
	 * double-click reset). The app persists the value and feeds it back through
	 * AppShell.setWorkspaceState.
	 */
	onRatioChange(ratio: number): void;
	/** The composed columns changed (geometry or injected slots); rebuild the root. */
	onResizeDemand?(): void;
	/**
	 * The far-right grip was clicked: toggle the explorer. Optional so hosts
	 * without an explorer wiring stay composable (the grip stays inert).
	 */
	onExplorerToggle?(): void;
}

function sameRect(a: Rect | undefined, b: Rect | undefined): boolean {
	if (!a || !b) return a === b;
	return a.x === b.x && a.y === b.y && a.columns === b.columns && a.rows === b.rows;
}

/** True when two layout results compose the exact same tree (field compare, not identity). */
export function sameLayoutGeometry(a: LayoutResult | undefined, b: LayoutResult | undefined): boolean {
	if (!a || !b) return a === b;
	return a.effectiveMode === b.effectiveMode
		&& a.reason === b.reason
		&& a.explorerOverlay === b.explorerOverlay
		&& sameRect(a.chat, b.chat)
		&& sameRect(a.editor, b.editor)
		&& sameRect(a.explorer, b.explorer)
		&& sameRect(a.grip, b.grip);
}

/** Placeholder occupying a geometry-reserved column until its component arrives. */
function blankComponent(): Component {
	return {
		render: () => [],
		invalidate: () => {},
	};
}

/**
 * One-column divider: a neutral vertical rule and the press→capture drag that
 * maps screen columns to the persisted ratio range. Pane minima stay the
 * geometry's job; this component only clamps to the ordinary 0.25–0.75 range.
 */
export class WorkspaceDivider implements Component {
	private readonly tui: TUI;
	private readonly options: WorkspaceShellOptions;
	private result: LayoutResult | undefined;
	private dragging = false;

	constructor(tui: TUI, options: WorkspaceShellOptions) {
		this.tui = tui;
		this.options = options;
	}

	/** Latest geometry snapshot; drag columns map through the same pane widths. */
	setResult(result: LayoutResult | undefined): void {
		this.result = result;
	}

	get isDragging(): boolean {
		return this.dragging;
	}

	/**
	 * Map a pointer screen column to a persisted ratio through the last
	 * snapshot: the pointer column is the pane width the user is asking for
	 * (chat on the left, editor on the right), and the ratio divides the same
	 * resizable span computeLayout used (divider and grip excluded).
	 */
	private ratioAt(screenX: number): number | undefined {
		const result = this.result;
		if (!result || result.effectiveMode !== "split" || !result.chat || !result.editor) return undefined;
		const total = result.chat.columns + LAYOUT_BUDGETS.dividerColumns + result.editor.columns
			+ (result.explorer?.columns ?? 0) + (result.grip?.columns ?? 0);
		const usable =
			total - LAYOUT_BUDGETS.dividerColumns - (result.grip?.columns ?? 0);
		if (usable <= 0) return undefined;
		// With the chat on the right the pointer column is the EDITOR width
		// asked for, i.e. the mirror of the same ratio.
		const requested = chatIsRight(result) ? usable - screenX : screenX;
		return clampSplitRatio(requested / usable);
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		switch (event.type) {
			case "press": {
				if (event.button !== "left") return undefined;
				this.dragging = true;
				// Capture routes drag/release here (even outside the column).
				// No focus yet: the divider has no keyboard behavior and must not
				// take the composer's; keyboard resize will add `focus: true` here.
				return { capture: true };
			}
			case "drag": {
				// A drag without a preceding press is not ours.
				if (!this.dragging) return undefined;
				return this.applyPointer(event.screenX);
			}
			case "move": {
				// Motion without a held button (any-event tracking): consume while
				// dragging, but only a button-held drag changes the ratio.
				if (!this.dragging) return undefined;
				return { handled: true, render: false };
			}
			case "click": {
				// Double-click resets the 50/50 split (one click of a double-click
				// sequence never resized anything, so the first click stays inert).
				if ((event.clickCount ?? 1) >= 2) {
					this.dragging = false;
					this.options.onRatioChange(LAYOUT_BUDGETS.defaultSplitRatio);
					return { handled: true };
				}
				if (!this.dragging) return undefined;
				this.dragging = false;
				return { handled: true };
			}
			case "release": {
				if (!this.dragging) return undefined;
				this.dragging = false;
				return { handled: true };
			}
			default:
				return undefined;
		}
	}

	private applyPointer(screenX: number): TuiMouseEventResult {
		const ratio = this.ratioAt(screenX);
		if (ratio !== undefined) this.options.onRatioChange(ratio);
		// One coalesced render request per move; the frame re-collects all moves.
		this.tui.requestRender();
		return { handled: true, render: false };
	}

	render(width: number): string[] {
		if (width <= 0) return [];
		const rows = this.result?.chat?.rows ?? this.result?.editor?.rows ?? 0;
		if (rows <= 0) return [];
		// The HStack clips the column to its real height after the footer and
		// gap rows are taken; rendering the full viewport height paints the rule
		// to the last row without duplicating the shell's row math.
		const rule = theme.border("│");
		return Array.from({ length: rows }, () => rule);
	}

	invalidate(): void {}
}

/**
 * One-column explorer grip at the viewport's far right: a faint notch at the
 * top and a mid-row hint; a click toggles the explorer through the host's
 * callback. Deliberately tiny chrome: it never takes focus and never widens
 * beyond its single geometry column.
 */
export class ExplorerGrip implements Component {
	private readonly options: WorkspaceShellOptions;
	private result: LayoutResult | undefined;

	constructor(options: WorkspaceShellOptions) {
		this.options = options;
	}

	setResult(result: LayoutResult | undefined): void {
		this.result = result;
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		if (event.button !== "left") return undefined;
		// Completed clicks only: a press falls through to the renderer's click
		// synthesis, so one gesture is exactly one toggle.
		if (event.type !== "click") return undefined;
		if (!this.options.onExplorerToggle) return undefined;
		this.options.onExplorerToggle();
		return { handled: true, render: true };
	}

	render(width: number): string[] {
		if (width <= 0) return [];
		const rows = this.result?.editor?.rows ?? this.result?.chat?.rows ?? 0;
		if (rows <= 0) return [];
		const lines = Array.from({ length: rows }, () => " ");
		lines[0] = theme.dim("▐");
		const mid = Math.floor(rows / 2);
		if (mid > 0 && mid < rows) lines[mid] = theme.dim("‹");
		return lines;
	}

	invalidate(): void {}
}

/**
 * Owns the workspace half of the shell: the divider, the far-right explorer
 * grip, the retained editor and explorer components, and the latest
 * LayoutResult. AppShell asks for fixed-basis HStack entries and does the
 * linear composition itself.
 */
export class WorkspaceShell {
	private readonly options: WorkspaceShellOptions;
	private readonly dividerComponent: WorkspaceDivider;
	private readonly gripComponent: ExplorerGrip;
	private readonly editorPlaceholder = blankComponent();
	private readonly explorerPlaceholder = blankComponent();
	private input: LayoutInput | undefined;
	private result: LayoutResult | undefined;
	private editorComponent: Component | undefined;
	private explorerComponent: Component | undefined;

	constructor(tui: TUI, options: WorkspaceShellOptions) {
		this.options = options;
		this.dividerComponent = new WorkspaceDivider(tui, options);
		this.gripComponent = new ExplorerGrip(options);
	}

	/** Latest computed geometry (undefined until the first setState). */
	get layout(): LayoutResult | undefined {
		return this.result;
	}

	/** Last stored intent, with the dimensions actually used. */
	get state(): LayoutInput | undefined {
		return this.input;
	}

	get editor(): Component | undefined {
		return this.editorComponent;
	}

	get explorer(): Component | undefined {
		return this.explorerComponent;
	}

	/** The explorer cannot dock at this size; the editor hosts it as an overlay (later task). */
	get explorerOverlay(): boolean {
		return this.result?.explorerOverlay ?? false;
	}

	/** The one-column divider (mouse owner today, keyboard owner later). */
	get divider(): WorkspaceDivider {
		return this.dividerComponent;
	}

	/** The far-right explorer grip (one column, editor modes only). */
	get grip(): ExplorerGrip {
		return this.gripComponent;
	}

	/**
	 * Store intent and recompute geometry. A rebuild is demanded only when the
	 * composed tree changes (LayoutResult field compare), so streaming state
	 * updates that keep the same rectangles do not churn the root.
	 */
	setState(input: LayoutInput): void {
		this.input = input;
		const next = computeLayout(input);
		const changed = !sameLayoutGeometry(this.result, next);
		this.result = next;
		this.dividerComponent.setResult(next);
		this.gripComponent.setResult(next);
		if (changed) this.options.onResizeDemand?.();
	}

	/** Injected editor; the same instance is composed in every later rebuild. */
	setEditor(component: Component | undefined): void {
		if (component === this.editorComponent) return;
		this.editorComponent = component;
		this.options.onResizeDemand?.();
	}

	/** Injected explorer (docked column only; overlay rendering is a later task). */
	setExplorer(component: Component | undefined): void {
		if (component === this.explorerComponent) return;
		this.explorerComponent = component;
		this.options.onResizeDemand?.();
	}

	/**
	 * HStack entries in visual order for the composed workspace row, always
	 * derived from the given LayoutResult (fixed basis widths). Pass `layout`
	 * from this shell — geometry stays single-sourced. When `chatEntry` is
	 * provided the chat column is spliced in on the geometry's side; the grip
	 * always closes the row so it sits at the viewport's far right.
	 */
	entries(result: LayoutResult | undefined, chatEntry?: StackEntry): StackEntry[] {
		if (!result) return [];
		const gripEntry: StackEntry | undefined = result.grip
			? { component: this.gripComponent, basis: result.grip.columns, shrink: 0, grow: 0 }
			: undefined;
		if (result.effectiveMode === "split" && result.chat && result.editor) {
			const dividerEntry: StackEntry = {
				component: this.dividerComponent,
				basis: LAYOUT_BUDGETS.dividerColumns,
				shrink: 0,
				grow: 0,
			};
			const editorEntry: StackEntry = {
				component: this.editorComponent ?? this.editorPlaceholder,
				basis: result.editor.columns,
				shrink: 0,
				grow: 0,
			};
			const trailing: StackEntry[] = [];
			if (result.explorer) {
				// Docked: its own column. Overlay mode is only reported, not composed.
				trailing.push({
					component: this.explorerComponent ?? this.explorerPlaceholder,
					basis: result.explorer.columns,
					shrink: 0,
					grow: 0,
				});
			}
			if (gripEntry) trailing.push(gripEntry);
			const chat: StackEntry = chatEntry ?? {
				component: this.editorPlaceholder,
				basis: result.chat.columns,
				shrink: 0,
				grow: 0,
			};
			// Chat on the left closes the row's leading edge; on the right it
			// sits between the divider and the explorer drawer.
			return chatIsRight(result)
				? [editorEntry, dividerEntry, chat, ...trailing]
				: [chat, dividerEntry, editorEntry, ...trailing];
		}
		if (result.effectiveMode === "editor" && result.editor) {
			const editorEntry: StackEntry = {
				component: this.editorComponent ?? this.editorPlaceholder,
				basis: result.editor.columns,
				shrink: 0,
				grow: 0,
			};
			return gripEntry ? [editorEntry, gripEntry] : [editorEntry];
		}
		return [];
	}
}
