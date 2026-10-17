// Shared safe display/ANSI/grapheme helpers and preview shaping.
// Pure functions only: no theme, App, or gateway imports.
// Renderer-generated cursor controls (e.g. pi-tui CURSOR_MARKER) are trusted;
// untrusted tool/approval/user content must pass through sanitizeDisplay first.
/* eslint-disable no-control-regex -- terminal control sequences are the subject matter here. */

import { truncateToWidth, visibleWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";

// OSC: ESC ] ... (BEL | ESC \). APC/DCS/SOS/PM: ESC [PX^_] ... ESC \.
const OSC_RE = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
const APC_RE = /\x1b[PX^_][^\x1b]*(?:\x1b\\|\x07)/g;
// C1 (8-bit) equivalents, same shape: OSC = \x9d ... (BEL | ST \x9c);
// DCS/SOS/PM/APC = \x90\x98\x9e\x9f ... ST. Payloads are removed with the
// introducer (fail closed on missing terminators) so no C1 control can ever
// reach the terminal or recombine with neighbouring bytes.
const C1_OSC_RE = /\x9d[^\x07\x9c]*(?:\x07|\x9c)?/g;
const C1_APC_RE = /[\x90\x98\x9e\x9f][^\x07\x9c]*(?:\x07|\x9c)?/g;
// Single-ESC sequences: ESC( X, ESC) X, ESC# X, ESC[@-_], ESC[\\^_].
const ESC_SINGLE_RE = /\x1b[()][012AB]|[\x1b\x9b][#()][0-9A-B]|[\x1b\x9b][@-Z\\-_]/g;
// CSI: ESC [ params intermediates final. Safe SGR (final `m`) is kept when
// params are allow-listed; every other CSI final is stripped.
const CSI_RE = /[\x1b\x9b]\[[0-?]*[ -/]*[@-~]/g;
// Safe SGR params: reset/bold/dim/italic/underline/inverse/strike + standard
// and bright foreground/background color codes. Anything else (cursor moves,
// clears, scroll, device reports, clipboard, etc.) is unsafe.
const SAFE_SGR_RE = /^[0-9;]*$/;
const SAFE_SGR_CODES = new Set([
	"0", "1", "2", "3", "4", "7", "9", "22", "23", "24", "27", "29",
	"30", "31", "32", "33", "34", "35", "36", "37", "39",
	"40", "41", "42", "43", "44", "45", "46", "47", "49",
	"90", "91", "92", "93", "94", "95", "96", "97",
	"100", "101", "102", "103", "104", "105", "106", "107",
]);

function isSafeSgr(sequence: string): boolean {
	// sequence looks like ESC [ params m (or C1 CSI equivalent).
	const body = sequence.replace(/^[\x1b\x9b]\[/, "").replace(/m$/, "");
	if (!SAFE_SGR_RE.test(body)) return false;
	if (body === "" || body === "0") return true;
	for (const part of body.split(";")) {
		const code = part === "" ? "0" : String(Number(part));
		if (!SAFE_SGR_CODES.has(code)) return false;
		// Reject extended-color forms (38;5;N / 48;2;R;G;B params include
		// codes 38/48/58 which are not in the allow-list above).
	}
	return true;
}

// C0 controls (except \n and expanded \t) + DEL + Unicode line/paragraph
// separators. \r and backspace never survive: they rewrite visible rows.
// NOTE: ESC (\x1b) is excluded here; CSI/ESC sequences are handled above so
// safe SGR color survives while cursor/title/clipboard controls are stripped.
const C0_RE = /[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f\u0085\u2028\u2029]/g;
const CARRIAGE_RE = /\r/g;

/**
 * Sanitize untrusted text for terminal display.
 * - Strips OSC/APC/DCS title/clipboard/hyperlink sequences entirely,
 *   including their C1 (8-bit) forms.
 * - Keeps only allow-listed SGR color sequences; strips cursor movement,
 *   clears, scroll, and all other CSI/ESC/C1 controls.
 * - Strips C0 controls, CR, backspace, DEL, U+0085/U+2028/U+2029.
 * - Expands tabs to two spaces so width math stays honest.
 * Newlines are preserved. Combining/CJK/emoji graphemes pass through.
 */
export function sanitizeDisplay(input: string): string {
	let out = input.replace(/\t/g, "  ");
	out = out.replace(OSC_RE, "");
	out = out.replace(APC_RE, "");
	out = out.replace(C1_OSC_RE, "");
	out = out.replace(C1_APC_RE, "");
	out = out.replace(CSI_RE, (m) => (m.endsWith("m") && isSafeSgr(m) ? m : ""));
	out = out.replace(ESC_SINGLE_RE, "");
	out = out.replace(CARRIAGE_RE, "");
	out = out.replace(C0_RE, "");
	// A lone ESC/C1 that survived (e.g. trailing "\x1b") carries no visible
	// text. Keep safe SGR sequences intact; strip any other ESC/C1 byte.
	out = out.replace(/\x1b(?!\[[0-9;]*m)/g, "").replace(/\x9b(?!\[[0-9;]*m)/g, "");
	// Remaining C1 controls (\x9b handled above so allow-listed SGR survives).
	out = out.replace(/[\x80-\x9a\x9c-\x9f]/g, "");
	return out;
}

/** True when a string carries no cursor-moving/control sequences. */
export function hasNoTerminalControls(text: string): boolean {
	// Allow printable text, \n, safe SGR color, and normal graphemes.
	// Forbid ESC/C1, C0 controls (except \n), DEL, and OSC/APC initiators.
	// ESC bytes are only acceptable as part of a safe SGR sequence.
	let stripped = text;
	if (/[\x1b\x9b]/.test(stripped)) {
		stripped = stripped.replace(CSI_RE, (m) => (m.endsWith("m") && isSafeSgr(m) ? "" : "\x00"));
		if (/[\x1b\x9b\x00]/.test(stripped)) return false;
	}
	return (
		!/[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f\r\u0085\u2028\u2029]/.test(stripped) &&
		// No C1 control bytes survive validation (safe SGR was removed above).
		!/[\x80-\x9f]/.test(stripped)
	);
}

/** Visible terminal width using the renderer's grapheme-aware measurement. */
export function displayWidth(text: string): number {
	return visibleWidth(text);
}

/** Truncate to a visible width (ANSI-aware). Widths 0/1/2 never throw. */
export function truncateDisplay(text: string, width: number): string {
	return truncateToWidth(text, Math.max(0, width));
}

// Bidi embedding/override/isolate controls + LTR/RTL marks (all invisible).
// A name like `\u202Egnp.exe` renders as `exe.png`; `\u2066...\u2069` can
// reorder a path so a hostile label looks like a trusted one.
const BIDI_CONTROLS_RE = /[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

/** SGR color runs (the terminal sanitizer's only allow-listed ESC form). */
const SGR_SEQUENCE_RE = /\x1b\[[0-9;]*m/g;

/** C0 + DEL + C1: every one is non-printing in a single-line label. */
const LABEL_CONTROL_BYTES_RE = /[\x00-\x1f\x7f-\x9f]/g;

/** Physical row breaks, flattened before the single-line pass. */
const LABEL_LINE_BREAKS_RE = /\r\n|[\n\r\t\u2028\u2029]/g;

/**
 * Sanitize one single-line label/name (menu row, dialog title, status word):
 * line breaks and tabs collapse to one space; SGR styling, C0/C1/DEL bytes
 * and bidi/isolate controls are removed. Stripping (not replacing) keeps the
 * honest byte order visible. This is the ONE label core; `editor/display.ts`
 * re-exports it so chrome and menu labels cannot drift apart. Idempotent; pure.
 */
export function sanitizeLabelText(input: string): string {
	const flattened = input.replace(LABEL_LINE_BREAKS_RE, " ");
	// Drop SGR runs wholesale before delegating: a label carries no styling,
	// and leaving the parameters behind would render literal `[31m` noise.
	const terminalSafe = sanitizeDisplay(flattened.replace(SGR_SEQUENCE_RE, ""));
	return terminalSafe.replace(LABEL_CONTROL_BYTES_RE, "").replace(BIDI_CONTROLS_RE, "");
}

/** True when `text` already carries no control/format character. */
export function hasNoLabelControls(text: string): boolean {
	return (
		!/[\x00-\x1f\x7f-\x9f]/.test(text) &&
		!/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/.test(text)
	);
}

/**
 * Collapse text to one physical row: control sequences stripped (newlines
 * become spaces) so a hostile label can never add rows or misroute layout.
 */
export function singleLine(text: string): string {
	return sanitizeLabelText(text);
}

/** Floor a possibly non-finite width to a safe cell count (never negative). */
export function normalizeWidth(width: number): number {
	if (!Number.isFinite(width)) return 0;
	return Math.max(0, Math.floor(width));
}

/** Right-pad an ANSI-bearing row to `width` visible cells (no-op when wider). */
export function padRow(row: string, width: number): string {
	const visible = visibleWidth(row);
	if (visible >= width) return row;
	return row + " ".repeat(width - visible);
}

/** Sanitize then wrap to physical rows of at most `width` cells. */
export function wrapDisplay(text: string, width: number): string[] {
	const safe = sanitizeDisplay(text);
	const w = Math.max(1, width);
	return safe.split("\n").flatMap((line) => wrapTextWithAnsi(line, w));
}

/** Count physical rendered rows for `text` at `width`, including wrapping. */
export function countRenderedLines(text: string, width: number): number {
	const w = Math.max(1, width);
	let rows = 0;
	for (const line of sanitizeDisplay(text).split("\n")) {
		rows += wrapTextWithAnsi(line, w).length;
	}
	return rows;
}

export type DiffKind = "diff-header" | "file-meta" | "hunk" | "add" | "del" | "context";

/** Pure diff classification (styling lives in theme/components). */
export function classifyDiffLine(line: string): DiffKind {
	if (/^diff --git /.test(line)) return "diff-header";
	if (/^(\+\+\+|---) /.test(line)) return "file-meta";
	if (/^@@/.test(line)) return "hunk";
	if (/^\+[^+]/.test(line) || line === "+") return "add";
	if (/^-[^-]/.test(line) || line === "-") return "del";
	return "context";
}

export interface PreviewShape {
	shown: string[];
	omitted: number;
	fullRenderedLines: number;
}

/**
 * Shape retained text into at most `maxLines` physical rows at `width`.
 * Wrapping counts toward the budget. Returns shown rows plus the local
 * omitted count (N = fullRetainedRenderedLines - shownLines).
 */
export function shapePreview(text: string, width: number, maxLines: number): PreviewShape {
	const w = Math.max(1, width);
	const cap = Math.max(0, maxLines);
	const rendered = sanitizeDisplay(text).split("\n").flatMap((line) => wrapTextWithAnsi(line, w));
	if (rendered.length <= cap) return { shown: rendered, omitted: 0, fullRenderedLines: rendered.length };
	return { shown: rendered.slice(0, cap), omitted: rendered.length - cap, fullRenderedLines: rendered.length };
}

/**
 * Keep a UTF-8 prefix of at most `maxBytes` without splitting a code point.
 * Returns the prefix plus whether a local cap truncation occurred.
 */
export function truncateUtf8ByBytes(text: string, maxBytes: number): { text: string; truncated: boolean } {
	const bytes = new TextEncoder().encode(text);
	if (bytes.length <= maxBytes) return { text, truncated: false };
	let cut = maxBytes;
	// Back off over UTF-8 continuation bytes (10xxxxxx).
	while (cut > 0 && (bytes[cut]! & 0xc0) === 0x80) cut--;
	const sliced = bytes.slice(0, cut);
	return { text: new TextDecoder().decode(sliced), truncated: true };
}
