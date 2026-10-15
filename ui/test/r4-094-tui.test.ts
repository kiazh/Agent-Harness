// R4-094 TUI cluster (round-4 review). Each test was written RED-first
// against the unfixed tree (see
// docs/verification/proofs/codebase_review_round4_2026-10-14/R4-094_tui.md):
//  (a) same-id re-resume of the visible session must not erase live output
//      or strand running-tool completions;
//  (b) a hidden session's turn-failure notice must survive completion and
//      replay when the user switches back;
//  (c) the composer's outer window must disclose hidden draft rows and map
//      mouse clicks through the window start;
//  (d) Ctrl+S must match under the Kitty keyboard protocol, not only \x13;
//  (e) selected rows keep >= 4.5:1 contrast on cobalt2/solarized (tokens);
//  (f) the FULL HOST notice points at the real /approvals revoke command;
//  (g) the /delegate client timeout outlasts the gateway delegation budget;
//  (h) /keys suggestions match the gateway's accepted secret allowlist;
//  (i) job prefix resolution is honest about the capped listing;
//  (j) MCP OAuth cancels the pending operation on transient failures.

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	foregroundAnsi,
	getTerminalColorMode,
	parseColor,
	stripTerminalSequences,
	TuiAltScreen,
	type Terminal,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { ComposerBox, SecretForm, TextForm } from "../src/components.ts";
import { editorTheme } from "../src/theme.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, ResumeResult, SessionInfo } from "../src/protocol.ts";
import type { FeatureHost, NoticeKind, OAuthProgressHandle, OAuthProgressOptions } from "../src/features/types.ts";

const MAIN: SessionInfo = {
	id: "11111111-1111-4111-8111-111111111111",
	title: "Main work",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const CHILD: SessionInfo = {
	id: "22222222-2222-4222-8222-222222222222",
	title: "researcher: find X",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
	parentSessionId: MAIN.id,
};

function terminal(columns = 100, rows = 40) {
	let input: (data: string) => void = () => {};
	const term: Terminal & { columns: number; rows: number } = {
		columns,
		rows,
		kittyProtocolActive: false,
		start(onInput) { input = onInput; },
		stop() {},
		async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	return { term, type: (data: string) => input(data) };
}

/** Records the per-call client timeout so (g) can assert the delegation budget. */
class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown>; timeoutMs?: number }> = [];
	sessions: SessionInfo[] = [];
	resume = new Map<string, ResumeResult>();
	autoTurnId = "t-auto";

	start(): void {}
	stderr(): string[] { return []; }
	async stop(): Promise<void> {}

	async request<T>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
		this.requests.push({ method, params, timeoutMs });
		switch (method) {
			case "initialize":
				return { version: "test", model: "m", provider: "p", cwd: "/tmp", branch: "main" } as T;
			case "config.get":
				return { config: {} } as T;
			case "mode.get":
				return { mode: "ask" } as T;
			case "profile.get":
				return {} as T;
			case "session.create":
				return { session: this.sessions[0] ?? MAIN } as T;
			case "session.list":
				return { sessions: this.sessions } as T;
			case "session.resume": {
				const fixture = this.resume.get(String(params.sessionId));
				if (fixture) return fixture as T;
				const session = this.sessions.find((candidate) => candidate.id === params.sessionId);
				if (!session) throw new Error(`no resume fixture for ${params.sessionId}`);
				return { session, history: [] } as T;
			}
			case "prompt.submit":
				return { turnId: this.autoTurnId } as T;
			case "agents.run":
				return { results: [{ agent: "a", task: "t", response: "", sessionId: null, tokens: 0, iterations: 0, status: "ok" }] } as T;
			case "prompt.cancel":
			case "approvals.resolve":
				return {} as T;
			default:
				throw new Error(`unexpected request ${method}`);
		}
	}

	emit(sessionId: string, turnId: string, event: Record<string, unknown>): void {
		this.onEvent?.({ sessionId, turnId, ...event } as GatewayEvent);
	}
}

async function settle(times = 8): Promise<void> {
	for (let index = 0; index < times; index++) await new Promise((resolve) => setImmediate(resolve));
}

async function boot(options: { sessions?: SessionInfo[]; resume?: Array<[string, ResumeResult]> } = {}) {
	const { term, type } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new FakeClient();
	client.sessions = options.sessions ?? [MAIN];
	for (const [id, result] of options.resume ?? []) client.resume.set(id, result);
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return { app, tui, type, client };
}

/** The visible transcript rendered as plain text. */
function transcriptText(app: App): string {
	const view = (app as unknown as {
		transcriptView: { children: Array<{ render(width: number): string[] }> };
	}).transcriptView;
	return view.children
		.flatMap((child) => child.render(100))
		.map(stripTerminalSequences)
		.join("\n");
}

// ─── (a) same-id re-resume guard ───────────────────────────────────────────

test("R4-094(a) re-resuming the visible session mid-turn keeps live output and tool completions", async (t) => {
	const { app, client } = await boot();
	t.after(() => app.exit());
	await settle();

	await app.submitTurn("go");
	client.emit(MAIN.id, "t-auto", { type: "message.start" });
	client.emit(MAIN.id, "t-auto", { type: "message.delta", text: "UNIQUE_LIVE_TEXT" });
	client.emit(MAIN.id, "t-auto", { type: "tool.start", id: "tool-live", name: "terminal", args: { command: "sleep 9" } });

	// `/resume <current>` (or picking the current row in a session list):
	// session.resume cannot return the in-flight turn, so replaying the
	// fetched history here would erase everything streamed so far.
	app.switchTo(MAIN, []);
	assert.equal(app.session()?.id, MAIN.id, "same-id resume stays on the session");
	assert.match(transcriptText(app), /UNIQUE_LIVE_TEXT/, "live streamed text survives the same-id re-resume");
	assert.match(transcriptText(app), /terminal/, "the running tool card survives");

	client.emit(MAIN.id, "t-auto", { type: "tool.complete", id: "tool-live", name: "terminal", result: "UNIQUE_TOOL_OUTPUT", isError: false });
	client.emit(MAIN.id, "t-auto", { type: "message.complete", text: "UNIQUE_LIVE_TEXT", tokens: 3, iterations: 1, toolCalls: 1, cancelled: false });
	const final = transcriptText(app);
	assert.match(final, /UNIQUE_TOOL_OUTPUT/, "the running tool's completion reconciles with its card");
	assert.equal((final.match(/UNIQUE_LIVE_TEXT/g) ?? []).length, 1, "assistant text renders exactly once");
});

test("R4-094(a2) switching to a different session still rebuilds from fetched history", async (t) => {
	const history = [{ role: "assistant" as const, content: "FETCHED_HISTORY_TEXT" }];
	const { app, client } = await boot({ sessions: [MAIN, CHILD], resume: [[CHILD.id, { session: CHILD, history }]] });
	t.after(() => app.exit());
	await settle();

	await app.submitTurn("go");
	client.emit(MAIN.id, "t-auto", { type: "message.start" });
	client.emit(MAIN.id, "t-auto", { type: "message.delta", text: "MAIN_LIVE_TEXT" });

	app.switchTo(CHILD, history);
	assert.equal(app.session()?.id, CHILD.id);
	const child = transcriptText(app);
	assert.match(child, /FETCHED_HISTORY_TEXT/, "a real switch replays the fetched history");
	assert.doesNotMatch(child, /MAIN_LIVE_TEXT/, "the other session's live text stays out of view");
});

// ─── (b) hidden turn-failure notices ───────────────────────────────────────

test("R4-094(b) a hidden session's turn failure survives completion and replays on switch-back", async (t) => {
	const { app, client } = await boot({ sessions: [MAIN, CHILD], resume: [[CHILD.id, { session: CHILD, history: [] }]] });
	t.after(() => app.exit());
	await settle();

	app.switchTo(CHILD, []);
	client.emit(MAIN.id, "t-fail", { type: "message.start" });
	client.emit(MAIN.id, "t-fail", { type: "error", message: "UNIQUE_FAILURE_NOTICE" });
	client.emit(MAIN.id, "t-fail", { type: "message.complete", text: "", tokens: 2, iterations: 1, toolCalls: 0, cancelled: false });

	app.switchTo(MAIN, []);
	assert.match(transcriptText(app), /UNIQUE_FAILURE_NOTICE/, "the failure notice survives completion and switch-back");
});

// ─── (c) composer window disclosure + mouse mapping ────────────────────────

test("R4-094(c) the composer window discloses hidden draft rows", () => {
	const box = new ComposerBox({ terminal: { rows: 40, columns: 80 }, requestRender() {}, invalidate() {} } as never, editorTheme);
	box.setMaxRows(4);
	box.editor.setText(Array.from({ length: 10 }, (_, index) => `line${index}`).join("\n"));
	const rows = box.render(60).map(stripTerminalSequences);
	assert.ok(rows.some((row) => /↑ \d+ more/.test(row)), "hidden rows above the window are disclosed");
	const visible = rows.filter((row) => /line\d+/.test(row)).length;
	assert.ok(visible >= 1, "the window keeps visible draft rows");
});

test("R4-094(c2) mouse clicks map through the composer window start", () => {
	const box = new ComposerBox({ terminal: { rows: 40, columns: 80 }, requestRender() {}, invalidate() {} } as never, editorTheme);
	box.setMaxRows(4);
	box.editor.setText(Array.from({ length: 10 }, (_, index) => `line${index}`).join("\n"));
	const rows = box.render(60).map(stripTerminalSequences);
	const rowIndex = rows.findIndex((row) => row.includes("line8"));
	assert.ok(rowIndex >= 0, "the target row is inside the window");
	const click = {
		type: "click", button: "left", x: 5, y: rowIndex,
		screenX: 5, screenY: rowIndex, width: 60, height: rows.length,
		shift: false, alt: false, ctrl: false,
	} as never;
	box.handleMouse(click);
	assert.equal(box.editor.getCursor().line, 8, "the click lands on the row it was drawn on");
});

// ─── (d) Kitty Ctrl+S ──────────────────────────────────────────────────────

test("R4-094(d) Ctrl+S saves the text and secret forms under the Kitty protocol", () => {
	const { term } = terminal(80, 24);
	const tui = new TuiAltScreen(term);
	try {
		const saved: string[] = [];
		const form = new TextForm(tui, editorTheme, { title: "Persona" });
		form.onSubmit = (value) => void saved.push(value);
		form.editor.setText("kitty save");
		form.handleInput("\x1b[115;5u"); // Kitty CSI-u ctrl+s
		assert.deepEqual(saved, ["kitty save"], "Kitty ctrl+s saves");
		form.setError("retry");
		form.handleInput("\x13"); // legacy ctrl+s
		assert.deepEqual(saved, ["kitty save", "kitty save"], "legacy ctrl+s still saves");

		const secretSaved: string[] = [];
		const secret = new SecretForm(tui, { title: "Set K" });
		secret.onSubmit = (value) => void secretSaved.push(value);
		secret.input.setValue("sk-kitty");
		secret.handleInput("\x1b[115;5u");
		assert.deepEqual(secretSaved, ["sk-kitty"], "the secret form saves on Kitty ctrl+s");
	} finally {
		tui.stop();
	}
});

// ─── (e) selection-row contrast tokens ─────────────────────────────────────

test("R4-094(e) selected rows keep >= 4.5:1 contrast on every skin (cobalt2/solarized included)", async () => {
	const design = await import("../src/design/theme.ts");
	const selectionInk = (design as { selectionInk?: (name?: string) => string }).selectionInk;
	const selectionFill = (design as { selectionFill?: (name?: string) => string }).selectionFill;
	assert.ok(selectionInk, "selectionInk token exists");
	assert.ok(selectionFill, "selectionFill token exists");
	const luminance = (hex: string): number => {
		const [r, g, b] = hex.slice(1).match(/../g)!.map((pair) => {
			const channel = Number.parseInt(pair, 16) / 255;
			return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
		});
		return r! * 0.2126 + g! * 0.7152 + b! * 0.0722;
	};
	const contrast = (a: string, b: string): number => {
		const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
		return (values[0]! + 0.05) / (values[1]! + 0.05);
	};
	for (const name of design.SKIN_NAMES) {
		const ratio = contrast(selectionInk(name), selectionFill(name));
		assert.ok(ratio >= 4.5, `${name} selection row contrast ${ratio.toFixed(2)}:1`);
	}

	const previous = design.getSkin();
	try {
		design.setSkin("cobalt2");
		const bar = design.selectionBar("alpha", 20);
		const ink = foregroundAnsi(parseColor(selectionInk("cobalt2")), getTerminalColorMode());
		assert.ok(bar.includes(ink), "selectionBar paints the derived ink");
	} finally {
		design.setSkin(previous);
	}
});

// ─── (f) /mode FULL HOST copy ──────────────────────────────────────────────

test("R4-094(f) the FULL HOST notice points at /approvals revoke", async () => {
	const { modeCommand } = await import("../src/features/mode.ts");
	const host = new MiniHost();
	host.responses["mode.set"] = { mode: "full", backend: "docker" };
	host.picks.push("session");
	await modeCommand.run("full", host as unknown as FeatureHost);
	const out = host.printed.join("\n");
	assert.match(out, /\/approvals revoke/, "points at the command that revokes grants");
	assert.doesNotMatch(out, /\/mode revoke/, "the nonexistent /mode revoke copy is gone");
});

// ─── (g) /delegate client budget ───────────────────────────────────────────

test("R4-094(g) the /delegate client timeout outlasts the 120s gateway delegation budget", async (t) => {
	const { app, client } = await boot();
	t.after(() => app.exit());
	await settle();

	const request = (app as unknown as { request(method: string, params?: Record<string, unknown>): Promise<unknown> }).request.bind(app);
	await request("agents.run", { sessionId: MAIN.id, steps: [{ agent: "a", task: "t" }] });
	const call = client.requests.find((item) => item.method === "agents.run");
	assert.ok(call, "the delegation request was dispatched");
	assert.ok(
		(call.timeoutMs ?? 0) > 120_000,
		`client timeout ${call.timeoutMs}ms must exceed the orchestrator's DELEGATION_TIMEOUT_SECONDS=120`,
	);
});

// ─── (h) /keys allowlist ───────────────────────────────────────────────────

test("R4-094(h) /keys suggestions match the gateway's accepted secret allowlist", async () => {
	const { keysCommand } = await import("../src/features/keys.ts");
	const completions = keysCommand.getArgumentCompletions;
	assert.ok(completions, "keys command offers completions");
	const result = completions("set ");
	const items: Array<{ value: string }> = Array.isArray(result) ? result : [];
	const suggested = items.map((item) => item.value);
	// Source of truth: machina/gateway/features/secrets.py `_ALLOWED_SECRET_KEYS`
	// (PROVIDER_KEYS + MANAGED_ENV_KEYS in machina/security/env_file.py).
	const gateway = [
		"OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
		"MISTRAL_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "DEEPSEEK_API_KEY",
		"XAI_API_KEY", "COHERE_API_KEY", "DATABASE_URL", "SEARXNG_URL", "MACHINA_MODEL",
	];
	for (const key of suggested) {
		assert.ok(gateway.includes(key), `/keys suggests ${key}, which secrets.set refuses`);
	}
	for (const key of gateway) {
		assert.ok(suggested.includes(key), `/keys omits accepted key ${key}`);
	}
});

// ─── (i) job prefix pagination honesty ─────────────────────────────────────

test("R4-094(i) /jobs prefix resolution is honest about the capped listing", async () => {
	const { jobsCommand } = await import("../src/features/jobs.ts");
	const host = new MiniHost();
	host.responses["jobs.list"] = {
		jobs: Array.from({ length: 500 }, (_, index) => ({ id: `job-${String(index).padStart(4, "0")}` })),
	};
	await assert.rejects(
		() => jobsCommand.run("off deadbeef", host as unknown as FeatureHost),
		/newest 500|full job id/i,
	);
	const lists = host.calls.filter((call) => call.method === "jobs.list");
	assert.equal(lists.length, 1, "one bounded listing, no dead cursor loop");
	assert.equal(lists[0]!.params.cursor, undefined, "no cursor the gateway cannot honor");
	assert.equal(lists[0]!.params.limit, 500, "asks for the gateway's documented page cap");

	const within = new MiniHost();
	within.responses["jobs.list"] = { jobs: [{ id: "job-abcdef01" }] };
	within.responses["jobs.setEnabled"] = {};
	await jobsCommand.run("off job-abc", within as unknown as FeatureHost);
	assert.equal(within.calls.at(-1)!.params.id, "job-abcdef01", "an in-page prefix still resolves");
});

// ─── (j) MCP OAuth cancel on transient failures ────────────────────────────

const OAUTH_SESSION: SessionInfo = {
	id: "33333333-3333-4333-8333-333333333333",
	title: "current",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

class OAuthHost {
	current: SessionInfo | undefined = OAUTH_SESSION;
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	picks: Array<string | undefined> = [];
	responses: Record<string, unknown | ((params: Record<string, unknown>) => unknown)> = {};

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.calls.push({ method, params });
		const response = this.responses[method];
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		return (typeof response === "function" ? (response as (p: Record<string, unknown>) => unknown)(params) : response) as T;
	}
	print(text: string, kind: NoticeKind = "info") {
		this.printed.push({ text, kind });
	}
	async pick(_title: string, items: Array<{ value: string }>) {
		const value = this.picks.shift();
		return items.find((item) => item.value === value);
	}
	session() {
		return this.current;
	}
	showOAuthProgress(_options: OAuthProgressOptions): OAuthProgressHandle {
		return { update: () => {}, dispose: () => {} };
	}
}

function oauthHost(): OAuthHost {
	const host = new OAuthHost();
	host.responses["mcp.oauth.preview"] = {
		previewId: "preview-1",
		digest: "digest-1",
		review: {
			connectorId: "docs", revision: 1, endpoint: "https://mcp.example.com/mcp",
			resource: "https://mcp.example.com/mcp", issuer: "https://auth.example.com",
			requestedScopes: ["read"], registrationMode: "dcr",
			endpoints: { authorization: "https://auth.example.com/authorize", token: "https://auth.example.com/token" },
			redirectUri: "http://127.0.0.1:9999/oauth/callback/x",
		},
	};
	host.responses["mcp.oauth.start"] = {
		operation: { operationId: "op-1", connectorId: "docs", state: "preparing", expiresAt: new Date().toISOString() },
	};
	host.responses["mcp.oauth.cancel"] = { cancelled: true };
	return host;
}

test("R4-094(j) exhausted transient status failures cancel the pending operation", async () => {
	const { authorizeConnector } = await import("../src/features/mcp-oauth.ts");
	const host = oauthHost();
	host.responses["mcp.oauth.status"] = () => { throw new Error("temporary network failure"); };
	host.picks.push("yes");
	await authorizeConnector(host as unknown as FeatureHost, "docs");
	const cancel = host.calls.find((call) => call.method === "mcp.oauth.cancel");
	assert.ok(cancel, "the pending operation is cancelled");
	assert.equal(cancel.params.operationId, "op-1");
	assert.equal(host.calls.filter((call) => call.method === "mcp.oauth.status").length, 3, "bounded retries");
	assert.equal(host.printed.at(-1)?.kind, "error");
});

test("R4-094(j2) a failed login-link load cancels the pending operation", async () => {
	const { authorizeConnector } = await import("../src/features/mcp-oauth.ts");
	const host = oauthHost();
	host.responses["mcp.oauth.status"] = {
		auth: { state: "signed_out", generation: 0 },
		operation: { operationId: "op-1", connectorId: "docs", state: "awaiting_browser", expiresAt: new Date().toISOString() },
	};
	host.responses["mcp.oauth.url"] = () => { throw new Error("transient rpc failure"); };
	host.picks.push("yes");
	await authorizeConnector(host as unknown as FeatureHost, "docs");
	const cancel = host.calls.find((call) => call.method === "mcp.oauth.cancel");
	assert.ok(cancel, "the pending operation is cancelled after the link fails");
	assert.equal(cancel.params.operationId, "op-1");
	assert.equal(host.printed.at(-1)?.kind, "error");
});

test("R4-094(j3) a start failure cancels the reviewed preview", async () => {
	const { authorizeConnector } = await import("../src/features/mcp-oauth.ts");
	const host = oauthHost();
	host.responses["mcp.oauth.start"] = () => { throw new Error("transient rpc failure"); };
	host.picks.push("yes");
	await authorizeConnector(host as unknown as FeatureHost, "docs");
	const cancel = host.calls.find((call) => call.method === "mcp.oauth.cancel");
	assert.ok(cancel, "the preview is released when start fails");
	assert.equal(cancel.params.previewId, "preview-1");
	assert.equal(host.printed.at(-1)?.kind, "error");
});

// ─── shared mini host (mode/keys/jobs command tests) ───────────────────────

class MiniHost {
	current: SessionInfo | undefined = MAIN;
	printed: string[] = [];
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	picks: Array<string | undefined> = [];
	responses: Record<string, unknown | ((params: Record<string, unknown>) => unknown)> = {};

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.calls.push({ method, params });
		const response = this.responses[method];
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		return (typeof response === "function" ? (response as (p: Record<string, unknown>) => unknown)(params) : response) as T;
	}
	print(text: string) {
		this.printed.push(text);
	}
	async pick(_title: string, items: Array<{ value: string }>) {
		const value = this.picks.shift();
		return items.find((item) => item.value === value);
	}
	session() {
		return this.current;
	}
}
