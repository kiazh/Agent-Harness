// Multi-agent orchestration: hidden-session live tails, lazy family
// discovery, the footer subagent count, the Down-arrow subagent menu, and
// the /agents sessions switch menu. Rendered through a real TuiAltScreen
// against a fake terminal (dialogs.test.ts pattern) so the counts and menus
// are asserted on actual screen lines.
//
// Pinned merge rule (see tail.ts): a hidden session's buffered tail is the
// conversation suffix and WINS over the freshly fetched history; matching
// trailing history entries are trimmed so a completed turn or settled tool
// renders exactly once.
//
// Delegation is non-streaming (agent.run): a delegated child never streams
// frames of its own. Discovery runs on the parent's `delegate` tool.complete
// or a turn-start frame from an unfamiliar session — never on every frame.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { parseCommand } from "../src/commands.ts";
import { runCommand } from "../src/features/index.ts";
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
	title: "researcher: find X",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
	parentSessionId: MAIN.id,
};

const CHILD_B: SessionInfo = {
	id: "44444444-4444-4444-8444-444444444444",
	title: "researcher: find Y",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
	parentSessionId: MAIN.id,
};

const UNRELATED: SessionInfo = {
	id: "33333333-3333-4333-8333-333333333333",
	title: "Other chat",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
	parentSessionId: null,
};

function terminal(columns = 100, rows = 30) {
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

class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	/** session.list page content (newest first); replaced per test. */
	sessions: SessionInfo[] = [];
	/** session.resume responses keyed by session id. */
	resume = new Map<string, ResumeResult>();
	autoTurnId = "t-auto";

	start(): void {}
	stderr(): string[] { return []; }
	async stop(): Promise<void> {}

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
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
				// Default: resume any listed session with empty history so tab
				// cycling round-trips mirror the real gateway.
				const session = this.sessions.find((candidate) => candidate.id === params.sessionId);
				if (!session) throw new Error(`no resume fixture for ${params.sessionId}`);
				return { session, history: [] } as T;
			}
			case "prompt.submit":
				return { turnId: this.autoTurnId } as T;
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

function screenOf(tui: TuiAltScreen): string {
	return tui.getScreenLines().map(stripTerminalSequences).join("\n");
}

interface BootOptions {
	sessions?: SessionInfo[];
	resume?: Array<[string, ResumeResult]>;
}

async function boot(options: BootOptions = {}) {
	const { term, type } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new FakeClient();
	client.sessions = options.sessions ?? [MAIN];
	for (const [id, result] of options.resume ?? []) client.resume.set(id, result);
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return { app, tui, type, client, term };
}

function internals(app: App) {
	return app as unknown as {
		inFlight: Set<string>;
		turns: Map<string, { turnId?: string }>;
		sessionTokensById: Map<string, number>;
		sessionTails: Map<string, { blocks: unknown[] }>;
		footer: { tokens: number };
		editor: { getText(): string; isShowingAutocomplete(): boolean };
	};
}


const { app, client } = await boot({ sessions: [MAIN, CHILD] });
await settle();
await app.submitTurn("audit prompt");
client.emit(MAIN.id, "t-auto", { type: "message.start" });
app.switchTo(CHILD, []);
client.emit(MAIN.id, "t-auto", { type: "message.delta", text: "UNIQUE_PARTIAL_TEXT" });
client.emit(MAIN.id, "t-auto", { type: "tool.start", id: "tool-audit", name: "terminal", args: {} });
app.switchTo(MAIN, []);
client.emit(MAIN.id, "t-auto", { type: "tool.complete", id: "tool-audit", name: "terminal", result: "TOOL_COMPLETED_OUTPUT", isError: false });
client.emit(MAIN.id, "t-auto", { type: "message.complete", text: "UNIQUE_PARTIAL_TEXT", tokens: 1, iterations: 1, toolCalls: 1, cancelled: false });
const view = (app as unknown as { transcriptView: {children: Array<{render(width:number): string[]}>} }).transcriptView;
const final = view.children.flatMap(c => c.render(100)).map(stripTerminalSequences).join("\n");
console.log(JSON.stringify({ partialCopies: (final.match(/UNIQUE_PARTIAL_TEXT/g) ?? []).length, staleRunning: final.includes("terminal running"), completedToolVisible: final.includes("TOOL_COMPLETED_OUTPUT") }));
console.log(final);
await app.exit();
