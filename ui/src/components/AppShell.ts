// Responsive docked root: one retained transcript ScrollView for the shell's
// lifetime, composer + footer pinned to the bottom. Density budgets derive
// from actual terminal dimensions at sync time; the same ScrollView instance
// survives root rebuilds so the user's scroll anchor is never lost.

import { isViewportTUI, ScrollView, VStack, type Component, type TUI } from "@earendil-works/pi-tui";
import { LIMITS } from "../design/tokens.ts";
import type { Footer } from "./Footer.ts";

const LAYOUT_NODE = Symbol.for("@earendil-works/pi-tui/layout-node");

export type Density = "full" | "reduced" | "compact" | "degraded";

export interface ComposerBudget {
	setMaxRows(rows: number): void;
	setDegradedMode(degraded: boolean): void;
}

export class AppShell {
	/** The single primary transcript ScrollView, retained for life. */
	readonly transcriptScroll: ScrollView;
	private currentRoot: VStack;
	private readonly viewportRoot: Component;
	private readonly tui: TUI;
	private readonly composer: Component & ComposerBudget;
	private readonly footer: Footer;
	private lastCols = -1;
	private lastRows = -1;
	private density: Density = "full";
	private dock: Component | undefined;
	private tabs: Component | undefined;

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

	/** Session tab strip above the composer; undefined hides it entirely. */
	setTabs(component?: Component): void {
		this.tabs = component;
		this.rebuild();
	}

	get root(): Component {
		return this.viewportRoot;
	}

	get currentDensity(): Density {
		return this.density;
	}

	/** Recompute budgets from actual terminal dimensions (cheap; idempotent). */
	sync(): void {
		const cols = Math.max(0, this.tui.terminal.columns);
		const rows = Math.max(0, this.tui.terminal.rows);
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
		if (changed || !this.currentRoot) this.rebuild();
	}

	/** Release shell state (the coordinator stays owned by the App). */
	dispose(): void {
		this.lastCols = -1;
		this.lastRows = -1;
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
		return new VStack(
			[
				{ component: this.transcriptScroll, grow: 1 },
				...(this.tabs ? [{ component: this.tabs, shrink: 0 }] : []),
				...(this.dock ? [{ component: this.dock, shrink: 0 }] : []),
				{ component: this.composer, shrink: 0 },
				{ component: this.footer, shrink: 0 },
			],
			{ gap: density === "full" && (!this.dock || this.lastRows >= 24) ? 1 : 0 },
		);
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
