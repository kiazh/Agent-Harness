// T12/W3-A2 app-level integration proof (plan §8/§9/§25): the real App wired
// to the workspace controller over a fake gateway/client. Two harnesses:
// - FakeTUI + FakeClient (from app.test.ts) for key routing, commands,
//   notices, RPC bookkeeping and dialogs;
// - a real TuiAltScreen over a mock terminal (from layout.test.ts) for the
//   composed-frame split/chat geometry at 160x48 and 80x24.
//
// Every test drives the production seams: App.start() (flag, background open,
// shell sync), App.onKey via the registered input listener, runCommand via the
// FeatureHost surface, and controller RPCs through the real adapter.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	isViewportTUI,
	stripTerminalSequences,
	TuiAltScreen,
	visibleWidth,
	type Component,
	type OverlayHandle,
	type Terminal,
	type TUI,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { runCommand } from "../src/features/index.ts";
import { RpcError } from "../src/protocol.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, SessionInfo } from "../src/protocol.ts";
import type { WorkspaceEvent } from "../src/workspace/protocol.ts";

const SESSION_A: SessionInfo = {
	id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
	title: "session A",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const SESSION_B: SessionInfo = {
	id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
	title: "session B",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const WORKSPACE_IDENTITY = {
	workspaceId: "ws1",
	clientId: "cl1",
	epoch: 1,
	rootLabel: "ws",
	resolvedRoot: "/ws",
	trusted: true,
	capabilities: ["list", "read", "save", "documentOpen", "intelligence"],
};

function workspaceIdentity(): Record<string, unknown> {
	return { ...WORKSPACE_IDENTITY };
}

/** Structure the fake workspace serves for read/quick-open flows. */
function readResult(uri: string): Record<string, unknown> {
	return {
		uri,
		text: "hello\n",
		revision: "rev-1",
		languageId: "plaintext",
		lineEnding: "LF",
		encoding: "utf-8",
		readOnly: false,
		sizeBytes: 6,
	};
}

// ─── FakeTUI harness ────────────────────────────────────────────────────────

class FakeTUI implements TUI {
	children: Component[] = [];
	focused: Component | null = null;
	overlay: OverlayHandle | undefined;
	inputListeners: Array<(data: string) => void> = [];
	started = false;
	stopped = false;
	renderCount = 0;
	mode = "fullscreen" as const;
	terminal = { columns: 160, rows: 48 } as TUI["terminal"];
	fullRedraws = 0;
	clearOnShrink = false;

	addChild(c: Component): void {
		this.children.push(c);
	}
	removeChild(c: Component): void {
		const index = this.children.indexOf(c);
		if (index >= 0) this.children.splice(index, 1);
	}
	clear(): void {
		this.children = [];
	}
	setFocus(c: Component | null): void {
		this.focused = c;
	}
	showOverlay(_c: Component): OverlayHandle {
		const handle: OverlayHandle = {
			hide: () => {
				if (this.overlay === handle) this.overlay = undefined;
			},
			setHidden: () => {},
			isHidden: () => false,
			focus: () => {},
			unfocus: () => {},
			isFocused: () => false,
			getBounds: () => ({ row: 0, col: 0, width: 80, height: 24 }),
		};
		this.overlay = handle;
		return handle;
	}
	hideOverlay(): void {
		this.overlay = undefined;
	}
	hasOverlay(): boolean {
		return this.overlay !== undefined;
	}
	start(): void {
		this.started = true;
	}
	stop(): void {
		this.stopped = true;
	}
	requestRender(): void {
		this.renderCount++;
	}
	addInputListener(listener: (data: string) => void): () => void {
		this.inputListeners.push(listener);
		return () => {};
	}
	removeInputListener(): void {}
	onTerminalColorSchemeChange(): () => void {
		return () => {};
	}
	setTerminalColorSchemeNotifications(): void {}
	queryTerminalColors(): Promise<never> {
		throw new Error("not implemented");
	}
	render(): string[] {
		return [];
	}
	invalidate(): void {}
	handleMouse(): undefined {
		return undefined;
	}
	getShowHardwareCursor(): boolean {
		return false;
	}
	setShowHardwareCursor(): void {}
	getClearOnShrink(): boolean {
		return this.clearOnShrink;
	}
	setClearOnShrink(enabled: boolean): void {
		this.clearOnShrink = enabled;
	}
	renderNow(): void {}

	/** Deliver one key exactly like the TUI input funnel does. */
	press(data: string): void {
		for (const listener of this.inputListeners) listener(data);
	}
}

class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	onWorkspaceEvent: ((event: WorkspaceEvent) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	responses = new Map<string, unknown>();
	handlers = new Map<string, (params: Record<string, unknown>) => unknown | Promise<unknown>>();
	stopped = false;

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {
		this.stopped = true;
	}

	request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		const handler = this.handlers.get(method);
		if (handler) {
			return Promise.resolve().then(() => handler(params)) as Promise<T>;
		}
		const response = this.responses.get(method);
		if (response === undefined) return Promise.reject(new Error(`unexpected request ${method}`));
		return Promise.resolve(response as T);
	}

	emit(event: GatewayEvent): void {
		this.onEvent?.(event);
	}

	emitWorkspace(event: WorkspaceEvent): void {
		this.onWorkspaceEvent?.(event);
	}

	workspaceCalls(): Array<{ method: string; params: Record<string, unknown> }> {
		return this.requests.filter((entry) => entry.method.startsWith("workspace."));
	}
}

function makeApp(): { app: App; tui: FakeTUI; client: FakeClient } {
	const tui = new FakeTUI();
	const client = new FakeClient();
	configureClient(client);
	const app = new App(tui, client as unknown as GatewayClient);
	return { app, tui, client };
}

/** Baseline fake responses shared by both harnesses. */
function configureClient(client: FakeClient): void {
	client.responses.set("initialize", {
		version: "1.0.0",
		model: "m",
		provider: "openrouter",
		cwd: "/ws",
		branch: "main",
	});
	client.responses.set("session.create", { session: SESSION_A });
	client.responses.set("prompt.submit", { turnId: "t" });
	client.responses.set("prompt.cancel", {});
	client.responses.set("config.get", { config: {}, secrets: [] });
	client.responses.set("config.set", { model: "m", provider: "openrouter", key: "workspace_ui_state", value: "" });
	client.responses.set("workspace.open", workspaceIdentity());
	client.responses.set("workspace.documentOpen", { acceptedVersion: 0, serverGeneration: 1, state: "ready" });
	client.responses.set("workspace.documentChange", { acceptedVersion: 1 });
	client.responses.set("workspace.documentClose", { closed: true, uri: "" });
	client.responses.set("workspace.close", { closed: true });
	client.responses.set("workspace.save", { ok: true, revision: "rev-2", savedUiVersion: 0 });
	client.responses.set("workspace.lspStatus", { servers: [] });
	client.responses.set("workspace.quickOpen", {
		entries: [],
		total: 0,
		truncated: false,
		reasons: [],
		indexing: false,
		scanned: 0,
		stale: false,
		workspaceEpoch: 1,
		exclusions: [],
	});
	client.handlers.set("workspace.read", (params) => readResult(String(params.uri)));
}

/** Let the background workspace chain settle (all RPCs are immediate fakes). */
async function settle(times = 40): Promise<void> {
	for (let index = 0; index < times; index++) await new Promise((resolve) => setImmediate(resolve));
}

/** Spy on transcript notices without touching the rendering path. */
function captureNotices(app: App): Array<{ text: string; kind: unknown }> {
	const notices: Array<{ text: string; kind: unknown }> = [];
	const transcript = (app as unknown as { transcript: { addNotice(text: string, kind?: unknown): void } }).transcript;
	const original = transcript.addNotice.bind(transcript);
	transcript.addNotice = (text: string, kind?: unknown) => {
		notices.push({ text, kind });
		original(text, kind);
	};
	return notices;
}

/** Internal handles the production wiring owns (read-only test access). */
function internals(app: App): {
	integration?: {
		controller: { phase: string; identity?: { workspaceId: string }; getLayoutInput(): { requestedMode: string; explorerRequested: boolean } };
		store: { documents(): Array<{ id: string; uri: string; dirty: boolean }>; insertText(id: string, text: string): unknown; getViewState(id: string): { cursorOffset: number } | undefined };
		tabs: { activeDocumentId(): string | null; count(): number };
		pane: {
			render(width: number): string[];
			handleIntelligenceKey(data: string): string;
			setProblems(component?: unknown): void;
		};
		problemsModel: { rows(): Array<{ message: string }> };
	};
	workspaceLayout?: { effectiveMode: string; editor?: { columns: number }; chat?: { columns: number } };
	workspaceLayoutActive: boolean;
	activeRegion: string;
	problemsOpen: boolean;
	completionSession?: unknown;
	quickOpenPane?: unknown;
	workspaceUnsupported: boolean;
	running: boolean;
	exiting: boolean;
	openWorkspacePath(path: string, options?: { preview?: boolean }): Promise<void>;
} {
	return app as unknown as ReturnType<typeof internals>;
}

function workspaceOpened(app: App): boolean {
	return internals(app).integration !== undefined && internals(app).workspaceLayoutActive;
}

async function waitForWorkspace(app: App): Promise<void> {
	for (let index = 0; index < 200 && !workspaceOpened(app); index++) {
		await new Promise((resolve) => setImmediate(resolve));
	}
	assert.ok(workspaceOpened(app), "workspace opened in the background");
}

// ─── flag + startup ─────────────────────────────────────────────────────────

test("MACHINA_WORKSPACE=0 disables the workspace entirely (no RPCs, chat-only shell)", async () => {
	const previous = process.env.MACHINA_WORKSPACE;
	process.env.MACHINA_WORKSPACE = "0";
	const { app, client } = makeApp();
	try {
		await app.start();
		await settle();
		assert.equal(
			client.requests.some((entry) => entry.method.startsWith("workspace.")),
			false,
			"no workspace RPC is issued",
		);
		assert.equal(internals(app).integration, undefined);
		assert.equal(internals(app).workspaceLayout, undefined, "shell stays chat-only");

		// Commands no-op with one clear notice (no handler at all).
		const notices: string[] = [];
		(app as unknown as { print(text: string): void }).print = (text: string) => notices.push(text);
		await runCommand({ name: "workspace", args: "status" }, app);
		assert.deepEqual(notices, ["Workspace is not available."]);

		// Chat still works.
		client.responses.set("prompt.submit", { turnId: "t" });
		await app.submitTurn("still chat");
		assert.equal(client.requests.filter((entry) => entry.method === "prompt.submit").length, 1);
	} finally {
		if (previous === undefined) delete process.env.MACHINA_WORKSPACE;
		else process.env.MACHINA_WORKSPACE = previous;
		await app.exit();
	}
});

test("a failed background open notifies once and leaves chat usable", async () => {
	const { app, client } = makeApp();
	client.handlers.set("workspace.open", () => {
		throw new Error("backend offline");
	});
	const notices = captureNotices(app);
	await app.start();
	await settle();
	const failures = notices.filter((notice) => notice.kind === "error" && notice.text.includes("could not open workspace"));
	assert.equal(failures.length, 1, "exactly one failure notice");
	assert.equal(internals(app).integration !== undefined, true, "panes exist; only the open failed");
	assert.equal(internals(app).workspaceLayoutActive, false, "shell was never switched");
	assert.equal(internals(app).workspaceLayout, undefined);

	client.responses.set("prompt.submit", { turnId: "t" });
	await app.submitTurn("chat after failure");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.submit").length, 1);
	await app.exit();
});

test("METHOD_NOT_FOUND disables the workspace quietly with one notice and never retries", async () => {
	const { app, client } = makeApp();
	client.handlers.set("workspace.open", () => {
		throw new RpcError(-32601, "Method not found");
	});
	const notices = captureNotices(app);
	await app.start();
	await settle();

	const disables = notices.filter((notice) => notice.text === "workspace commands unavailable on this backend");
	assert.equal(disables.length, 1, "one info notice");
	assert.equal(disables[0]?.kind, "info");
	assert.equal(
		notices.some((notice) => notice.kind === "error" && notice.text.includes("could not open workspace")),
		false,
		"the controller's failing-open error is suppressed while disabled",
	);
	assert.equal(internals(app).workspaceUnsupported, true);

	const callsBefore = client.workspaceCalls().length;
	const noticesBefore = notices.length;
	await runCommand({ name: "workspace", args: "status" }, app);
	assert.equal(notices.at(-1)?.text, "Workspace is not available.");
	assert.equal(client.workspaceCalls().length, callsBefore, "subsequent commands issue no workspace RPC");
	assert.equal(notices.length, noticesBefore + 1, "exactly one notice per no-op command");
	await app.exit();
});

// ─── successful open + geometry ─────────────────────────────────────────────

test("successful open wires the default split and pushes shell state", async () => {
	const { app, client } = makeApp();
	await app.start();
	await waitForWorkspace(app);

	const layout = internals(app).workspaceLayout;
	assert.equal(layout?.effectiveMode, "split");
	assert.ok((layout?.editor?.columns ?? 0) >= 48, "editor column keeps its minimum at 160 cols");
	assert.ok((layout?.chat?.columns ?? 0) >= 40, "chat column keeps its minimum");
	assert.equal(internals(app).integration?.controller.phase, "ready");
	const opens = client.requests.filter((entry) => entry.method === "workspace.open");
	assert.equal(opens.length, 1, "opened exactly once, in the background");
	assert.equal(opens[0]?.params.root, "/ws");
	await app.exit();
});

test("a persisted state restores mode, remembered files and cursors", async () => {
	const { app, client } = makeApp();
	const persisted = JSON.stringify({
		version: 1,
		byRoot: {
			"/ws": {
				version: 1,
				requestedMode: "editor",
				splitRatio: 0.6,
				explorerRequested: false,
				openUris: ["file:///ws/a.ts"],
				activeUri: "file:///ws/a.ts",
				cursorByUri: { "file:///ws/a.ts": { offset: 3 } },
			},
		},
	});
	client.handlers.set("config.get", () => ({ config: { workspace_ui_state: persisted }, secrets: [] }));
	await app.start();
	await waitForWorkspace(app);
	await settle();

	assert.equal(internals(app).workspaceLayout?.effectiveMode, "editor", "stored editor mode applied");
	const reads = client.requests.filter((entry) => entry.method === "workspace.read");
	assert.equal(reads.length, 1, "remembered file reopened");
	assert.equal(reads[0]?.params.uri, "file:///ws/a.ts");
	const integration = internals(app).integration!;
	assert.equal(integration.tabs.count(), 1);
	const documentId = integration.tabs.activeDocumentId();
	assert.ok(documentId);
	assert.equal(integration.store.getViewState(documentId!)?.cursorOffset, 3, "stored cursor restored");
	await app.exit();
});

// ─── composed frames: 160x48 split vs 80-col chat-only ──────────────────────

interface MockTerminal extends Terminal {
	writes: string[];
	columns: number;
	rows: number;
}

function mockTerminal(columns: number, rows: number): MockTerminal {
	return {
		writes: [],
		columns,
		rows,
		kittyProtocolActive: false,
		start() {},
		stop() {},
		async drainInput() {},
		write(data: string) {
			this.writes.push(data);
		},
		moveBy() {},
		hideCursor() {},
		showCursor() {},
		clearLine() {},
		clearFromCursor() {},
		clearScreen() {},
		setTitle() {},
		setProgress() {},
	};
}

async function bootFramed(columns: number, rows: number): Promise<{
	app: App;
	tui: TuiAltScreen;
	term: MockTerminal;
	client: FakeClient;
}> {
	const term = mockTerminal(columns, rows);
	const tui = new TuiAltScreen(term);
	const client = new FakeClient();
	configureClient(client);
	assert.equal(isViewportTUI(tui), true);
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	await waitForWorkspace(app);
	await settle();
	tui.renderNow(true);
	return { app, tui, term, client };
}

test("at 160x48 the composed frame carries a real editor column beside chat", async () => {
	const { app, tui, client } = await bootFramed(160, 48);
	try {
		const screen = tui.getScreenLines().map(stripTerminalSequences);
		assert.equal(screen.length, 48);
		for (const line of screen) assert.ok(visibleWidth(line) <= 160);
		const joined = screen.join("\n");
		assert.match(joined, /Ask anything/, "chat composer visible");
		assert.match(joined, /No open files/, "editor tab strip visible in the split");
		assert.match(joined, /No open document/, "editor status row visible");
		assert.equal(internals(app).workspaceLayout?.effectiveMode, "split");
	} finally {
		await app.exit();
		assert.equal(client.stopped, true);
	}
});

test("at 80 columns the frame stays a single chat pane (narrow fallback)", async () => {
	const { app, tui } = await bootFramed(80, 24);
	try {
		const screen = tui.getScreenLines().map(stripTerminalSequences);
		assert.equal(screen.length, 24);
		const joined = screen.join("\n");
		assert.match(joined, /Ask anything/, "chat composer visible");
		assert.doesNotMatch(joined, /No open files/, "no editor column at this width");
		assert.equal(internals(app).workspaceLayout?.effectiveMode, "chat");
	} finally {
		await app.exit();
	}
});

// ─── keys ───────────────────────────────────────────────────────────────────

test("Ctrl+Right toggles chat maximize and back to split (global wins when delivered)", async () => {
	const { app, tui } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	assert.equal(internals(app).workspaceLayout?.effectiveMode, "split");

	tui.press("\x1b[1;5C");
	assert.equal(internals(app).workspaceLayout?.effectiveMode, "chat", "chat maximized");
	tui.press("\x1b[1;5C");
	assert.equal(internals(app).workspaceLayout?.effectiveMode, "split", "restored to the split");
	// The workspace host records the persistence intent too.
	assert.equal(internals(app).integration?.controller.getLayoutInput().requestedMode, "split");
	await app.exit();
});

test("F6 cycles composer → editor and retargets TUI focus", async () => {
	const { app, tui } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	assert.equal(internals(app).activeRegion, "chat-composer");

	tui.press("\x1b[17~");
	assert.equal(internals(app).activeRegion, "editor");
	assert.equal(tui.focused, internals(app).integration?.pane, "editing focus moved to the pane");

	tui.press("\x1b[17~");
	assert.equal(internals(app).activeRegion, "chat-composer", "cycles back");
	assert.notEqual(tui.focused, internals(app).integration?.pane);
	await app.exit();
});

test("Ctrl+S saves the active buffer with the disk revision expectation", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	await internals(app).openWorkspacePath("src/a.ts");
	// Opening a document focuses the editor region (controller focus callback);
	// Ctrl+S is editor-scoped.
	assert.equal(internals(app).activeRegion, "editor");

	tui.press("\x13");
	await settle();
	const saves = client.requests.filter((entry) => entry.method === "workspace.save");
	assert.equal(saves.length, 1);
	assert.equal(saves[0]?.params.uri, "file:///ws/src/a.ts");
	assert.equal(saves[0]?.params.expectedRevision, "rev-1");
	assert.equal(saves[0]?.params.text, "hello\n");
	await app.exit();
});

test("Esc with an open completion popup dismisses it without cancelling the running turn", async () => {
	const { app, tui, client } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	await internals(app).openWorkspacePath("src/a.ts");
	client.handlers.set("workspace.intelligence", (params) => ({
		workspaceId: "ws1",
		source: "editor-buffer",
		uri: params.uri,
		documentVersion: params.version,
		serverGeneration: 1,
		workspaceEpoch: 1,
		operation: params.operation,
		state: "ready",
		freshness: "versioned",
		truncated: false,
		result: { items: [{ label: "alpha" }] },
	}));

	// Opening the document focused the editor region; Ctrl+Space is editor-scoped.
	assert.equal(internals(app).activeRegion, "editor");
	tui.press("\x00"); // Ctrl+Space (legacy NUL form)
	await settle();
	assert.ok(internals(app).completionSession, "popup session mounted");

	// A running turn is untouched by the popup's Esc.
	client.responses.set("prompt.submit", { turnId: "t" });
	await app.submitTurn("turn while the popup is open");
	assert.equal(internals(app).running, true);
	tui.press("\x1b");
	assert.equal(internals(app).completionSession, undefined, "first Esc dismissed the popup");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.cancel").length, 0, "turn not cancelled");

	tui.press("\x1b");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.cancel").length, 1, "second Esc stops the turn");
	await app.exit();
});

// ─── dirty exit review ──────────────────────────────────────────────────────

async function dirtyApp(): Promise<{ app: App; tui: FakeTUI; client: FakeClient; documentId: string }> {
	const { app, tui, client } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	await internals(app).openWorkspacePath("src/a.ts");
	const integration = internals(app).integration!;
	const documentId = integration.tabs.activeDocumentId()!;
	integration.store.insertText(documentId, "dirty!");
	assert.equal((integration.store.documents().find((document) => document.id === documentId) ?? { dirty: false }).dirty, true);
	return { app, tui, client, documentId };
}

test("dirty exit review: Cancel keeps the app alive with nothing written", async () => {
	const { app, tui, client } = await dirtyApp();
	(app as unknown as { pick: () => Promise<{ value: string }> }).pick = async () => ({ value: "cancel" });
	await app.exit();
	assert.equal(internals(app).exiting, false, "exit was intercepted");
	assert.equal(tui.stopped, false);
	assert.equal(client.stopped, false);
	assert.equal(client.requests.filter((entry) => entry.method === "workspace.save").length, 0);
	// A later exit still reviews and can proceed (guard is re-entrant-safe).
	(app as unknown as { pick: () => Promise<{ value: string }> }).pick = async () => ({ value: "save" });
	await app.exit();
	assert.equal(internals(app).exiting, true);
	assert.equal(tui.stopped, true);
	assert.equal(client.requests.filter((entry) => entry.method === "workspace.save").length, 1, "save-all wrote the buffer");
});

test("dirty exit review: a failed save keeps the app open and names the file", async () => {
	const { app, tui, client } = await dirtyApp();
	client.handlers.set("workspace.save", () => {
		throw new Error("disk full");
	});
	const notices = captureNotices(app);
	(app as unknown as { pick: () => Promise<{ value: string }> }).pick = async () => ({ value: "save" });
	await app.exit();
	assert.equal(internals(app).exiting, false, "failure keeps the app open");
	assert.equal(tui.stopped, false);
	const named = notices.filter((notice) => notice.text.includes("a.ts") && notice.text.includes("Exit cancelled"));
	assert.equal(named.length, 1, "exactly one notice naming the failed file");
	assert.equal(named[0]?.kind, "error");
	// Clean up without the review interfering.
	(app as unknown as { pick: () => Promise<{ value: string }> }).pick = async () => ({ value: "discard" });
	await app.exit();
});

// ─── quick open ─────────────────────────────────────────────────────────────

test("Ctrl+P opens the Quick Open overlay, Enter opens the file, Esc closes it", async () => {
	const { app, tui, client } = makeApp();
	client.handlers.set("workspace.quickOpen", () => ({
		entries: [{ path: "src/a.ts", name: "a.ts", directory: "src" }],
		total: 1,
		truncated: false,
		reasons: [],
		indexing: false,
		scanned: 1,
		stale: false,
		workspaceEpoch: 1,
		exclusions: [],
	}));
	await app.start();
	await waitForWorkspace(app);

	tui.press("\x10");
	assert.ok(internals(app).quickOpenPane, "overlay opened");
	await settle();
	const queries = client.requests.filter((entry) => entry.method === "workspace.quickOpen");
	assert.equal(queries.length, 1);
	assert.equal(queries[0]?.params.workspaceId, "ws1");

	// Enter reaches the focused overlay component; drive the pane directly.
	const pane = internals(app).quickOpenPane as { handleInput(data: string): void };
	pane.handleInput("\r");
	await settle();
	assert.equal(internals(app).quickOpenPane, undefined, "overlay closed on open");
	const reads = client.requests.filter((entry) => entry.method === "workspace.read");
	assert.equal(reads.length, 1);
	assert.equal(reads[0]?.params.uri, "file:///ws/src/a.ts");

	// Reopen and close with Esc through the app's key funnel.
	tui.press("\x10");
	assert.ok(internals(app).quickOpenPane);
	tui.press("\x1b");
	await settle();
	assert.equal(internals(app).quickOpenPane, undefined, "Esc closes the overlay");
	assert.equal(internals(app).exiting, false);
	await app.exit();
});

// ─── session switching, diagnostics, LSP, commands ──────────────────────────

test("switchTo never touches workspace state (workspaces outlive chat sessions)", async () => {
	const { app, client } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	await internals(app).openWorkspacePath("src/a.ts");
	const integration = internals(app).integration!;
	const documentsBefore = integration.store.documents().map((document) => document.uri);
	const closesBefore = client.requests.filter((entry) => entry.method === "workspace.close").length;

	app.switchTo(SESSION_B, []);
	await settle();
	assert.equal(integration.controller.phase, "ready", "workspace stays open");
	assert.deepEqual(integration.store.documents().map((document) => document.uri), documentsBefore);
	assert.equal(integration.tabs.count(), 1);
	assert.equal(
		client.requests.filter((entry) => entry.method === "workspace.close").length,
		closesBefore,
		"no workspace teardown on session switch",
	);
	await app.exit();
});

test("workspace.diagnostics events feed the Problems pane and /problems opens it", async () => {
	const { app, client } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	client.emitWorkspace({
		type: "workspace.diagnostics",
		workspaceId: "ws1",
		epoch: 1,
		serverGeneration: 1,
		uri: "file:///ws/src/a.ts",
		version: null,
		source: "typescript",
		diagnostics: [
			{
				range: { start: { line: 4, character: 0 }, end: { line: 4, character: 5 } },
				severity: 1,
				message: "boom",
			},
		],
	} as WorkspaceEvent);
	await settle();
	const rows = internals(app).integration!.problemsModel.rows();
	assert.equal(rows.length, 1);
	assert.equal(rows[0]?.message, "boom");

	await runCommand({ name: "problems", args: "" }, app);
	assert.equal(internals(app).problemsOpen, true);
	assert.equal(internals(app).activeRegion, "problems");
	const rendered = internals(app).integration!.pane.render(120).join("\n");
	assert.match(rendered, /boom/, "done rows render inside the pane");
	await app.exit();
});

test("/lsp status prints per-profile state, generation and capabilities", async () => {
	const { app, client } = makeApp();
	client.responses.set("workspace.lspStatus", {
		servers: [
			{ profile: "pyright", languageId: "python", state: "ready", generation: 3, capabilities: { completion: true, hover: true } },
			{ profile: "typescript-language-server", languageId: "typescript", state: "missing", generation: 0 },
		],
	});
	const printed: string[] = [];
	await app.start();
	await waitForWorkspace(app);
	(app as unknown as { print(text: string): void }).print = (text: string) => printed.push(text);
	await runCommand({ name: "lsp", args: "status" }, app);
	const output = printed.join("\n");
	assert.match(output, /python — ready \(generation 3\)/);
	assert.match(output, /capabilities: completion, hover/);
	assert.match(output, /typescript — missing \(generation 0\)/);
	assert.match(output, /capabilities unknown/);
	await app.exit();
});

test("/editor maximize and /workspace split drive the shell through the host", async () => {
	const { app } = makeApp();
	await app.start();
	await waitForWorkspace(app);
	await runCommand({ name: "editor", args: "maximize" }, app);
	assert.equal(internals(app).workspaceLayout?.effectiveMode, "editor");
	await runCommand({ name: "workspace", args: "split" }, app);
	assert.equal(internals(app).workspaceLayout?.effectiveMode, "split");
	await app.exit();
});
