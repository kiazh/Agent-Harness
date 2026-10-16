// T-SYN contract tests: language-aware syntax highlighting (pure module).
// Fixtures are the spec: each multi-line document is threaded through the
// state machine exactly as the view will thread it (line n's returned state
// feeds line n+1), and every result — including pathological inputs — is
// checked against the span invariants: in-bounds, sorted, non-overlapping,
// never "plain" (plain is implied by omission), never covering a carriage
// return.
import { test } from "node:test";
import assert from "node:assert/strict";
import { LineHighlighter, languageForId } from "../src/editor/syntax.ts";
import type { HighlightLanguage, LineSyntaxState, SyntaxKind, SyntaxSpan } from "../src/editor/syntax.ts";

type Token = [SyntaxKind, string];
type Run = { tokens: Token[]; state: LineSyntaxState };

function assertResultInvariants(text: string, result: { spans: SyntaxSpan[]; state: LineSyntaxState }): void {
	assert.equal(typeof result.state.inBlockComment, "boolean");
	let previousEnd = 0;
	for (const span of result.spans) {
		assert.ok(span.start >= previousEnd, `spans sorted and non-overlapping: ${JSON.stringify(span)} in ${JSON.stringify(text)}`);
		assert.ok(span.start >= 0 && span.start < span.end, `span non-empty and in-bounds: ${JSON.stringify(span)}`);
		assert.ok(span.end <= text.length, `span end within line: ${JSON.stringify(span)}`);
		assert.notEqual(span.kind, "plain", "plain spans are implied by omission, never emitted");
		assert.ok(!text.slice(span.start, span.end).includes("\r"), `span must not cover a carriage return: ${JSON.stringify(span)}`);
		previousEnd = span.end;
	}
}

function runFixture(highlighter: LineHighlighter, lines: string[], initial?: LineSyntaxState): Run[] {
	const runs: Run[] = [];
	let state: LineSyntaxState | undefined = initial;
	for (const line of lines) {
		const result = highlighter.line(line, state);
		assertResultInvariants(line, result);
		runs.push({
			tokens: result.spans.map((span) => [span.kind, line.slice(span.start, span.end)] as Token),
			state: result.state,
		});
		state = result.state;
	}
	return runs;
}

function tokens(highlighter: LineHighlighter, text: string, state?: LineSyntaxState): Token[] {
	const result = highlighter.line(text, state);
	assertResultInvariants(text, result);
	return result.spans.map((span) => [span.kind, text.slice(span.start, span.end)] as Token);
}

// ------------------------------------------------------------------ languageForId

test("languageForId maps the pyright and tsls ids; everything else is plain", () => {
	assert.equal(languageForId("python"), "python");
	assert.equal(languageForId("pyright"), "python");
	assert.equal(languageForId("Pyright"), "python");
	assert.equal(languageForId("  Python  "), "python");
	assert.equal(languageForId("typescript"), "typescript");
	assert.equal(languageForId("typescriptreact"), "typescript");
	assert.equal(languageForId("tsls"), "typescript");
	assert.equal(languageForId("typescript-language-server"), "typescript");
	assert.equal(languageForId("javascript"), "javascript");
	assert.equal(languageForId("javascriptreact"), "javascript");
	assert.equal(languageForId("rust"), "plain");
	assert.equal(languageForId(""), "plain");
});

test("languageForId feeds the constructor; unknown ids highlight nothing", () => {
	const py = new LineHighlighter(languageForId("pyright"));
	assert.deepEqual(tokens(py, "def f(): pass"), [["keyword", "def"], ["function", "f"], ["operator", ":"], ["keyword", "pass"]]);
	const ts = new LineHighlighter(languageForId("typescript-language-server"));
	assert.deepEqual(tokens(ts, "let n = 1;"), [["keyword", "let"], ["operator", "="], ["number", "1"]]);
	const js = new LineHighlighter(languageForId("javascriptreact"));
	assert.deepEqual(tokens(js, "let n = 1;"), [["keyword", "let"], ["operator", "="], ["number", "1"]]);
	const unknown = new LineHighlighter(languageForId("ruby"));
	assert.deepEqual(tokens(unknown, "def f(): pass"), []);
});

test("plain language never emits spans and never carries state", () => {
	const plain = new LineHighlighter("plain");
	assert.deepEqual(plain.line('const x = "a"; # comment'), { spans: [], state: { inBlockComment: false } });
	assert.deepEqual(plain.line('"""still nothing', { inBlockComment: true, blockTerminator: '"""' }), {
		spans: [],
		state: { inBlockComment: false },
	});
});

test("a runtime-invalid language degrades to plain", () => {
	const alien = new LineHighlighter("ruby" as unknown as HighlightLanguage);
	assert.deepEqual(alien.line("def f(): pass"), { spans: [], state: { inBlockComment: false } });
});

// ------------------------------------------------------------------ python

const PYTHON_FIXTURE: string[] = [
	"import os",
	"from typing import TYPE_CHECKING",
	"",
	"@decorator",
	'@app.route("/index")',
	"def greet(name: str) -> None:",
	'\t"""Say hello."""',
	'\tmessage = f"hello {name}"',
	"\tprint(message)  # greet",
	"\tvalues = [1, 2.5, 0xFF, 0b1010, 1_000]",
	"\tclass Handler(Base):",
	"\t\tpass",
];

const PYTHON_FIXTURE_TOKENS: Token[][] = [
	[["keyword", "import"]],
	[["keyword", "from"], ["keyword", "import"], ["type", "TYPE_CHECKING"]],
	[],
	[["decorator", "@decorator"]],
	[["decorator", "@app.route"], ["string", '"/index"']],
	[
		["keyword", "def"],
		["function", "greet"],
		["operator", ":"],
		["type", "str"],
		["operator", "->"],
		["keyword", "None"],
		["operator", ":"],
	],
	[["string", '"""Say hello."""']],
	[["operator", "="], ["string", 'f"hello {name}"']],
	[["function", "print"], ["comment", "# greet"]],
	[
		["operator", "="],
		["number", "1"],
		["number", "2.5"],
		["number", "0xFF"],
		["number", "0b1010"],
		["number", "1_000"],
	],
	[["keyword", "class"], ["type", "Handler"], ["operator", ":"]],
	[["keyword", "pass"]],
];

test("python fixture: keywords, decorators, docstrings, f-strings, comments, numbers", () => {
	const py = new LineHighlighter("python");
	const runs = runFixture(py, PYTHON_FIXTURE);
	assert.deepEqual(runs.map((run) => run.tokens), PYTHON_FIXTURE_TOKENS);
	assert.deepEqual(runs[runs.length - 1].state, { inBlockComment: false });
});

test("every documented python keyword classifies as keyword", () => {
	const keywords = [
		"def", "class", "return", "if", "else", "elif", "for", "while", "import", "from", "as",
		"with", "lambda", "yield", "try", "except", "finally", "raise", "global", "nonlocal",
		"assert", "del", "pass", "break", "continue", "and", "or", "not", "in", "is", "None",
		"True", "False", "async", "await", "match", "case",
	];
	const py = new LineHighlighter("python");
	for (const keyword of keywords) {
		assert.deepEqual(tokens(py, keyword), [["keyword", keyword]], keyword);
	}
});

test("python soft keywords match/case highlight in context (documented limitation)", () => {
	const py = new LineHighlighter("python");
	assert.deepEqual(tokens(py, "match value:"), [["keyword", "match"], ["operator", ":"]]);
	assert.deepEqual(tokens(py, "case 1:"), [["keyword", "case"], ["number", "1"], ["operator", ":"]]);
});

test("python numbers: int/float/hex/oct/bin with underscores and exponents", () => {
	const py = new LineHighlighter("python");
	assert.deepEqual(tokens(py, "value = 0xFF_FF + 1_000.5e-3 - 0b1010_1010 + 0o7_5_5"), [
		["operator", "="],
		["number", "0xFF_FF"],
		["operator", "+"],
		["number", "1_000.5e-3"],
		["operator", "-"],
		["number", "0b1010_1010"],
		["operator", "+"],
		["number", "0o7_5_5"],
	]);
	assert.deepEqual(tokens(py, "count = 42_000"), [["operator", "="], ["number", "42_000"]]);
});

test("python string prefixes and raw escape semantics", () => {
	const py = new LineHighlighter("python");
	// Raw: the backslash does not escape the closing quote.
	assert.deepEqual(tokens(py, 'x = r"a\\" + 1'), [
		["operator", "="],
		["string", 'r"a\\"'],
		["operator", "+"],
		["number", "1"],
	]);
	// Non-raw: the escaped quote does not close the string, so the rest of
	// the line stays inside the (unterminated) string.
	assert.deepEqual(tokens(py, 'x = "a\\" + 1'), [["operator", "="], ["string", '"a\\" + 1']]);
	assert.deepEqual(tokens(py, "x = F'f-string'"), [["operator", "="], ["string", "F'f-string'"]]);
	assert.deepEqual(tokens(py, "x = '''doc''' + 1"), [
		["operator", "="],
		["string", "'''doc'''"],
		["operator", "+"],
		["number", "1"],
	]);
	assert.deepEqual(tokens(py, 'x = b"bytes""adjacent"'), [
		["operator", "="],
		["string", 'b"bytes"'],
		["string", '"adjacent"'],
	]);
});

test("python: hash inside strings is not a comment; comments run to end of line", () => {
	const py = new LineHighlighter("python");
	assert.deepEqual(tokens(py, 'x = "# not a comment"  # real'), [
		["operator", "="],
		["string", '"# not a comment"'],
		["comment", "# real"],
	]);
	assert.deepEqual(tokens(py, "import os  # stdlib"), [["keyword", "import"], ["comment", "# stdlib"]]);
});

test("python builtins classify by role; attribute access stays plain", () => {
	const py = new LineHighlighter("python");
	assert.deepEqual(tokens(py, "print(len(items))"), [["function", "print"], ["function", "len"]]);
	assert.deepEqual(tokens(py, "values = dict.fromkeys(items)"), [["operator", "="], ["type", "dict"]]);
	assert.deepEqual(tokens(py, "if isinstance(x, type):"), [
		["keyword", "if"],
		["function", "isinstance"],
		["type", "type"],
		["operator", ":"],
	]);
	assert.deepEqual(tokens(py, "obj.print = 1"), [["operator", "="], ["number", "1"]]);
});

// ------------------------------------------------------------------ typescript

const TS_FIXTURE: string[] = [
	'import type { Options } from "./options";',
	"",
	"@Injectable()",
	"class Service extends Base implements Lifecycle {",
	'\treadonly name = "service";',
	"\tprivate count = 0x1f + 1_000;",
	"\t/* a block",
	"\t   comment */",
	"\tsum(a: number, b: number): number {",
	"\t\treturn a + b;",
	"\t}",
	"}",
	"const template = `first",
	"second ${interpolated}",
	"third`;",
	"export default new Service();",
	"function helper(flag: boolean = true): void {",
	"\tif (flag) { helper(); }",
	"}",
];

const TS_FIXTURE_TOKENS: Token[][] = [
	[["keyword", "import"], ["keyword", "type"], ["keyword", "from"], ["string", '"./options"']],
	[],
	[["decorator", "@Injectable"]],
	[["keyword", "class"], ["type", "Service"], ["keyword", "extends"], ["keyword", "implements"]],
	[["keyword", "readonly"], ["operator", "="], ["string", '"service"']],
	[["keyword", "private"], ["operator", "="], ["number", "0x1f"], ["operator", "+"], ["number", "1_000"]],
	[["comment", "/* a block"]],
	[["comment", "\t   comment */"]],
	[["function", "sum"], ["operator", ":"], ["operator", ":"], ["operator", ":"]],
	[["keyword", "return"], ["operator", "+"]],
	[],
	[],
	[["keyword", "const"], ["operator", "="], ["string", "`first"]],
	[["string", "second ${interpolated}"]],
	[["string", "third`"]],
	[["keyword", "export"], ["keyword", "default"], ["keyword", "new"], ["type", "Service"]],
	[
		["keyword", "function"],
		["function", "helper"],
		["operator", ":"],
		["operator", "="],
		["keyword", "true"],
		["operator", ":"],
		["keyword", "void"],
	],
	[["keyword", "if"], ["function", "helper"]],
	[],
];

test("typescript fixture: interface/class/template/block comment/arrow-free declarations", () => {
	const ts = new LineHighlighter("typescript");
	const runs = runFixture(ts, TS_FIXTURE);
	assert.deepEqual(runs.map((run) => run.tokens), TS_FIXTURE_TOKENS);
	assert.deepEqual(runs[6].state, { inBlockComment: true, blockTerminator: "*/" });
	assert.deepEqual(runs[7].state, { inBlockComment: false });
	assert.deepEqual(runs[12].state, { inBlockComment: false, inTemplateString: true });
	assert.deepEqual(runs[14].state, { inBlockComment: false });
	assert.deepEqual(runs[runs.length - 1].state, { inBlockComment: false });
});

test("typescript declaration and constructor positions classify names as type", () => {
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(ts, "class Point {}"), [["keyword", "class"], ["type", "Point"]]);
	assert.deepEqual(tokens(ts, "interface Shape {}"), [["keyword", "interface"], ["type", "Shape"]]);
	assert.deepEqual(tokens(ts, "type Alias = string;"), [["keyword", "type"], ["type", "Alias"], ["operator", "="]]);
	assert.deepEqual(tokens(ts, "enum Color { Red }"), [["keyword", "enum"], ["type", "Color"]]);
	assert.deepEqual(tokens(ts, "new Point(1)"), [["keyword", "new"], ["type", "Point"], ["number", "1"]]);
	assert.deepEqual(tokens(ts, "new _point(1)"), [["keyword", "new"], ["function", "_point"], ["number", "1"]]);
	assert.deepEqual(tokens(ts, "const p = new point(1);"), [
		["keyword", "const"],
		["operator", "="],
		["keyword", "new"],
		["function", "point"],
		["number", "1"],
	]);
	// `type` only arms the declaration name when a name follows: property
	// access must not leak the marker onto the right-hand side.
	assert.deepEqual(tokens(ts, "x.type = value;"), [["keyword", "type"], ["operator", "="]]);
});

test("typescript call heuristic: identifier before ( with an allowed predecessor", () => {
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(ts, "foo()"), [["function", "foo"]]);
	assert.deepEqual(tokens(ts, "x = f(1)"), [["operator", "="], ["function", "f"], ["number", "1"]]);
	assert.deepEqual(tokens(ts, "a, g(b)"), [["function", "g"]]);
	assert.deepEqual(tokens(ts, "obj.method()"), []);
	assert.deepEqual(tokens(ts, "(fn(1))"), [["function", "fn"], ["number", "1"]]);
	assert.deepEqual(tokens(ts, "if (x) {}"), [["keyword", "if"]]);
	assert.deepEqual(tokens(ts, "return (x);"), [["keyword", "return"]]);
	assert.deepEqual(tokens(ts, "function declared() {}"), [["keyword", "function"], ["function", "declared"]]);
	assert.deepEqual(tokens(ts, "{ shorthand() {} }"), [["function", "shorthand"]]);
	assert.deepEqual(tokens(ts, "sum (a)"), [["function", "sum"]]);
});

test("operator runs collapse; punctuation stays plain", () => {
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(ts, "a === b ? c : d"), [["operator", "==="], ["operator", "?"], ["operator", ":"]]);
	assert.deepEqual(tokens(ts, "x += 1; y ??= 2;"), [
		["operator", "+="],
		["number", "1"],
		["operator", "??="],
		["number", "2"],
	]);
	assert.deepEqual(tokens(ts, "const z = a < b && c > d;"), [
		["keyword", "const"],
		["operator", "="],
		["operator", "<"],
		["operator", "&&"],
		["operator", ">"],
	]);
	const py = new LineHighlighter("python");
	assert.deepEqual(tokens(py, "result = a // b ** c"), [["operator", "="], ["operator", "//"], ["operator", "**"]]);
	assert.deepEqual(tokens(py, "matrix = a @ b"), [["operator", "="], ["operator", "@"]]);
});

test("typescript numbers: hex/bin/oct, underscores, exponents, bigint", () => {
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(ts, "0xFF + 0b1010 + 0o17 + 1_000_000 + 1e10 + 1.5e-3 + 2n"), [
		["number", "0xFF"],
		["operator", "+"],
		["number", "0b1010"],
		["operator", "+"],
		["number", "0o17"],
		["operator", "+"],
		["number", "1_000_000"],
		["operator", "+"],
		["number", "1e10"],
		["operator", "+"],
		["number", "1.5e-3"],
		["operator", "+"],
		["number", "2n"],
	]);
	assert.deepEqual(tokens(ts, "const v = 0xFFn;"), [["keyword", "const"], ["operator", "="], ["number", "0xFFn"]]);
});

test("typescript string escapes and template interpolation", () => {
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(ts, 'const s = "a\\"b";'), [["keyword", "const"], ["operator", "="], ["string", '"a\\"b"']]);
	assert.deepEqual(tokens(ts, "const s = `a${b}c`;"), [["keyword", "const"], ["operator", "="], ["string", "`a${b}c`"]]);
	assert.deepEqual(tokens(ts, "const s = `a\\`b`;"), [["keyword", "const"], ["operator", "="], ["string", "`a\\`b`"]]);
});

test("every documented typescript keyword classifies as keyword", () => {
	const keywords = [
		"const", "let", "var", "function", "class", "extends", "implements", "interface", "type",
		"enum", "return", "if", "else", "for", "while", "do", "switch", "case", "default", "break",
		"continue", "new", "delete", "typeof", "instanceof", "in", "of", "import", "export", "from",
		"as", "async", "await", "yield", "throw", "try", "catch", "finally", "this", "super",
		"null", "undefined", "true", "false", "void", "static", "readonly", "public", "private",
		"protected", "get", "set", "declare", "namespace",
	];
	const ts = new LineHighlighter("typescript");
	for (const keyword of keywords) {
		assert.deepEqual(tokens(ts, keyword), [["keyword", keyword]], keyword);
	}
});

// ------------------------------------------------------------------ javascript

const JS_FIXTURE: string[] = [
	"const type = 1;",
	"interface Point {}",
	"class Foo {}",
	"const add = (a, b) => a + b;",
	"async function load() { await fetch(url); }",
];

const JS_FIXTURE_TOKENS: Token[][] = [
	[["keyword", "const"], ["operator", "="], ["number", "1"]],
	[],
	[["keyword", "class"], ["type", "Foo"]],
	[["keyword", "const"], ["operator", "="], ["operator", "=>"], ["operator", "+"]],
	[["keyword", "async"], ["keyword", "function"], ["function", "load"], ["keyword", "await"], ["function", "fetch"]],
];

test("javascript fixture: TS-only words stay plain, arrows and functions still highlight", () => {
	const js = new LineHighlighter("javascript");
	const runs = runFixture(js, JS_FIXTURE);
	assert.deepEqual(runs.map((run) => run.tokens), JS_FIXTURE_TOKENS);
});

test("typescript treats the same words as keywords: languageForId matters", () => {
	const js = new LineHighlighter("javascript");
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(js, "const type = 1;"), [["keyword", "const"], ["operator", "="], ["number", "1"]]);
	assert.deepEqual(tokens(ts, "const type = 1;"), [["keyword", "const"], ["keyword", "type"], ["operator", "="], ["number", "1"]]);
	assert.deepEqual(tokens(js, "interface Point {}"), []);
	assert.deepEqual(tokens(ts, "interface Point {}"), [["keyword", "interface"], ["type", "Point"]]);
});

// ------------------------------------------------------------------ multi-line state round-trips

test("python triple-quoted docstring carries state across 20 lines", () => {
	const py = new LineHighlighter("python");
	const lines = ['value = """'];
	for (let i = 1; i <= 18; i += 1) lines.push(`docstring line ${i}`);
	lines.push('"""');
	lines.push("return value");
	const runs = runFixture(py, lines);
	assert.equal(runs.length, 21);
	assert.deepEqual(runs[0], {
		tokens: [["operator", "="], ["string", '"""']],
		state: { inBlockComment: true, blockTerminator: '"""' },
	});
	for (let i = 1; i <= 18; i += 1) {
		assert.deepEqual(runs[i].tokens, [["string", lines[i]]], `docstring boundary at line ${i}`);
		assert.deepEqual(runs[i].state, { inBlockComment: true, blockTerminator: '"""' });
	}
	assert.deepEqual(runs[19], { tokens: [["string", '"""']], state: { inBlockComment: false } });
	assert.deepEqual(runs[20].tokens, [["keyword", "return"]]);
});

test("typescript block comment carries state across 20 lines", () => {
	const ts = new LineHighlighter("typescript");
	const lines = ["/* header"];
	for (let i = 1; i <= 18; i += 1) lines.push(` * line ${i}`);
	lines.push(" */");
	lines.push("const done = true;");
	const runs = runFixture(ts, lines);
	assert.equal(runs.length, 21);
	assert.deepEqual(runs[0].state, { inBlockComment: true, blockTerminator: "*/" });
	for (let i = 1; i <= 18; i += 1) {
		assert.deepEqual(runs[i].tokens, [["comment", lines[i]]], `comment boundary at line ${i}`);
		assert.deepEqual(runs[i].state, { inBlockComment: true, blockTerminator: "*/" });
	}
	assert.deepEqual(runs[19], { tokens: [["comment", " */"]], state: { inBlockComment: false } });
	assert.deepEqual(runs[20].tokens, [["keyword", "const"], ["operator", "="], ["keyword", "true"]]);
});

test("typescript template literal carries string state across 20 lines", () => {
	const ts = new LineHighlighter("typescript");
	const lines = ["const page = `line 1"];
	for (let i = 2; i <= 19; i += 1) lines.push(`line ${i}`);
	lines.push("line 20`;");
	lines.push("const after = 1;");
	const runs = runFixture(ts, lines);
	assert.equal(runs.length, 21);
	assert.deepEqual(runs[0], {
		tokens: [["keyword", "const"], ["operator", "="], ["string", "`line 1"]],
		state: { inBlockComment: false, inTemplateString: true },
	});
	for (let i = 1; i <= 18; i += 1) {
		assert.deepEqual(runs[i].tokens, [["string", lines[i]]], `template boundary at line ${i}`);
		assert.deepEqual(runs[i].state, { inBlockComment: false, inTemplateString: true });
	}
	assert.deepEqual(runs[19], { tokens: [["string", "line 20`"]], state: { inBlockComment: false } });
	assert.deepEqual(runs[20].tokens, [["keyword", "const"], ["operator", "="], ["number", "1"]]);
});

// ------------------------------------------------------------------ boundaries and edge cases

test("empty lines preserve state and emit no spans", () => {
	const py = new LineHighlighter("python");
	assert.deepEqual(py.line(""), { spans: [], state: { inBlockComment: false } });
	assert.deepEqual(py.line("", { inBlockComment: true, blockTerminator: '"""' }), {
		spans: [],
		state: { inBlockComment: true, blockTerminator: '"""' },
	});
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(ts.line("", { inBlockComment: true, blockTerminator: "*/" }), {
		spans: [],
		state: { inBlockComment: true, blockTerminator: "*/" },
	});
	assert.deepEqual(ts.line("", { inBlockComment: false, inTemplateString: true }), {
		spans: [],
		state: { inBlockComment: false, inTemplateString: true },
	});
});

test("malformed input states degrade to closed instead of inventing a mode", () => {
	const py = new LineHighlighter("python");
	assert.deepEqual(tokens(py, "print(1)", { inBlockComment: true }), [["function", "print"], ["number", "1"]]);
	assert.deepEqual(tokens(py, "print(1)", { inBlockComment: true, blockTerminator: "*/" }), [
		["function", "print"],
		["number", "1"],
	]);
	assert.deepEqual(tokens(py, "print(1)", { inBlockComment: false, inTemplateString: true }), [
		["function", "print"],
		["number", "1"],
	]);
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(tokens(ts, "foo()", { inBlockComment: true, blockTerminator: "nonsense" }), [["function", "foo"]]);
	// Block comment wins when both flags are set.
	assert.deepEqual(ts.line("still /* open", { inBlockComment: true, blockTerminator: "*/", inTemplateString: true }).state, {
		inBlockComment: true,
		blockTerminator: "*/",
	});
	assert.deepEqual(ts.line("done */", { inBlockComment: true, blockTerminator: "*/", inTemplateString: true }).state, {
		inBlockComment: false,
	});
});

test("unterminated constructs stay honest at end of line", () => {
	const py = new LineHighlighter("python");
	const triple = py.line("x = '''", undefined);
	assert.deepEqual(triple.spans, [
		{ start: 2, end: 3, kind: "operator" },
		{ start: 4, end: 7, kind: "string" },
	]);
	assert.deepEqual(triple.state, { inBlockComment: true, blockTerminator: "'''" });
	const carried = py.line("still open", triple.state);
	assert.deepEqual(carried.spans, [{ start: 0, end: 10, kind: "string" }]);
	assert.deepEqual(carried.state, { inBlockComment: true, blockTerminator: "'''" });

	const ts = new LineHighlighter("typescript");
	const openComment = ts.line("/* never closed", undefined);
	assert.deepEqual(openComment.spans, [{ start: 0, end: 15, kind: "comment" }]);
	assert.deepEqual(openComment.state, { inBlockComment: true, blockTerminator: "*/" });
	const openTemplate = ts.line("const s = `no end", undefined);
	assert.deepEqual(openTemplate.state, { inBlockComment: false, inTemplateString: true });

	// Unterminated single-line quotes end at the line (no cross-line state).
	const quote = py.line('x = "abc\\', undefined);
	assert.deepEqual(quote.spans, [
		{ start: 2, end: 3, kind: "operator" },
		{ start: 4, end: 9, kind: "string" },
	]);
	assert.deepEqual(quote.state, { inBlockComment: false });
	const template = ts.line("const s = `a\\`", undefined);
	assert.deepEqual(template.spans, [
		{ start: 0, end: 5, kind: "keyword" },
		{ start: 8, end: 9, kind: "operator" },
		{ start: 10, end: 14, kind: "string" },
	]);
	assert.deepEqual(template.state, { inBlockComment: false, inTemplateString: true });
});

test("CRLF safety: trailing carriage returns are plain and offsets are unchanged", () => {
	const py = new LineHighlighter("python");
	const plainLine = py.line("def foo():");
	const crlfLine = py.line("def foo():\r");
	assert.deepEqual(crlfLine.spans, plainLine.spans);
	assert.deepEqual(crlfLine.state, plainLine.state);
	assert.deepEqual(tokens(py, "  # note\r"), [["comment", "# note"]]);
	const opened = py.line('x = """\r');
	assert.deepEqual(opened.spans, [
		{ start: 2, end: 3, kind: "operator" },
		{ start: 4, end: 7, kind: "string" },
	]);
	assert.deepEqual(opened.state, { inBlockComment: true, blockTerminator: '"""' });
	const ts = new LineHighlighter("typescript");
	assert.deepEqual(ts.line("code // note\r").spans, [{ start: 5, end: 12, kind: "comment" }]);
	// A lone interior carriage return ends the scanned segment; the rest is plain.
	assert.deepEqual(py.line("a\rdef f():").spans, []);
});

test("tabs do not shift offsets", () => {
	const py = new LineHighlighter("python");
	const result = py.line("\tdef f(): return 1");
	assert.deepEqual(result.spans, [
		{ start: 1, end: 4, kind: "keyword" },
		{ start: 5, end: 6, kind: "function" },
		{ start: 8, end: 9, kind: "operator" },
		{ start: 10, end: 16, kind: "keyword" },
		{ start: 17, end: 18, kind: "number" },
	]);
});

test("span invariants hold across a mixed corpus in every language and state", () => {
	const corpus: string[] = [
		"",
		" ",
		"\t",
		"plain words only",
		"def f(x): return x",
		"@decorator('''x''')",
		'x = """',
		'x = """done""" # end',
		"# comment only",
		"print(len(values))  # builtins",
		"0xFF + 1_000.5e-3 - 0b1010j + 0o755",
		"class C(Base): pass",
		"const x = `a${b}c`;",
		"const x = `unterminated",
		"/* open",
		"closed */ const y = 1;",
		"// line comment",
		"@Component({ selector: 'app' })",
		"function f(a: number = 1) { return a; }",
		"obj.method().chain(value)(other)",
		"x = 'unterminated single quote",
		"a === b ? c : d; let ok = true;",
		"interface I extends J { readonly k: T; }",
		"type T = { p: string };",
		"new Map<string, number>()",
		"weird\rCR mixed",
		"emoji 👍 漢字 σ = 1",
		"\\",
		'"""',
		"--",
		"@",
		"'",
	];
	const languages: HighlightLanguage[] = ["python", "typescript", "javascript", "plain"];
	for (const language of languages) {
		const highlighter = new LineHighlighter(language);
		runFixture(highlighter, corpus);
		runFixture(highlighter, corpus, { inBlockComment: true, blockTerminator: language === "python" ? '"""' : "*/" });
		runFixture(highlighter, corpus, { inBlockComment: false, inTemplateString: true });
		runFixture(highlighter, corpus, { inBlockComment: true });
	}
});

test("pathological 10k-character inputs tokenize in linear time", () => {
	const started = Date.now();
	const cases: Array<{ highlighter: LineHighlighter; text: string }> = [
		{ highlighter: new LineHighlighter("python"), text: `"${"a".repeat(10_000)}` },
		{ highlighter: new LineHighlighter("python"), text: `"""${"a".repeat(10_000)}` },
		{ highlighter: new LineHighlighter("python"), text: `x = ${"1+".repeat(5_000)}1` },
		{ highlighter: new LineHighlighter("typescript"), text: "`" + "a".repeat(10_000) },
		{ highlighter: new LineHighlighter("typescript"), text: "/*" + "*".repeat(10_000) },
		{ highlighter: new LineHighlighter("typescript"), text: '"' + '\\"'.repeat(5_000) },
		{ highlighter: new LineHighlighter("typescript"), text: "x = 1 + 2;".repeat(1_250) },
	];
	for (const { highlighter, text } of cases) {
		const result = highlighter.line(text);
		assertResultInvariants(text, result);
	}
	assert.ok(Date.now() - started < 2_000, "10k-character inputs tokenize quickly");
});
