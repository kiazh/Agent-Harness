// T9-logic contract tests: bounded back/forward jump history (plan §17).
// Classic browser semantics over plain data entries: dedupe, forward-stack
// truncation, boundary walks and oldest-first eviction at the 50-entry bound.
import { test } from "node:test";
import assert from "node:assert/strict";
import { JUMP_HISTORY_LIMIT, JumpHistory } from "../src/editor/navigation.ts";

test("empty history returns undefined from every traversal", () => {
	const history = new JumpHistory();
	assert.equal(history.size, 0);
	assert.equal(history.current(), undefined);
	assert.equal(history.back(), undefined);
	assert.equal(history.forward(), undefined);
});

test("push records entries and deduplicates identical consecutive targets", () => {
	const history = new JumpHistory();
	history.push({ uri: "file:///w/a.ts", offset: 10 });
	assert.equal(history.size, 1);
	assert.deepEqual(history.current(), { uri: "file:///w/a.ts", offset: 10 });
	history.push({ uri: "file:///w/a.ts", offset: 10 });
	assert.equal(history.size, 1);
	// Optional fields are part of entry identity.
	history.push({ uri: "file:///w/a.ts", offset: 10, documentId: "doc-1" });
	assert.equal(history.size, 2);
	history.push({ uri: "file:///w/b.ts", offset: 0, line: 3 });
	assert.equal(history.size, 3);
	assert.deepEqual(history.current(), { uri: "file:///w/b.ts", offset: 0, line: 3 });
});

test("back and forward move through the stack with browser semantics", () => {
	const history = new JumpHistory();
	history.push({ uri: "file:///w/a.ts", offset: 1 });
	history.push({ uri: "file:///w/b.ts", offset: 2 });
	history.push({ uri: "file:///w/c.ts", offset: 3 });

	assert.deepEqual(history.back(), { uri: "file:///w/b.ts", offset: 2 });
	assert.deepEqual(history.current(), { uri: "file:///w/b.ts", offset: 2 });
	assert.deepEqual(history.back(), { uri: "file:///w/a.ts", offset: 1 });
	// The oldest entry ends the walk; the cursor stays put.
	assert.equal(history.back(), undefined);
	assert.deepEqual(history.current(), { uri: "file:///w/a.ts", offset: 1 });

	assert.deepEqual(history.forward(), { uri: "file:///w/b.ts", offset: 2 });
	assert.deepEqual(history.forward(), { uri: "file:///w/c.ts", offset: 3 });
	assert.equal(history.forward(), undefined);
	assert.deepEqual(history.current(), { uri: "file:///w/c.ts", offset: 3 });
});

test("any push truncates the forward stack, even for a duplicate of the current target", () => {
	const history = new JumpHistory();
	history.push({ uri: "file:///w/a.ts", offset: 1 });
	history.push({ uri: "file:///w/b.ts", offset: 2 });
	history.push({ uri: "file:///w/c.ts", offset: 3 });
	history.back(); // at B
	history.back(); // at A
	// A push that lands on the current target adds no entry but still
	// supersedes the forward tail (pushing after a back clears forward).
	history.push({ uri: "file:///w/a.ts", offset: 1 });
	assert.equal(history.size, 1);
	assert.equal(history.forward(), undefined);
	assert.deepEqual(history.current(), { uri: "file:///w/a.ts", offset: 1 });
	// A genuinely new target appends normally.
	history.push({ uri: "file:///w/d.ts", offset: 4 });
	assert.equal(history.size, 2);
	assert.deepEqual(history.current(), { uri: "file:///w/d.ts", offset: 4 });
	assert.equal(history.forward(), undefined);
	assert.deepEqual(history.back(), { uri: "file:///w/a.ts", offset: 1 });
	assert.equal(history.back(), undefined);
});

test("history is bounded with oldest-first eviction", () => {
	const history = new JumpHistory();
	for (let index = 0; index < JUMP_HISTORY_LIMIT + 5; index++) {
		history.push({ uri: `file:///w/f${index}.ts`, offset: index });
	}
	assert.equal(history.size, JUMP_HISTORY_LIMIT);
	assert.deepEqual(history.current(), {
		uri: `file:///w/f${JUMP_HISTORY_LIMIT + 4}.ts`,
		offset: JUMP_HISTORY_LIMIT + 4,
	});
	const visited: string[] = [];
	for (let entry = history.back(); entry !== undefined; entry = history.back()) {
		visited.push(entry.uri);
	}
	assert.equal(visited.length, JUMP_HISTORY_LIMIT - 1);
	assert.equal(visited[visited.length - 1], "file:///w/f5.ts"); // oldest retained
	assert.equal(history.forward()?.uri, "file:///w/f6.ts");
});

test("entries are plain data: pushed and returned values are detached copies", () => {
	const history = new JumpHistory();
	const pushed = { uri: "file:///w/a.ts", offset: 4, documentId: "doc-1", line: 0 };
	history.push(pushed);
	pushed.uri = "file:///w/mutated.ts";
	pushed.offset = 99;
	assert.deepEqual(history.current(), { uri: "file:///w/a.ts", offset: 4, documentId: "doc-1", line: 0 });

	const current = history.current();
	assert.ok(current);
	current.offset = -5;
	assert.deepEqual(history.current(), { uri: "file:///w/a.ts", offset: 4, documentId: "doc-1", line: 0 });

	history.push({ uri: "file:///w/b.ts", offset: 7 });
	const previous = history.back();
	assert.ok(previous);
	previous.uri = "file:///w/tampered.ts";
	assert.equal(history.back(), undefined); // at the oldest entry
	assert.deepEqual(history.current(), { uri: "file:///w/a.ts", offset: 4, documentId: "doc-1", line: 0 });
	assert.deepEqual(history.forward(), { uri: "file:///w/b.ts", offset: 7 });
});
