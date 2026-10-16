// Hidden-session switch-back (review round 3, F12): a buffered live tail must
// re-enter the transcript with its ACTIVE identity. Regression this pins:
// replaying the tail as historical entries duplicated the assistant text at
// message.complete, rendered a running tool as a "terminal running…" notice,
// and dropped the tool result when its completion arrived.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
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

class FakeClient {
	onEvent: ((event: GatewayEvent) => void) | undefined;
	onExit: ((code: number | null, stderr: string[]) => void) | undefined;
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	sessions: SessionInfo[] = [];
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
				return { config: { nowplaying_enabled: false } } as T;
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

test("switch-back reconciles a live turn instead of duplicating it", async () => {
	const { term } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new FakeClient();
	client.sessions = [MAIN, CHILD];
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	await settle();

	await app.submitTurn("audit prompt");
	client.emit(MAIN.id, "t-auto", { type: "message.start" });
	app.switchTo(CHILD, []);
	client.emit(MAIN.id, "t-auto", { type: "message.delta", text: "UNIQUE_PARTIAL_TEXT" });
	client.emit(MAIN.id, "t-auto", { type: "tool.start", id: "tool-audit", name: "terminal", args: {} });
	app.switchTo(MAIN, []);
	client.emit(MAIN.id, "t-auto", {
		type: "tool.complete", id: "tool-audit", name: "terminal",
		result: "TOOL_COMPLETED_OUTPUT", isError: false,
	});
	client.emit(MAIN.id, "t-auto", {
		type: "message.complete", text: "UNIQUE_PARTIAL_TEXT", tokens: 1, iterations: 1,
		toolCalls: 1, cancelled: false,
	});

	const view = (app as unknown as {
		transcriptView: { children: Array<{ render(width: number): string[] }> };
	}).transcriptView;
	const final = view.children
		.flatMap((child) => child.render(100))
		.map(stripTerminalSequences)
		.join("\n");
	assert.equal((final.match(/UNIQUE_PARTIAL_TEXT/g) ?? []).length, 1, "assistant text renders once");
	assert.equal(final.includes("terminal running"), false, "no stale running marker");
	assert.equal(final.includes("TOOL_COMPLETED_OUTPUT"), true, "the completed tool result is visible");
	await app.exit();
});
