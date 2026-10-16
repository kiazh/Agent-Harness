// R5-D (finding 8) RED-first: the mode indicator must reflect the authority
// governing execution — the ACTIVE session's effective mode, never the
// global default.
//
//  D1a: bare `/mode` must pass the active session ID on `mode.get`, so the
//       gateway resolves session authority instead of the global default.
//  D1b: switching sessions must refresh the effective mode (footer) for the
//       newly visible session.
//
// Both tests FAIL on the unfixed tree (RED) and pass after the fix (GREEN).

import assert from "node:assert/strict";
import { test } from "node:test";
import { TuiAltScreen } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { modeCommand } from "../src/features/mode.ts";
import type { FeatureHost } from "../src/features/types.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { GatewayEvent, ResumeResult, SessionInfo } from "../src/protocol.ts";

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
	title: "second session",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

function terminal(columns = 100, rows = 40) {
	let input: (data: string) => void = () => {};
	const term = {
		columns,
		rows,
		kittyProtocolActive: false,
		start(onInput: (data: string) => void) { input = onInput; },
		stop() {},
		async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	return { term, type: (data: string) => input(data) };
}

/** Records mode.get params; answers per-session authority (MAIN=full, else ask). */
class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown>; timeoutMs?: number }> = [];
	sessions: SessionInfo[] = [MAIN, CHILD];
	resume = new Map<string, ResumeResult>([
		[MAIN.id, { session: MAIN, history: [] }],
		[CHILD.id, { session: CHILD, history: [] }],
	]);

	start(): void {}
	stderr(): string[] { return []; }
	async stop(): Promise<void> {}

	async request<T>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
		this.requests.push({ method, params, timeoutMs });
		switch (method) {
			case "initialize":
				return { version: "test", model: "m", provider: "p", cwd: "/tmp", branch: "main" } as T;
			case "config.get":
				return { config: { nowplaying_enabled: false } } as T;
			case "mode.get": {
				// Session authority: MAIN runs full, everything else (and the
				// session-less query) falls back to the global default ask.
				const sid = params.sessionId ?? params.session_id;
				return { mode: sid === MAIN.id ? "full" : "ask", backend: "host" } as T;
			}
			case "profile.get":
				return {} as T;
			case "session.create":
				return { session: MAIN } as T;
			case "session.resume": {
				const fixture = this.resume.get(String(params.sessionId));
				if (!fixture) throw new Error(`no resume fixture for ${params.sessionId}`);
				return fixture as T;
			}
			default:
				throw new Error(`unexpected request ${method}`);
		}
	}
}

async function settle(times = 8): Promise<void> {
	for (let index = 0; index < times; index++) await new Promise((resolve) => setImmediate(resolve));
}

async function boot() {
	const { term } = terminal();
	const tui = new TuiAltScreen(term as never);
	const client = new FakeClient();
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	await settle();
	return { app, tui, client };
}

class MiniHost {
	current: SessionInfo | undefined = MAIN;
	printed: string[] = [];
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	picks: Array<string | undefined> = [];
	responses: Record<string, unknown> = {
		"mode.get": { mode: "full", backend: "host" },
	};

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.calls.push({ method, params });
		const response = this.responses[method];
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		return response as T;
	}
	print(text: string) {
		this.printed.push(text);
	}
	async pick(_title: string, _items: Array<{ value: string }>) {
		return undefined;
	}
	session() {
		return this.current;
	}
}

// ─── D1a ────────────────────────────────────────────────────────────────────

test("R5-D1a bare /mode passes the active session ID on mode.get", async () => {
	const host = new MiniHost();
	await modeCommand.run("", host as unknown as FeatureHost);
	const call = host.calls.find((item) => item.method === "mode.get");
	assert.ok(call, "bare /mode queries mode.get");
	assert.equal(
		(call.params.sessionId ?? call.params.session_id),
		MAIN.id,
		"status requests carry the active session so the gateway resolves session authority, not the global default",
	);
});

// ─── D1b ────────────────────────────────────────────────────────────────────

test("R5-D1b switching sessions refreshes the footer to the new session's authority", async (t) => {
	const { app, client } = await boot();
	t.after(() => app.exit());
	await settle();

	const footer = (app as unknown as { footer: { mode?: string } }).footer;
	const before = client.requests.filter((item) => item.method === "mode.get").length;
	app.switchTo(CHILD, []);
	await settle();

	const refreshes = client.requests
		.slice(before)
		.filter((item) => item.method === "mode.get");
	assert.ok(
		refreshes.some((item) => (item.params.sessionId ?? item.params.session_id) === CHILD.id),
		"session switch re-queries mode.get for the newly visible session",
	);
	assert.equal(footer.mode, "ask", "footer renders the governing session authority, not a stale mode");
});
