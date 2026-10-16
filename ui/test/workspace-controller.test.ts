// T3 controller tests (plan §7/§8/§19/§21): the orchestration hub against a
// fake gateway (with a mirror that enforces the backend's documented version
// rules), fake persistence and the real EditorStore/TabModel.
//
// The fake gateway mirrors DocumentMirrorStore semantics: opens accept only
// strictly increasing versions, changes require base version equality and a
// strictly increasing version, and anything else answers `resync: true`. That
// makes version-monotonicity and snapshot-resend assertions meaningful instead
// of self-referential.

import assert from "node:assert/strict";
import { test } from "node:test";
import { EditorStore } from "../src/editor/model.ts";
import { TabModel } from "../src/editor/tabs.ts";
import type {
	LspPosition,
	WorkspaceDiagnostic,
	WorkspaceDocumentChange,
	WorkspaceDocumentChangeResult,
	WorkspaceDocumentOpenResult,
	WorkspaceEvent,
	WorkspaceIntelligenceResult,
	WorkspaceListResult,
	WorkspaceLspStatusResult,
	WorkspaceOpenResult,
	WorkspaceReadRefusal,
	WorkspaceReadResult,
	WorkspaceSaveResult,
} from "../src/workspace/protocol.ts";
import {
	WorkspaceGatewayAdapter,
	isWorkspaceRefusal,
	type WorkspaceGateway,
	type WorkspaceIntelligenceOperation,
	type WorkspaceLspAction,
	type WorkspaceLspControlResult,
	type WorkspaceListOptions,
	type WorkspaceRequestFn,
} from "../src/workspace/gateway.ts";
import {
	WORKSPACE_DIAGNOSTICS_CAP,
	WorkspaceController,
	type NoticeKind,
	type WorkspaceLspStatusSnapshot,
	type WorkspacePersistenceLike,
} from "../src/workspace/controller.ts";
import type { FocusRegion, WorkspacePersistedState } from "../src/workspace/state.ts";
import { WORKSPACE_STATE_VERSION } from "../src/workspace/state.ts";

// ─── fixtures ───────────────────────────────────────────────────────────────

function fileUri(path: string): string {
	return `file:///ws/${path}`;
}

function delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
	let resolveFn: ((value: T) => void) | undefined;
	const promise = new Promise<T>((resolve) => {
		resolveFn = resolve;
	});
	return {
		promise,
		resolve: (value: T) => {
			resolveFn?.(value);
		},
	};
}

function readResult(uri: string, text = "hello\n", overrides: Partial<WorkspaceReadResult> = {}): WorkspaceReadResult {
	return {
		uri,
		text,
		revision: "rev-1",
		languageId: "plaintext",
		lineEnding: "LF",
		encoding: "utf-8",
		readOnly: false,
		sizeBytes: text.length,
		...overrides,
	};
}

function intelligenceResult(
	operation: WorkspaceIntelligenceOperation,
	uri: string | null,
	version: number | null,
): WorkspaceIntelligenceResult {
	return {
		workspaceId: "ws_1",
		source: "editor-buffer",
		uri: uri ?? "",
		documentVersion: version,
		serverGeneration: 1,
		workspaceEpoch: 1,
		operation,
		state: "ready",
		freshness: "versioned",
		truncated: false,
		result: null,
	};
}

function diagnostic(message: string, severity: 1 | 2 | 3 | 4 = 1): WorkspaceDiagnostic {
	return {
		range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
		severity,
		message,
	};
}

/** LSP (line, character) to offset for LF-only fixture text. */
function positionOffset(text: string, position: LspPosition): number {
	const lines = text.split("\n");
	let offset = 0;
	for (let index = 0; index < position.line && index < lines.length; index += 1) {
		offset += (lines[index] ?? "").length + 1;
	}
	const line = lines[position.line] ?? "";
	return offset + Math.min(position.character, line.length);
}

/** The backend mirror rules the fake enforces (shared with overrides). */
function applyChangeToMirror(
	mirrors: Map<string, { version: number; text: string }>,
	uri: string,
	change: WorkspaceDocumentChange,
): WorkspaceDocumentChangeResult {
	const mirror = mirrors.get(uri);
	if (!mirror) return { acceptedVersion: 0, resync: true };
	if (change.baseVersion !== mirror.version || change.version <= change.baseVersion) {
		return { acceptedVersion: mirror.version, resync: true };
	}
	let text = mirror.text;
	for (const entry of change.changes) {
		const start = positionOffset(text, entry.range.start);
		const end = positionOffset(text, entry.range.end);
		if (end < start) return { acceptedVersion: mirror.version, resync: true };
		text = text.slice(0, start) + entry.text + text.slice(end);
	}
	mirrors.set(uri, { version: change.version, text });
	return { acceptedVersion: change.version };
}

// ─── fakes ──────────────────────────────────────────────────────────────────

class FakeGateway implements WorkspaceGateway {
	readonly calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	readonly mirrors = new Map<string, { version: number; text: string }>();
	identityValue: WorkspaceOpenResult | undefined;
	closeCalls = 0;
	private saveSeq = 0;

	openHandler: (root: string, trust: boolean) => Promise<WorkspaceOpenResult> = async (root, trust) => ({
		workspaceId: "ws_1",
		clientId: "cl_1",
		epoch: 1,
		rootLabel: "ws",
		resolvedRoot: root,
		trusted: trust,
		capabilities: ["list", "read", "save", "documentOpen", "intelligence"],
	});
	readHandler: (uri: string) => Promise<WorkspaceReadResult | WorkspaceReadRefusal> = async (uri) =>
		readResult(uri);
	saveHandler: (
		uri: string,
		expectedRevision: string | null,
		text: string,
		uiVersion: number,
	) => Promise<WorkspaceSaveResult> = async (_uri, _expected, _text, uiVersion) => ({
		ok: true,
		revision: `save-${++this.saveSeq}`,
		savedUiVersion: uiVersion,
	});
	intelligenceHandler: (
		operation: WorkspaceIntelligenceOperation,
		uri: string | null,
		version: number | null,
		position: LspPosition | null,
	) => Promise<WorkspaceIntelligenceResult | WorkspaceReadRefusal> = async (operation, uri, version) =>
		intelligenceResult(operation, uri, version);
	lspStatusHandler: () => Promise<WorkspaceLspStatusResult | WorkspaceReadRefusal> = async () => ({
		servers: [],
	});
	lspControlHandler: (
		profile: string,
		action: WorkspaceLspAction,
	) => Promise<WorkspaceLspControlResult | WorkspaceReadRefusal> = async (profile, action) => ({
		action,
		profile,
		state: "starting",
	});
	/** Optional override for documentChange (defaults to the mirror rules). */
	documentChangeHandler:
		| ((
				uri: string,
				change: WorkspaceDocumentChange,
			) => Promise<WorkspaceDocumentChangeResult | WorkspaceReadRefusal>)
		| undefined;

	identity(): WorkspaceOpenResult | undefined {
		return this.identityValue;
	}

	async open(root: string, trust: boolean): Promise<WorkspaceOpenResult> {
		this.record("workspace.open", { root, trust });
		this.identityValue = await this.openHandler(root, trust);
		return this.identityValue;
	}

	async list(path: string, cursor?: string, options: WorkspaceListOptions = {}): Promise<WorkspaceListResult> {
		this.record("workspace.list", { path, cursor: cursor ?? null, ...options });
		return { entries: [], nextCursor: null, exclusions: [], truncated: false };
	}

	async read(uri: string): Promise<WorkspaceReadResult | WorkspaceReadRefusal> {
		this.record("workspace.read", { uri });
		return this.readHandler(uri);
	}

	async save(
		uri: string,
		expectedRevision: string | null,
		text: string,
		uiVersion: number,
	): Promise<WorkspaceSaveResult> {
		this.record("workspace.save", { uri, expectedRevision, text, uiVersion });
		return this.saveHandler(uri, expectedRevision, text, uiVersion);
	}

	async documentOpen(
		uri: string,
		languageId: string,
		version: number,
		text: string,
	): Promise<WorkspaceDocumentOpenResult | WorkspaceReadRefusal> {
		this.record("workspace.documentOpen", { uri, languageId, version, text });
		const existing = this.mirrors.get(uri);
		if (existing && version <= existing.version) {
			return { acceptedVersion: existing.version, serverGeneration: 1, state: "ready" };
		}
		this.mirrors.set(uri, { version, text });
		return { acceptedVersion: version, serverGeneration: 1, state: "ready" };
	}

	async documentChange(
		uri: string,
		change: WorkspaceDocumentChange,
	): Promise<WorkspaceDocumentChangeResult | WorkspaceReadRefusal> {
		this.record("workspace.documentChange", {
			uri,
			baseVersion: change.baseVersion,
			version: change.version,
			changes: change.changes,
		});
		if (this.documentChangeHandler) return this.documentChangeHandler(uri, change);
		return applyChangeToMirror(this.mirrors, uri, change);
	}

	async documentClose(uri: string): Promise<{ closed: boolean; uri: string } | WorkspaceReadRefusal> {
		this.record("workspace.documentClose", { uri });
		this.mirrors.delete(uri);
		return { closed: true, uri };
	}

	async intelligence(
		operation: WorkspaceIntelligenceOperation,
		uri: string | null,
		version: number | null,
		position?: LspPosition | null,
	): Promise<WorkspaceIntelligenceResult | WorkspaceReadRefusal> {
		this.record("workspace.intelligence", { operation, uri, version, position: position ?? null });
		return this.intelligenceHandler(operation, uri, version, position ?? null);
	}

	async lspStatus(): Promise<WorkspaceLspStatusResult | WorkspaceReadRefusal> {
		this.record("workspace.lspStatus", {});
		return this.lspStatusHandler();
	}

	async lspControl(
		profile: string,
		action: WorkspaceLspAction,
	): Promise<WorkspaceLspControlResult | WorkspaceReadRefusal> {
		this.record("workspace.lspControl", { profile, action });
		return this.lspControlHandler(profile, action);
	}

	async close(): Promise<{ closed: boolean } | WorkspaceReadRefusal> {
		this.closeCalls += 1;
		this.mirrors.clear();
		this.identityValue = undefined;
		return { closed: true };
	}

	callsOf(method: string): Array<{ method: string; params: Record<string, unknown> }> {
		return this.calls.filter((call) => call.method === method);
	}

	private record(method: string, params: Record<string, unknown>): void {
		this.calls.push({ method, params });
	}
}

class FakePersistence implements WorkspacePersistenceLike {
	readonly saves: Array<{ rootKey: string; state: WorkspacePersistedState }> = [];
	private readonly states = new Map<string, WorkspacePersistedState>();

	async load(rootKey: string): Promise<WorkspacePersistedState | undefined> {
		return this.states.get(rootKey);
	}

	async save(rootKey: string, state: WorkspacePersistedState): Promise<void> {
		this.saves.push({ rootKey, state });
		this.states.set(rootKey, state);
	}
}

interface Harness {
	gateway: FakeGateway;
	persistence: FakePersistence;
	store: EditorStore;
	tabs: TabModel;
	notices: Array<{ kind: NoticeKind; text: string }>;
	focusRegions: FocusRegion[];
	controller: WorkspaceController;
}

function build(options: { editDebounceMs?: number; persistDebounceMs?: number } = {}): Harness {
	const gateway = new FakeGateway();
	const persistence = new FakePersistence();
	const store = new EditorStore();
	const tabs = new TabModel({ isDirty: (documentId) => store.getDocument(documentId)?.dirty ?? false });
	const notices: Array<{ kind: NoticeKind; text: string }> = [];
	const focusRegions: FocusRegion[] = [];
	const controller = new WorkspaceController({
		gateway,
		persistence,
		store,
		tabs,
		requestRender: () => {},
		notify: (kind, text) => {
			notices.push({ kind, text });
		},
		focus: (region) => {
			focusRegions.push(region);
		},
		editDebounceMs: options.editDebounceMs ?? 10,
		persistDebounceMs: options.persistDebounceMs ?? 10,
	});
	return { gateway, persistence, store, tabs, notices, focusRegions, controller };
}

/** Open a document whose read returns `contents`; restores the read handler. */
async function mustOpen(
	h: Harness,
	path = "a.ts",
	contents = "hello\n",
): Promise<{ documentId: string; uri: string }> {
	const uri = fileUri(path);
	const previous = h.gateway.readHandler;
	h.gateway.readHandler = async (requested) => readResult(requested, contents);
	const outcome = await h.controller.openUri(uri);
	h.gateway.readHandler = previous;
	if (outcome.status !== "opened") assert.fail(`open failed: ${outcome.status}`);
	return { documentId: outcome.documentId, uri: outcome.uri };
}

async function openWorkspace(h: Harness, root = "/ws"): Promise<WorkspaceOpenResult> {
	const identity = await h.controller.openWorkspace(root);
	if (!identity) assert.fail("workspace open failed");
	return identity;
}

// ─── open / restore ─────────────────────────────────────────────────────────

test("openWorkspace mints the identity, persists intent and restore round-trips", async () => {
	const h = build();
	const identity = await openWorkspace(h);
	assert.equal(identity.workspaceId, "ws_1");
	assert.equal(h.controller.phase, "ready");
	assert.equal(h.controller.rootKey, "/ws");
	assert.equal(h.persistence.saves.length, 1, "requested mode/ratio persist at open");
	assert.ok(h.notices.some((notice) => notice.kind === "info" && notice.text.includes("workspace open")));

	h.controller.setMode("editor");
	h.controller.setRatio(0.62);
	h.controller.toggleExplorer();
	await delay(60);
	assert.equal(h.persistence.saves.length, 2, "meaningful changes coalesce into one debounced save");
	const persisted = h.persistence.saves.at(-1)?.state;
	assert.ok(persisted);
	assert.equal(persisted.requestedMode, "editor");
	assert.equal(persisted.splitRatio, 0.62);
	assert.equal(persisted.explorerRequested, true);
	assert.deepEqual(await h.controller.restore("/ws"), persisted);
	assert.equal(await h.controller.restore("/other"), undefined);
	await h.controller.dispose();
});

test("openWorkspace merges layout intent without clobbering stored open files", async () => {
	const h = build();
	const stored: WorkspacePersistedState = {
		version: WORKSPACE_STATE_VERSION,
		requestedMode: "editor",
		splitRatio: 0.4,
		explorerRequested: true,
		openUris: ["file:///ws/main.ts", "file:///ws/util.ts"],
		activeUri: "file:///ws/main.ts",
		cursorByUri: { "file:///ws/main.ts": { offset: 33 } },
	};
	await h.persistence.save("/ws", stored);
	await openWorkspace(h);
	const written = h.persistence.saves.at(-1)?.state;
	assert.ok(written);
	assert.deepEqual(written.openUris, stored.openUris, "stored open files survive the open-time persist");
	assert.equal(written.activeUri, stored.activeUri);
	assert.deepEqual(written.cursorByUri, stored.cursorByUri);
	const restored = await h.controller.restore("/ws");
	assert.ok(restored);
	assert.deepEqual(restored.openUris, stored.openUris, "restore still reads the previous session");
	await h.controller.dispose();
});

test("reopenRecent reopens up to eight remembered files with one summary notice", async () => {
	const h = build();
	await openWorkspace(h);
	const openUris = Array.from({ length: 10 }, (_, index) => fileUri(`f${index}.ts`));
	h.gateway.readHandler = async (uri) =>
		uri === fileUri("f1.ts")
			? { refused: { reason: "missing", message: "file not found: f1.ts" } }
			: readResult(uri);
	const noticesBefore = h.notices.length;
	const state: WorkspacePersistedState = {
		version: 1,
		requestedMode: "split",
		splitRatio: 0.5,
		explorerRequested: false,
		openUris,
		activeUri: openUris[2],
	};
	const outcome = await h.controller.reopenRecent(state);
	assert.deepEqual(outcome, { opened: 7, skipped: 1 });
	assert.equal(h.gateway.callsOf("workspace.read").length, 8, "bounded to eight attempts");
	assert.equal(h.tabs.count(), 7);
	const summaries = h.notices.slice(noticesBefore).filter((notice) => notice.text.includes("remembered"));
	assert.equal(summaries.length, 1, "one summary notice, not one per file");
	const active = h.store.documents().find((document) => document.uri === openUris[2]);
	assert.ok(active);
	assert.equal(h.tabs.activeDocumentId(), active.id, "the remembered active file is activated");
	await h.controller.dispose();
});

test("a failed workspace open notifies and leaves the controller usable", async () => {
	const h = build();
	h.gateway.openHandler = async () => {
		throw new Error("backend offline");
	};
	const identity = await h.controller.openWorkspace("/ws");
	assert.equal(identity, undefined);
	assert.equal(h.controller.phase, "closed");
	assert.ok(h.notices.some((notice) => notice.kind === "error" && notice.text.includes("backend offline")));
	assert.equal(h.controller.getLayoutInput().requestedMode, "split");
	assert.equal((await h.controller.openUri(fileUri("a.ts"))).status, "no-workspace");
	await h.controller.dispose();
});

// ─── open refusals and preview rules ────────────────────────────────────────

test("structured read refusals notify and create no tab", async () => {
	const h = build();
	await openWorkspace(h);
	h.gateway.readHandler = async () => ({
		refused: { reason: "binary", message: "file contains NUL bytes; refusing to guess its encoding" },
	});
	const outcome = await h.controller.openUri(fileUri("bin.dat"));
	assert.equal(outcome.status, "refused");
	assert.equal(h.tabs.count(), 0);
	assert.equal(h.store.documents().length, 0);
	assert.ok(h.notices.some((notice) => notice.text.includes("NUL bytes")));

	h.gateway.readHandler = async (uri) => readResult(uri, "x", { readOnly: true });
	const readOnly = await h.controller.openUri(fileUri("ro.ts"));
	assert.equal(readOnly.status, "opened");
	if (readOnly.status !== "opened") assert.fail("expected opened");
	assert.equal(h.store.getDocument(readOnly.documentId)?.readOnly, true);
	assert.ok(h.notices.some((notice) => notice.text.includes("read-only")));
	await h.controller.dispose();
});

test("preview tabs replace each other; editing or reopening pins them", async () => {
	const h = build();
	await openWorkspace(h);
	h.gateway.readHandler = async (uri) => readResult(uri);
	const a = await h.controller.openUri(fileUri("a.ts"));
	const b = await h.controller.openUri(fileUri("b.ts"), { preview: true });
	const c = await h.controller.openUri(fileUri("c.ts"), { preview: true });
	if (a.status !== "opened" || b.status !== "opened" || c.status !== "opened") assert.fail("opens failed");
	assert.equal(h.tabs.count(), 2, "one preview slot");
	assert.equal(h.store.getDocument(b.documentId), undefined, "the replaced preview buffer is closed");
	assert.ok(
		h.gateway.callsOf("workspace.documentClose").some((call) => call.params.uri === fileUri("b.ts")),
		"the replaced preview mirror is closed",
	);

	// Editing converts the preview to persistent.
	h.store.insertText(c.documentId, "x");
	h.controller.noteEdit(c.documentId);
	assert.equal(h.tabs.list().find((tab) => tab.documentId === c.documentId)?.preview, false);

	// An explicit reopen pins an existing preview.
	const d = await h.controller.openUri(fileUri("d.ts"), { preview: true });
	if (d.status !== "opened") assert.fail("open failed");
	const again = await h.controller.openUri(fileUri("d.ts"));
	assert.equal(again.status, "focused");
	assert.equal(h.tabs.list().find((tab) => tab.documentId === d.documentId)?.preview, false);

	// An existing document is never re-read.
	const reads = h.gateway.callsOf("workspace.read").length;
	await h.controller.openUri(fileUri("a.ts"));
	assert.equal(h.gateway.callsOf("workspace.read").length, reads);
	await h.controller.dispose();
});

// ─── edit sync ──────────────────────────────────────────────────────────────

test("edit debounce coalesces one documentChange per window; versions strictly increase", async () => {
	const h = build({ editDebounceMs: 40 });
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "abc\n");
	h.store.insertText(documentId, "x");
	h.controller.noteEdit(documentId);
	h.store.insertText(documentId, "y");
	h.controller.noteEdit(documentId);
	h.store.insertText(documentId, "z");
	h.controller.noteEdit(documentId);
	await delay(200);
	const first = h.gateway.callsOf("workspace.documentChange");
	assert.equal(first.length, 1, "one send per debounce window");
	assert.equal(first[0]?.params.baseVersion, 0);
	assert.equal(first[0]?.params.version, 3);
	assert.equal((first[0]?.params.changes as unknown[]).length, 3, "an incremental chain");
	assert.equal(h.gateway.mirrors.get(uri)?.text, h.store.getDocument(documentId)?.text);

	h.store.insertText(documentId, "!");
	h.controller.noteEdit(documentId);
	await delay(200);
	const all = h.gateway.callsOf("workspace.documentChange");
	assert.equal(all.length, 2);
	assert.equal(all[1]?.params.baseVersion, 3, "the next base is the last accepted version");
	assert.equal(all[1]?.params.version, 4);
	await h.controller.dispose();
});

test("long edit runs flush as one full replacement change", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "hello\n");
	for (let index = 0; index < 6; index += 1) {
		h.store.insertText(documentId, String(index));
		h.controller.noteEdit(documentId);
	}
	await h.controller.flushPending();
	const calls = h.gateway.callsOf("workspace.documentChange");
	assert.equal(calls.length, 1);
	assert.equal((calls[0]?.params.changes as unknown[]).length, 1, "kind-agnostic single replacement");
	assert.equal(calls[0]?.params.baseVersion, 0);
	assert.equal(calls[0]?.params.version, 6);
	assert.equal(h.gateway.mirrors.get(uri)?.text, h.store.getDocument(documentId)?.text);
	await h.controller.dispose();
});

test("a refused documentChange notifies once per reason and retries later", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "one\n");
	let refuse = true;
	h.gateway.documentChangeHandler = async (requestedUri, change) => {
		if (refuse) return { refused: { reason: "too-large", message: "mirror budget exceeded" } };
		return applyChangeToMirror(h.gateway.mirrors, requestedUri, change);
	};
	h.store.insertText(documentId, "x");
	h.controller.noteEdit(documentId);
	await h.controller.flushPending();
	const refusals = h.notices.filter((notice) => notice.text.includes("mirror budget exceeded"));
	assert.equal(refusals.length, 1, "the first refusal is announced");
	h.store.insertText(documentId, "y");
	h.controller.noteEdit(documentId);
	await h.controller.flushPending();
	assert.equal(
		h.notices.filter((notice) => notice.text.includes("mirror budget exceeded")).length,
		1,
		"the same reason is not re-announced while typing",
	);

	refuse = false;
	h.store.insertText(documentId, "z");
	h.controller.noteEdit(documentId);
	await h.controller.flushPending();
	assert.equal(h.gateway.mirrors.get(uri)?.text, h.store.getDocument(documentId)?.text);
	await h.controller.dispose();
});

test("resync response resends a full snapshot at the current version", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "one\n");
	h.store.insertText(documentId, "A");
	h.controller.noteEdit(documentId);
	await h.controller.flushPending();
	assert.equal(h.gateway.mirrors.get(uri)?.version, 1);

	// Simulate the backend dropping the mirror behind our back.
	h.gateway.mirrors.delete(uri);
	h.store.insertText(documentId, "B");
	h.controller.noteEdit(documentId);
	await h.controller.flushPending();
	const opens = h.gateway.callsOf("workspace.documentOpen");
	assert.equal(opens.length, 2, "the resync sends a full documentOpen snapshot");
	assert.equal(opens[1]?.params.version, 2);
	assert.equal(opens[1]?.params.text, h.store.getDocument(documentId)?.text);
	assert.equal(h.gateway.mirrors.get(uri)?.version, 2);
	assert.equal(h.gateway.mirrors.get(uri)?.text, h.store.getDocument(documentId)?.text);

	// Later edits stream incrementally from the snapshot version.
	h.store.insertText(documentId, "C");
	h.controller.noteEdit(documentId);
	await h.controller.flushPending();
	assert.equal(h.gateway.callsOf("workspace.documentChange").at(-1)?.params.baseVersion, 2);
	await h.controller.dispose();
});

// ─── save handshake ─────────────────────────────────────────────────────────

test("save clears dirty only when the buffer is unchanged; edits during save stay dirty", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId } = await mustOpen(h, "a.ts", "hello\n");
	h.store.insertText(documentId, "x");
	h.controller.noteEdit(documentId);
	const saved = await h.controller.save(documentId);
	assert.equal(saved.status, "saved");
	const document = h.store.getDocument(documentId);
	assert.equal(document?.dirty, false);
	assert.equal(document?.savedRevision, "save-1");
	const saveCall = h.gateway.callsOf("workspace.save").at(-1);
	assert.equal(saveCall?.params.expectedRevision, "rev-1");
	assert.equal(saveCall?.params.text, "xhello\n");
	assert.equal(saveCall?.params.uiVersion, 1);

	// Edits typed while the save is in flight keep the buffer dirty.
	const gate = deferred<WorkspaceSaveResult>();
	h.gateway.saveHandler = () => gate.promise;
	h.store.insertText(documentId, "y");
	h.controller.noteEdit(documentId);
	const pending = h.controller.save(documentId);
	h.store.insertText(documentId, "z");
	h.controller.noteEdit(documentId);
	h.store.insertText(documentId, "!");
	h.controller.noteEdit(documentId);
	gate.resolve({ ok: true, revision: "save-2", savedUiVersion: 2 });
	assert.equal((await pending).status, "saved");
	const after = h.store.getDocument(documentId);
	assert.equal(after?.dirty, true, "later edits stay dirty");
	assert.equal(after?.savedRevision, "save-2", "the saved identity still advances");
	await h.controller.dispose();
});

test("a save-origin fileChanged echo during an in-flight save never marks a conflict", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "one\n");
	h.store.insertText(documentId, "x");
	h.controller.noteEdit(documentId);
	const gate = deferred<WorkspaceSaveResult>();
	h.gateway.saveHandler = () => gate.promise;
	const pending = h.controller.save(documentId);
	// The backend emits fileChanged before the save response reaches us.
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.fileChanged",
		uri,
		revision: "save-new",
		origin: "save",
	}));
	assert.equal(h.store.getDocument(documentId)?.conflict, undefined, "no spurious conflict");
	assert.equal(h.controller.externalChangeFor(documentId), undefined);
	gate.resolve({ ok: true, revision: "save-new", savedUiVersion: 1 });
	assert.equal((await pending).status, "saved");
	assert.equal(h.store.getDocument(documentId)?.savedRevision, "save-new");
	await h.controller.dispose();
});

// ─── conflicts ──────────────────────────────────────────────────────────────

test("save conflicts resolve via keep, reload and overwrite-explicit", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId } = await mustOpen(h, "a.ts", "one\n");
	h.gateway.saveHandler = async (_uri, expectedRevision) => ({
		ok: false,
		conflict: { diskRevision: "disk-9", baseRevision: expectedRevision ?? "", reason: "revision-mismatch" },
	});
	h.store.insertText(documentId, "edit");
	h.controller.noteEdit(documentId);

	const conflicted = await h.controller.save(documentId);
	assert.equal(conflicted.status, "conflict");
	assert.equal(h.store.getDocument(documentId)?.conflict?.diskRevision, "disk-9");
	assert.equal(h.controller.conflictFor(documentId)?.diskRevision, "disk-9");

	const kept = await h.controller.resolveConflict(documentId, "keep");
	assert.equal(kept.status, "kept");
	assert.equal(h.store.getDocument(documentId)?.conflict, undefined);
	assert.equal(h.store.getDocument(documentId)?.dirty, true, "keeping leaves the buffer dirty");
	assert.equal(h.controller.conflictFor(documentId), undefined);

	assert.equal((await h.controller.save(documentId)).status, "conflict");
	h.gateway.readHandler = async (uri) => readResult(uri, "disk text\n", { revision: "disk-9" });
	const reloaded = await h.controller.resolveConflict(documentId, "reload");
	assert.equal(reloaded.status, "reloaded");
	const afterReload = h.store.getDocument(documentId);
	assert.equal(afterReload?.text, "disk text\n");
	assert.equal(afterReload?.dirty, false);
	assert.equal(afterReload?.savedRevision, "disk-9");

	h.store.insertText(documentId, "local");
	h.controller.noteEdit(documentId);
	assert.equal((await h.controller.save(documentId)).status, "conflict");
	h.gateway.saveHandler = async (_uri, _expected, _text, uiVersion) => ({
		ok: true,
		revision: "disk-10",
		savedUiVersion: uiVersion,
	});
	const overwritten = await h.controller.resolveConflict(documentId, "overwrite-explicit");
	assert.equal(overwritten.status, "saved");
	assert.equal(
		h.gateway.callsOf("workspace.save").at(-1)?.params.expectedRevision,
		"disk-9",
		"overwrite uses the disk revision as the expectation",
	);
	const afterOverwrite = h.store.getDocument(documentId);
	assert.equal(afterOverwrite?.dirty, false);
	assert.equal(afterOverwrite?.conflict, undefined);
	assert.equal(h.controller.conflictFor(documentId), undefined);
	await h.controller.dispose();
});

test("resolveClose save that conflicts leaves the tab open with the conflict", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId } = await mustOpen(h, "a.ts", "one\n");
	h.store.insertText(documentId, "x");
	h.controller.noteEdit(documentId);
	h.gateway.saveHandler = async (_uri, expectedRevision) => ({
		ok: false,
		conflict: { diskRevision: "disk-9", baseRevision: expectedRevision ?? "", reason: "revision-mismatch" },
	});
	const decision = await h.controller.closeTab(documentId);
	assert.equal(decision.status, "needs-decision");
	const outcome = await h.controller.resolveClose(documentId, "save");
	assert.equal(outcome.status, "save-conflict");
	assert.equal(h.tabs.has(documentId), true, "the tab stays open pending resolution");
	await h.controller.dispose();
});

// ─── close decisions ────────────────────────────────────────────────────────

test("dirty close decisions: cancel is a no-op, discard drops edits, save closes", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "one\n");
	h.store.insertText(documentId, "x");
	h.controller.noteEdit(documentId);

	const first = await h.controller.closeTab(documentId);
	if (first.status !== "needs-decision") assert.fail("expected a dirty-close decision");
	assert.equal(first.decision.kind, "dirty-close");
	assert.deepEqual(first.decision.choices, ["save", "discard", "cancel"]);

	const cancelled = await h.controller.resolveClose(documentId, "cancel");
	assert.equal(cancelled.status, "cancelled");
	assert.equal(h.tabs.has(documentId), true);
	assert.equal(h.store.getDocument(documentId)?.text, "xone\n");

	const saved = await h.controller.resolveClose(documentId, "save");
	assert.equal(saved.status, "closed");
	assert.equal(h.tabs.has(documentId), false);
	assert.equal(h.store.getDocument(documentId), undefined);
	assert.ok(h.gateway.callsOf("workspace.documentClose").some((call) => call.params.uri === uri));

	// Discard closes without a save.
	const second = await mustOpen(h, "b.ts", "two\n");
	h.store.insertText(second.documentId, "y");
	h.controller.noteEdit(second.documentId);
	const savesBefore = h.gateway.callsOf("workspace.save").length;
	assert.equal((await h.controller.closeTab(second.documentId)).status, "needs-decision");
	assert.equal((await h.controller.resolveClose(second.documentId, "discard")).status, "closed");
	assert.equal(h.gateway.callsOf("workspace.save").length, savesBefore);
	assert.equal(h.store.getDocument(second.documentId), undefined);
	await h.controller.dispose();
});

test("a clean tab closes immediately and closes its mirror", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts");
	const outcome = await h.controller.closeTab(documentId);
	assert.equal(outcome.status, "closed");
	assert.equal(h.tabs.count(), 0);
	assert.equal(h.store.getDocument(documentId), undefined);
	assert.ok(h.gateway.callsOf("workspace.documentClose").some((call) => call.params.uri === uri));
	assert.equal((await h.controller.closeTab(documentId)).status, "not-open");
	await h.controller.dispose();
});

// ─── events ─────────────────────────────────────────────────────────────────

type LocalWorkspaceEvent = (
	| Omit<Extract<WorkspaceEvent, { type: "workspace.fileChanged" }>, "workspaceId" | "epoch">
	| Omit<Extract<WorkspaceEvent, { type: "workspace.diagnostics" }>, "workspaceId" | "epoch">
	| Omit<Extract<WorkspaceEvent, { type: "workspace.lspState" }>, "workspaceId" | "epoch">
	| Omit<Extract<WorkspaceEvent, { type: "workspace.indexProgress" }>, "workspaceId" | "epoch">
) & { workspaceId?: string; epoch?: number };

function workspaceEvent(event: LocalWorkspaceEvent): WorkspaceEvent {
	return { workspaceId: "ws_1", epoch: 1, ...event } as WorkspaceEvent;
}

test("diagnostics keep the latest producer set and drop stale identities", async () => {
	const h = build();
	await openWorkspace(h);
	const uri = fileUri("a.ts");

	h.controller.handleEvent(workspaceEvent({
		type: "workspace.diagnostics",
		serverGeneration: 1,
		uri,
		version: 1,
		source: "pyright",
		diagnostics: [diagnostic("one"), diagnostic("two")],
	}));
	assert.equal(h.controller.diagnosticsFor(uri).total, 2);

	h.controller.handleEvent(workspaceEvent({
		type: "workspace.diagnostics",
		serverGeneration: 2,
		uri,
		version: 2,
		source: "pyright",
		diagnostics: [diagnostic("three")],
	}));
	const view = h.controller.diagnosticsFor(uri);
	assert.equal(view.total, 1, "the same producer replaces its whole set");
	assert.equal(view.diagnostics[0]?.message, "three");

	h.controller.handleEvent(workspaceEvent({
		type: "workspace.diagnostics",
		serverGeneration: 3,
		uri,
		version: 2,
		source: "tsserver",
		diagnostics: [diagnostic("four")],
	}));
	assert.equal(h.controller.diagnosticsFor(uri).producers.length, 2);

	// Epoch and workspace mismatches are ignored.
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.diagnostics",
		epoch: 99,
		serverGeneration: 4,
		uri,
		version: 3,
		source: "pyright",
		diagnostics: [diagnostic("stale-epoch")],
	}));
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.diagnostics",
		workspaceId: "ws_other",
		serverGeneration: 5,
		uri,
		version: 3,
		source: "pyright",
		diagnostics: [diagnostic("stale-workspace")],
	}));
	assert.equal(h.controller.diagnosticsFor(uri).total, 2, "stale identities never mutate retained sets");
	await h.controller.dispose();
});

test("the diagnostics cap drops the oldest producer sets and counts them as omitted", async () => {
	const h = build();
	await openWorkspace(h);
	const uri = fileUri("a.ts");
	const producers = WORKSPACE_DIAGNOSTICS_CAP + 5;
	for (let index = 0; index < producers; index += 1) {
		h.controller.handleEvent(workspaceEvent({
			type: "workspace.diagnostics",
			serverGeneration: 1,
			uri,
			version: null,
			source: `s${index}`,
			diagnostics: [diagnostic(`d${index}`)],
		}));
	}
	const view = h.controller.diagnosticsFor(uri);
	assert.equal(view.total, WORKSPACE_DIAGNOSTICS_CAP);
	assert.equal(view.omitted, 5);
	assert.equal(h.controller.diagnosticsOmitted(), 5);
	await h.controller.dispose();
});

test("fileChanged on a clean buffer flags a notice without mutating it", async () => {
	const h = build();
	await openWorkspace(h);
	const { documentId, uri } = await mustOpen(h, "a.ts", "one\n");
	const savedRevision = h.store.getDocument(documentId)?.savedRevision ?? "";

	h.controller.handleEvent(workspaceEvent({
		type: "workspace.fileChanged",
		uri,
		revision: "disk-77",
		origin: "external",
	}));
	const document = h.store.getDocument(documentId);
	assert.equal(document?.text, "one\n", "no auto-reload");
	assert.equal(document?.savedRevision, savedRevision, "saved identity untouched");
	assert.equal(document?.dirty, false);
	assert.equal(h.store.hasPendingReload(documentId), true);
	assert.equal(h.controller.externalChangeFor(documentId)?.revision, "disk-77");

	h.controller.dismissExternalChange(documentId);
	assert.equal(h.controller.externalChangeFor(documentId), undefined);
	assert.equal(h.store.hasPendingReload(documentId), false);

	// Our own save echo carries the acknowledged revision: no notice.
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.fileChanged",
		uri,
		revision: savedRevision,
		origin: "save",
	}));
	assert.equal(h.controller.externalChangeFor(documentId), undefined);

	// A dirty buffer records a conflict instead.
	h.store.insertText(documentId, "edit");
	h.controller.noteEdit(documentId);
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.fileChanged",
		uri,
		revision: "disk-99",
		origin: "external",
	}));
	assert.equal(h.store.getDocument(documentId)?.conflict?.diskRevision, "disk-99");
	await h.controller.dispose();
});

test("lspState events cache per profile and notify the status listener", async () => {
	const h = build();
	await openWorkspace(h);
	const snapshots: WorkspaceLspStatusSnapshot[] = [];
	h.controller.setLspStatusListener((snapshot) => snapshots.push(snapshot));
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.lspState",
		profile: "pyright",
		state: "ready",
		generation: 4,
	}));
	assert.deepEqual(h.controller.lspStateFor("pyright"), { state: "ready", generation: 4 });
	assert.equal(snapshots.length, 1);
	assert.equal(snapshots[0]?.profiles.pyright?.state, "ready");
	h.controller.setLspStatusListener(undefined);
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.lspState",
		profile: "pyright",
		state: "failed",
		generation: 5,
		detail: "spawn failed",
	}));
	assert.equal(h.controller.lspStateFor("pyright")?.detail, "spawn failed");
	assert.equal(snapshots.length, 1, "a detached listener is never called");
	await h.controller.dispose();
});

// ─── intelligence and LSP ───────────────────────────────────────────────────

test("superseded completion responses are dropped; newer ones resolve", async () => {
	const h = build();
	await openWorkspace(h);
	const { uri } = await mustOpen(h, "a.ts");
	const first = deferred<WorkspaceIntelligenceResult>();
	const second = deferred<WorkspaceIntelligenceResult>();
	const queue = [first, second];
	h.gateway.intelligenceHandler = async () => (queue.shift() ?? second).promise;
	const position: LspPosition = { line: 0, character: 0 };
	const p1 = h.controller.request("completion", uri, 0, position);
	const p2 = h.controller.request("completion", uri, 0, position);
	first.resolve(intelligenceResult("completion", uri, 0));
	assert.equal((await p1).status, "superseded");
	second.resolve(intelligenceResult("completion", uri, 0));
	assert.equal((await p2).status, "ok");

	// A hover never cancels an in-flight completion (different popups).
	const completion = deferred<WorkspaceIntelligenceResult>();
	const hover = deferred<WorkspaceIntelligenceResult>();
	const mixed = [completion, hover];
	h.gateway.intelligenceHandler = async () => (mixed.shift() ?? hover).promise;
	const pc = h.controller.request("completion", uri, 0, position);
	const ph = h.controller.request("hover", uri, 0, position);
	hover.resolve(intelligenceResult("hover", uri, 0));
	completion.resolve(intelligenceResult("completion", uri, 0));
	assert.equal((await pc).status, "ok");
	assert.equal((await ph).status, "ok");

	// Non-superseding operations keep every response.
	const third = deferred<WorkspaceIntelligenceResult>();
	const fourth = deferred<WorkspaceIntelligenceResult>();
	const nonSuperseding = [third, fourth];
	h.gateway.intelligenceHandler = async () => (nonSuperseding.shift() ?? fourth).promise;
	const d1 = h.controller.request("definition", uri, 0, position);
	const d2 = h.controller.request("definition", uri, 0, position);
	third.resolve(intelligenceResult("definition", uri, 0));
	fourth.resolve(intelligenceResult("definition", uri, 0));
	assert.equal((await d1).status, "ok");
	assert.equal((await d2).status, "ok");
	await h.controller.dispose();
});

test("intelligence refusals and transport failures become typed outcomes", async () => {
	const h = build();
	await openWorkspace(h);
	const { uri } = await mustOpen(h, "a.ts");
	h.gateway.intelligenceHandler = async () => ({
		refused: { reason: "denied", message: "workspace is untrusted" },
	});
	const refused = await h.controller.request("hover", uri, 0, { line: 0, character: 0 });
	assert.equal(refused.status, "refused");
	h.gateway.intelligenceHandler = async () => {
		throw new Error("transport down");
	};
	const failed = await h.controller.request("hover", uri, 0, { line: 0, character: 0 });
	assert.equal(failed.status, "failed");
	await h.controller.dispose();
});

test("lsp status/control are typed passthroughs", async () => {
	const h = build();
	await openWorkspace(h);
	h.gateway.lspStatusHandler = async () => ({
		servers: [{ profile: "pyright", languageId: "python", state: "ready", generation: 3 }],
	});
	const snapshots: WorkspaceLspStatusSnapshot[] = [];
	h.controller.setLspStatusListener((snapshot) => snapshots.push(snapshot));
	const status = await h.controller.lspStatus();
	assert.equal(status?.servers.length, 1);
	assert.equal(snapshots.length, 1);
	h.gateway.lspControlHandler = async (profile, action) => ({
		action,
		profile,
		state: "starting",
		detail: "spawned",
	});
	const control = await h.controller.lspControl("pyright", "start");
	assert.equal(control.status, "ok");
	assert.equal(h.controller.lspStateFor("pyright")?.state, "starting");
	assert.equal((await h.controller.lspControl("pyright", "restart")).status, "ok");
	// The control result carries no generation: an event-derived one survives.
	h.controller.handleEvent(workspaceEvent({
		type: "workspace.lspState",
		profile: "pyright",
		state: "ready",
		generation: 9,
	}));
	await h.controller.lspControl("pyright", "restart");
	assert.equal(h.controller.lspStateFor("pyright")?.generation, 9, "control never regresses the generation");
	h.gateway.lspControlHandler = async () => ({
		refused: { reason: "denied", message: "workspace is not trusted" },
	});
	const refused = await h.controller.lspControl("pyright", "stop");
	assert.equal(refused.status, "refused");
	await h.controller.dispose();
});

// ─── persistence bounds, toggles and dispose ────────────────────────────────

test("persisted open URIs are bounded at twenty and cursors mirror the store", async () => {
	const h = build();
	await openWorkspace(h);
	h.gateway.readHandler = async (uri) => readResult(uri);
	for (let index = 0; index < 25; index += 1) {
		const outcome = await h.controller.openUri(fileUri(`f${index}.ts`));
		assert.equal(outcome.status, "opened");
	}
	const first = h.store.documents()[0];
	assert.ok(first);
	h.store.setSelection(first.id, { cursorOffset: 3, anchorOffset: 1 });
	h.store.setViewport(first.id, { scrollLine: 7 });
	const state = h.controller.buildPersistedState();
	assert.equal(state.openUris.length, 20);
	assert.equal(state.openUris[0], fileUri("f0.ts"));
	assert.deepEqual(state.cursorByUri?.[first.uri], { offset: 3, anchorOffset: 1, scrollLine: 7 });
	assert.equal(state.activeUri, fileUri("f24.ts"));
	await h.controller.dispose();
});

test("mode toggles preserve the previous non-chat mode", async () => {
	const h = build();
	await openWorkspace(h);
	assert.equal(h.controller.getLayoutInput().requestedMode, "split");
	h.controller.toggleChatMaximize();
	assert.equal(h.controller.mode, "chat");
	h.controller.toggleChatMaximize();
	assert.equal(h.controller.mode, "split", "chat maximize restores the previous split");

	h.controller.setMode("editor");
	h.controller.toggleChatMaximize();
	assert.equal(h.controller.mode, "chat");
	h.controller.toggleChatMaximize();
	assert.equal(h.controller.mode, "editor", "chat maximize restores the mode it was entered from");

	h.controller.toggleEditorMaximize();
	assert.equal(h.controller.mode, "split");
	h.controller.toggleEditorMaximize();
	assert.equal(h.controller.mode, "editor");
	h.controller.restoreSplit();
	assert.equal(h.controller.mode, "split");

	h.controller.adjustRatio(-1);
	assert.ok(Math.abs(h.controller.getLayoutInput().splitRatio - 0.45) < 1e-9);
	h.controller.setRatio(0.05);
	assert.equal(h.controller.getLayoutInput().splitRatio, 0.25);
	h.controller.setRatio(0.95);
	assert.equal(h.controller.getLayoutInput().splitRatio, 0.75);
	h.controller.toggleExplorer();
	assert.equal(h.controller.getLayoutInput().explorerRequested, true);
	h.controller.noteFocus("explorer");
	assert.equal(h.controller.getLayoutInput().preferredPane, "editor");
	h.controller.noteFocus("chat-composer");
	assert.equal(h.controller.getLayoutInput().preferredPane, "chat");
	h.controller.noteFocus("modal");
	assert.equal(h.controller.getLayoutInput().preferredPane, "chat", "modal keeps the previous pane");
	await h.controller.dispose();
});

test("dispose closes every open document mirror and the workspace, once", async () => {
	const h = build();
	await openWorkspace(h);
	await mustOpen(h, "a.ts");
	await mustOpen(h, "b.ts");
	await h.controller.dispose();
	const closes = h.gateway
		.callsOf("workspace.documentClose")
		.map((call) => String(call.params.uri))
		.sort();
	assert.deepEqual(closes, [fileUri("a.ts"), fileUri("b.ts")].sort());
	assert.equal(h.gateway.closeCalls, 1);
	assert.equal(h.controller.phase, "closed");
	await h.controller.dispose();
	assert.equal(h.gateway.closeCalls, 1, "dispose is idempotent");
});

// ─── adapter seam ───────────────────────────────────────────────────────────

test("WorkspaceGatewayAdapter binds identity and shares the request function", async () => {
	const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	const request = (async (method: string, params: Record<string, unknown>): Promise<unknown> => {
		calls.push({ method, params });
		if (method === "workspace.open") {
			return {
				workspaceId: "ws_9",
				clientId: "cl_9",
				epoch: 7,
				rootLabel: "ws",
				resolvedRoot: "/ws",
				trusted: true,
				capabilities: [],
			};
		}
		if (method === "workspace.read") return readResult(String(params.uri));
		return { closed: true };
	}) as unknown as WorkspaceRequestFn;
	const adapter = new WorkspaceGatewayAdapter(request);
	const identity = await adapter.open("/ws", true);
	assert.equal(identity.epoch, 7);
	assert.equal(adapter.identity()?.workspaceId, "ws_9");
	const read = await adapter.read(fileUri("a.ts"));
	assert.equal(isWorkspaceRefusal(read), false);
	assert.deepEqual(calls.at(-1)?.params, { workspaceId: "ws_9", epoch: 7, uri: fileUri("a.ts") });
	const closed = await adapter.close();
	assert.ok(!isWorkspaceRefusal(closed));
	assert.equal(adapter.identity(), undefined);
	await assert.rejects(() => adapter.read(fileUri("a.ts")), /workspace is not open/);
});
