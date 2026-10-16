// Editor view (plan §9/§10/§23; T0 ADR §3): the terminal code viewport over the
// authoritative EditorStore.
//
// Boundaries:
// - Every text mutation is delegated to the store as a transaction
//   (`insertText`, `backspace`, `deleteForward`, `newline`, `applyEdit`,
//   `undo`, `redo`). The view never edits document text itself; it owns only
//   viewport concerns: a per-document EditorViewState map (cursor mirror,
//   scrollLine, horizontalCellOffset, preferredColumn), drag state and render
//   caches. `getViewState()` exposes the active document's state for
//   persistence.
// - Virtualization: components cannot know their allocated height, so the
//   pane calls `setViewportRows(rows)` (default 20). `render(width)` emits
//   exactly that many rows — only the visible window.
// - Display safety (plan §10): stored text is raw. Rows are projected through
//   `projectControlForDisplay`, which maps every C0/DEL/C1 control and the
//   Unicode line/paragraph separators to visible placeholders (Control
//   Pictures where they exist, U+FFFD otherwise). Tabs are expanded to
//   `tabWidth` stops for display only (terminal tab stops are absolute
//   8-column stops and would break gutter alignment); the model keeps real
//   tabs. The projection never changes stored text or saved bytes, and the
//   sanitized projection is never written back.
// - Keyboard (verified against pi-tui 1.0.0 keybindings, T0 ADR §4): the alt
//   screen consumes bare pageUp/pageDown/home/end before the focused
//   component, so paging uses ctrl+pageUp/ctrl+pageDown and document
//   start/end use ctrl+home/ctrl+end (bare home/end are still handled here so
//   tests and direct dispatch behave). ctrl+shift+k deletes the current line
//   (ctrl+k is left unbound); the kitty CSI-u form is required for that chord
//   on terminals without the Kitty protocol, where ctrl+shift+k can arrive as
//   plain ctrl+k — T3's configurable chords can add a legacy alternative.
//   tab inserts a literal "\t". ctrl+shift+left/right select word-wise, with
//   shift+alt+left/right as the non-colliding alternative because App
//   currently claims ctrl+shift+left/right for the family cycle. ctrl+c/ctrl+x
//   are consumed only when a selection exists; otherwise they are left
//   unconsumed so the app's stop/clear path keeps working (handleInput
//   returns false).
// - Paste is verbatim, as one store transaction: bracketed paste arrives as
//   one `\x1b[200~...\x1b[201~` string from the toolkit; tabs and newlines are
//   preserved exactly (including CRLF bytes). The store detects the resulting
//   line-ending metadata; the view never normalizes text.

import {
	backgroundAnsi,
	CURSOR_MARKER,
	foregroundAnsi,
	getTerminalColorMode,
	Key,
	matchesKey,
	parseColor,
	styleTextWithAnsi,
	type Component,
	type Focusable,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
// Deep import (pinned by a parity test in editor-view.test.ts): the toolkit's
// own prompt Editor decodes Kitty CSI-u printable keys through this helper,
// but the package root does not re-export it. A toolkit bump that moves this
// API fails the parity test loudly instead of silently dropping Kitty input.
import { decodePrintableKey } from "@earendil-works/pi-tui/dist/keys.js";
import { displayWidth, truncateDisplay } from "../design/text.ts";
import { selectionFill, selectionInk, theme } from "../design/theme.ts";
import type { DocumentRecord, EditorViewState } from "./model.ts";
import { EditorStore } from "./model.ts";
import {
	languageForId,
	LineHighlighter,
	type HighlightLanguage,
	type LineSyntaxState,
	type SyntaxSpan,
} from "./syntax.ts";
import { applySyntax } from "./syntaxTheme.ts";
import {
	EDITOR_TAB_WIDTH,
	displayColumnToOffset,
	displayWidthOf,
	lineEndOffset,
	lineStartOffset,
	nextGraphemeOffset,
	nextWordOffset,
	offsetToPosition,
	prevGraphemeOffset,
	prevWordOffset,
} from "./positions.ts";

/** Viewport rows rendered when the pane has not allocated height yet. */
export const DEFAULT_EDITOR_VIEWPORT_ROWS = 20;

/** Clipboard surface injected by the app (never imported directly here). */
export interface EditorClipboard {
	copy(text: string): Promise<boolean>;
	paste(): Promise<string | null>;
}

export interface EditorViewOptions {
	store: EditorStore;
	view: EditorViewState;
	onChange?(documentId: string): void;
	onCursorMove?(): void;
	requestRender(): void;
	clipboard: EditorClipboard;
	/** Wrap long lines; default false (code uses horizontal scrolling, plan §9). */
	softWrap?: boolean;
	/** Display tab width; default 4 (positions.EDITOR_TAB_WIDTH). */
	tabWidth?: number;
}

const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

const CONTROL_PICTURES_BASE = 0x2400;
const DELETE_PLACEHOLDER = "\u2421"; // ␡
const UNKNOWN_CONTROL_PLACEHOLDER = "\uFFFD";

/**
 * Display projection for one document line: control characters become visible
 * placeholders so live controls can never reach the terminal, while printable
 * text (including CJK/emoji) passes through unchanged. Tabs are preserved for
 * tab-stop expansion. Every replacement is one UTF-16 code unit for one code
 * unit, so projected offsets equal raw offsets (1:1) and positions helpers can
 * map columns on the projected text directly.
 */
export function projectControlForDisplay(text: string): string {
	let out = "";
	for (let index = 0; index < text.length; index++) {
		const code = text.charCodeAt(index);
		if (code === 0x09) {
			out += "\t";
		} else if (
			code >= 0x20 &&
			code !== 0x7f &&
			!(code >= 0x80 && code <= 0x9f) &&
			code !== 0x2028 &&
			code !== 0x2029
		) {
			out += text[index];
		} else if (code <= 0x1f) {
			out += String.fromCodePoint(CONTROL_PICTURES_BASE + code);
		} else if (code === 0x7f) {
			out += DELETE_PLACEHOLDER;
		} else {
			// C1 controls and U+2028/U+2029: no safe rendering, show a marker.
			out += UNKNOWN_CONTROL_PLACEHOLDER;
		}
	}
	return out;
}

const terminalColorMode = getTerminalColorMode();

/** The empty-editor hint: the single restatement, at the first content row. */
export const EMPTY_EDITOR_HINT = "No open files · Ctrl+P to open";
/** Cells between a gutter number and the content column. */
const GUTTER_RIGHT_PADDING = 2;

/** Highlighting for a language with no spans (unknown/plain documents). */
const NO_SPANS: readonly SyntaxSpan[] = [];

/**
 * Per-document syntax state cache. `entering.get(n)` is the state entering
 * line n (entry 0 is the closed state); `spans.get(n)` is line n's spans;
 * `through` is the highest line index scanned (so `entering.get(through+1)`
 * is known). Edits invalidate from the earliest affected line — every
 * view-driven mutation moves the caret to (or across) the change, so
 * `min(pre-line, post-line)` is a safe lower bound for that edit; adopting an
 * external view state drops the cache entirely.
 */
interface SyntaxCache {
	version: number;
	entering: Map<number, LineSyntaxState>;
	spans: Map<number, SyntaxSpan[]>;
	through: number;
}

/** Selected text: the skin's selection fill with its contrast-checked ink. */
function paintSelected(text: string): string {
	return styleTextWithAnsi(
		text,
		foregroundAnsi(parseColor(selectionInk()), terminalColorMode),
		backgroundAnsi(parseColor(selectionFill()), terminalColorMode),
		{},
	);
}

/** Fake caret cell: inverse video + bold, the fallback when no hardware cursor. */
function paintCaret(text: string): string {
	return styleTextWithAnsi(text, undefined, undefined, { inverse: true, bold: true });
}

interface Segment {
	/** Display-safe text (tabs already expanded to spaces). */
	text: string;
	/** Starting display cell within the line. */
	col: number;
	/** Display cells. */
	width: number;
	/** Offset range within the line (raw, 1:1 with the projection). */
	from: number;
	to: number;
}

/** Grapheme segments of a projected line with tab-stop accurate columns. */
function segmentsOfLine(projected: string, tabWidth: number): Segment[] {
	const segments: Segment[] = [];
	let col = 0;
	for (const part of graphemeSegmenter.segment(projected)) {
		const raw = part.segment;
		const width = raw === "\t" ? tabWidth - (col % tabWidth) : displayWidth(raw);
		segments.push({
			text: raw === "\t" ? " ".repeat(width) : raw,
			col,
			width,
			from: part.index,
			to: part.index + raw.length,
		});
		col += width;
	}
	return segments;
}

/** Line start offsets; a text ending with a terminator has a trailing line. */
function lineStartsOf(text: string): number[] {
	const starts = [0];
	for (let index = 0; index < text.length; index++) {
		const code = text.charCodeAt(index);
		if (code === 0x0a) {
			starts.push(index + 1);
		} else if (code === 0x0d) {
			if (text.charCodeAt(index + 1) === 0x0a) index += 1;
			starts.push(index + 1);
		}
	}
	return starts;
}

/** Offset just before the line's terminator (content end). */
function contentEndOf(text: string, start: number): number {
	let index = start;
	while (index < text.length) {
		const code = text.charCodeAt(index);
		if (code === 0x0a || code === 0x0d) break;
		index += 1;
	}
	return index;
}

function clamp(value: number, min: number, max: number): number {
	if (!Number.isFinite(value)) return min;
	return Math.min(max, Math.max(min, Math.floor(value)));
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

interface LineInfo {
	line: number;
	start: number;
	end: number;
	raw: string;
	projected: string;
	segments: Segment[];
	width: number;
}

interface VisualRow {
	line: number;
	fromCell: number;
	toCell: number;
}

interface FrameCache {
	key: string;
	lines: string[];
}

/**
 * Terminal code viewport for one active document. Implements Component and
 * Focusable so the TUI can focus it and position the hardware cursor.
 */
export class EditorView implements Component, Focusable {
	focused = false;
	/**
	 * App focus-router seam: invoked whenever a mouse event claims focus, so
	 * the host can track the active region (plan §8 focus priority v1).
	 */
	onFocusRequested?: () => void;

	private readonly store: EditorStore;
	private readonly onChangeCallback: ((documentId: string) => void) | undefined;
	private readonly onCursorMoveCallback: (() => void) | undefined;
	private readonly requestRenderCallback: () => void;
	private readonly clipboard: EditorClipboard;
	private softWrap: boolean;
	private tabWidth: number;
	/** Language-aware highlighting; off by default (the app opts in via prefs). */
	private syntaxEnabled = false;
	/** One highlighter per language, created lazily (pure, no per-line alloc). */
	private readonly highlighters = new Map<HighlightLanguage, LineHighlighter>();
	/** Per-document prefix-state cache (see SyntaxCache). */
	private readonly syntaxCache = new Map<string, SyntaxCache>();
	private readonly states = new Map<string, EditorViewState>();
	private activeDocumentId: string;
	private viewportRows = DEFAULT_EDITOR_VIEWPORT_ROWS;
	/** True while the viewport follows the caret; explicit scroll freezes it. */
	private followCaret = true;
	private dragging = false;
	private lastWidth = 0;
	private readonly lineCache = new Map<string, { version: number; starts: number[] }>();
	private readonly wrapCache = new Map<string, { version: number; contentCells: number; rows: VisualRow[] }>();
	private lastFrame: FrameCache | null = null;

	constructor(options: EditorViewOptions) {
		this.store = options.store;
		this.onChangeCallback = options.onChange;
		this.onCursorMoveCallback = options.onCursorMove;
		this.requestRenderCallback = options.requestRender;
		this.clipboard = options.clipboard;
		this.softWrap = options.softWrap ?? false;
		const tabWidth = options.tabWidth ?? EDITOR_TAB_WIDTH;
		this.tabWidth = Number.isFinite(tabWidth) ? Math.max(1, Math.floor(tabWidth)) : EDITOR_TAB_WIDTH;
		const state: EditorViewState = { ...options.view };
		const fromStore = this.store.getViewState(state.documentId);
		if (fromStore) {
			// The store is authoritative for cursor/selection; the injected state
			// contributes scroll position when restoring a tab.
			state.cursorOffset = fromStore.cursorOffset;
			state.selectionAnchorOffset = fromStore.selectionAnchorOffset;
		}
		this.states.set(state.documentId, state);
		this.activeDocumentId = state.documentId;
	}

	// ------------------------------------------------------------ top-level

	/** Switch the active document; retains per-document scroll when known. */
	setDocument(documentId: string): void {
		this.activeDocumentId = documentId;
		let state = this.states.get(documentId);
		const fromStore = this.store.getViewState(documentId);
		if (!state) {
			state = fromStore ?? {
				documentId,
				cursorOffset: 0,
				selectionAnchorOffset: undefined,
				scrollLine: 0,
				horizontalCellOffset: 0,
			};
			this.states.set(documentId, state);
		} else if (fromStore) {
			state.cursorOffset = fromStore.cursorOffset;
			state.selectionAnchorOffset = fromStore.selectionAnchorOffset;
		}
		this.dragging = false;
		this.followCaret = true;
		this.requestRenderCallback();
	}

	/** Upsert an explicit view state (tab switch / persistence restore). */
	setView(state: EditorViewState): void {
		this.states.set(state.documentId, { ...state });
		this.activeDocumentId = state.documentId;
		this.dragging = false;
		this.followCaret = true;
		// An adopted view state follows an external store mutation (completion
		// apply, rename, conflict reload, restore): the changed lines are
		// unknown, so the document's syntax cache is dropped wholesale.
		this.syntaxCache.delete(state.documentId);
		this.requestRenderCallback();
	}

	/** Enable or disable language-aware highlighting (default off). */
	setSyntaxEnabled(enabled: boolean): void {
		const next = enabled === true;
		if (next === this.syntaxEnabled) return;
		this.syntaxEnabled = next;
		this.requestRenderCallback();
	}

	/** Toggle soft wrap (persisted editor preference). */
	setSoftWrap(enabled: boolean): void {
		const next = enabled === true;
		if (next === this.softWrap) return;
		this.softWrap = next;
		this.followCaret = true;
		this.requestRenderCallback();
	}

	/** Set the display tab width (1–16; persisted editor preference). */
	setTabWidth(width: number): void {
		const next = Number.isFinite(width) ? Math.min(16, Math.max(1, Math.floor(width))) : EDITOR_TAB_WIDTH;
		if (next === this.tabWidth) return;
		this.tabWidth = next;
		this.requestRenderCallback();
	}

	/** Active document view state copy; undefined when no document is active. */
	getViewState(): EditorViewState | undefined {
		const state = this.states.get(this.activeDocumentId);
		return state ? { ...state } : undefined;
	}

	/** Drop per-document caches when a tab is closed (lifecycle hygiene). */
	releaseDocument(documentId: string): void {
		this.states.delete(documentId);
		this.lineCache.delete(documentId);
		this.wrapCache.delete(documentId);
		this.syntaxCache.delete(documentId);
		this.lastFrame = null;
	}

	/** Allocated viewport height in rows (the pane calls this). */
	setViewportRows(rows: number): void {
		const next = Math.max(1, clamp(rows, 1, Number.MAX_SAFE_INTEGER));
		if (next === this.viewportRows) return;
		this.viewportRows = next;
		this.requestRenderCallback();
	}

	/** 1-based display position of the caret for the pane's status row. */
	cursorDisplayPosition(): { line: number; column: number } {
		const record = this.store.getDocument(this.activeDocumentId);
		if (!record) return { line: 1, column: 1 };
		const state = this.stateFor(record.id);
		const cursor = clamp(state.cursorOffset, 0, record.text.length);
		const starts = this.linesFor(record);
		const position = offsetToPosition(record.text, cursor);
		const info = this.lineInfo(record, starts, position.line);
		return { line: position.line + 1, column: this.cellInLine(info, cursor) + 1 };
	}

	// --------------------------------------------------------------- render

	render(width: number): string[] {
		const safeWidth = normalizeWidth(width);
		this.lastWidth = safeWidth;
		const rows = this.viewportRows;
		const record = this.store.getDocument(this.activeDocumentId);
		if (!record) return this.emptyRows(safeWidth, rows);
		const state = this.stateFor(record.id);
		const starts = this.linesFor(record);
		const digits = String(starts.length).length;
		const gutterCells = digits + GUTTER_RIGHT_PADDING;
		const contentCells = Math.max(0, safeWidth - gutterCells);
		const rowCount = this.rowCount(record, starts, contentCells);
		const maxScroll = Math.max(0, rowCount - rows);
		if (this.followCaret) {
			this.ensureCaretVisible(record, starts, contentCells, rowCount, state);
		}
		state.scrollLine = clamp(state.scrollLine, 0, maxScroll);
		if (this.softWrap) state.horizontalCellOffset = 0;
		const cursor = clamp(state.cursorOffset, 0, record.text.length);
		const anchor =
			state.selectionAnchorOffset === undefined
				? undefined
				: clamp(state.selectionAnchorOffset, 0, record.text.length);
		const selection =
			anchor !== undefined && anchor !== cursor
				? { startOffset: Math.min(anchor, cursor), endOffset: Math.max(anchor, cursor) }
				: undefined;
		const activeLine = offsetToPosition(record.text, cursor).line;
		const caretRow = this.rowIndexOfOffset(record, starts, contentCells, cursor);
		const marker = this.focused ? CURSOR_MARKER : "";

		const cacheKey = [
			record.id,
			String(record.version),
			String(safeWidth),
			String(rows),
			String(state.scrollLine),
			String(state.horizontalCellOffset),
			String(cursor),
			String(anchor ?? -1),
			String(activeLine),
			String(caretRow),
			this.focused ? "1" : "0",
			this.softWrap ? "w" : "n",
			String(this.tabWidth),
			this.syntaxEnabled ? "s" : "p",
		].join(":");
		if (this.lastFrame && this.lastFrame.key === cacheKey) return [...this.lastFrame.lines];

		const language = languageForId(record.languageId);
		const highlight = this.syntaxEnabled && language !== "plain";
		const lines: string[] = [];
		for (let slot = 0; slot < rows; slot++) {
			const rowIndex = state.scrollLine + slot;
			const row = rowIndex >= 0 && rowIndex < rowCount ? this.rowAt(record, starts, contentCells, rowIndex) : undefined;
			if (!row) {
				lines.push(" ".repeat(safeWidth));
				continue;
			}
			const info = this.lineInfo(record, starts, row.line);
			const gutterNumber = String(row.line + 1).padStart(digits);
			const gutter = row.line === activeLine ? theme.accent(gutterNumber) : theme.dim(gutterNumber);
			const caretCell = this.focused && rowIndex === caretRow ? this.cellInLine(info, cursor) : undefined;
			const windowFrom = this.softWrap ? row.fromCell : state.horizontalCellOffset;
			const spans = highlight ? this.syntaxSpansFor(record, starts, language, row.line) : undefined;
			const content = this.composeRow(info, windowFrom, contentCells, selection, caretCell, marker, spans);
			lines.push(padRow(truncateDisplay(`${gutter}  ${content}`, safeWidth), safeWidth));
		}
		this.lastFrame = { key: cacheKey, lines };
		return [...lines];
	}

	invalidate(): void {
		this.lastFrame = null;
		this.lineCache.clear();
		this.wrapCache.clear();
		this.syntaxCache.clear();
	}

	// --------------------------------------------------------------- input

	/**
	 * Returns true when the key was consumed. ctrl+c/ctrl+x without a
	 * selection and unrecognized sequences return false so the app's global
	 * handlers keep working.
	 */
	handleInput(data: string): boolean {
		const documentId = this.activeDocumentId;
		const record = this.store.getDocument(documentId);
		if (!record) return false;

		// Bracketed paste: one string from the toolkit's stdin buffering.
		const pasteStart = data.indexOf("\x1b[200~");
		if (pasteStart !== -1) {
			const rest = data.slice(pasteStart + 6);
			const end = rest.indexOf("\x1b[201~");
			// Defensive: a truncated paste inserts what arrived (the buffering
			// contract guarantees a complete unit).
			const content = end === -1 ? rest : rest.slice(0, end);
			if (content.length > 0) this.insertVerbatim(documentId, content);
			return true;
		}

		// Clipboard: copy/cut are consumed only with a live selection.
		if (matchesKey(data, Key.ctrl("c"))) return this.copySelection(documentId);
		if (matchesKey(data, Key.ctrl("x"))) return this.cutSelection(documentId);
		if (matchesKey(data, Key.ctrl("v"))) {
			void this.pasteFromClipboard(documentId);
			return true;
		}
		if (matchesKey(data, Key.ctrl("a"))) return this.selectAll(documentId);
		if (matchesKey(data, Key.ctrl("z"))) return this.undo(documentId);
		if (matchesKey(data, Key.ctrlShift("z")) || matchesKey(data, Key.ctrl("y"))) {
			return this.redo(documentId);
		}
		if (matchesKey(data, Key.ctrlShift("k"))) return this.deleteLine(documentId);
		if (matchesKey(data, Key.alt("up"))) return this.moveLine(documentId, -1);
		if (matchesKey(data, Key.alt("down"))) return this.moveLine(documentId, 1);

		// Selection-extending movement first: shift modifies every motion.
		if (matchesKey(data, Key.shift("left"))) return this.moveHorizontal(documentId, -1, "grapheme", true);
		if (matchesKey(data, Key.shift("right"))) return this.moveHorizontal(documentId, 1, "grapheme", true);
		if (matchesKey(data, Key.shiftCtrl("left")) || matchesKey(data, Key.shiftAlt("left"))) {
			return this.moveHorizontal(documentId, -1, "word", true);
		}
		if (matchesKey(data, Key.shiftCtrl("right")) || matchesKey(data, Key.shiftAlt("right"))) {
			return this.moveHorizontal(documentId, 1, "word", true);
		}
		if (matchesKey(data, Key.shift("up"))) return this.moveVertical(documentId, -1, true);
		if (matchesKey(data, Key.shift("down"))) return this.moveVertical(documentId, 1, true);
		if (matchesKey(data, Key.shiftCtrl("home"))) return this.moveCaret(documentId, 0, true);
		if (matchesKey(data, Key.shiftCtrl("end"))) return this.moveCaret(documentId, record.text.length, true);
		if (matchesKey(data, Key.shift("home"))) return this.moveCaret(documentId, lineStartOffset(record.text, this.cursor()), true);
		if (matchesKey(data, Key.shift("end"))) return this.moveCaret(documentId, lineEndOffset(record.text, this.cursor()), true);

		if (matchesKey(data, Key.left)) return this.moveHorizontal(documentId, -1, "grapheme", false);
		if (matchesKey(data, Key.right)) return this.moveHorizontal(documentId, 1, "grapheme", false);
		if (matchesKey(data, Key.ctrl("left")) || matchesKey(data, Key.alt("left"))) {
			return this.moveHorizontal(documentId, -1, "word", false);
		}
		if (matchesKey(data, Key.ctrl("right")) || matchesKey(data, Key.alt("right"))) {
			return this.moveHorizontal(documentId, 1, "word", false);
		}
		if (matchesKey(data, Key.up)) return this.moveVertical(documentId, -1, false);
		if (matchesKey(data, Key.down)) return this.moveVertical(documentId, 1, false);
		if (matchesKey(data, Key.home)) return this.moveCaret(documentId, lineStartOffset(record.text, this.cursor()), false);
		if (matchesKey(data, Key.end)) return this.moveCaret(documentId, lineEndOffset(record.text, this.cursor()), false);
		if (matchesKey(data, Key.ctrl("home"))) return this.moveCaret(documentId, 0, false);
		if (matchesKey(data, Key.ctrl("end"))) return this.moveCaret(documentId, record.text.length, false);

		// Bare pageUp/pageDown never reach the focused component in the app
		// (alt-screen viewport ownership); ctrl+pageUp/ctrl+pageDown do.
		const page = Math.max(1, this.viewportRows - 1);
		if (matchesKey(data, Key.ctrl("pageUp"))) return this.moveVertical(documentId, -page, false);
		if (matchesKey(data, Key.ctrl("pageDown"))) return this.moveVertical(documentId, page, false);

		// Editing.
		if (matchesKey(data, Key.backspace)) return this.mutate(documentId, () => this.store.backspace(documentId));
		if (matchesKey(data, Key.delete)) return this.mutate(documentId, () => this.store.deleteForward(documentId));
		if (matchesKey(data, Key.enter)) return this.mutate(documentId, () => this.store.newline(documentId));
		if (matchesKey(data, Key.tab)) return this.insertVerbatim(documentId, "\t");

		const printable = decodePrintableKey(data);
		if (printable !== undefined) return this.insertVerbatim(documentId, printable);
		if (data.length > 0 && data.charCodeAt(0) >= 32) return this.insertVerbatim(documentId, data);
		return false;
	}

	// --------------------------------------------------------------- mouse

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		const documentId = this.activeDocumentId;
		const record = this.store.getDocument(documentId);
		if (!record) return undefined;

		if (event.type === "wheel") {
			const delta = event.wheelDelta ?? 0;
			if (delta === 0) return undefined;
			this.scrollByLines(delta);
			return { handled: true };
		}
		if (event.type === "release") {
			if (!this.dragging) return undefined;
			this.dragging = false;
			return { handled: true };
		}
		if (event.type === "move") return undefined;

		const state = this.stateFor(record.id);
		const starts = this.linesFor(record);
		const digits = String(starts.length).length;
		const gutterCells = digits + GUTTER_RIGHT_PADDING;
		const contentCells = Math.max(0, this.lastWidth - gutterCells);
		const x = Math.floor(event.x);
		if (x < gutterCells) {
			// Gutter: claim focus but never move the caret (plan §2 chrome).
			// A synthesized click (no press delivered to the view) still claims it.
			if ((event.type === "press" || event.type === "click") && event.button === "left") {
				this.onFocusRequested?.();
				return { focus: true, handled: true };
			}
			return undefined;
		}
		const rows = this.viewportRows;
		const rawY = Math.floor(event.y);
		const clampedY = Math.min(rows - 1, Math.max(0, rawY));
		if (event.type === "press" && (rawY < 0 || rawY >= rows)) return undefined;
		if (event.type === "press" && event.button !== "left") return undefined;
		const offset = this.offsetForCell(record, starts, contentCells, state, clampedY, x - gutterCells);
		if (event.type === "press") {
			this.dragging = true;
			this.onFocusRequested?.();
			this.moveCaret(documentId, offset, false);
			return { capture: true, focus: true, handled: true };
		}
		if (event.type === "drag") {
			if (!this.dragging) return undefined;
			this.onFocusRequested?.();
			this.moveCaret(documentId, offset, true);
			return { capture: true, focus: true, handled: true };
		}
		if (event.type === "click") {
			if (event.clickCount === 2) {
				const start = prevWordOffset(record.text, offset);
				const end = nextWordOffset(record.text, offset);
				if (start !== end) {
					this.setSelection(documentId, end, start);
				} else {
					this.moveCaret(documentId, offset, false);
				}
			} else {
				// Self-sufficient single click: position the caret even when the
				// press was not delivered to the view, so a direct viewport click
				// restores the caret immediately (never waits for a keystroke).
				// With a delivered press this is the same offset and a no-op.
				this.moveCaret(documentId, offset, false);
			}
			this.onFocusRequested?.();
			return { handled: true, focus: true };
		}
		return undefined;
	}

	// ------------------------------------------------------------ internals

	/**
	 * No active document: one dim hint (the single empty-state restatement)
	 * gutter-aligned at the first content row, blank rows below.
	 */
	private emptyRows(width: number, rows: number): string[] {
		const hint = truncateDisplay(theme.dim(EMPTY_EDITOR_HINT), width);
		const gutter = " ".repeat(1 + GUTTER_RIGHT_PADDING);
		const lines = [truncateDisplay(`${gutter}${hint}`, width)];
		while (lines.length < rows) lines.push(" ".repeat(width));
		return lines;
	}

	private cursor(): number {
		const state = this.states.get(this.activeDocumentId);
		const record = this.store.getDocument(this.activeDocumentId);
		if (!state || !record) return 0;
		return clamp(state.cursorOffset, 0, record.text.length);
	}

	private stateFor(documentId: string): EditorViewState {
		let state = this.states.get(documentId);
		if (!state) {
			const fromStore = this.store.getViewState(documentId);
			state =
				fromStore ??
				({
					documentId,
					cursorOffset: 0,
					selectionAnchorOffset: undefined,
					scrollLine: 0,
					horizontalCellOffset: 0,
				} satisfies EditorViewState);
			this.states.set(documentId, state);
		}
		return state;
	}

	private linesFor(record: DocumentRecord): number[] {
		const cached = this.lineCache.get(record.id);
		if (cached && cached.version === record.version) return cached.starts;
		const starts = lineStartsOf(record.text);
		this.lineCache.set(record.id, { version: record.version, starts });
		return starts;
	}

	private lineInfo(record: DocumentRecord, starts: number[], line: number): LineInfo {
		const start = starts[line] ?? record.text.length;
		const end = contentEndOf(record.text, start);
		const raw = record.text.slice(start, end);
		const projected = projectControlForDisplay(raw);
		const segments = segmentsOfLine(projected, this.tabWidth);
		const last = segments[segments.length - 1];
		return { line, start, end, raw, projected, segments, width: last ? last.col + last.width : 0 };
	}

	private cellInLine(info: LineInfo, offset: number): number {
		const inLine = clamp(offset - info.start, 0, info.raw.length);
		return displayWidthOf(info.projected.slice(0, inLine), this.tabWidth);
	}

	// ------------------------------------------------------------- syntax

	/** Cached highlighter per language (the parser is pure and stateful only via `state`). */
	private highlighterFor(language: HighlightLanguage): LineHighlighter {
		let highlighter = this.highlighters.get(language);
		if (!highlighter) {
			highlighter = new LineHighlighter(language);
			this.highlighters.set(language, highlighter);
		}
		return highlighter;
	}

	/** Raw content of one line (terminators excluded; 1:1 with the projection). */
	private rawLineAt(text: string, starts: number[], line: number): string {
		const start = starts[line] ?? text.length;
		return text.slice(start, contentEndOf(text, start));
	}

	/**
	 * Spans for one document line, prefix-scanning only as far as needed.
	 *
	 * Strategy (documented per the closing-pass spec): the cache stores the
	 * lexer state ENTERING every scanned line plus that line's spans, so a
	 * render of lines [first..last] costs `O(last - through)` new lines —
	 * typically the edited tail of the visible window, never the whole file.
	 * A version change the mutation hooks did not observe (an external store
	 * edit without `setView`) resets the cache; jumps into an unscanned
	 * region scan forward from the last known state (line 0 for a fresh
	 * cache), which is correct and paid once, not per keystroke.
	 */
	private syntaxSpansFor(
		record: DocumentRecord,
		starts: number[],
		language: HighlightLanguage,
		line: number,
	): readonly SyntaxSpan[] {
		const cache = this.syntaxCacheFor(record);
		while (cache.through < line) {
			const next = cache.through + 1;
			const state = next === 0 ? undefined : cache.entering.get(next);
			const result = this.highlighterFor(language).line(this.rawLineAt(record.text, starts, next), state);
			cache.entering.set(next + 1, result.state);
			cache.spans.set(next, result.spans);
			cache.through = next;
		}
		return cache.spans.get(line) ?? NO_SPANS;
	}

	private syntaxCacheFor(record: DocumentRecord): SyntaxCache {
		let cache = this.syntaxCache.get(record.id);
		if (!cache || cache.version !== record.version) {
			// Unknown edit position: correctness over cleverness — rebuild.
			cache = { version: record.version, entering: new Map(), spans: new Map(), through: -1 };
			this.syntaxCache.set(record.id, cache);
		}
		return cache;
	}

	/**
	 * Drop cached states at/after `line` (the edited line must be re-scanned:
	 * its spans were computed from the pre-edit text, and when it is the
	 * highest scanned line a bare `>` check would keep them stale).
	 */
	private syntaxInvalidateFrom(documentId: string, line: number, version: number): void {
		const cache = this.syntaxCache.get(documentId);
		if (!cache) return;
		cache.version = version;
		for (const key of [...cache.entering.keys()]) {
			if (key > line) cache.entering.delete(key);
		}
		for (const key of [...cache.spans.keys()]) {
			if (key >= line) cache.spans.delete(key);
		}
		if (cache.through >= line) cache.through = line - 1;
	}

	private wrapRows(record: DocumentRecord, starts: number[], contentCells: number): VisualRow[] {
		const cached = this.wrapCache.get(record.id);
		if (cached && cached.version === record.version && cached.contentCells === contentCells) {
			return cached.rows;
		}
		const per = Math.max(1, contentCells);
		const rows: VisualRow[] = [];
		for (let line = 0; line < starts.length; line++) {
			const width = this.lineInfo(record, starts, line).width;
			const count = Math.max(1, Math.ceil(width / per));
			for (let chunk = 0; chunk < count; chunk++) {
				rows.push({ line, fromCell: chunk * per, toCell: Math.min(width, (chunk + 1) * per) });
			}
		}
		this.wrapCache.set(record.id, { version: record.version, contentCells, rows });
		return rows;
	}

	private rowCount(record: DocumentRecord, starts: number[], contentCells: number): number {
		if (!this.softWrap) return starts.length;
		return this.wrapRows(record, starts, contentCells).length;
	}

	private rowAt(record: DocumentRecord, starts: number[], contentCells: number, index: number): VisualRow | undefined {
		if (!this.softWrap) {
			if (index < 0 || index >= starts.length) return undefined;
			return { line: index, fromCell: 0, toCell: Number.POSITIVE_INFINITY };
		}
		return this.wrapRows(record, starts, contentCells)[index];
	}

	private rowIndexOfOffset(record: DocumentRecord, starts: number[], contentCells: number, offset: number): number {
		const position = offsetToPosition(record.text, offset);
		if (!this.softWrap) return Math.min(starts.length - 1, Math.max(0, position.line));
		const rows = this.wrapRows(record, starts, contentCells);
		if (rows.length === 0) return 0;
		// Rows are ordered by (line, chunk): binary search the line's first row.
		let low = 0;
		let high = rows.length - 1;
		let first = 0;
		while (low <= high) {
			const middle = (low + high) >> 1;
			const row = rows[middle] as VisualRow;
			if (row.line < position.line) {
				low = middle + 1;
			} else {
				first = middle;
				high = middle - 1;
			}
		}
		const info = this.lineInfo(record, starts, position.line);
		const per = Math.max(1, contentCells);
		const chunk = Math.floor(this.cellInLine(info, offset) / per);
		const chunkCount = Math.max(1, Math.ceil(info.width / per));
		return Math.min(rows.length - 1, first + Math.min(chunkCount - 1, Math.max(0, chunk)));
	}

	/** Row index for a document cell column (soft-wrap aware movement). */
	private offsetForVisualRow(
		record: DocumentRecord,
		starts: number[],
		contentCells: number,
		rowIndex: number,
		column: number,
	): number {
		const row = this.rowAt(record, starts, contentCells, rowIndex);
		if (!row) return record.text.length;
		const info = this.lineInfo(record, starts, row.line);
		const cell = Math.min(Math.max(column, row.fromCell), Math.min(row.toCell, info.width));
		const inLine = displayColumnToOffset(info.projected, cell, this.tabWidth);
		return info.start + Math.min(inLine, info.end - info.start);
	}

	private offsetForCell(
		record: DocumentRecord,
		starts: number[],
		contentCells: number,
		state: EditorViewState,
		y: number,
		contentCol: number,
	): number {
		const rowIndex = Math.min(this.rowCount(record, starts, contentCells) - 1, Math.max(0, state.scrollLine + y));
		const row = this.rowAt(record, starts, contentCells, rowIndex);
		if (!row) return record.text.length;
		const info = this.lineInfo(record, starts, row.line);
		const cell =
			(this.softWrap ? row.fromCell : state.horizontalCellOffset) + Math.max(0, contentCol);
		const inLine = displayColumnToOffset(info.projected, cell, this.tabWidth);
		return info.start + Math.min(inLine, info.end - info.start);
	}

	private ensureCaretVisible(
		record: DocumentRecord,
		starts: number[],
		contentCells: number,
		rowCount: number,
		state: EditorViewState,
	): void {
		const rows = this.viewportRows;
		const maxScroll = Math.max(0, rowCount - rows);
		let scroll = clamp(state.scrollLine, 0, maxScroll);
		const caretRow = this.rowIndexOfOffset(record, starts, contentCells, state.cursorOffset);
		if (caretRow < scroll) scroll = caretRow;
		else if (caretRow >= scroll + rows) scroll = caretRow - rows + 1;
		state.scrollLine = clamp(scroll, 0, maxScroll);
		if (!this.softWrap) {
			const position = offsetToPosition(record.text, state.cursorOffset);
			const info = this.lineInfo(record, starts, position.line);
			const caretCell = this.cellInLine(info, state.cursorOffset);
			if (caretCell < state.horizontalCellOffset) state.horizontalCellOffset = caretCell;
			else if (contentCells > 0 && caretCell >= state.horizontalCellOffset + contentCells) {
				state.horizontalCellOffset = caretCell - contentCells + 1;
			}
			state.horizontalCellOffset = Math.max(0, state.horizontalCellOffset);
		}
	}

	/**
	 * One visible content window. Cells outside [windowFrom, windowFrom +
	 * contentCells) are dropped; a wide grapheme clipped by the window edge
	 * renders as blanks so column alignment survives.
	 *
	 * Syntax spans arrive in RAW line offsets. The control-character
	 * projection is 1:1 (projectControlForDisplay maps every control to one
	 * code unit) and tab expansion is a display-only transform, so a part is
	 * syntaxable exactly when its text is a 1:1 slice of the raw line. Plain
	 * runs of such parts are styled by `applySyntax` (offsets shifted into the
	 * run); tab blanks, clipped edges and the EOL caret are emitted verbatim.
	 * The caret cell (inverse) and selection fill always win over syntax, so
	 * their SGR nesting never mixes.
	 */
	private composeRow(
		info: LineInfo,
		windowFrom: number,
		contentCells: number,
		selection: { startOffset: number; endOffset: number } | undefined,
		caretCell: number | undefined,
		marker: string,
		syntax: readonly SyntaxSpan[] | undefined,
	): string {
		const windowEnd = windowFrom + contentCells;
		const windowContentTo = Math.min(info.width, windowEnd);
		const selStart = selection ? selection.startOffset - info.start : 0;
		const selEnd = selection ? selection.endOffset - info.start : 0;
		const isSelected = (segment: Segment): boolean =>
			selection !== undefined && segment.from < selEnd && segment.to > selStart;
		const selectToEol =
			selection !== undefined && selStart <= info.raw.length && selEnd > info.raw.length;
		const caretVisible = caretCell !== undefined && caretCell >= windowFrom && caretCell < windowEnd;

		interface Part {
			text: string;
			width: number;
			style: "plain" | "selected" | "caret";
			/** Raw line offsets when the text is the 1:1 raw slice; else undefined. */
			from?: number;
			to?: number;
		}
		const parts: Part[] = [];
		let emitted = 0;
		for (const segment of info.segments) {
			const segStart = segment.col;
			const segEnd = segment.col + segment.width;
			if (segEnd <= windowFrom) continue;
			if (segStart >= windowContentTo) break;
			const caretHere = caretVisible && caretCell === segStart;
			if (segStart < windowFrom) {
				const visible = Math.max(0, Math.min(segEnd, windowEnd) - windowFrom);
				if (visible > 0) {
					parts.push({ text: " ".repeat(visible), width: visible, style: isSelected(segment) ? "selected" : "plain" });
					emitted += visible;
				}
				continue;
			}
			if (segEnd > windowContentTo) {
				const visible = windowContentTo - segStart;
				if (visible > 0) {
					parts.push({
						text: " ".repeat(visible),
						width: visible,
						style: caretHere ? "caret" : isSelected(segment) ? "selected" : "plain",
					});
					emitted += visible;
				}
				continue;
			}
			parts.push({
				text: segment.text,
				width: segment.width,
				style: caretHere ? "caret" : isSelected(segment) ? "selected" : "plain",
				from: segment.from,
				to: segment.to,
			});
			emitted += segment.width;
		}
		const caretAtEol = caretVisible && caretCell === info.width;
		if (selectToEol) {
			const reserve = caretAtEol ? 1 : 0;
			const fill = Math.max(0, contentCells - emitted - reserve);
			if (fill > 0) {
				parts.push({ text: " ".repeat(fill), width: fill, style: "selected" });
				emitted += fill;
			}
		}
		if (caretAtEol) parts.push({ text: " ", width: 1, style: "caret" });
		let out = "";
		let run = "";
		let runStart = -1;
		let runEnd = -1;
		const flush = (): void => {
			if (runStart < 0) return;
			out += this.styleSyntaxRun(run, runStart, syntax);
			run = "";
			runStart = -1;
			runEnd = -1;
		};
		for (const part of parts) {
			if (part.style === "caret") {
				flush();
				out += marker + paintCaret(part.text);
				continue;
			}
			if (part.style === "selected") {
				flush();
				out += paintSelected(part.text);
				continue;
			}
			const oneToOne =
				part.from !== undefined && part.to !== undefined && part.text.length === part.to - part.from;
			if (!oneToOne) {
				flush();
				out += part.text;
				continue;
			}
			// Consecutive 1:1 parts are adjacent raw slices; a gap (skipped
			// grapheme) starts a new run so run text always matches its offsets.
			if (runStart >= 0 && part.from !== runEnd) flush();
			if (runStart < 0) runStart = part.from as number;
			run += part.text;
			runEnd = part.to as number;
		}
		flush();
		return out;
	}

	/** Apply raw-line spans to one contiguous 1:1 run starting at `from`. */
	private styleSyntaxRun(text: string, from: number, spans: readonly SyntaxSpan[] | undefined): string {
		if (spans === undefined || spans.length === 0) return text;
		const to = from + text.length;
		const clipped: SyntaxSpan[] = [];
		for (const span of spans) {
			if (span.end <= from || span.start >= to) continue;
			clipped.push({
				start: Math.max(span.start, from) - from,
				end: Math.min(span.end, to) - from,
				kind: span.kind,
			});
		}
		return clipped.length === 0 ? text : applySyntax(text, clipped);
	}

	// ------------------------------------------------------- store plumbing

	/** Push the view's caret into the store before a transaction runs. */
	private syncSelectionToStore(documentId: string): void {
		const state = this.states.get(documentId);
		const record = this.store.getDocument(documentId);
		if (!state || !record) return;
		this.store.setSelection(documentId, {
			cursorOffset: clamp(state.cursorOffset, 0, record.text.length),
			anchorOffset:
				state.selectionAnchorOffset === undefined
					? undefined
					: clamp(state.selectionAnchorOffset, 0, record.text.length),
		});
	}

	/** Pull cursor/selection back from the store and announce a real change. */
	private afterStoreMutation(documentId: string, versionBefore: number): boolean {
		const record = this.store.getDocument(documentId);
		if (!record || record.version === versionBefore) return false;
		const fromStore = this.store.getViewState(documentId);
		const state = this.states.get(documentId);
		if (fromStore && state) {
			state.cursorOffset = fromStore.cursorOffset;
			state.selectionAnchorOffset = fromStore.selectionAnchorOffset;
		}
		this.followCaret = true;
		this.onChangeCallback?.(documentId);
		this.onCursorMoveCallback?.();
		this.requestRenderCallback();
		return true;
	}

	private mutate(documentId: string, action: () => { ok: boolean }): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const versionBefore = record.version;
		// The pre-edit caret line bounds the syntax invalidation below: every
		// view-driven edit happens at (or spans) the caret.
		const lineBefore = offsetToPosition(record.text, this.cursor()).line;
		this.syncSelectionToStore(documentId);
		const result = action();
		if (!result.ok) return true; // refused (read-only, …): consumed, unchanged
		this.afterStoreMutation(documentId, versionBefore);
		const after = this.store.getDocument(documentId);
		if (after && after.version !== versionBefore) {
			const state = this.states.get(documentId);
			const lineAfter = offsetToPosition(after.text, clamp(state?.cursorOffset ?? 0, 0, after.text.length)).line;
			this.syntaxInvalidateFrom(documentId, Math.min(lineBefore, lineAfter), after.version);
		}
		return true;
	}

	private insertVerbatim(documentId: string, text: string): boolean {
		return this.mutate(documentId, () => this.store.insertText(documentId, text));
	}

	private undo(documentId: string): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		return this.mutate(documentId, () => this.store.undo(documentId));
	}

	private redo(documentId: string): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		return this.mutate(documentId, () => this.store.redo(documentId));
	}

	private selectionRange(documentId: string): { startOffset: number; endOffset: number } | undefined {
		const state = this.states.get(documentId);
		if (!state || state.selectionAnchorOffset === undefined) return undefined;
		const startOffset = Math.min(state.selectionAnchorOffset, state.cursorOffset);
		const endOffset = Math.max(state.selectionAnchorOffset, state.cursorOffset);
		return startOffset === endOffset ? undefined : { startOffset, endOffset };
	}

	private copySelection(documentId: string): boolean {
		const record = this.store.getDocument(documentId);
		const selection = this.selectionRange(documentId);
		if (!record || !selection) return false;
		const text = record.text.slice(selection.startOffset, selection.endOffset);
		if (text.length === 0) return false;
		void this.clipboard.copy(text);
		return true;
	}

	private cutSelection(documentId: string): boolean {
		const record = this.store.getDocument(documentId);
		const selection = this.selectionRange(documentId);
		if (!record || !selection) return false;
		const text = record.text.slice(selection.startOffset, selection.endOffset);
		if (text.length > 0) void this.clipboard.copy(text);
		return this.mutate(documentId, () =>
			this.store.applyEdit(documentId, [{ range: selection, text: "" }]),
		);
	}

	private async pasteFromClipboard(documentId: string): Promise<void> {
		let text: string | null = null;
		try {
			text = await this.clipboard.paste();
		} catch {
			text = null;
		}
		if (text === null || text.length === 0) return;
		// The user may have switched tabs while the clipboard read was in
		// flight; never paste into the wrong document.
		if (this.activeDocumentId !== documentId) return;
		this.insertVerbatim(documentId, text);
	}

	private selectAll(documentId: string): boolean {
		const result = this.store.selectAll(documentId);
		if (!result.ok) return false;
		const state = this.states.get(documentId);
		if (state) {
			state.cursorOffset = result.viewState.cursorOffset;
			state.selectionAnchorOffset = result.viewState.selectionAnchorOffset;
		}
		this.followCaret = true;
		this.onCursorMoveCallback?.();
		this.requestRenderCallback();
		return true;
	}

	private setSelection(documentId: string, cursorOffset: number, anchorOffset: number): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const state = this.stateFor(documentId);
		const cursor = clamp(cursorOffset, 0, record.text.length);
		const anchor = clamp(anchorOffset, 0, record.text.length);
		state.cursorOffset = cursor;
		state.selectionAnchorOffset = anchor;
		state.preferredColumn = undefined;
		this.store.setSelection(documentId, { cursorOffset: cursor, anchorOffset: anchor });
		this.followCaret = true;
		this.onCursorMoveCallback?.();
		this.requestRenderCallback();
		return true;
	}

	private moveCaret(documentId: string, nextOffset: number, extend: boolean): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const state = this.stateFor(documentId);
		const cursorOffset = clamp(nextOffset, 0, record.text.length);
		let anchor = state.selectionAnchorOffset;
		if (extend) {
			if (anchor === undefined) anchor = clamp(state.cursorOffset, 0, record.text.length);
		} else {
			anchor = undefined;
		}
		if (cursorOffset === state.cursorOffset && anchor === state.selectionAnchorOffset) return true;
		state.cursorOffset = cursorOffset;
		state.selectionAnchorOffset = anchor;
		// Non-vertical placement (mouse, Home/End, line moves) drops the
		// vertical column preference; moveVertical re-arms it after the move.
		state.preferredColumn = undefined;
		this.store.setSelection(documentId, { cursorOffset, anchorOffset: anchor });
		this.followCaret = true;
		this.onCursorMoveCallback?.();
		this.requestRenderCallback();
		return true;
	}

	private moveHorizontal(
		documentId: string,
		direction: 1 | -1,
		unit: "grapheme" | "word",
		extend: boolean,
	): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const cursor = this.cursor();
		const text = record.text;
		let next: number;
		if (unit === "grapheme") {
			next = direction > 0 ? nextGraphemeOffset(text, cursor) : prevGraphemeOffset(text, cursor);
		} else {
			next = direction > 0 ? nextWordOffset(text, cursor) : prevWordOffset(text, cursor);
		}
		const state = this.stateFor(documentId);
		state.preferredColumn = undefined;
		return this.moveCaret(documentId, next, extend);
	}

	private moveVertical(documentId: string, amount: number, extend: boolean): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const state = this.stateFor(documentId);
		const starts = this.linesFor(record);
		const contentCells = Math.max(0, this.lastWidth - (String(starts.length).length + GUTTER_RIGHT_PADDING));
		const rowCount = this.rowCount(record, starts, contentCells);
		if (rowCount === 0) return true;
		const caretRow = this.rowIndexOfOffset(record, starts, contentCells, state.cursorOffset);
		const position = offsetToPosition(record.text, state.cursorOffset);
		const info = this.lineInfo(record, starts, position.line);
		const preferred = state.preferredColumn ?? this.cellInLine(info, state.cursorOffset);
		const targetRow = clamp(caretRow + amount, 0, rowCount - 1);
		if (targetRow !== caretRow) {
			const offset = this.offsetForVisualRow(record, starts, contentCells, targetRow, preferred);
			this.moveCaret(documentId, offset, extend);
		}
		state.preferredColumn = preferred;
		return true;
	}

	private scrollByLines(delta: number): void {
		const record = this.store.getDocument(this.activeDocumentId);
		if (!record) return;
		const state = this.stateFor(record.id);
		const starts = this.linesFor(record);
		const contentCells = Math.max(0, this.lastWidth - (String(starts.length).length + GUTTER_RIGHT_PADDING));
		const rowCount = this.rowCount(record, starts, contentCells);
		const maxScroll = Math.max(0, rowCount - this.viewportRows);
		const next = clamp(state.scrollLine + delta, 0, maxScroll);
		if (next === state.scrollLine) return;
		state.scrollLine = next;
		// Explicit scrolling freezes caret following until the caret moves.
		this.followCaret = false;
		this.requestRenderCallback();
	}

	// -------------------------------------------------------- line editing

	private deleteLine(documentId: string): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const cursor = this.cursor();
		const starts = this.linesFor(record);
		const line = offsetToPosition(record.text, cursor).line;
		const start = starts[line] ?? record.text.length;
		const end = line + 1 < starts.length ? (starts[line + 1] as number) : record.text.length;
		if (end > start) {
			return this.mutate(documentId, () =>
				this.store.applyEdit(documentId, [{ range: { startOffset: start, endOffset: end }, text: "" }]),
			);
		}
		// The trailing empty line has no span: delete the previous terminator.
		if (line > 0) {
			const previousStart = starts[line - 1] as number;
			const previousEnd = contentEndOf(record.text, previousStart);
			if (start > previousEnd) {
				return this.mutate(documentId, () =>
					this.store.applyEdit(documentId, [
						{ range: { startOffset: previousEnd, endOffset: start }, text: "" },
					]),
				);
			}
		}
		return true;
	}

	private moveLine(documentId: string, direction: 1 | -1): boolean {
		const record = this.store.getDocument(documentId);
		if (!record) return false;
		const cursor = this.cursor();
		const starts = this.linesFor(record);
		const text = record.text;
		const line = offsetToPosition(text, cursor).line;
		const pairStart = direction === -1 ? line - 1 : line;
		if (pairStart < 0 || pairStart + 1 >= starts.length) return true;
		const firstStart = starts[pairStart] as number;
		const secondStart = starts[pairStart + 1] as number;
		const firstEnd = contentEndOf(text, firstStart);
		const secondEnd = contentEndOf(text, secondStart);
		const firstTerminator = text.slice(firstEnd, secondStart);
		const secondTerminatorEnd = pairStart + 2 < starts.length ? (starts[pairStart + 2] as number) : text.length;
		const secondTerminator = text.slice(secondEnd, secondTerminatorEnd);
		const firstContent = text.slice(firstStart, firstEnd);
		const secondContent = text.slice(secondStart, secondEnd);
		const replacement = secondContent + firstTerminator + firstContent + secondTerminator;
		// Caret stays inside the moved line's content.
		const cursorInLine = clamp(
			cursor - (direction === -1 ? secondStart : firstStart),
			0,
			direction === -1 ? secondContent.length : firstContent.length,
		);
		const caretBase =
			direction === -1
				? firstStart + cursorInLine
				: firstStart + secondContent.length + firstTerminator.length + cursorInLine;
		this.mutate(documentId, () =>
			this.store.applyEdit(documentId, [
				{ range: { startOffset: firstStart, endOffset: secondTerminatorEnd }, text: replacement },
			]),
		);
		this.moveCaret(documentId, caretBase, false);
		return true;
	}
}
