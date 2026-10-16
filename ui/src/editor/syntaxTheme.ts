// SyntaxKind -> theme style mapping (closing-pass seam).
// Kept separate from `syntax.ts` (pure) so the tokenizer stays import-free;
// this module is the only place that knows about the theme. The view imports
// `applySyntax(line, spans)` and appends the result after its gutter.
//
// Distinctness within every skin: keyword(accent) > type(info) > function(
// secondary) > string(success) > number/decorator(warning/accentBold) >
// operator(muted) > comment(dim). `visibleWidth` is preserved: styles only
// wrap text in SGR sequences.

import { theme } from "../theme.ts";
import type { SyntaxKind, SyntaxSpan } from "./syntax.ts";

type StyleFn = (text: string) => string;

const STYLES: Record<Exclude<SyntaxKind, "plain">, StyleFn> = {
	keyword: theme.accent,
	string: theme.success,
	comment: theme.dim,
	number: theme.warning,
	function: theme.secondary,
	type: theme.info,
	decorator: theme.accentBold,
	operator: theme.muted,
};

/** Style one already-extracted span; plain (or unknown) renders verbatim. */
export function styleSpan(kind: SyntaxKind, text: string): string {
	if (kind === "plain") return text;
	const style = STYLES[kind as Exclude<SyntaxKind, "plain">];
	return style ? style(text) : text;
}

/**
 * Apply syntax spans to a plain line. Spans must come from the same text
 * (offsets are UTF-16); malformed spans are skipped rather than guessed at.
 * Uncovered ranges render plain.
 */
export function applySyntax(line: string, spans: readonly SyntaxSpan[]): string {
	if (spans.length === 0) return line;
	let out = "";
	let cursor = 0;
	for (const span of spans) {
		if (
			span.kind === "plain" ||
			span.start < cursor ||
			span.end <= span.start ||
			span.end > line.length
		) {
			continue;
		}
		if (span.start > cursor) out += line.slice(cursor, span.start);
		out += styleSpan(span.kind, line.slice(span.start, span.end));
		cursor = span.end;
	}
	return out + line.slice(cursor);
}
