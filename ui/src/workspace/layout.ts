// Pure workspace geometry (plan §7). No component imports; integer rects only.
// Every pane derives from this function; no component computes its own split.

import {
	LAYOUT_BUDGETS,
	SPLIT_MINIMUM_COLUMNS,
	type LayoutInput,
	type LayoutResult,
	type Rect,
} from "./state.ts";

/** Clamp a ratio into the persisted range. NaN falls back to the default. */
export function clampSplitRatio(ratio: number): number {
	if (!Number.isFinite(ratio)) return LAYOUT_BUDGETS.defaultSplitRatio;
	return Math.min(
		LAYOUT_BUDGETS.maxSplitRatio,
		Math.max(LAYOUT_BUDGETS.minSplitRatio, ratio),
	);
}

/** One keyboard resize step; direction +1 moves the divider right. */
export function adjustSplitRatio(ratio: number, direction: 1 | -1): number {
	return clampSplitRatio(clampSplitRatio(ratio) + direction * LAYOUT_BUDGETS.ratioStep);
}

/**
 * Explorer column budget when docked.
 * Docked only when the editor keeps its minimum after the drawer takes its share;
 * otherwise the drawer is an editor-local overlay (plan §2).
 */
export function explorerColumnBudget(
	editorColumns: number,
	requestedWidth?: number,
): { docked: boolean; columns: number } {
	const preferred = requestedWidth ?? LAYOUT_BUDGETS.explorerPreferred;
	const clamped = Math.min(
		LAYOUT_BUDGETS.explorerMax,
		Math.max(LAYOUT_BUDGETS.explorerMin, preferred),
	);
	const available = editorColumns - LAYOUT_BUDGETS.minEditorColumns;
	if (available < LAYOUT_BUDGETS.explorerMin) return { docked: false, columns: 0 };
	return { docked: true, columns: Math.min(clamped, available) };
}

function rect(x: number, width: number, rows: number): Rect {
	return { x, y: 0, columns: Math.max(0, width), rows: Math.max(0, rows) };
}

/**
 * Compute the integer pane rectangles for the current terminal and intent.
 *
 * - Requested mode is distinct from effective mode: shrinking must not overwrite
 *   the persisted preference, so `requestedMode`/`splitRatio` are inputs only.
 * - Below the split minimum, one pane is shown (the preferred one) and the reason
 *   is reported so the shell can offer a switch action.
 */
export function computeLayout(input: LayoutInput): LayoutResult {
	const columns = Math.max(0, Math.floor(input.columns));
	const rows = Math.max(0, Math.floor(input.rows));
	const ratio = clampSplitRatio(input.splitRatio);

	if (input.requestedMode !== "split") {
		// Maximized modes always win while they fit; below degraded width they still
		// render (panes tolerate tiny widths) but report requested degradation.
		const pane = rect(0, columns, rows);
		return input.requestedMode === "chat"
			? { effectiveMode: "chat", chat: pane, explorerOverlay: false, reason: "requested" }
			: { effectiveMode: "editor", editor: pane, explorerOverlay: false, reason: "requested" };
	}

	const narrow = columns < SPLIT_MINIMUM_COLUMNS;
	const short = rows < LAYOUT_BUDGETS.minSplitRows;
	if (narrow || short) {
		const single = rect(0, columns, rows);
		return {
			effectiveMode: input.preferredPane,
			chat: input.preferredPane === "chat" ? single : undefined,
			editor: input.preferredPane === "editor" ? single : undefined,
			explorerOverlay: input.explorerRequested && input.preferredPane === "editor",
			reason: narrow ? "narrow" : "short",
		};
	}

	// Split is available. Allocate explorer off the editor side first.
	const usable = columns - LAYOUT_BUDGETS.dividerColumns;
	let chat = Math.round(usable * ratio);
	chat = Math.min(Math.max(chat, LAYOUT_BUDGETS.minChatColumns), usable - LAYOUT_BUDGETS.minEditorColumns);
	let editor = usable - chat;

	let explorer: Rect | undefined;
	let explorerOverlay = false;
	if (input.explorerRequested) {
		const budget = explorerColumnBudget(editor);
		if (budget.docked) {
			const explorerX = LAYOUT_BUDGETS.dividerColumns + chat + editor - budget.columns;
			explorer = rect(explorerX, budget.columns, rows);
			editor -= budget.columns;
		} else {
			explorerOverlay = true;
		}
	}

	return {
		effectiveMode: "split",
		chat: rect(0, chat, rows),
		editor: rect(LAYOUT_BUDGETS.dividerColumns + chat, editor, rows),
		explorer,
		explorerOverlay,
	};
}

/** Divider hitbox for mouse drag: the single column between chat and editor. */
export function dividerRect(result: LayoutResult): Rect | undefined {
	if (result.effectiveMode !== "split" || !result.chat || !result.editor) return undefined;
	return {
		x: result.chat.columns,
		y: 0,
		columns: LAYOUT_BUDGETS.dividerColumns,
		rows: result.editor.rows,
	};
}
