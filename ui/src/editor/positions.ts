// Editor position conversions (plan §10): UTF-16 offsets <-> logical
// line/character positions, grapheme boundaries for motion/deletion, word
// motion, and terminal display cells.
// Pure functions. The single external dependency is pi-tui's grapheme-aware
// `visibleWidth` for cell measurement; tab stops are computed here because the
// toolkit expands tabs to a fixed width rather than to tab stops.
//
// Line terminators: any of "\r\n", "\n", "\r" ends a line. A line's raw
// character span extends to the start of the next line, so the position of the
// LF inside a CRLF pair is addressable ("{ line: 0, character: 2 }" for
// "a\r\nb"); offset -> position -> offset always round-trips. Motion helpers
// never produce that in-pair offset: `Intl.Segmenter` keeps "\r\n" as one
// grapheme and whitespace classification covers both code units.

import { visibleWidth } from "@earendil-works/pi-tui";

export interface Position {
	line: number;
	character: number;
}

/**
 * Editor display tab width in cells. Terminal tabs advance to the next stop;
 * the editor context uses 4 (the renderer must expand tabs identically).
 */
export const EDITOR_TAB_WIDTH = 4;

const graphemeSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

const LF = 0x0a;
const CR = 0x0d;
const TAB = 0x09;

function clampOffset(text: string, offset: number): number {
	if (!Number.isFinite(offset)) return 0;
	const floored = Math.floor(offset);
	if (floored <= 0) return 0;
	if (floored >= text.length) return text.length;
	return floored;
}

function clampNonNegative(value: number): number {
	if (!Number.isFinite(value)) return 0;
	return Math.max(0, Math.floor(value));
}

/** 0-based line/character (UTF-16 code units) for a text offset. */
export function offsetToPosition(text: string, offset: number): Position {
	const target = clampOffset(text, offset);
	let line = 0;
	let lineStart = 0;
	let index = 0;
	while (index < target) {
		const code = text.charCodeAt(index);
		if (code === LF) {
			index += 1;
			line += 1;
			lineStart = index;
			continue;
		}
		if (code === CR) {
			const nextIsLf = index + 1 < text.length && text.charCodeAt(index + 1) === LF;
			if (nextIsLf && index + 1 >= target) {
				// The target sits on the LF inside a CRLF pair: it belongs to the
				// raw character span of the current line.
				break;
			}
			if (nextIsLf) index += 1;
			index += 1;
			line += 1;
			lineStart = index;
			continue;
		}
		index += 1;
	}
	return { line, character: target - lineStart };
}

/**
 * Text offset for a 0-based line/character position (LSP default units).
 * Both components clamp safely: the line clamps onto the last line and the
 * character clamps to the start of the following line (the line's raw span
 * includes its terminator, so exact in-pair positions round-trip).
 */
export function positionToOffset(text: string, position: Position): number {
	const line = clampNonNegative(position.line);
	const character = clampNonNegative(position.character);
	let currentLine = 0;
	let lineStart = 0;
	let index = 0;
	while (index < text.length && currentLine < line) {
		const code = text.charCodeAt(index);
		if (code === LF) {
			index += 1;
			currentLine += 1;
			lineStart = index;
			continue;
		}
		if (code === CR) {
			const nextIsLf = index + 1 < text.length && text.charCodeAt(index + 1) === LF;
			if (nextIsLf) index += 1;
			index += 1;
			currentLine += 1;
			lineStart = index;
			continue;
		}
		index += 1;
	}
	// Raw span end for the resolved line: terminator start plus terminator length.
	let terminator = lineStart;
	while (terminator < text.length) {
		const code = text.charCodeAt(terminator);
		if (code === LF || code === CR) break;
		terminator += 1;
	}
	let spanEnd = terminator;
	if (terminator < text.length) {
		const isCrLf =
			text.charCodeAt(terminator) === CR &&
			terminator + 1 < text.length &&
			text.charCodeAt(terminator + 1) === LF;
		spanEnd = terminator + (isCrLf ? 2 : 1);
	}
	return Math.min(lineStart + character, spanEnd);
}

/** Offset of the first character of the line containing `offset`. */
export function lineStartOffset(text: string, offset: number): number {
	const target = clampOffset(text, offset);
	let lineStart = 0;
	let index = 0;
	while (index < target) {
		const code = text.charCodeAt(index);
		if (code === LF) {
			index += 1;
			lineStart = index;
			continue;
		}
		if (code === CR) {
			const nextIsLf = index + 1 < text.length && text.charCodeAt(index + 1) === LF;
			if (nextIsLf && index + 1 >= target) break;
			if (nextIsLf) index += 1;
			index += 1;
			lineStart = index;
			continue;
		}
		index += 1;
	}
	return lineStart;
}

/** Offset just before the line's terminator (the visible end of the line). */
export function lineEndOffset(text: string, offset: number): number {
	const start = lineStartOffset(text, offset);
	let scan = start;
	while (scan < text.length) {
		const code = text.charCodeAt(scan);
		if (code === LF || code === CR) break;
		scan += 1;
	}
	return scan;
}

/** Number of user-perceived characters (grapheme clusters) in `text`. */
export function graphemeCount(text: string): number {
	return [...graphemeSegmenter.segment(text)].length;
}

/**
 * Next grapheme boundary strictly after `offset` (clamped). Backspace/delete
 * must use these boundaries: emoji, ZWJ sequences, combining marks, flags and
 * CRLF pairs are indivisible units for motion and deletion.
 */
export function nextGraphemeOffset(text: string, offset: number): number {
	const target = clampOffset(text, offset);
	if (target >= text.length) return text.length;
	for (const segment of graphemeSegmenter.segment(text)) {
		const end = segment.index + segment.segment.length;
		if (end > target) return end;
	}
	return text.length;
}

/** Previous grapheme boundary strictly before `offset` (clamped). */
export function prevGraphemeOffset(text: string, offset: number): number {
	const target = clampOffset(text, offset);
	if (target <= 0) return 0;
	let boundary = 0;
	for (const segment of graphemeSegmenter.segment(text)) {
		if (segment.index >= target) break;
		boundary = segment.index;
	}
	return boundary;
}

type WordClass = "identifier" | "whitespace" | "punctuation";

const WHITESPACE_RE = /\s/u;
const IDENTIFIER_RE = /[\p{L}\p{N}\p{M}_]/u;

function classifyCodePoint(codePoint: number): WordClass {
	const character = String.fromCodePoint(codePoint);
	if (WHITESPACE_RE.test(character)) return "whitespace";
	if (IDENTIFIER_RE.test(character)) return "identifier";
	return "punctuation";
}

/** Boundary offsets between identifier/whitespace/punctuation grapheme runs. */
function wordBoundaries(text: string): number[] {
	const segments = [...graphemeSegmenter.segment(text)];
	const boundaries: number[] = [0];
	for (let index = 1; index < segments.length; index++) {
		const previous = segments[index - 1];
		const current = segments[index];
		if (!previous || !current) continue;
		if (classifyCodePoint(previous.segment.codePointAt(0) ?? 0) !== classifyCodePoint(current.segment.codePointAt(0) ?? 0)) {
			boundaries.push(current.index);
		}
	}
	boundaries.push(text.length);
	return boundaries;
}

/**
 * Next word boundary strictly after `offset` (identifier / whitespace /
 * punctuation class changes). Boundaries only fall on grapheme edges, so word
 * motion never splits an emoji or combining sequence.
 */
export function nextWordOffset(text: string, offset: number): number {
	const target = clampOffset(text, offset);
	for (const boundary of wordBoundaries(text)) {
		if (boundary > target) return boundary;
	}
	return text.length;
}

/** Previous word boundary strictly before `offset` (clamped). */
export function prevWordOffset(text: string, offset: number): number {
	const target = clampOffset(text, offset);
	let boundary = 0;
	for (const candidate of wordBoundaries(text)) {
		if (candidate >= target) break;
		boundary = candidate;
	}
	return boundary;
}

function clampTabWidth(tabWidth: number): number {
	if (!Number.isFinite(tabWidth)) return EDITOR_TAB_WIDTH;
	return Math.max(1, Math.floor(tabWidth));
}

/**
 * Terminal cells for one display line. Tabs advance to the next multiple of
 * `tabWidth` (default 4, the editor tab width); wide characters count 2 via
 * pi-tui's grapheme-aware `visibleWidth`; control characters count 0.
 * Pass a single line: line terminators are not meaningful for cell math.
 */
export function displayWidthOf(text: string, tabWidth: number = EDITOR_TAB_WIDTH): number {
	const stop = clampTabWidth(tabWidth);
	let width = 0;
	let start = 0;
	for (let index = 0; index < text.length; index++) {
		if (text.charCodeAt(index) === TAB) {
			if (index > start) width += visibleWidth(text.slice(start, index));
			width += stop - (width % stop);
			start = index + 1;
		}
	}
	if (start < text.length) width += visibleWidth(text.slice(start));
	return width;
}

/**
 * Display column (0-based cells) for `offset` within `text`. `text` may be a
 * whole document or a single line; the column always measures from the line
 * start. Tabs expand to tab stops, not to a fixed width.
 */
export function offsetToDisplayColumn(text: string, offset: number, tabWidth: number = EDITOR_TAB_WIDTH): number {
	const target = clampOffset(text, offset);
	const start = lineStartOffset(text, target);
	return displayWidthOf(text.slice(start, target), tabWidth);
}

/**
 * Offset in `lineText` for a mouse column (0-based cells). The caret lands on
 * the grapheme whose cell span contains `column`; inside a wide character or a
 * tab's padding the offset stays before that grapheme. Columns past the line
 * end clamp to the end of the line.
 */
export function displayColumnToOffset(lineText: string, column: number, tabWidth: number = EDITOR_TAB_WIDTH): number {
	const stop = clampTabWidth(tabWidth);
	const target = clampNonNegative(column);
	let cells = 0;
	for (const segment of graphemeSegmenter.segment(lineText)) {
		const segmentWidth = segment.segment === "\t" ? stop - (cells % stop) : visibleWidth(segment.segment);
		if (target < cells + segmentWidth) return segment.index;
		cells += segmentWidth;
	}
	return lineText.length;
}
