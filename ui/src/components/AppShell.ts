// Responsive docked root: one retained transcript ScrollView for the shell's
// lifetime, composer + footer pinned to the bottom. Density budgets derive
// from actual terminal dimensions at sync time; the same ScrollView instance
// survives root rebuilds so the user's scroll anchor is never lost.
//
// The workspace composition (plan §7 + UX-5) is opt-in: once a layout state
// arrives, the top row becomes an HStack of the panes (chat transcript on its
// geometry side, then WorkspaceShell's divider/editor/explorer/grip columns),
// and the composer + approval dock become FULL-WIDTH rows beneath it. Every
// width comes from computeLayout(); this file never re-derives a split, and
// retained components keep their identity.

import { HStack, isViewportTUI, ScrollView, VStack, type Component, type TUI } from "@earendil-works/pi-tui";
import { LIMITS } from "../design/tokens.ts";
import type { LayoutInput } from "../workspace/state.ts";
import type { Footer } from "./Footer.ts";
import { WorkspaceShell } from "./WorkspaceShell.ts";

const LAYOUT_NODE = Symbol.for("@earendil-works/pi-tui/layout-node");

export type Density = "full" | "reduced" | "compact" | "degraded";

export interface ComposerBudget {
	setMaxRows(rows: number): void;
	setDegradedMode(degraded: boolean): void;
}

export class AppShell {
	/** The single primary transcript ScrollView, retained for life. */
	readonly transcriptScroll: ScrollView;
	/**
	 * Divider-drag ratio hook: the app persists the ratio and feeds it back
	 * through setWorkspaceState. Keyboard resizing will reuse the same path.
	 */
	onWorkspaceRatioChange?: (ratio: number) => void;
	/** Far-right explorer grip clicked: the app flips its explorer request. */
	onWorkspaceExplorerToggle?: () => void;
	private currentRoot: VStack;
	private readonly viewportRoot: Component;
	private readonly tui: TUI;
	private readonly composer: Component & ComposerBudget;
	private readonly footer: Footer;
	private lastCols = -1;
	private lastRows = -1;
	private density: Density = "full";
	private dock: Component | undefined;
	/** Created on first workspace use; its absence means today's chat-only tree. */
	private workspace: WorkspaceShell | undefined;

	constructor(tui: TUI, transcript: Component, composer: Component & ComposerBudget, footer: Footer) {
		this.tui = tui;
		this.composer = composer;
		this.footer = footer;
		// Follow new output only when already following the end; scrolling
		// upward freezes follow until an explicit return-to-end.
		this.transcriptScroll = new ScrollView(transcript, { follow: "end" });
		this.currentRoot = this.build("full");
		// Resolve responsive budgets before the viewport engine measures children.
		const root = {
			[LAYOUT_NODE]: () => {
				this.sync();
				const node = this.currentRoot as unknown as { [LAYOUT_NODE](): unknown };
				return node[LAYOUT_NODE]();
			},
			render: (width: number) => { this.sync(); return this.currentRoot.render(width); },
			invalidate: () => this.currentRoot.invalidate(),
		};
		this.viewportRoot = root;
		this.sync();
	}

	setDock(component?: Component): void {
		this.dock = component;
		this.applyBudgets();
		this.rebuild();
	}

	get root(): Component {
		return this.viewportRoot;
	}

	get currentDensity(): Density {
		return this.density;
	}

	/**
	 * Opt-in workspace composition. `state` carries intent (requested mode,
	 * persisted ratio, explorer request, preferred pane); the terminal
	 * dimensions are taken from the live terminal so callers never race a
	 * resize. The root is rebuilt only when the composed geometry changes.
	 */
	setWorkspaceState(state: LayoutInput): void {
		this.workspaceShell().setState({ ...state, columns: this.liveColumns(), rows: this.liveRows() });
	}

	/** Injected editor pane; retained across every later rebuild. */
	setWorkspaceEditor(editor?: Component): void {
		this.workspaceShell().setEditor(editor);
	}

	/** Injected explorer drawer; composed only when geometry says docked. */
	setWorkspaceExplorer(explorer?: Component): void {
		this.workspaceShell().setExplorer(explorer);
	}

	/** Recompute budgets from actual terminal dimensions (cheap; idempotent). */
	sync(): void {
		const cols = this.liveColumns();
		const rows = this.liveRows();
		if (cols === this.lastCols && rows === this.lastRows) {
			this.applyBudgets();
			return;
		}
		this.lastCols = cols;
		this.lastRows = rows;
		const density = densityFor(cols, rows);
		const changed = density !== this.density;
		this.density = density;
		this.applyBudgets();
		this.syncWorkspace(cols, rows);
		if (changed || !this.currentRoot) this.rebuild();
	}

	/** Release shell state (the coordinator stays owned by the App). */
	dispose(): void {
		this.lastCols = -1;
		this.lastRows = -1;
	}

	/**
	 * Rows (including inter-section gaps) the workspace root reserves below the
	 * panes row for the optional dock, the pinned composer and the footer. The
	 * app subtracts this from the pane's allocated rows so render() height and
	 * the live viewport stay exactly aligned.
	 */
	chromeRows(): number {
		const width = this.liveColumns();
		const gap = this.gapRows();
		let rows = this.composer.render(width).length + this.footer.render(width).length;
		let sections = 3; // panes row + composer + footer
		if (this.dock) {
			rows += this.dock.render(width).length;
			sections += 1;
		}
		return rows + gap * (sections - 1);
	}

	/** Inter-section gap for the current density and dock state (single source). */
	private gapRows(): number {
		return this.density === "full" && (!this.dock || this.lastRows >= 24) ? 1 : 0;
	}

	private liveColumns(): number {
		return Math.max(0, this.tui.terminal.columns);
	}

	private liveRows(): number {
		return Math.max(0, this.tui.terminal.rows);
	}

	private workspaceShell(): WorkspaceShell {
		this.workspace ??= new WorkspaceShell(this.tui, {
			onRatioChange: (ratio) => this.onWorkspaceRatioChange?.(ratio),
			onResizeDemand: () => this.rebuild(),
			onExplorerToggle: () => this.onWorkspaceExplorerToggle?.(),
		});
		return this.workspace;
	}

	/**
	 * Re-run workspace geometry against the live terminal; a changed
	 * LayoutResult (field compare in WorkspaceShell) demands a rebuild.
	 */
	private syncWorkspace(cols: number, rows: number): void {
		const workspace = this.workspace;
		const state = workspace?.state;
		if (!workspace || !state) return;
		workspace.setState({ ...state, columns: cols, rows });
	}

	private applyBudgets(): void {
		if (this.dock) {
			this.composer.setMaxRows(this.lastRows < 16 ? 1 : 3);
			this.composer.setDegradedMode(this.density === "degraded");
			this.footer.compact = this.lastRows < 16;
			return;
		}
		switch (this.density) {
			case "full":
				this.composer.setMaxRows(LIMITS.composerMaxRows);
				this.composer.setDegradedMode(false);
				this.footer.compact = false;
				break;
			case "reduced":
				this.composer.setMaxRows(4);
				this.composer.setDegradedMode(false);
				this.footer.compact = false;
				break;
			case "compact":
				this.composer.setMaxRows(2);
				this.composer.setDegradedMode(false);
				this.footer.compact = true;
				break;
			case "degraded":
				this.composer.setMaxRows(1);
				this.composer.setDegradedMode(true);
				this.footer.compact = true;
				break;
		}
	}

	private build(density: Density): VStack {
		// Full density keeps one chrome gap row; smaller viewports spend
		// every row on transcript, composer, or status.
		const gap = density === "full" && (!this.dock || this.lastRows >= 24) ? 1 : 0;
		const topRow = this.workspaceRow();
		if (!topRow) {
			return new VStack(
				[
					{ component: this.transcriptScroll, grow: 1 },
					...(this.dock ? [{ component: this.dock, shrink: 0 }] : []),
					{ component: this.composer, shrink: 0 },
					{ component: this.footer, shrink: 0 },
				],
				{ gap },
			);
		}
		// Workspace composition (plan §7 + UX-5): the panes row grows, then the
		// optional approval dock, the pinned composer and the footer are
		// FULL-WIDTH rows. The chat column keeps only the transcript.
		return new VStack(
			[
				{ component: topRow, grow: 1 },
				...(this.dock ? [{ component: this.dock, shrink: 0 }] : []),
				{ component: this.composer, shrink: 0 },
				{ component: this.footer, shrink: 0 },
			],
			{ gap },
		);
	}

	/**
	 * Workspace top row for the effective mode, or undefined when the chat-only
	 * tree is exact (no state yet, chat mode, or no editor injected). The chat
	 * objects are retained in every branch; only composition changes. The chat
	 * transcript keeps its geometry column (side per `chatSide`); the explorer
	 * drawer and the grip follow the same LayoutResult.
	 */
	private workspaceRow(): Component | undefined {
		const workspace = this.workspace;
		const result = workspace?.layout;
		if (!workspace || !result || !workspace.editor) return undefined;
		if (result.effectiveMode === "split" && result.chat && result.editor) {
			return new HStack(
				workspace.entries(result, {
					component: this.transcriptScroll,
					basis: result.chat.columns,
					shrink: 0,
					grow: 0,
				}),
				{ gap: 0 },
			);
		}
		if (result.effectiveMode === "editor" && result.editor) {
			return new HStack(workspace.entries(result), { gap: 0 });
		}
		return undefined;
	}

	private rebuild(): void {
		this.currentRoot = this.build(this.density);
		if (isViewportTUI(this.tui)) this.tui.setLayoutRoot(this.viewportRoot);
	}
}

export function densityFor(columns: number, rows: number): Density {
	if (columns < 20 || rows < 6) return "degraded";
	if (rows <= 8) return "compact";
	if (rows <= 15) return "reduced";
	return "full";
}
