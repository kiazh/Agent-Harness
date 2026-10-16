// T4a contract tests: editor position conversions (plan §10, §24B).
// Pure offset/line/character/grapheme/display-cell math; no store or renderer.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	EDITOR_TAB_WIDTH,
	displayColumnToOffset,
	displayWidthOf,
	lineEndOffset,
	lineStartOffset,
	nextGraphemeOffset,
	nextWordOffset,
	offsetToDisplayColumn,
	offsetToPosition,
	positionToOffset,
	prevGraphemeOffset,
	prevWordOffset,
} from "../src/editor/positions.ts";

// Fixture corpus for the mandatory offset -> position -> offset roundtrip.
// Covers LF, CRLF, CR, mixed endings, empty lines, emoji, ZWJ, flags,
// combining marks, CJK and tabs.
const FIXTURES: string[] = [
	"",
	"a",
	"hello\nworld",
	"a\n",
	"a\n\n",
	"\n",
	"a\r\nb",
	"a\r\nb\r\n",
	"a\rb",
	"a\rb\r",
	"a\r\nb\nc\rd",
	"👍\n漢字",
	"e\u0301\nx",
	"👨‍👩‍👧‍👦 ZWJ",
	"🇺🇸 flag",
	"tab\there",
	"line1\r\nline2\nline3\rline4",
	"trailing \n\n",
];

test("offsetToPosition maps LF text and an empty last line", () => {
	const text = "hello\nworld\n";
	assert.deepEqual(offsetToPosition(text, 0), { line: 0, character: 0 });
	assert.deepEqual(offsetToPosition(text, 5), { line: 0, character: 5 });
	assert.deepEqual(offsetToPosition(text, 6), { line: 1, character: 0 });
	assert.deepEqual(offsetToPosition(text, 11), { line: 1, character: 5 });
	assert.deepEqual(offsetToPosition(text, 12), { line: 2, character: 0 });
	// Clamps beyond the end onto the empty last line.
	assert.deepEqual(offsetToPosition(text, 99), { line: 2, character: 0 });
	assert.deepEqual(offsetToPosition(text, -4), { line: 0, character: 0 });
});

test("offsetToPosition preserves CRLF pairing; positions after \\r\\n land on the next line", () => {
	const text = "a\r\nb"; // a, CR, LF, b
	assert.deepEqual(offsetToPosition(text, 0), { line: 0, character: 0 });
	assert.deepEqual(offsetToPosition(text, 1), { line: 0, character: 1 });
	// Offset 2 is the exact index of the LF inside the pair. It is a real
	// position (needed for round-trips) but motion helpers never produce it.
	assert.deepEqual(offsetToPosition(text, 2), { line: 0, character: 2 });
	assert.deepEqual(offsetToPosition(text, 3), { line: 1, character: 0 });
	assert.deepEqual(offsetToPosition(text, 4), { line: 1, character: 1 });
	// position after \r\n lands on the next line
	assert.equal(positionToOffset(text, { line: 1, character: 0 }), 3);
});

test("offsetToPosition handles CR-only endings", () => {
	const text = "a\rb\r"; // a, CR, b, CR
	assert.deepEqual(offsetToPosition(text, 0), { line: 0, character: 0 });
	assert.deepEqual(offsetToPosition(text, 1), { line: 0, character: 1 });
	assert.deepEqual(offsetToPosition(text, 2), { line: 1, character: 0 });
	assert.deepEqual(offsetToPosition(text, 3), { line: 1, character: 1 });
	assert.deepEqual(offsetToPosition(text, 4), { line: 2, character: 0 });
});

test("positionToOffset clamps line and character safely", () => {
	const text = "a\r\nb"; // line 0 raw span [0,3), line 1 raw span [3,4)
	assert.equal(positionToOffset(text, { line: 0, character: 0 }), 0);
	assert.equal(positionToOffset(text, { line: 0, character: 2 }), 2);
	// Character past the raw span clamps to the start of the next line.
	assert.equal(positionToOffset(text, { line: 0, character: 3 }), 3);
	assert.equal(positionToOffset(text, { line: 0, character: 99 }), 3);
	assert.equal(positionToOffset(text, { line: 1, character: 1 }), 4);
	assert.equal(positionToOffset(text, { line: 1, character: 42 }), 4);
	// Line clamps onto the last line; negative values clamp to the start.
	assert.equal(positionToOffset(text, { line: 99, character: 0 }), 3);
	assert.equal(positionToOffset(text, { line: 99, character: 42 }), 4);
	assert.equal(positionToOffset(text, { line: -3, character: -3 }), 0);
	assert.equal(positionToOffset("", { line: 5, character: 5 }), 0);
});

test("offset <-> position round-trips for every offset in the fixture corpus", () => {
	for (const text of FIXTURES) {
		let previousLine = 0;
		let previousCharacter = 0;
		for (let offset = 0; offset <= text.length; offset++) {
			const position = offsetToPosition(text, offset);
			assert.equal(
				positionToOffset(text, position),
				offset,
				`roundtrip failed for ${JSON.stringify(text)} at offset ${offset}`,
			);
			// Monotonicity: positions never move backwards as offsets grow.
			assert.ok(
				position.line > previousLine ||
					(position.line === previousLine && position.character >= previousCharacter),
				`positions not monotonic for ${JSON.stringify(text)} at offset ${offset}`,
			);
			previousLine = position.line;
			previousCharacter = position.character;
		}
	}
});

test("lineStartOffset and lineEndOffset report the visible line span", () => {
	const text = "a\r\nb\nc"; // a CR LF b LF c
	assert.equal(lineStartOffset(text, 0), 0);
	assert.equal(lineEndOffset(text, 0), 1); // before the CR of the CRLF pair
	assert.equal(lineStartOffset(text, 2), 0); // inside the CRLF terminator
	assert.equal(lineEndOffset(text, 2), 1);
	assert.equal(lineStartOffset(text, 3), 3);
	assert.equal(lineEndOffset(text, 3), 4); // before the LF
	assert.equal(lineStartOffset(text, 5), 5);
	assert.equal(lineEndOffset(text, 5), 6); // last line, no terminator
	assert.equal(lineEndOffset("abc", 99), 3); // clamped into the last line
});

test("grapheme motion never splits emoji, ZWJ sequences, combining marks or flags", () => {
	// Surrogate pair
	const thumbs = "x👍y"; // x, 👍 (2 units), y
	assert.equal(nextGraphemeOffset(thumbs, 0), 1);
	assert.equal(nextGraphemeOffset(thumbs, 1), 3);
	assert.equal(nextGraphemeOffset(thumbs, 2), 3); // inside the pair
	assert.equal(prevGraphemeOffset(thumbs, 3), 1);
	assert.equal(prevGraphemeOffset(thumbs, 2), 1);
	assert.equal(prevGraphemeOffset(thumbs, 1), 0);

	// ZWJ family sequence (11 UTF-16 units, one grapheme)
	const family = "👨‍👩‍👧‍👦";
	assert.equal(family.length, 11);
	assert.equal(nextGraphemeOffset(family, 0), 11);
	assert.equal(nextGraphemeOffset(family, 5), 11);
	assert.equal(prevGraphemeOffset(family, 11), 0);
	assert.equal(prevGraphemeOffset(family, 6), 0);

	// Combining accent
	const accent = "e\u0301x";
	assert.equal(nextGraphemeOffset(accent, 0), 2);
	assert.equal(nextGraphemeOffset(accent, 1), 2);
	assert.equal(prevGraphemeOffset(accent, 2), 0);
	assert.equal(prevGraphemeOffset(accent, 1), 0);

	// CJK (each character is one BMP grapheme)
	const cjk = "漢字";
	assert.equal(nextGraphemeOffset(cjk, 0), 1);
	assert.equal(nextGraphemeOffset(cjk, 1), 2);
	assert.equal(prevGraphemeOffset(cjk, 2), 1);
	assert.equal(prevGraphemeOffset(cjk, 1), 0);

	// Regional-indicator flag
	const flag = "🇺🇸";
	assert.equal(flag.length, 4);
	assert.equal(nextGraphemeOffset(flag, 0), 4);
	assert.equal(nextGraphemeOffset(flag, 1), 4);
	assert.equal(prevGraphemeOffset(flag, 4), 0);
	assert.equal(prevGraphemeOffset(flag, 1), 0);

	// Repeated backspace walks exactly the grapheme boundaries.
	let cursor = family.length;
	const visited: number[] = [cursor];
	while (cursor > 0) {
		cursor = prevGraphemeOffset(family, cursor);
		visited.push(cursor);
	}
	assert.deepEqual(visited, [11, 0]);
});

test("grapheme motion treats CRLF as one cluster and never lands between CR and LF", () => {
	const text = "a\r\nb\r\n"; // a CR LF b CR LF
	assert.equal(nextGraphemeOffset(text, 1), 3); // skips CRLF as one unit
	assert.equal(nextGraphemeOffset(text, 2), 3); // inside the pair -> after it
	assert.equal(nextGraphemeOffset(text, 3), 4);
	assert.equal(nextGraphemeOffset(text, 4), 6);
	assert.equal(prevGraphemeOffset(text, 6), 4);
	assert.equal(prevGraphemeOffset(text, 3), 1);
	assert.equal(prevGraphemeOffset(text, 2), 1); // inside the pair -> before it
	// Motion results never decode to a position between \r and \n.
	assert.deepEqual(offsetToPosition(text, nextGraphemeOffset(text, 1)), { line: 1, character: 0 });
	assert.deepEqual(offsetToPosition(text, prevGraphemeOffset(text, 3)), { line: 0, character: 1 });
});

test("grapheme motion clamps at document edges and for non-finite offsets", () => {
	assert.equal(nextGraphemeOffset("abc", 3), 3);
	assert.equal(nextGraphemeOffset("abc", 99), 3);
	assert.equal(nextGraphemeOffset("abc", -7), 1);
	assert.equal(prevGraphemeOffset("abc", 0), 0);
	assert.equal(prevGraphemeOffset("abc", -7), 0);
	assert.equal(nextGraphemeOffset("", 0), 0);
	assert.equal(prevGraphemeOffset("", 0), 0);
	assert.equal(nextGraphemeOffset("abc", Number.NaN), 1);
});

test("displayWidthOf expands tabs to tab stops with the editor default of 4", () => {
	assert.equal(EDITOR_TAB_WIDTH, 4);
	assert.equal(displayWidthOf(""), 0);
	assert.equal(displayWidthOf("abc"), 3);
	assert.equal(displayWidthOf("a\tb"), 5); // 1 + 3 pad + 1
	assert.equal(displayWidthOf("a\tb", 8), 9); // 1 + 7 pad + 1
	assert.equal(displayWidthOf("\t"), 4);
	assert.equal(displayWidthOf("\t\t"), 8);
	assert.equal(displayWidthOf("\t", 3), 3);
	assert.equal(displayWidthOf("ab\tc"), 5); // 2 + 2 pad + 1
	assert.equal(displayWidthOf("abcd\te"), 9); // 4 + 4 pad + 1
	assert.equal(displayWidthOf("漢\tx"), 5); // 2 + 2 pad + 1
	// tabWidth below 1 clamps to 1 cell; non-finite falls back to the default.
	assert.equal(displayWidthOf("\t", 0), 1);
	assert.equal(displayWidthOf("\t", -3), 1);
	assert.equal(displayWidthOf("\t", Number.NaN), 4);
});

test("displayWidthOf counts wide and zero-width graphemes", () => {
	assert.equal(displayWidthOf("漢"), 2);
	assert.equal(displayWidthOf("漢字"), 4);
	assert.equal(displayWidthOf("👍"), 2);
	assert.equal(displayWidthOf("e\u0301"), 1); // combining accent adds no cell
	assert.equal(displayWidthOf("👨‍👩‍👧‍👦"), 2);
	assert.equal(displayWidthOf("🇺🇸"), 2);
	assert.equal(displayWidthOf("a\r"), 1); // CR occupies no cell
});

test("offsetToDisplayColumn maps offsets after tabs and wide characters", () => {
	const text = "a\tb";
	assert.equal(offsetToDisplayColumn(text, 0), 0);
	assert.equal(offsetToDisplayColumn(text, 1), 1);
	assert.equal(offsetToDisplayColumn(text, 2), 4);
	assert.equal(offsetToDisplayColumn(text, 3), 5);
	assert.equal(offsetToDisplayColumn(text, 99), 5); // clamps to the end
	assert.equal(offsetToDisplayColumn(text, -2), 0);
	const cjk = "漢\tx";
	assert.equal(offsetToDisplayColumn(cjk, 0), 0);
	assert.equal(offsetToDisplayColumn(cjk, 1), 2);
	assert.equal(offsetToDisplayColumn(cjk, 2), 4);
	assert.equal(offsetToDisplayColumn(cjk, 3), 5);
	// Works on a whole document as well: the column resets per line.
	const doc = "ab\ncd";
	assert.equal(offsetToDisplayColumn(doc, 3), 0);
	assert.equal(offsetToDisplayColumn(doc, 4), 1);
	assert.equal(offsetToDisplayColumn(doc, 5), 2);
});

test("displayColumnToOffset maps mouse columns back to offsets", () => {
	const text = "a\tb";
	assert.equal(displayColumnToOffset(text, 0), 0);
	assert.equal(displayColumnToOffset(text, 1), 1);
	// Inside the tab padding the caret stays before the tab character.
	assert.equal(displayColumnToOffset(text, 2), 1);
	assert.equal(displayColumnToOffset(text, 3), 1);
	assert.equal(displayColumnToOffset(text, 4), 2);
	assert.equal(displayColumnToOffset(text, 5), 3); // past the end
	assert.equal(displayColumnToOffset(text, 99), 3);
	assert.equal(displayColumnToOffset(text, -4), 0);

	const cjk = "漢字";
	assert.equal(displayColumnToOffset(cjk, 0), 0);
	assert.equal(displayColumnToOffset(cjk, 1), 0); // second half of 漢 -> its start
	assert.equal(displayColumnToOffset(cjk, 2), 1); // first cell of 字
	assert.equal(displayColumnToOffset(cjk, 4), 2);
	assert.equal(displayColumnToOffset(cjk, 3), 1);

	// Custom tab width moves the stops.
	assert.equal(displayColumnToOffset("a\tb", 3, 8), 1);
	assert.equal(displayColumnToOffset("a\tb", 8, 8), 2);
});

test("offsetToDisplayColumn and displayColumnToOffset round-trip at grapheme starts", () => {
	const lines = ["a\tb", "漢字", "x👍y", "e\u0301z", "\t\tindent", "plain words"];
	for (const line of lines) {
		let offset = 0;
		while (offset < line.length) {
			const column = offsetToDisplayColumn(line, offset);
			assert.equal(
				displayColumnToOffset(line, column),
				offset,
				`display roundtrip failed for ${JSON.stringify(line)} at offset ${offset}`,
			);
			offset = nextGraphemeOffset(line, offset);
		}
		// End of line is also stable.
		const end = line.length;
		assert.equal(displayColumnToOffset(line, offsetToDisplayColumn(line, end)), end);
	}
});

test("word motion stops at identifier/whitespace/punctuation boundaries", () => {
	const text = "const foo_bar = 42;  // note";
	assert.equal(text.length, 28);
	assert.equal(nextWordOffset(text, 0), 5); // end of "const"
	assert.equal(nextWordOffset(text, 5), 6); // across the space
	assert.equal(nextWordOffset(text, 6), 13); // end of "foo_bar"
	assert.equal(nextWordOffset(text, 13), 14); // at "="
	assert.equal(nextWordOffset(text, 14), 15); // after "="
	assert.equal(nextWordOffset(text, 15), 16); // at "42"
	assert.equal(nextWordOffset(text, 16), 18); // after "42"
	assert.equal(nextWordOffset(text, 18), 19); // at ";"
	assert.equal(nextWordOffset(text, 19), 21); // skips the whitespace run at "//"
	assert.equal(nextWordOffset(text, 21), 23); // after "//"
	assert.equal(nextWordOffset(text, 23), 24); // at "note"
	assert.equal(nextWordOffset(text, 24), 28); // end of text
	assert.equal(nextWordOffset(text, 28), 28);

	assert.equal(prevWordOffset(text, 28), 24);
	assert.equal(prevWordOffset(text, 24), 23);
	assert.equal(prevWordOffset(text, 23), 21);
	assert.equal(prevWordOffset(text, 21), 19);
	assert.equal(prevWordOffset(text, 19), 18);
	assert.equal(prevWordOffset(text, 18), 16);
	assert.equal(prevWordOffset(text, 16), 15);
	assert.equal(prevWordOffset(text, 15), 14);
	assert.equal(prevWordOffset(text, 14), 13);
	assert.equal(prevWordOffset(text, 13), 6);
	assert.equal(prevWordOffset(text, 6), 5);
	assert.equal(prevWordOffset(text, 5), 0);
	assert.equal(prevWordOffset(text, 0), 0);

	// From inside a run motion snaps to the run edges.
	assert.equal(nextWordOffset(text, 2), 5);
	assert.equal(prevWordOffset(text, 2), 0);
});

test("word motion is grapheme-safe for emoji and clamps", () => {
	const text = "a👍b";
	assert.equal(nextWordOffset(text, 0), 1);
	assert.equal(nextWordOffset(text, 1), 3);
	assert.equal(nextWordOffset(text, 3), 4);
	assert.equal(nextWordOffset(text, 2), 3); // inside the emoji
	assert.equal(prevWordOffset(text, 4), 3);
	assert.equal(prevWordOffset(text, 3), 1);
	assert.equal(prevWordOffset(text, 2), 1);
	assert.equal(nextWordOffset("", 0), 0);
	assert.equal(prevWordOffset("", 0), 0);
	assert.equal(nextWordOffset("abc", 99), 3);
	assert.equal(prevWordOffset("abc", -9), 0);
});
