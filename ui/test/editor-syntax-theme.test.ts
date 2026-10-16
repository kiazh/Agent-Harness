// Syntax styling seam tests: mapping distinctness and width preservation.
import { test } from "node:test";
import assert from "node:assert/strict";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import {
	applySyntax,
	styleSpan,
} from "../src/editor/syntaxTheme.ts";
import { LineHighlighter, languageForId, type SyntaxKind } from "../src/editor/syntax.ts";

test("applySyntax preserves the visible text exactly", () => {
	const line = 'def greet(name: str) -> str:  # say hi';
	const highlighter = new LineHighlighter(languageForId("python"));
	const { spans } = highlighter.line(line);
	const styled = applySyntax(line, spans);
	assert.equal(stripTerminalSequences(styled), line);
	assert.equal(visibleWidth(styled), visibleWidth(line));
	assert.notEqual(styled, line, "styling actually applied");
});

test("plain spans and out-of-range spans are inert", () => {
	const line = "abc";
	assert.equal(applySyntax(line, []), line);
	assert.equal(applySyntax(line, [{ start: 0, end: 3, kind: "plain" }]), line);
	assert.equal(
		applySyntax(line, [{ start: 2, end: 99, kind: "keyword" }]),
		line,
		"overflowing span skipped, not clipped by guess",
	);
	assert.equal(
		applySyntax(line, [{ start: 2, end: 2, kind: "keyword" }]),
		line,
		"empty span ignored",
	);
});

test("overlapping or backwards spans are skipped, never reordered", () => {
	const line = "const x = 1;";
	const styled = applySyntax(line, [
		{ start: 0, end: 5, kind: "keyword" },
		{ start: 3, end: 9, kind: "string" }, // overlaps the previous span
		{ start: 6, end: 7, kind: "number" },
	]);
	assert.equal(stripTerminalSequences(styled), line);
	const kindsApplied = styled.includes("\x1b[");
	assert.ok(kindsApplied, "first span still styled");
});

test("every kind styles distinctly-ish (at least differs from verbatim)", () => {
	const kinds: SyntaxKind[] = [
		"keyword",
		"string",
		"comment",
		"number",
		"function",
		"type",
		"decorator",
		"operator",
	];
	const rendered = kinds.map((kind) => styleSpan(kind, "x"));
	for (const [index, out] of rendered.entries()) {
		assert.equal(stripTerminalSequences(out), "x", `kind ${kinds[index]} keeps text`);
		assert.notEqual(out, "x", `kind ${kinds[index]} styles`);
	}
	const unique = new Set(rendered);
	assert.ok(unique.size >= 6, `kinds should not collapse to one color (got ${unique.size})`);
});

test("a full python line styles from the real tokenizer without width drift", () => {
	const highlighter = new LineHighlighter(languageForId("python"));
	for (const line of [
		'x = f"value: {n}"  # trailing',
		"@decorator",
		"class Point:",
		"return 0x1F + 3.14_2",
		"",
		"\tif x: pass",
	]) {
		const { spans } = highlighter.line(line);
		const styled = applySyntax(line, spans);
		assert.equal(visibleWidth(styled), visibleWidth(line), JSON.stringify(line));
	}
});
