// T10 explicit selection attachment (plan §20): `/attach` moves the active
// editor selection into the composer draft with a visible scope banner and
// submits nothing. Driven through the real App wiring over a fake gateway
// (same harness shape as workspace-app-integration.test.ts).

import assert from "node:assert/strict";
import { test } from "node:test";
import type { Component, OverlayHandle, TUI } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { runCommand } from "../src/features/index.ts";
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

const SOURCE = "const a = 1;\nconst b = 2;\nconst c = 3;\n";

function workspaceIdentity(): Record<string, unknown> {
	return {
		workspaceId: "ws1",
		clientId: "cl1",
		epoch: 1,
		rootLabel: "ws",
		resolvedRoot: "/ws",
		trusted: true,
		capabilities: ["list", "read", "save", "documentOpen", "intelligence"],
	};
}

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
		if (handler) return Promise.resolve().then(() => handler(params)) as Promise<T>;
		const response = this.responses.get(method);
		if (response === undefined) return Promise.reject(new Error(`unexpected request ${method}`));
		return Promise.resolve(response as T);
	}
	emits(): number {
		return this.requests.filter((entry) => entry.method === "prompt.submit").length;
	}
}

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
	client.responses.set("config.get", { config: { nowplaying_enabled: false }, secrets: [] });
	client.responses.set("config.set", { model: "m", provider: "openrouter", key: "workspace_ui_state", value: "" });
	client.responses.set("workspace.open", workspaceIdentity());
	client.responses.set("workspace.documentOpen", { acceptedVersion: 0, serverGeneration: 1, state: "ready" });
	client.responses.set("workspace.documentChange", { acceptedVersion: 1 });
	client.responses.set("workspace.documentClose", { closed: true, uri: "" });
	client.responses.set("workspace.close", { closed: true });
	client.responses.set("workspace.save", { ok: true, revision: "rev-2", savedUiVersion: 0 });
	client.responses.set("workspace.lspStatus", { servers: [] });
	client.handlers.set("workspace.read", (params) => ({
		uri: String(params.uri),
		text: SOURCE,
		revision: "rev-1",
		languageId: "javascript",
		lineEnding: "LF",
		encoding: "utf-8",
		readOnly: false,
		sizeBytes: SOURCE.length,
	}));
}

function makeApp(): { app: App; tui: FakeTUI; client: FakeClient } {
	const tui = new FakeTUI();
	const client = new FakeClient();
	configureClient(client);
	const app = new App(tui, client as unknown as GatewayClient);
	return { app, tui, client };
}

async function settle(times = 40): Promise<void> {
	for (let index = 0; index < times; index++) await new Promise((resolve) => setImmediate(resolve));
}

/** Internal handles the production wiring owns (read-only test access). */
function internals(app: App): {
	editor: { getText(): string; setText(text: string): void; insertTextAtCursor(text: string): void };
	integration?: {
		store: {
			documents(): Array<{ id: string; uri: string; text: string; version: number; dirty: boolean }>;
			getDocument(id: string): { id: string; uri: string; text: string; version: number; dirty: boolean } | undefined;
			insertText(id: string, text: string): unknown;
			setSelection(id: string, selection: { cursorOffset: number; anchorOffset?: number }): unknown;
			getViewState(id: string): { cursorOffset: number; selectionAnchorOffset?: number } | undefined;
		};
		tabs: { activeDocumentId(): string | null };
		controller: { phase: string };
	};
	workspaceLayoutActive: boolean;
	activeRegion: string;
	running: boolean;
	openWorkspacePath(path: string, options?: { preview?: boolean }): Promise<void>;
	exiting: boolean;
} {
	return app as unknown as ReturnType<typeof internals>;
}

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

async function waitForWorkspace(app: App): Promise<void> {
	for (let index = 0; index < 200; index++) {
		const view = internals(app);
		if (view.integration !== undefined && view.workspaceLayoutActive) return;
		await new Promise((resolve) => setImmediate(resolve));
	}
	assert.ok(internals(app).workspaceLayoutActive, "workspace opened in the background");
}

/** Open the workspace, open src/a.ts, and select an exact offset range. */
async function openWithSelection(
	app: App,
	selection?: { start: number; end: number },
): Promise<{ documentId: string }> {
	await waitForWorkspace(app);
	await internals(app).openWorkspacePath("src/a.ts");
	await settle();
	const integration = internals(app).integration!;
	const documentId = integration.tabs.activeDocumentId()!;
	if (selection) {
		const record = integration.store.getDocument(documentId)!;
		assert.ok(
			selection.start >= 0 && selection.end > selection.start && selection.end <= record.text.length,
			"fixture selection resolves in the document",
		);
		integration.store.setSelection(documentId, {
			cursorOffset: selection.end,
			anchorOffset: selection.start,
		});
	}
	return { documentId };
}

test("no workspace: one honest notice, draft untouched, nothing submitted", async () => {
	const previous = process.env.MACHINA_WORKSPACE;
	process.env.MACHINA_WORKSPACE = "0";
	const { app, client } = makeApp();
	const notices = captureNotices(app);
	try {
		await app.start();
		await settle();
		await runCommand({ name: "attach", args: "" }, app);
		assert.equal(notices.at(-1)?.text, "Workspace is not available.");
		assert.equal(notices.at(-1)?.kind, "warning");
		assert.equal(internals(app).editor.getText(), "", "draft untouched");
		assert.equal(client.emits(), 0, "nothing submitted");
	} finally {
		if (previous === undefined) delete process.env.MACHINA_WORKSPACE;
		else process.env.MACHINA_WORKSPACE = previous;
		await app.exit();
	}
});

test("no file and no selection: the exact no-selection notice, nothing inserted", async () => {
	const { app, client } = makeApp();
	const notices = captureNotices(app);
	await app.start();
	await waitForWorkspace(app);

	// No document open at all.
	await runCommand({ name: "attach", args: "" }, app);
	assert.equal(notices.at(-1)?.text, "No file is open to attach a selection from.");

	// A file open with only a caret (no anchor): empty selection.
	await openWithSelection(app);
	await runCommand({ name: "attach", args: "" }, app);
	assert.equal(notices.at(-1)?.text, "No selection to attach");
	assert.equal(notices.at(-1)?.kind, "warning");
	assert.equal(internals(app).editor.getText(), "", "draft stays empty");
	assert.equal(client.emits(), 0, "nothing submitted");
	await app.exit();
});

test("selection attaches the exact fenced block with a visible unsaved scope", async () => {
	const { app, client } = makeApp();
	const notices = captureNotices(app);
	await app.start();
	const { documentId } = await openWithSelection(app);
	const integration = internals(app).integration!;
	// Unsaved edit at the top: version 1 and dirty (the unsaved-sharing case).
	integration.store.insertText(documentId, "const d = 4;\n");
	const record = integration.store.getDocument(documentId)!;
	assert.equal(record.dirty, true);
	// Select the "const b …" and "const c …" lines of the edited text (L3–4).
	const start = record.text.indexOf("const b = 2;");
	const end = record.text.indexOf("const c = 3;\n") + "const c = 3;\n".length;
	integration.store.setSelection(documentId, { cursorOffset: end, anchorOffset: start });

	await runCommand({ name: "attach", args: "" }, app);

	const draft = internals(app).editor.getText();
	assert.ok(draft.startsWith("<attached: src/a.ts L3–4 (unsaved v1)>\n"), `draft header: ${draft}`);
	assert.ok(
		draft.includes("```javascript\nconst b = 2;\nconst c = 3;\n```\n"),
		`exact fenced selection with language; draft=${JSON.stringify(draft)}`,
	);
	assert.ok(draft.includes("const b = 2;\nconst c = 3;\n"), "the exact selected source is present");
	assert.equal(
		notices.at(-1)?.text,
		"Attached src/a.ts L3–4 (unsaved, v1) — file:///ws/src/a.ts — review and send when ready.",
	);
	assert.equal(notices.at(-1)?.kind, "info");
	assert.equal(client.emits(), 0, "attachment never submits the message");
	// The buffer is deliberately dirty; answer the quit review without writing.
	(app as unknown as { pick: () => Promise<{ value: string }> }).pick = async () => ({ value: "discard" });
	await app.exit();
});

test("a clean selection is labelled saved and the range is exact", async () => {
	const { app, client } = makeApp();
	const notices = captureNotices(app);
	await app.start();
	// Line 3 exactly: "const c = 3;\n".
	await openWithSelection(app, { start: 26, end: 39 });
	await runCommand({ name: "attach", args: "" }, app);
	const draft = internals(app).editor.getText();
	assert.ok(draft.startsWith("<attached: src/a.ts L3 (saved)>\n"), `draft: ${draft}`);
	assert.ok(draft.includes("```javascript\nconst c = 3;\n```"), "single-line exact block");
	assert.equal(
		notices.at(-1)?.text,
		"Attached src/a.ts L3 (saved, v0) — file:///ws/src/a.ts — review and send when ready.",
	);
	assert.equal(client.emits(), 0);
	await app.exit();
});

test("attach never submits and leaves a running turn untouched", async () => {
	const { app, client } = makeApp();
	await app.start();
	// Line 1: "const a = 1;\n".
	await openWithSelection(app, { start: 0, end: 13 });
	await app.submitTurn("a turn in flight");
	assert.equal(internals(app).running, true);
	const submitted = client.emits();

	await runCommand({ name: "attach", args: "" }, app);
	assert.equal(internals(app).running, true, "running turn unaffected");
	assert.equal(client.emits(), submitted, "no prompt.submit from attaching");
	assert.ok(internals(app).editor.getText().includes("const a = 1;\n"), "block is in the draft");
	await app.exit();
});

test("the attached draft stays editable and clearable like any draft", async () => {
	const { app } = makeApp();
	await app.start();
	await openWithSelection(app, { start: 0, end: 12 });
	await runCommand({ name: "attach", args: "" }, app);
	const editor = internals(app).editor;
	assert.ok(editor.getText().includes("<attached:"));
	editor.setText("");
	assert.equal(editor.getText(), "", "the draft clears as usual");
	editor.insertTextAtCursor("plain draft");
	assert.equal(editor.getText(), "plain draft", "typing continues after clearing");
	await app.exit();
});

test("Ctrl+Shift+A (Kitty CSI-u) attaches while the editor region is focused", async () => {
	const { app, tui, client } = makeApp();
	const notices = captureNotices(app);
	await app.start();
	// Lines 2–3: "const b = 2;\nconst c = 3;\n".
	await openWithSelection(app, { start: 13, end: 39 });
	assert.equal(internals(app).activeRegion, "editor", "opening focused the editor region");

	tui.press("\x1b[97;6u"); // Ctrl+Shift+A
	assert.equal(
		notices.at(-1)?.text,
		"Attached src/a.ts L2–3 (saved, v0) — file:///ws/src/a.ts — review and send when ready.",
	);
	assert.ok(internals(app).editor.getText().includes("const b = 2;\n"));
	assert.equal(client.emits(), 0);

	// Legacy Ctrl+A (0x01) is not this binding: it must never attach.
	const draft = internals(app).editor.getText();
	tui.press("\x01");
	assert.equal(internals(app).editor.getText(), draft, "legacy Ctrl+A leaves the draft as it was");
	await app.exit();
});
