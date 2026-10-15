// H24 (holistic review 2026-10-15): /delegate must model a paused
// (waiting_approval) child as a distinct UI state.
//
// RED before fix: delegateCommand treated every non-error status as ordinary
// output, so a child paused for approval rendered like a finished response —
// the approval actions and the child-session continuation target were
// discarded. GREEN: a distinct warning state renders the approval table,
// resolves approvals against the child session, and offers switching to it.

import assert from "node:assert/strict";
import { test } from "node:test";
import type { SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { runCommand, type FeatureHost, type NoticeKind } from "../src/features/index.ts";
import type { ConfigResult, HistoryEntry, SessionInfo } from "../src/protocol.ts";

const MAIN: SessionInfo = {
	id: "11111111-2222-3333-4444-555555555555",
	title: "main",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const CHILD = "22222222-3333-4444-5555-666666666666";
const CHILD_SHORT = "22222222";

class FakeHost implements FeatureHost {
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	markdown: string[] = [];
	picks: Array<string | undefined> = [];
	pickTitles: string[] = [];
	pickItems: SelectItem[][] = [];
	switched: Array<{ session: SessionInfo; history: HistoryEntry[] }> = [];
	responses: Record<string, unknown | ((params: Record<string, unknown>) => unknown)> = {};
	current: SessionInfo | undefined = MAIN;

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.calls.push({ method, params });
		const response = this.responses[method];
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		return (typeof response === "function" ? (response as (p: Record<string, unknown>) => unknown)(params) : response) as T;
	}
	print(text: string, kind: NoticeKind = "info") {
		this.printed.push({ text, kind });
	}
	printMarkdown(text: string) {
		this.markdown.push(text);
	}
	session() {
		return this.current;
	}
	async submitTurn() {}
	switchTo(session: SessionInfo, history: HistoryEntry[]) {
		this.current = session;
		this.switched.push({ session, history });
	}
	updateSession(session: SessionInfo) {
		this.current = session;
	}
	async pick(title: string, items: SelectItem[]) {
		this.pickTitles.push(title);
		this.pickItems.push(items);
		const value = this.picks.shift();
		return items.find((i) => i.value === value);
	}
	async pickMany() {
		return undefined;
	}
	async pickTheme() {
		return undefined;
	}
	async askText() {
		return undefined;
	}
	showOAuthProgress() {
		return { update() {}, dispose() {} };
	}
	async inspectOutput() {}
	profileId() {
		return "default";
	}
	setProfile() {}
	sessionAgent() {
		return undefined;
	}
	async writeFile(path: string) {
		return `/abs/${path}`;
	}
	onConfig(_result: ConfigResult) {}
	clear() {}
	banner() {}
	restyle() {}
	async exit() {}

	last(): { text: string; kind: NoticeKind } {
		return this.printed.at(-1)!;
	}
}

const run = (text: string, host: FakeHost) => runCommand(parseCommand(text)!, host);

function pausedPayload() {
	return {
		results: [
			{
				agent: "researcher",
				task: "find X",
				response: "waiting for approval before running the command",
				sessionId: CHILD,
				tokens: 12,
				iterations: 1,
				status: "waiting_approval",
				needsApproval: [
					{
						requestId: "aaaaaaaa-1111-4000-a000-000000000001",
						operation: "terminal",
						target: "rm -rf /tmp/scratch",
						status: "pending",
						sessionId: CHILD,
						agentId: "researcher",
						turnId: "child-turn",
					},
				],
			},
		],
	};
}

test("/delegate renders a paused child as a distinct approval state", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = pausedPayload();
	host.picks.push(undefined);
	await run("/delegate researcher find X", host);

	// Not an ordinary (finished) answer.
	assert.equal(host.markdown.length, 0, "a paused child is not a finished answer");
	const warning = host.printed.find((p) => p.kind === "warning");
	assert.ok(warning, "the pause renders as a warning notice");
	assert.match(warning.text, /waiting for approval/);
	assert.match(warning.text, /did not finish/);

	// Approval details are rendered (request id, operation, target).
	const details = host.printed.map((p) => p.text).join("\n");
	assert.match(details, /aaaaaaaa/);
	assert.match(details, /terminal/);
	assert.match(details, /rm -rf \/tmp\/scratch/);

	// The child session stays the continuation target.
	const items = host.pickItems.at(-1)!;
	assert.ok(
		items.some((i) => `${i.label} ${i.description ?? ""}`.includes(CHILD_SHORT)),
		"the child session id must remain visible as the continuation target",
	);
});

test("/delegate paused state offers approval and continuation actions", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = pausedPayload();
	host.picks.push(undefined);
	await run("/delegate researcher find X", host);
	const values = host.pickItems.at(-1)!.map((i) => i.value);
	assert.ok(values.includes("allow:aaaaaaaa-1111-4000-a000-000000000001"), "approve action");
	assert.ok(values.includes("deny:aaaaaaaa-1111-4000-a000-000000000001"), "deny action");
	assert.ok(values.includes("switch"), "child-session continuation action");
});

test("/delegate approving a paused child resolves against the child session", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = pausedPayload();
	host.responses["approvals.resolve"] = {};
	host.picks.push("allow:aaaaaaaa-1111-4000-a000-000000000001");
	await run("/delegate researcher find X", host);
	const resolve = host.calls.find((c) => c.method === "approvals.resolve");
	assert.ok(resolve, "approval must be resolved through the gateway");
	assert.deepEqual(resolve.params, {
		requestId: "aaaaaaaa-1111-4000-a000-000000000001",
		sessionId: CHILD,
		verdict: "approved",
	});
	assert.match(host.last().text, /[Aa]llowed/);
	assert.match(host.last().text, new RegExp(CHILD_SHORT));
});

test("/delegate denying a paused child resolves a denial against the child session", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = pausedPayload();
	host.responses["approvals.resolve"] = {};
	host.picks.push("deny:aaaaaaaa-1111-4000-a000-000000000001");
	await run("/delegate researcher find X", host);
	const resolve = host.calls.find((c) => c.method === "approvals.resolve");
	assert.deepEqual(resolve!.params, {
		requestId: "aaaaaaaa-1111-4000-a000-000000000001",
		sessionId: CHILD,
		verdict: "denied",
	});
	assert.match(host.last().text, /[Dd]enied/);
});

test("/delegate switching to the paused child resumes it as the current session", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = pausedPayload();
	host.responses["session.resume"] = {
		session: { ...MAIN, id: CHILD, title: "researcher: find X" },
		history: [{ role: "assistant", content: "paused for approval" }],
	};
	host.picks.push("switch");
	await run("/delegate researcher find X", host);
	const resume = host.calls.find((c) => c.method === "session.resume");
	assert.deepEqual(resume!.params, { sessionId: CHILD });
	assert.equal(host.switched.at(-1)!.session.id, CHILD);
	assert.match(host.last().text, /Find the agent sessions|resolve the approval/i);
});

test("/delegate leaving the pause keeps the child-session continuation target", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = pausedPayload();
	host.picks.push("leave");
	await run("/delegate researcher find X", host);
	assert.equal(host.calls.filter((c) => c.method === "approvals.resolve").length, 0);
	assert.match(host.last().text, /[Pp]aused/);
	assert.match(host.last().text, new RegExp(CHILD_SHORT));
});

test("/delegate complete and error states keep their existing rendering", async () => {
	const done = new FakeHost();
	done.responses["agents.run"] = {
		results: [{ agent: "researcher", task: "find X", response: "found X", sessionId: CHILD, tokens: 20, iterations: 1, status: "complete" }],
	};
	await run("/delegate researcher find X", done);
	assert.match(done.markdown.at(-1)!, /found X/);
	assert.equal(done.printed.at(-1)!.kind, "plain");

	const failed = new FakeHost();
	failed.responses["agents.run"] = {
		results: [{ agent: "researcher", task: "find X", response: "boom", sessionId: CHILD, tokens: 0, iterations: 0, status: "error" }],
	};
	await run("/delegate researcher find X", failed);
	assert.match(failed.last().text, /researcher failed: boom/);
	assert.equal(failed.last().kind, "error");
});
