// Chained slash-command completion must not eat the previous word:
// `/keys set␣` + accept key name must yield `/keys set KEY`, not `/keys KEY`.
import assert from "node:assert/strict";
import { test } from "node:test";
import { ArgumentAwareAutocompleteProvider } from "../src/autocomplete.ts";

// Mirrors the two-stage shape of the real /keys completions.
const keysLike = {
	name: "keys",
	description: "keys",
	getArgumentCompletions: (prefix: string) => {
		const typed = prefix.trimStart();
		if (!/\s/.test(typed)) {
			return ["list", "set", "clear"]
				.filter((w) => w.startsWith(typed))
				.map((value) => ({ value, label: value }));
		}
		const space = typed.indexOf(" ");
		const sub = typed.slice(0, space);
		const rest = typed.slice(space + 1);
		if (sub !== "set") return null;
		const items = ["OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"]
			.filter((k) => k.startsWith(rest.trimStart()))
			.map((value) => ({ value, label: value }));
		return items.length ? items : null;
	},
};

const themeLike = {
	name: "theme",
	description: "theme",
	getArgumentCompletions: (prefix: string) => {
		const typed = prefix.trimStart().toLowerCase();
		if (/\s/.test(typed)) return null;
		const items = ["dracula", "nord"]
			.filter((v) => v.startsWith(typed))
			.map((value) => ({ value, label: value }));
		return items.length ? items : null;
	},
};

function provider() {
	return new ArgumentAwareAutocompleteProvider([keysLike, themeLike] as never, process.cwd());
}

async function accept(line: string, value: string): Promise<string> {
	const p = provider();
	const { signal } = new AbortController();
	const suggestions = await p.getSuggestions([line], 0, line.length, { signal });
	assert.ok(suggestions, `expected suggestions for ${JSON.stringify(line)}`);
	const item = suggestions.items.find((i) => i.value === value);
	assert.ok(item, `expected ${value} among ${suggestions.items.map((i) => i.value)}`);
	const applied = p.applyCompletion([line], 0, line.length, item, suggestions.prefix);
	return applied.lines[0]!;
}

test("second-stage argument completion keeps the previous word", async () => {
	assert.equal(await accept("/keys set ", "OPENROUTER_API_KEY"), "/keys set OPENROUTER_API_KEY");
});

test("partial second-stage token completes in place", async () => {
	assert.equal(await accept("/keys set D", "DEEPSEEK_API_KEY"), "/keys set DEEPSEEK_API_KEY");
});

test("first-stage argument completion still works", async () => {
	assert.equal(await accept("/keys s", "set"), "/keys set");
});

test("single-word arguments are unaffected", async () => {
	assert.equal(await accept("/theme dra", "dracula"), "/theme dracula");
});

test("bare command-name completion is unaffected", async () => {
	assert.equal(await accept("/key", "keys"), "/keys ");
});
