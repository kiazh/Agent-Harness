// T6 persistence tests (plan §21): versioned round-trip, corrupt/unknown
// inputs never throw, whitelisted non-sensitive fields only, bounded roots
// with LRU-by-last-save eviction, and best-effort adapter behavior.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	MAX_PERSISTED_DOCUMENT_BYTES,
	MAX_PERSISTED_ROOTS,
	WorkspacePersistence,
	parseWorkspaceState,
	serializeWorkspaceState,
	type WorkspacePersistenceStore,
} from "../src/workspace/persistence.ts";
import {
	defaultWorkspacePersistedState,
	type WorkspacePersistedState,
} from "../src/workspace/state.ts";

/** Document-text-like fixture: none of this may ever reach persisted output. */
const DOC_TEXT = "PRIVATE_SOURCE_TEXT = 'if (secret) { exfiltrate(); }'";

function fullState(): WorkspacePersistedState {
	return {
		version: 1,
		requestedMode: "editor",
		splitRatio: 0.6,
		explorerRequested: true,
		explorerWidth: 32,
		openUris: ["file:///ws/a.ts", "file:///ws/b.ts"],
		activeUri: "file:///ws/b.ts",
		cursorByUri: {
			"file:///ws/a.ts": { offset: 10, anchorOffset: 4, scrollLine: 2 },
			"file:///ws/b.ts": { offset: 3 },
		},
	};
}

class MemoryStore implements WorkspacePersistenceStore {
	value: string | undefined;
	failGet = false;
	failSet = false;
	async get(): Promise<string | undefined> {
		if (this.failGet) throw new Error("store get failed");
		return this.value;
	}
	async set(value: string): Promise<void> {
		if (this.failSet) throw new Error("store set failed");
		this.value = value;
	}
}

function byRootOf(store: MemoryStore): Record<string, unknown> {
	const document = JSON.parse(store.value ?? "{}") as { byRoot?: Record<string, unknown> };
	return document.byRoot ?? {};
}

// ─── serialize / parse ──────────────────────────────────────────────────────

test("a full state round-trips through serialize and parse", () => {
	const state = fullState();
	const parsed = parseWorkspaceState(serializeWorkspaceState(state));
	assert.ok(parsed);
	assert.equal(parsed.migrated, false);
	assert.deepEqual(parsed.state, state);
});

test("serialize emits only whitelisted non-sensitive fields", () => {
	const hostile = {
		...fullState(),
		text: DOC_TEXT,
		documentText: DOC_TEXT,
		cursorByUri: {
			"file:///ws/a.ts": { offset: 10, text: DOC_TEXT },
		},
	} as unknown as WorkspacePersistedState;
	const serialized = serializeWorkspaceState(hostile);
	assert.doesNotMatch(serialized, /PRIVATE_SOURCE_TEXT/);
	assert.doesNotMatch(serialized, /exfiltrate/);
	const parsed = JSON.parse(serialized) as Record<string, unknown>;
	assert.deepEqual(Object.keys(parsed).sort(), [
		"activeUri",
		"cursorByUri",
		"explorerRequested",
		"explorerWidth",
		"openUris",
		"requestedMode",
		"splitRatio",
		"version",
	]);
	const cursor = (parsed.cursorByUri as Record<string, Record<string, unknown>>)["file:///ws/a.ts"];
	assert.deepEqual(cursor, { offset: 10 });
});

test("parse never throws and rejects corrupt or unknown-version input", () => {
	for (const raw of [
		undefined,
		"",
		"   ",
		"not json {",
		"[1, 2, 3]",
		"null",
		"5",
		JSON.stringify({ version: "1" }),
		JSON.stringify({ version: 2, byRoot: {} }),
		JSON.stringify({ requestedMode: "split" }),
	]) {
		assert.equal(parseWorkspaceState(raw), undefined, `raw=${String(raw)}`);
	}
	// A versionless object at the wrong shape is rejected without throwing.
	assert.equal(parseWorkspaceState(JSON.stringify({ version: 1, requestedMode: "nope" }))?.state.requestedMode, "split");
});

test("parse normalizes hostile field values instead of propagating them", () => {
	const raw = JSON.stringify({
		version: 1,
		requestedMode: "bogus",
		splitRatio: 99,
		explorerRequested: "yes",
		explorerWidth: -4,
		openUris: ["file:///ok.ts", 12, "x".repeat(5000), "file:///ok.ts"],
		activeUri: null,
		cursorByUri: { "file:///ok.ts": { offset: -1 }, "file:///x.ts": { offset: 4.9 } },
	});
	const parsed = parseWorkspaceState(raw);
	assert.ok(parsed);
	assert.equal(parsed.state.requestedMode, "split");
	assert.equal(parsed.state.splitRatio, 0.75, "the layout clamp holds at the persisted boundary");
	assert.equal(parsed.state.explorerRequested, false);
	assert.equal(parsed.state.explorerWidth, undefined);
	assert.deepEqual(parsed.state.openUris, ["file:///ok.ts"]);
	assert.equal(parsed.state.activeUri, undefined);
	assert.deepEqual(parsed.state.cursorByUri, { "file:///x.ts": { offset: 4 } });
});

test("serialize rejects an unsupported state version", () => {
	const state = { ...fullState(), version: 2 } as unknown as WorkspacePersistedState;
	assert.throws(() => serializeWorkspaceState(state), /unsupported workspace state version/);
});

// ─── adapter ────────────────────────────────────────────────────────────────

test("adapter round-trips per root and keeps roots separate", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	assert.equal(await persistence.load("/ws/a"), undefined, "nothing stored yet");
	await persistence.save("/ws/a", fullState());
	const loaded = await persistence.load("/ws/a");
	assert.ok(loaded);
	assert.equal(loaded.migrated, false);
	assert.deepEqual(loaded.state, fullState());
	assert.equal(await persistence.load("/ws/b"), undefined, "another root has no state");
	assert.match(store.value ?? "", /"version":1/);
});

test("adapter load is total: corrupt store, unknown versions and missing roots", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	store.value = "corrupt json {";
	assert.equal(await persistence.load("/ws/a"), undefined);

	store.value = JSON.stringify({ version: 2, byRoot: { "/ws/a": fullState() } });
	const migrated = await persistence.load("/ws/a");
	assert.ok(migrated);
	assert.equal(migrated.migrated, true);
	assert.deepEqual(migrated.state, defaultWorkspacePersistedState());

	store.value = JSON.stringify({ version: 1, byRoot: { "/ws/a": { version: 9 } } });
	const badEntry = await persistence.load("/ws/a");
	assert.ok(badEntry);
	assert.equal(badEntry.migrated, true);
	assert.deepEqual(badEntry.state, defaultWorkspacePersistedState());

	store.failGet = true;
	assert.equal(await persistence.load("/ws/a"), undefined, "a throwing store read is nothing stored");
});

test("invalid stored JSON is replaced harmlessly on the next save", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	store.value = "not json at all";
	await persistence.save("/ws/a", fullState());
	assert.deepEqual(await persistence.load("/ws/a"), { state: fullState(), migrated: false });
	assert.equal((await persistence.load("/ws/b")) ?? "absent", "absent");
});

test("unknown-version documents are rebuilt on save without throwing", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	store.value = JSON.stringify({ version: 7, byRoot: { "/old": { version: 1 } } });
	await persistence.save("/ws/a", fullState());
	const document = JSON.parse(store.value ?? "{}") as { version: number; byRoot: Record<string, unknown> };
	assert.equal(document.version, 1);
	assert.deepEqual(Object.keys(document.byRoot), ["/ws/a"]);
});

test("saved states are bounded to MAX_PERSISTED_ROOTS by last save (LRU)", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	for (let index = 0; index < MAX_PERSISTED_ROOTS + 1; index++) {
		await persistence.save(`/ws/root-${String(index).padStart(3, "0")}`, fullState());
	}
	assert.equal(Object.keys(byRootOf(store)).length, MAX_PERSISTED_ROOTS);
	assert.equal(await persistence.load("/ws/root-000"), undefined, "the least recently saved root is evicted");
	assert.ok(await persistence.load("/ws/root-100"));
	// Touching an old root moves it to the most-recent end of the bound.
	await persistence.save("/ws/root-050", fullState());
	await persistence.save("/ws/root-101", fullState());
	assert.equal(await persistence.load("/ws/root-001"), undefined, "the next-oldest root was evicted");
	assert.ok(await persistence.load("/ws/root-050"), "the touched root survived");
	assert.ok(await persistence.load("/ws/root-101"));
});

test("no document text ever reaches the stored document", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	const hostile = {
		...fullState(),
		text: DOC_TEXT,
		cursorByUri: { "file:///ws/a.ts": { offset: 1, text: DOC_TEXT } },
	} as unknown as WorkspacePersistedState;
	await persistence.save("/ws/a", hostile);
	assert.ok(store.value);
	assert.doesNotMatch(store.value, /PRIVATE_SOURCE_TEXT/);
	assert.doesNotMatch(store.value, /exfiltrate/);
	// A later save of another root never resurrects prior junk either.
	await persistence.save("/ws/b", fullState());
	assert.doesNotMatch(store.value, /PRIVATE_SOURCE_TEXT/);
});

test("a rejecting write propagates; a throwing read never blocks a save", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	store.failSet = true;
	await assert.rejects(persistence.save("/ws/a", fullState()), /store set failed/);
	store.failSet = false;
	store.failGet = true;
	await persistence.save("/ws/a", fullState());
	store.failGet = false;
	assert.deepEqual(await persistence.load("/ws/a"), { state: fullState(), migrated: false });
});

test("byte-bound eviction keeps the stored document under the backend 64 KiB cap", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	// Two 4 KiB URIs per root (~8 KiB serialized each): nine roots exceed the
	// backend bound, so the oldest roots must be evicted before writing.
	const bigState = (): WorkspacePersistedState => ({
		version: 1,
		requestedMode: "editor",
		splitRatio: 0.6,
		explorerRequested: true,
		explorerWidth: 32,
		openUris: [`file:///ws/${"a".repeat(4000)}`, `file:///ws/${"b".repeat(4000)}`],
		activeUri: "file:///ws/b.ts",
	});
	for (let index = 0; index < 9; index++) {
		await persistence.save(`/ws/big-root-${String(index).padStart(2, "0")}`, bigState());
	}
	const bytes = new TextEncoder().encode(store.value ?? "").length;
	assert.ok(bytes < 64 * 1024, `stored document must respect WORKSPACE_UI_STATE_MAX_BYTES: ${bytes}`);
	assert.ok(bytes < MAX_PERSISTED_DOCUMENT_BYTES, "the safety margin keeps the document under the bound");
	const byRoot = byRootOf(store);
	assert.ok(Object.keys(byRoot).length < 9, "byte-bound eviction actually ran");
	assert.equal(byRoot["/ws/big-root-00"], undefined, "the least recently saved root is evicted first");
	assert.ok(byRoot["/ws/big-root-08"], "the just-saved root always survives its own save");
	assert.deepEqual(await persistence.load("/ws/big-root-08"), { state: bigState(), migrated: false });
});

test("small documents under the byte bound keep every root", async () => {
	const store = new MemoryStore();
	const persistence = new WorkspacePersistence(store);
	assert.equal(
		MAX_PERSISTED_DOCUMENT_BYTES,
		64 * 1024,
		"bound matches WORKSPACE_UI_STATE_MAX_BYTES in core/config.py",
	);
	for (let index = 0; index < 5; index++) await persistence.save(`/ws/small-${String(index)}`, fullState());
	assert.equal(Object.keys(byRootOf(store)).length, 5, "no spurious eviction for a small document");
});
