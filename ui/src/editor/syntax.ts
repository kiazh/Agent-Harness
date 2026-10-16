// Language-aware syntax classification for the editor (pure, line-oriented).
//
// The editor renders one line at a time, so highlighting is a pure per-line
// function plus a small state object threaded from the previous line. Nothing
// here touches the theme, the store, or pi-tui: the view maps `SyntaxKind` to
// colors, and every range of a line not covered by a span is `plain` by
// omission — `line()` never emits a `"plain"` span.
//
// Honesty rules:
// - This is a lexer, not a parser: it never guesses compile state and never
//   produces diagnostics.
// - An unknown `languageId` classifies as `plain` (no spans) rather than a
//   best-effort language.
// - A malformed input state (an open block comment without a recognized
//   terminator, or a template state for a language without templates) is
//   treated as closed instead of continuing a mode that was never opened.
//
// Offsets are UTF-16 code units within the line, pre-tab-expansion:
// `0 <= start < end <= text.length`. Content at/after a `\r` is never
// highlighted, so a CRLF view that passes the terminator through is safe and
// a trailing `\r` is always plain.
//
// Deliberate simplifications:
// - Python soft keywords `match`/`case` are always `keyword`; f-string
//   interpolation and TS template `${}` interpolation are not split (the
//   whole literal is one `string` span).
// - Unterminated single/double-quoted strings do not carry across lines (the
//   state type has no field for them): the span ends at the line end.
// - Raw-string prefixes disable escape handling on the opening line; for raw
//   triple-quoted strings continued across lines, continuation lines always
//   honor backslash escapes because the state does not carry raw-ness.
// - TS/JS regex literals are not recognized (`/` is an operator, `//` and
//   `/*` are comments). Decorators are recognized only at line start.
// - The TS/JS `function` heuristic: an identifier directly before `(`
//   (whitespace allowed) whose preceding character is line start, space/tab,
//   `(`, `)`, `,`, or `=`, and which is not a keyword.
// - Python builtins: callable names are `function`; type names (and the
//   `TYPE_CHECKING` sentinel) are `type`; attribute access (`obj.print`) is
//   plain.
// - Keyword matching is context-free except for the documented name
//   positions (`def NAME`, `class NAME`, `interface/type/enum NAME`,
//   capital-initial after `new`). `type` only arms the next name when an
//   identifier follows it, so `x.type = value` does not leak the marker.
// - Pending name positions are line-local: `class` alone on a line does not
//   mark the first identifier of the next line.

export type HighlightLanguage = "python" | "typescript" | "javascript" | "plain";

export type SyntaxKind =
	| "keyword"
	| "string"
	| "comment"
	| "number"
	| "function"
	| "decorator"
	| "type"
	| "operator"
	| "plain";

/** Half-open UTF-16 range within one line: `start` inclusive, `end` exclusive. */
export interface SyntaxSpan {
	start: number;
	end: number;
	kind: SyntaxKind;
}

/**
 * State threaded from one line to the next.
 *
 * Canonical shapes produced by `line()`:
 * - `{ inBlockComment: false }` — nothing open.
 * - `{ inBlockComment: true, blockTerminator: "'''" | '"""' }` — a Python
 *   triple-quoted string is open (continuation spans are `string`).
 * - `{ inBlockComment: true, blockTerminator: "*" + "/" }` — a TS/JS block
 *   comment is open (continuation spans are `comment`).
 * - `{ inBlockComment: false, inTemplateString: true }` — a TS/JS template
 *   literal is open (continuation spans are `string`).
 *
 * When both flags are set on input, the block comment wins. An
 * `inBlockComment: true` state without a recognized `blockTerminator` is
 * treated as closed.
 */
export interface LineSyntaxState {
	inBlockComment: boolean;
	blockTerminator?: string;
	inTemplateString?: boolean;
}

/**
 * Map a language-server profile id or plain language name to a highlighter
 * language. Pyright serves `"python"`; typescript-language-server serves
 * `"typescript"`, `"javascript"`, `"typescriptreact"` and
 * `"javascriptreact"`. Anything unknown is `"plain"`.
 */
export function languageForId(languageId: string): HighlightLanguage {
	switch (languageId.trim().toLowerCase()) {
		case "python":
		case "pyright":
		case "pyright-langserver":
			return "python";
		case "typescript":
		case "typescriptreact":
		case "tsls":
		case "typescript-language-server":
			return "typescript";
		case "javascript":
		case "javascriptreact":
			return "javascript";
		default:
			return "plain";
	}
}

// ------------------------------------------------------------------ keywords

const PYTHON_KEYWORDS: ReadonlySet<string> = new Set([
	"def", "class", "return", "if", "else", "elif", "for", "while", "import", "from", "as",
	"with", "lambda", "yield", "try", "except", "finally", "raise", "global", "nonlocal",
	"assert", "del", "pass", "break", "continue", "and", "or", "not", "in", "is", "None",
	"True", "False", "async", "await", "match", "case",
]);

// Documented builtin split: callables are `function`, names that denote types
// (plus the `TYPE_CHECKING` sentinel) are `type`.
const PYTHON_BUILTIN_FUNCTIONS: ReadonlySet<string> = new Set([
	"print", "len", "range", "open", "super", "isinstance", "enumerate", "zip", "map",
	"filter", "sorted", "reversed", "any", "all", "min", "max", "sum", "abs", "round", "input",
]);

const PYTHON_BUILTIN_TYPES: ReadonlySet<string> = new Set([
	"str", "int", "float", "dict", "list", "set", "tuple", "frozenset", "bytes", "bytearray",
	"type", "TYPE_CHECKING",
]);

const PYTHON_STRING_PREFIXES: ReadonlySet<string> = new Set(["r", "b", "u", "f", "rb", "br", "fr", "rf"]);

const TS_KEYWORDS: ReadonlySet<string> = new Set([
	"const", "let", "var", "function", "class", "extends", "implements", "interface", "type",
	"enum", "return", "if", "else", "for", "while", "do", "switch", "case", "default", "break",
	"continue", "new", "delete", "typeof", "instanceof", "in", "of", "import", "export", "from",
	"as", "async", "await", "yield", "throw", "try", "catch", "finally", "this", "super",
	"null", "undefined", "true", "false", "void", "static", "readonly", "public", "private",
	"protected", "get", "set", "declare", "namespace",
]);

/** Words that are keywords in TypeScript but ordinary identifiers in JavaScript. */
const TS_ONLY_KEYWORDS: ReadonlySet<string> = new Set([
	"implements", "interface", "type", "enum", "readonly", "public", "private", "protected",
	"declare", "namespace",
]);

const JS_KEYWORDS: ReadonlySet<string> = new Set([...TS_KEYWORDS].filter((keyword) => !TS_ONLY_KEYWORDS.has(keyword)));

/**
 * Keywords whose following identifier names a declared type. `type` is
 * handled separately because it is contextual (see `nextIdentifierStart`).
 */
const DECLARATION_KEYWORDS: ReadonlySet<string> = new Set(["class", "interface", "enum"]);

// ------------------------------------------------------------------ utilities

function isDigit(code: number): boolean {
	return code >= 0x30 && code <= 0x39;
}

function isLetter(code: number): boolean {
	return (code >= 0x41 && code <= 0x5a) || (code >= 0x61 && code <= 0x7a);
}

function isUpperLetter(code: number): boolean {
	return code >= 0x41 && code <= 0x5a;
}

function isHexDigit(code: number): boolean {
	return isDigit(code) || (code >= 0x41 && code <= 0x46) || (code >= 0x61 && code <= 0x66);
}

function isPythonIdentStart(code: number): boolean {
	return isLetter(code) || code === 0x5f; // _
}

function isPythonIdentChar(code: number): boolean {
	return isPythonIdentStart(code) || isDigit(code);
}

function isJsIdentStart(code: number): boolean {
	return isLetter(code) || code === 0x5f || code === 0x24; // _ $
}

function isJsIdentChar(code: number): boolean {
	return isJsIdentStart(code) || isDigit(code);
}

function isSpaceOrTab(code: number): boolean {
	return code === 0x20 || code === 0x09;
}

/** The operator run characters, plus `@` in Python (matrix multiply). */
function isOperatorChar(code: number, python: boolean): boolean {
	if (code === 0x40) return python; // @
	switch (code) {
		case 0x3d: // =
		case 0x2b: // +
		case 0x2d: // -
		case 0x2a: // *
		case 0x2f: // /
		case 0x25: // %
		case 0x3c: // <
		case 0x3e: // >
		case 0x21: // !
		case 0x26: // &
		case 0x7c: // |
		case 0x5e: // ^
		case 0x7e: // ~
		case 0x3f: // ?
		case 0x3a: // :
			return true;
		default:
			return false;
	}
}

// ------------------------------------------------------------------ state

function closedState(): LineSyntaxState {
	return { inBlockComment: false };
}

function openBlockState(terminator: string): LineSyntaxState {
	return { inBlockComment: true, blockTerminator: terminator };
}

function openTemplateState(): LineSyntaxState {
	return { inBlockComment: false, inTemplateString: true };
}

type OpenState = { kind: "closed" } | { kind: "block"; terminator: string } | { kind: "template" };

function normalizeState(language: HighlightLanguage, state: LineSyntaxState | undefined): OpenState {
	if (state !== undefined && state.inBlockComment === true) {
		const terminator = state.blockTerminator;
		if (typeof terminator !== "string" || terminator.length === 0) return { kind: "closed" };
		if (language === "python" && (terminator === "'''" || terminator === '"""')) {
			return { kind: "block", terminator };
		}
		if ((language === "typescript" || language === "javascript") && terminator === "*/") {
			return { kind: "block", terminator };
		}
		return { kind: "closed" };
	}
	if (state !== undefined && state.inTemplateString === true && (language === "typescript" || language === "javascript")) {
		return { kind: "template" };
	}
	return { kind: "closed" };
}

// ------------------------------------------------------------------ scanners

/** Index just past the first block-comment terminator at/after `from`, or -1. */
function scanBlockCommentBody(text: string, from: number, limit: number): number {
	const at = text.indexOf("*/", from);
	return at === -1 || at + 2 > limit ? -1 : at + 2;
}

/** Index just past the closing backtick at/after `from`, or -1. Honors `\` escapes. */
function scanTemplateBody(text: string, from: number, limit: number): number {
	let index = from;
	while (index < limit) {
		const code = text.charCodeAt(index);
		if (code === 0x5c) {
			// backslash: the next character is escaped
			index += 2;
			continue;
		}
		if (code === 0x60) return index + 1; // `
		index += 1;
	}
	return -1;
}

/**
 * Scan a quoted body for `terminator` (one or three quote characters).
 * `start` is the first body character (after the opening quote/prefix).
 * Backslash escapes are honored unless `raw` (Python raw strings).
 * Returns the index just past the closing quote(s), or -1.
 */
function scanQuotedBody(text: string, start: number, limit: number, terminator: string, raw: boolean): number {
	const firstCode = terminator.charCodeAt(0);
	let index = start;
	while (index < limit) {
		const code = text.charCodeAt(index);
		if (!raw && code === 0x5c) {
			// backslash: the next character is escaped
			index += 2;
			continue;
		}
		if (terminator.length === 1) {
			if (code === firstCode) return index + 1;
		} else if (text.startsWith(terminator, index)) {
			return index + terminator.length;
		}
		index += 1;
	}
	return -1;
}

/**
 * `@` + dotted identifier at the start of a line. `start` points at the `@`.
 * Returns the index just past the decorator, or -1 when no identifier follows.
 */
function scanDecorator(
	text: string,
	start: number,
	limit: number,
	isIdentStart: (code: number) => boolean,
	isIdentChar: (code: number) => boolean,
): number {
	let index = start + 1;
	if (index >= limit || !isIdentStart(text.charCodeAt(index))) return -1;
	for (;;) {
		index += 1;
		while (index < limit && isIdentChar(text.charCodeAt(index))) index += 1;
		if (index < limit && text.charCodeAt(index) === 0x2e && index + 1 < limit && isIdentStart(text.charCodeAt(index + 1))) {
			index += 1;
			continue;
		}
		return index;
	}
}

function scanPythonNumber(text: string, start: number, limit: number): number {
	let index = start;
	if (text.charCodeAt(start) === 0x30 && start + 1 < limit) {
		const prefix = text.charCodeAt(start + 1);
		if (prefix === 0x78 || prefix === 0x58) {
			index = start + 2;
			while (index < limit && (isHexDigit(text.charCodeAt(index)) || text.charCodeAt(index) === 0x5f)) index += 1;
			return index;
		}
		if (prefix === 0x6f || prefix === 0x4f) {
			index = start + 2;
			while (index < limit) {
				const code = text.charCodeAt(index);
				if ((code < 0x30 || code > 0x37) && code !== 0x5f) break;
				index += 1;
			}
			return index;
		}
		if (prefix === 0x62 || prefix === 0x42) {
			index = start + 2;
			while (index < limit) {
				const code = text.charCodeAt(index);
				if (code !== 0x30 && code !== 0x31 && code !== 0x5f) break;
				index += 1;
			}
			return index;
		}
	}
	index = start;
	while (index < limit && (isDigit(text.charCodeAt(index)) || text.charCodeAt(index) === 0x5f)) index += 1;
	if (index < limit && text.charCodeAt(index) === 0x2e) {
		index += 1;
		while (index < limit && (isDigit(text.charCodeAt(index)) || text.charCodeAt(index) === 0x5f)) index += 1;
	}
	if (index < limit) {
		const code = text.charCodeAt(index);
		if (code === 0x65 || code === 0x45) {
			let cursor = index + 1;
			if (cursor < limit && (text.charCodeAt(cursor) === 0x2b || text.charCodeAt(cursor) === 0x2d)) cursor += 1;
			if (cursor < limit && isDigit(text.charCodeAt(cursor))) {
				while (cursor < limit && (isDigit(text.charCodeAt(cursor)) || text.charCodeAt(cursor) === 0x5f)) cursor += 1;
				index = cursor;
			}
		}
	}
	return index;
}

function scanJsNumber(text: string, start: number, limit: number): number {
	let index = start;
	if (text.charCodeAt(start) === 0x30 && start + 1 < limit) {
		const prefix = text.charCodeAt(start + 1);
		if (prefix === 0x78 || prefix === 0x58) {
			index = start + 2;
			while (index < limit && (isHexDigit(text.charCodeAt(index)) || text.charCodeAt(index) === 0x5f)) index += 1;
			if (index < limit && text.charCodeAt(index) === 0x6e) index += 1;
			return index;
		}
		if (prefix === 0x62 || prefix === 0x42 || prefix === 0x6f || prefix === 0x4f) {
			const high = prefix === 0x62 || prefix === 0x42 ? 0x31 : 0x37;
			index = start + 2;
			while (index < limit) {
				const code = text.charCodeAt(index);
				if ((code < 0x30 || code > high) && code !== 0x5f) break;
				index += 1;
			}
			if (index < limit && text.charCodeAt(index) === 0x6e) index += 1;
			return index;
		}
	}
	index = start;
	while (index < limit && (isDigit(text.charCodeAt(index)) || text.charCodeAt(index) === 0x5f)) index += 1;
	let integral = true;
	if (index < limit && text.charCodeAt(index) === 0x2e && index + 1 < limit && isDigit(text.charCodeAt(index + 1))) {
		integral = false;
		index += 2;
		while (index < limit && (isDigit(text.charCodeAt(index)) || text.charCodeAt(index) === 0x5f)) index += 1;
	}
	if (index < limit) {
		const code = text.charCodeAt(index);
		if (code === 0x65 || code === 0x45) {
			let cursor = index + 1;
			if (cursor < limit && (text.charCodeAt(cursor) === 0x2b || text.charCodeAt(cursor) === 0x2d)) cursor += 1;
			if (cursor < limit && isDigit(text.charCodeAt(cursor))) {
				integral = false;
				while (cursor < limit && (isDigit(text.charCodeAt(cursor)) || text.charCodeAt(cursor) === 0x5f)) cursor += 1;
				index = cursor;
			}
		}
	}
	if (integral && index < limit && text.charCodeAt(index) === 0x6e) index += 1; // bigint suffix
	return index;
}

/**
 * The call heuristic: an identifier followed by `(` (whitespace allowed) and
 * preceded by line start, space/tab, `(`, `)`, `,`, or `=`.
 */
function isCallLike(text: string, start: number, end: number, limit: number): boolean {
	let index = end;
	while (index < limit && isSpaceOrTab(text.charCodeAt(index))) index += 1;
	if (index >= limit || text.charCodeAt(index) !== 0x28) return false; // (
	if (start === 0) return true;
	const previous = text.charCodeAt(start - 1);
	return (
		previous === 0x20 ||
		previous === 0x09 ||
		previous === 0x28 ||
		previous === 0x29 ||
		previous === 0x2c ||
		previous === 0x3d
	);
}

/** First non-space/tab code after `from`, or -1. */
function nextNonSpace(text: string, from: number, limit: number): number {
	let index = from;
	while (index < limit && isSpaceOrTab(text.charCodeAt(index))) index += 1;
	return index < limit ? text.charCodeAt(index) : -1;
}

// ------------------------------------------------------------------ highlighter

export class LineHighlighter {
	readonly language: HighlightLanguage;

	constructor(language: HighlightLanguage) {
		// Node runs the .ts sources with types stripped, so normalize defensively.
		this.language =
			language === "python" || language === "typescript" || language === "javascript" ? language : "plain";
	}

	/** Spans for one line given the state produced by the previous line. */
	line(text: string, state?: LineSyntaxState): { spans: SyntaxSpan[]; state: LineSyntaxState } {
		if (this.language === "plain") return { spans: [], state: closedState() };

		const spans: SyntaxSpan[] = [];
		// A line never carries its terminator; content from the first "\r" on
		// stays plain so a CRLF view that passes "\r" through cannot shift
		// offsets or corrupt state.
		const carriageReturn = text.indexOf("\r");
		const limit = carriageReturn === -1 ? text.length : carriageReturn;

		// Resolve the state carried in from the previous line first: the head
		// of this line is inside a block comment, triple-quoted string, or
		// template literal until its terminator is seen.
		let index = 0;
		const open = normalizeState(this.language, state);
		if (open.kind === "template") {
			const end = scanTemplateBody(text, 0, limit);
			if (end === -1) {
				if (limit > 0) spans.push({ start: 0, end: limit, kind: "string" });
				return { spans, state: openTemplateState() };
			}
			spans.push({ start: 0, end, kind: "string" });
			index = end;
		} else if (open.kind === "block") {
			if (this.language === "python") {
				const end = scanQuotedBody(text, 0, limit, open.terminator, false);
				if (end === -1) {
					if (limit > 0) spans.push({ start: 0, end: limit, kind: "string" });
					return { spans, state: openBlockState(open.terminator) };
				}
				spans.push({ start: 0, end, kind: "string" });
				index = end;
			} else {
				const end = scanBlockCommentBody(text, 0, limit);
				if (end === -1) {
					if (limit > 0) spans.push({ start: 0, end: limit, kind: "comment" });
					return { spans, state: openBlockState(open.terminator) };
				}
				spans.push({ start: 0, end, kind: "comment" });
				index = end;
			}
		}

		const python = this.language === "python";
		const keywords = python ? PYTHON_KEYWORDS : this.language === "javascript" ? JS_KEYWORDS : TS_KEYWORDS;
		const isIdentStart = python ? isPythonIdentStart : isJsIdentStart;
		const isIdentChar = python ? isPythonIdentChar : isJsIdentChar;

		let atLineStart = index === 0;
		let pendingFunctionName = false; // Python `def NAME`
		let pendingTypeName = false; // `class|interface|type|enum NAME`
		let pendingConstructor = false; // TS/JS `new NAME`

		while (index < limit) {
			const code = text.charCodeAt(index);

			if (isSpaceOrTab(code)) {
				index += 1;
				continue;
			}

			if (code === 0x40 && atLineStart) {
				// @: decorator position (dotted name directly after @)
				const end = scanDecorator(text, index, limit, isIdentStart, isIdentChar);
				if (end !== -1) {
					spans.push({ start: index, end, kind: "decorator" });
					index = end;
					atLineStart = false;
					continue;
				}
			}
			atLineStart = false;

			if (python) {
				if (code === 0x23) {
					// # comment to end of line
					spans.push({ start: index, end: limit, kind: "comment" });
					index = limit;
					continue;
				}
				if (code === 0x27 || code === 0x22) {
					// ' or " (no prefix: prefixes are caught on the identifier path)
					const quoteCode = code;
					const quote = String.fromCharCode(code);
					const triple =
						index + 2 < limit && text.charCodeAt(index + 1) === quoteCode && text.charCodeAt(index + 2) === quoteCode;
					const terminator = triple ? quote + quote + quote : quote;
					const end = scanQuotedBody(text, index + terminator.length, limit, terminator, false);
					if (end === -1) {
						spans.push({ start: index, end: limit, kind: "string" });
						if (triple) return { spans, state: openBlockState(terminator) };
						index = limit;
						continue;
					}
					spans.push({ start: index, end, kind: "string" });
					index = end;
					continue;
				}
				if (isDigit(code)) {
					const end = scanPythonNumber(text, index, limit);
					spans.push({ start: index, end, kind: "number" });
					index = end;
					continue;
				}
				if (isPythonIdentStart(code)) {
					let end = index + 1;
					while (end < limit && isPythonIdentChar(text.charCodeAt(end))) end += 1;
					const name = text.slice(index, end);
					const next = end < limit ? text.charCodeAt(end) : -1;
					if ((next === 0x27 || next === 0x22) && PYTHON_STRING_PREFIXES.has(name.toLowerCase())) {
						// prefixed string literal: f/r/b/u combinations, case-insensitive
						const quoteCode = next;
						const quote = String.fromCharCode(quoteCode);
						const triple =
							end + 2 < limit && text.charCodeAt(end + 1) === quoteCode && text.charCodeAt(end + 2) === quoteCode;
						const terminator = triple ? quote + quote + quote : quote;
						const raw = name.toLowerCase().includes("r");
						const close = scanQuotedBody(text, end + terminator.length, limit, terminator, raw);
						if (close === -1) {
							spans.push({ start: index, end: limit, kind: "string" });
							if (triple) return { spans, state: openBlockState(terminator) };
							index = limit;
							continue;
						}
						spans.push({ start: index, end: close, kind: "string" });
						index = close;
						continue;
					}
					if (pendingFunctionName) {
						pendingFunctionName = false;
						spans.push({ start: index, end, kind: "function" });
					} else if (pendingTypeName) {
						pendingTypeName = false;
						spans.push({ start: index, end, kind: "type" });
					} else if (PYTHON_KEYWORDS.has(name)) {
						spans.push({ start: index, end, kind: "keyword" });
						if (name === "def") pendingFunctionName = true;
						else if (name === "class") pendingTypeName = true;
					} else {
						const attributed = index > 0 && text.charCodeAt(index - 1) === 0x2e;
						if (!attributed && PYTHON_BUILTIN_FUNCTIONS.has(name)) {
							spans.push({ start: index, end, kind: "function" });
						} else if (!attributed && PYTHON_BUILTIN_TYPES.has(name)) {
							spans.push({ start: index, end, kind: "type" });
						}
					}
					index = end;
					continue;
				}
			} else {
				if (code === 0x2f && index + 1 < limit) {
					// / : line or block comment
					const next = text.charCodeAt(index + 1);
					if (next === 0x2f) {
						spans.push({ start: index, end: limit, kind: "comment" });
						index = limit;
						continue;
					}
					if (next === 0x2a) {
						const end = scanBlockCommentBody(text, index + 2, limit);
						if (end === -1) {
							spans.push({ start: index, end: limit, kind: "comment" });
							return { spans, state: openBlockState("*/") };
						}
						spans.push({ start: index, end, kind: "comment" });
						index = end;
						continue;
					}
				}
				if (code === 0x27 || code === 0x22) {
					// ' or "
					const terminator = String.fromCharCode(code);
					const end = scanQuotedBody(text, index + 1, limit, terminator, false);
					if (end === -1) {
						spans.push({ start: index, end: limit, kind: "string" });
						index = limit;
						continue;
					}
					spans.push({ start: index, end, kind: "string" });
					index = end;
					continue;
				}
				if (code === 0x60) {
					// ` template literal (whole literal one span; ${} not split)
					const end = scanTemplateBody(text, index + 1, limit);
					if (end === -1) {
						spans.push({ start: index, end: limit, kind: "string" });
						return { spans, state: openTemplateState() };
					}
					spans.push({ start: index, end, kind: "string" });
					index = end;
					continue;
				}
				if (isDigit(code)) {
					const end = scanJsNumber(text, index, limit);
					spans.push({ start: index, end, kind: "number" });
					index = end;
					continue;
				}
				if (isJsIdentStart(code)) {
					let end = index + 1;
					while (end < limit && isJsIdentChar(text.charCodeAt(end))) end += 1;
					const name = text.slice(index, end);
					if (pendingTypeName) {
						pendingTypeName = false;
						spans.push({ start: index, end, kind: "type" });
					} else if (keywords.has(name)) {
						spans.push({ start: index, end, kind: "keyword" });
						if (name === "new") {
							pendingConstructor = true;
						} else if (name === "type") {
							// Contextual: only a following identifier is a declared name.
							pendingTypeName = isJsIdentStart(nextNonSpace(text, end, limit));
							pendingConstructor = false;
						} else if (DECLARATION_KEYWORDS.has(name)) {
							pendingTypeName = true;
							pendingConstructor = false;
						} else {
							pendingConstructor = false;
						}
					} else if (pendingConstructor && isUpperLetter(name.charCodeAt(0))) {
						pendingConstructor = false;
						spans.push({ start: index, end, kind: "type" });
					} else {
						pendingConstructor = false;
						if (isCallLike(text, index, end, limit)) spans.push({ start: index, end, kind: "function" });
					}
					index = end;
					continue;
				}
			}

			if (isOperatorChar(code, python)) {
				let end = index + 1;
				while (end < limit && isOperatorChar(text.charCodeAt(end), python)) end += 1;
				spans.push({ start: index, end, kind: "operator" });
				index = end;
				continue;
			}

			index += 1;
		}
		return { spans, state: closedState() };
	}
}
