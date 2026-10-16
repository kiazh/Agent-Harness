// Workspace layout and view-state types (plan §7).
// Pure data contracts shared by the workspace controller, shell components and tests.
// No imports from App, features, or renderer state.

/** Requested (user intent) vs effective (what geometry allows) layout mode. */
export type WorkspaceMode = "chat" | "split" | "editor";

/** Primary focus regions inside the workspace shell (plan §8 focus priority). */
export type FocusRegion =
	| "chat-composer"
	| "transcript"
	| "editor"
	| "explorer"
	| "tabs"
	| "problems"
	| "modal";

/** Which single pane a narrow terminal falls back to. */
export type PanePreference = "chat" | "editor";

/** Which side of a split the chat pane occupies (default "left"). */
export type ChatSide = "left" | "right";

export interface Rect {
	x: number;
	y: number;
	columns: number;
	rows: number;
}

export interface LayoutInput {
	columns: number;
	rows: number;
	requestedMode: WorkspaceMode;
	/** Ordinary persisted range 0.25–0.75; clamped during layout. */
	splitRatio: number;
	explorerRequested: boolean;
	/** Which pane the user last meaningfully focused; drives narrow fallback. */
	preferredPane: PanePreference;
	/** Chat placement in a split; absent means "left" (the compatibility default). */
	chatSide?: ChatSide;
}

export interface LayoutResult {
	effectiveMode: WorkspaceMode;
	chat?: Rect;
	editor?: Rect;
	explorer?: Rect;
	/** One-column explorer grip at the viewport's far right (editor visible only). */
	grip?: Rect;
	explorerOverlay: boolean;
	reason?: "narrow" | "short" | "requested";
}

/** Initial budgets (plan §7); tune through captures, never per-component. */
export const LAYOUT_BUDGETS = {
	/** Minimum usable chat pane columns. */
	minChatColumns: 40,
	/** Minimum editor pane columns including line-number gutter/status. */
	minEditorColumns: 48,
	/** Divider between chat and editor. */
	dividerColumns: 1,
	/** Explorer grip strip at the viewport's far-right edge (editor modes). */
	gripColumns: 1,
	/** Explorer preferred width when docked. */
	explorerPreferred: 28,
	explorerMin: 20,
	explorerMax: 40,
	/**
	 * Maximum tree rows the docked explorer windows before it scrolls. The
	 * drawer is a bounded panel: a long tree must never paint its surface fill
	 * across the whole panes column (which reads as a full-page rectangle), so
	 * the painted panel is capped well below the page and the tree scrolls
	 * inside it with `+N more` disclosure. A short tree stays content-sized.
	 */
	explorerMaxTreeRows: 16,
	/** Ordinary persisted ratio range. */
	minSplitRatio: 0.25,
	maxSplitRatio: 0.75,
	defaultSplitRatio: 0.5,
	/** Ratio step for a single keyboard resize command. */
	ratioStep: 0.05,
	/** Below this many rows, split degrades to a single pane. */
	minSplitRows: 8,
} as const;

/**
 * Derived split threshold: both pane minima plus the divider and the grip.
 * Never hardcode 89 elsewhere; import this.
 */
export const SPLIT_MINIMUM_COLUMNS =
	LAYOUT_BUDGETS.minChatColumns +
	LAYOUT_BUDGETS.minEditorColumns +
	LAYOUT_BUDGETS.dividerColumns +
	LAYOUT_BUDGETS.gripColumns;

/** Persisted, non-sensitive workspace metadata (plan §21). */
export interface WorkspacePersistedState {
	version: 1;
	requestedMode: WorkspaceMode;
	splitRatio: number;
	explorerRequested: boolean;
	explorerWidth?: number;
	/** Chat placement; absent means "left" (older stored states). */
	chatSide?: ChatSide;
	openUris: string[];
	activeUri?: string;
	cursorByUri?: Record<string, { offset: number; anchorOffset?: number; scrollLine?: number }>;
}

export const WORKSPACE_STATE_VERSION = 1 as const;

export function defaultWorkspacePersistedState(): WorkspacePersistedState {
	return {
		version: WORKSPACE_STATE_VERSION,
		requestedMode: "split",
		splitRatio: LAYOUT_BUDGETS.defaultSplitRatio,
		// Fresh state opens the explorer: the tree is one click away and the
		// far-right grip keeps a visible affordance when it is closed.
		explorerRequested: true,
		openUris: [],
	};
}
