// ui_preferences persistence contract (closing pass): total parsing, safe
// defaults, exact round-trip and best-effort store failures.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
	UI_PREFERENCES_VERSION,
	defaultUiPreferences,
	parseUiPreferences,
	serializeUiPreferences,
	UiPreferencesPersistence,
} from "../src/workspace/preferences.ts";

test("defaults are syntax on, soft wrap off, tab width 4", () => {
	assert.deepEqual(defaultUiPreferences(), { syntax: true, softWrap: false, tabWidth: 4 });
	assert.deepEqual(parseUiPreferences(undefined).preferences, defaultUiPreferences());
	assert.deepEqual(parseUiPreferences("").preferences, defaultUiPreferences());
	assert.equal(parseUiPreferences(undefined).migrated, false);
});

test("valid documents round-trip through serialize/parse", () => {
	const preferences = { syntax: false, softWrap: true, tabWidth: 8 as const };
	const serialized = serializeUiPreferences(preferences);
	assert.deepEqual(JSON.parse(serialized), {
		version: UI_PREFERENCES_VERSION,
		syntax: false,
		softWrap: true,
		tabWidth: 8,
	});
	assert.deepEqual(parseUiPreferences(serialized), { preferences, migrated: false });
});

test("corrupt, wrong-shaped and unknown-version data degrade to defaults without throwing", () => {
	for (const raw of [
		"{",
		"null",
		"[]",
		'"nope"',
		"123",
		JSON.stringify({ version: 2, syntax: false }),
		JSON.stringify({ version: "1", syntax: true }),
		JSON.stringify({ version: 1, syntax: { nested: true } }),
	]) {
		const parsed = parseUiPreferences(raw);
		assert.deepEqual(parsed.preferences, defaultUiPreferences(), raw);
		assert.equal(parsed.migrated, true, raw);
	}
});

test("field-level junk falls back per field; unknown keys are dropped on save", () => {
	const parsed = parseUiPreferences(
		JSON.stringify({ version: 1, syntax: "yes", softWrap: true, tabWidth: 3, secret: "leak me" }),
	);
	assert.deepEqual(parsed.preferences, { syntax: true, softWrap: true, tabWidth: 4 });
	assert.equal(parsed.migrated, true);

	const stored = JSON.parse(
		serializeUiPreferences({ syntax: true, softWrap: true, tabWidth: 8, extra: "drop me" } as never),
	);
	assert.deepEqual(Object.keys(stored).sort(), ["softWrap", "syntax", "tabWidth", "version"]);
	assert.equal(stored.extra, undefined);
});

test("the persistence adapter loads defaults on a throwing read and writes the exact document", async () => {
	const writes: string[] = [];
	const failing = new UiPreferencesPersistence({
		async get() {
			throw new Error("down");
		},
		async set(value) {
			writes.push(value);
		},
	});
	assert.deepEqual(await failing.load(), defaultUiPreferences());

	const documents = new Map<string, string>();
	const store = new UiPreferencesPersistence({
		async get() {
			return documents.get("ui_preferences");
		},
		async set(value) {
			documents.set("ui_preferences", value);
		},
	});
	await store.save({ syntax: false, softWrap: true, tabWidth: 2 });
	assert.deepEqual(documents.get("ui_preferences"), serializeUiPreferences({ syntax: false, softWrap: true, tabWidth: 2 }));
	assert.deepEqual(await store.load(), { syntax: false, softWrap: true, tabWidth: 2 });
	documents.set("ui_preferences", "{corrupt");
	assert.deepEqual(await store.load(), defaultUiPreferences());
});
