import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, type SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { type AskTextOptions, type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "../src/features/index.ts";
import { resolvePrefix, table } from "../src/format.ts";
import type { ConfigResult, HistoryEntry, SessionInfo } from "../src/protocol.ts";
import type { OAuthProgressHandle, OAuthProgressOptions } from "../src/features/types.ts";

const SESSION: SessionInfo = {
	id: "11111111-2222-3333-4444-555555555555",
	title: "current",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

class FakeHost implements FeatureHost {
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	markdown: string[] = [];
	files = new Map<string, string>();
	picks: Array<string | undefined> = [];
	pickTitles: string[] = [];
	manyQueue: Array<string[] | undefined> = [];
	manyItems: SelectItem[][] = [];
	textQueue: Array<string | undefined> = [];
	asked: AskTextOptions[] = [];
	current: SessionInfo | undefined = SESSION;
	switched: Array<{ session: SessionInfo; history: HistoryEntry[] }> = [];
	responses: Record<string, unknown | ((params: Record<string, unknown>) => unknown)> = {};
	configured: ConfigResult[] = [];

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
	submitted: string[] = [];
	async submitTurn(text: string) {
		this.submitted.push(text);
	}
	switchTo(session: SessionInfo, history: HistoryEntry[]) {
		this.current = session;
		this.switched.push({ session, history });
	}
	updateSession(session: SessionInfo) {
		this.current = session;
	}
	async pick(title: string, items: SelectItem[]) {
		this.pickTitles.push(title);
		const value = this.picks.shift();
		return items.find((i) => i.value === value);
	}
	async askText(options: AskTextOptions): Promise<string | undefined> {
		this.asked.push(options);
		return this.textQueue.shift();
	}
	progressOptions: OAuthProgressOptions[] = [];
	progressUpdates: Array<{ status: string; loginUrl?: string }> = [];
	progressDisposals = 0;
	progressCallbacks: OAuthProgressOptions[] = [];
	showOAuthProgress(options: OAuthProgressOptions): OAuthProgressHandle {
		this.progressOptions.push(options);
		this.progressCallbacks.push(options);
		return {
			update: (status: string, loginUrl?: string) => {
				this.progressUpdates.push({ status, loginUrl });
			},
			dispose: () => {
				this.progressDisposals++;
			},
		};
	}
	async pickMany(_title: string, items: SelectItem[]) {
		this.manyItems.push(items);
		const values = this.manyQueue.shift();
		return values ? items.filter(item => values.includes(item.value)) : undefined;
	}
	themeQueue: Array<string | undefined> = [];
	async pickTheme(): Promise<string | undefined> {
		const value = this.themeQueue.shift();
		if (value !== undefined) {
			const { setSkin } = await import("../src/theme.ts");
			setSkin(value);
		}
		return value;
	}
	inspected: Array<string | undefined> = [];
	async inspectOutput(toolId?: string): Promise<void> {
		this.inspected.push(toolId);
	}
	currentProfile = "default";
	profileId() {
		return this.currentProfile;
	}
	setProfile(userId: string) {
		this.currentProfile = userId;
	}
	sessionAgent(): string | undefined {
		return this.currentProfile === "default" ? undefined : `harness-${this.currentProfile}`;
	}
	async writeFile(path: string, text: string) {
		this.files.set(path, text);
		return `/abs/${path}`;
	}
	onConfig(result: ConfigResult) {
		this.configured.push(result);
	}
	cleared = 0;
	restyled = 0;
	clear() {
		this.cleared++;
	}
	banner() {}
	restyle() {
		this.restyled++;
	}
	async exit() {}

	last(): { text: string; kind: NoticeKind } {
		return this.printed.at(-1)!;
	}
}

const run = (text: string, host: FakeHost) => runCommand(parseCommand(text)!, host);

test("/persona saves and clears via explicit args without opening the menu", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = { key: "persona_description", value: "Warm, witty and concise" };
	await run("/persona Warm, witty and concise", host);
	assert.deepEqual(host.calls.at(-1), { method: "config.set", params: { key: "persona_description", value: "Warm, witty and concise", persist: true } });
	await run("/persona --clear", host);
	assert.deepEqual(host.calls.at(-1), { method: "config.set", params: { key: "persona_description", value: "", persist: true } });
	assert.equal(host.pickTitles.length, 0);
	assert.equal(host.asked.length, 0);
});

test("bare /persona Add collects text and saves once with persist:true", async () => {
	const host = new FakeHost();
	host.responses["config.get"] = { config: { persona_description: "A patient mentor" }, secrets: [] };
	host.responses["config.set"] = { key: "persona_description", value: "Warm, witty and concise" };
	host.picks.push("add");
	host.textQueue.push("Warm, witty and concise");
	await run("/persona", host);
	assert.deepEqual(host.pickTitles, ["Persona"]);
	assert.equal(host.asked.length, 1);
	assert.equal(host.asked[0]!.title, "Add persona");
	assert.equal(host.asked[0]!.initial, "A patient mentor");
	assert.equal(host.asked[0]!.maxLength, 2000);
	const sets = host.calls.filter((c) => c.method === "config.set");
	assert.equal(sets.length, 1);
	assert.deepEqual(sets[0], { method: "config.set", params: { key: "persona_description", value: "Warm, witty and concise", persist: true } });
	assert.equal(host.configured.length, 1);
	assert.match(host.last().text, /Default persona saved/);
	assert.equal(host.submitted.length, 0);
});

test("bare /persona Add replaces the existing value instead of appending", async () => {
	const host = new FakeHost();
	host.responses["config.get"] = { config: { persona_description: "Old value" }, secrets: [] };
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ key: p.key, value: p.value });
	host.picks.push("add");
	host.textQueue.push("New value");
	await run("/persona", host);
	assert.deepEqual(host.calls.at(-1), { method: "config.set", params: { key: "persona_description", value: "New value", persist: true } });
});

test("bare /persona Add rejects blank and over-limit input without saving", async () => {
	const host = new FakeHost();
	host.responses["config.get"] = { config: { persona_description: "" }, secrets: [] };
	host.picks.push("add");
	host.textQueue.push("   ", undefined);
	await run("/persona", host);
	assert.match(host.printed.map((p) => p.text).join("\n"), /cannot be blank/);
	assert.equal(host.calls.filter((c) => c.method === "config.set").length, 0);

	const long = new FakeHost();
	long.responses["config.get"] = { config: { persona_description: "" }, secrets: [] };
	long.picks.push("add");
	long.textQueue.push(`${"x".repeat(2001)}`, undefined);
	await run("/persona", long);
	assert.match(long.printed.map((p) => p.text).join("\n"), /at most 2000/);
	assert.equal(long.calls.filter((c) => c.method === "config.set").length, 0);
});

test("bare /persona cancel at the picker or editor never persists or submits", async () => {
	const host = new FakeHost();
	host.responses["config.get"] = { config: { persona_description: "Kept" }, secrets: [] };
	await run("/persona", host);
	assert.equal(host.calls.map((c) => c.method).join(","), "config.get");
	assert.equal(host.submitted.length, 0);

	const editorCancel = new FakeHost();
	editorCancel.responses["config.get"] = { config: { persona_description: "Kept" }, secrets: [] };
	editorCancel.picks.push("add");
	editorCancel.textQueue.push(undefined);
	await run("/persona", editorCancel);
	assert.equal(editorCancel.calls.filter((c) => c.method === "config.set").length, 0);
	assert.equal(editorCancel.submitted.length, 0);
});

test("bare /persona Reset defaults to No and Yes clears only the persona", async () => {
	const noHost = new FakeHost();
	noHost.responses["config.get"] = { config: { persona_description: "Custom" }, secrets: [] };
	noHost.picks.push("reset", "no");
	await run("/persona", noHost);
	assert.deepEqual(noHost.pickTitles, ["Persona", "Reset your default persona?"]);
	assert.equal(noHost.calls.filter((c) => c.method === "config.set").length, 0);

	const yesHost = new FakeHost();
	yesHost.responses["config.get"] = { config: { persona_description: "Custom" }, secrets: [] };
	yesHost.responses["config.set"] = { key: "persona_description", value: "" };
	yesHost.picks.push("reset", "yes");
	await run("/persona", yesHost);
	assert.deepEqual(yesHost.calls.at(-1), { method: "config.set", params: { key: "persona_description", value: "", persist: true } });
	assert.match(yesHost.last().text, /Default persona reset/);
	assert.equal(yesHost.submitted.length, 0);
});

test("bare /persona Reset with no custom value explains instead of confirming", async () => {
	const host = new FakeHost();
	host.responses["config.get"] = { config: { persona_description: "" }, secrets: [] };
	host.picks.push("reset");
	await run("/persona", host);
	assert.deepEqual(host.pickTitles, ["Persona"]);
	assert.match(host.last().text, /Already using the agent default/);
	assert.equal(host.calls.filter((c) => c.method === "config.set").length, 0);
});

test("/growth teaches and rates the current session without an approval prompt", async () => {
	const host = new FakeHost();
	host.responses["growth.teach"] = { notes: { "style:verbosity": "Keep it short" } };
	await run("/growth teach style verbosity Keep it short", host);
	assert.deepEqual(host.calls[0], { method: "growth.teach", params: { sessionId: SESSION.id, kind: "style", key: "verbosity", content: "Keep it short" } });
	host.responses["growth.get"] = { growth: { notes: {}, interactions: 1, feedback_count: 0 }, episodes: [{ id: "episode-1", completed: true, reward: null, action: "retrieve" }] };
	host.responses["growth.feedback"] = { accepted: true };
	await run("/growth feedback good", host);
	assert.deepEqual(host.calls.at(-1), { method: "growth.feedback", params: { sessionId: SESSION.id, episodeId: "episode-1", reward: 1 } });
	assert.match(host.last().text, /learned from your rating/);
	assert.equal(host.picks.length, 0);
});

test("/growth does not rate interrupted turns and supports explicit correction", async () => {
	const host = new FakeHost();
	host.responses["growth.get"] = { growth: { notes: {}, interactions: 0, feedback_count: 0 }, episodes: [{ id: "unfinished", completed: false, reward: null, action: "retrieve" }] };
	await run("/growth feedback bad", host);
	assert.match(host.last().text, /No completed, unrated/);
	assert.equal(host.calls.length, 1);
	host.responses["growth.forget"] = { removed: true };
	await run("/growth forget style:verbosity", host);
	assert.deepEqual(host.calls.at(-1), { method: "growth.forget", params: { sessionId: SESSION.id, key: "style:verbosity" } });
});

test("bare /growth immediately offers actions and cancelled menus do no work", async () => {
	const host = new FakeHost();
	await run("/growth", host);
	assert.deepEqual(host.pickTitles, ["Personality growth"]);
	assert.equal(host.calls.length, 0);
	assert.equal(host.printed.length, 0);
});

test("/growth Teach collects a note interactively without submitting a chat turn", async () => {
	const host = new FakeHost();
	host.picks.push("teach", "style");
	host.textQueue.push("tone", "Be concise and warm");
	host.responses["growth.teach"] = { notes: { "style:tone": "Be concise and warm" } };
	await run("/growth", host);
	assert.deepEqual(host.calls.at(-1), { method: "growth.teach", params: { sessionId: SESSION.id, kind: "style", key: "tone", content: "Be concise and warm" } });
	assert.equal(host.submitted.length, 0);
});

test("/growth rollback selects a revision and requires an explicit restore decision", async () => {
	const host = new FakeHost();
	host.responses["growth.get"] = { growth: { notes: {}, interactions: 3, feedback_count: 1 }, episodes: [], rsi: { enabled: true, revision: 3, trial: null, observations: 2, automatic_observations: 2, human_ratings: 1, history: [{ revision: 2, reason: "teach", status: "stable", created_at: "2026-10-11T00:00:00Z" }] } };
	host.responses["growth.rollback"] = { restored: true };
	host.picks.push("2", "yes");
	await run("/growth rollback", host);
	assert.deepEqual(host.calls.at(-1), { method: "growth.rollback", params: { sessionId: SESSION.id, revision: 2 } });
	assert.match(host.last().text, /Restored/);
	assert.equal(host.submitted.length, 0);
});

test("/growth rollback cancellation never changes the saved revision", async () => {
	const host = new FakeHost();
	host.picks.push("no");
	await run("/growth rollback 2", host);
	assert.equal(host.calls.length, 0);
});

test("/growth can restore the original revision zero from its menu or arguments", async () => {
    for (const command of ["/growth rollback", "/growth rollback 0"]) {
        const host = new FakeHost();
        host.responses["growth.get"] = { rsi: { revision: 1, history: [{ revision: 0, reason: "baseline", status: "stable" }] } };
        host.responses["growth.rollback"] = { restored: true };
        host.picks.push(...(command.endsWith(" 0") ? ["yes"] : ["0", "yes"]));
        await run(command, host);
        assert.deepEqual(host.calls.at(-1), { method: "growth.rollback", params: { sessionId: SESSION.id, revision: 0 } });
    }
});

test("/growth show distinguishes operational observations, human feedback, and a persona trial", async () => {
	const host = new FakeHost();
	host.responses["growth.get"] = { growth: { notes: {}, interactions: 3, feedback_count: 1 }, episodes: [], rsi: { enabled: true, revision: 2, observations: 2, automatic_observations: 2, human_ratings: 1, trial: { revision: 2, ratings: 1, required: 3 }, history: [] } };
	await run("/growth show", host);
	assert.match(host.last().text, /operational observations/);
	assert.match(host.last().text, /human ratings/);
	assert.match(host.last().text, /1\/3/);
});

test("/recall searches transcript evidence and opens an anchored window", async () => {
	const host = new FakeHost();
	const sourceId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
	const chunkId = "99999999-8888-7777-6666-555555555555";
	host.responses["session.recall"] = {
		hits: [{ sessionId: sourceId, chunkId, title: "Older conversation", source: "archive", preview: "The migration decision", score: 0.5, occurredAt: "2026-10-01T00:00:00Z" }],
	};
	host.responses["session.recall.window"] = {
		messages: [{ sessionId: sourceId, chunkId, type: "user_message", source: "archive", payload: { content: "The migration decision" }, occurredAt: "2026-10-01T00:00:00Z" }],
	};
	host.picks.push(chunkId);
	await run("/recall migration", host);
	assert.deepEqual(host.calls[0], { method: "session.recall", params: { sessionId: SESSION.id, query: "migration", limit: 30 } });
	assert.deepEqual(host.calls[1], { method: "session.recall.window", params: { sessionId: SESSION.id, targetSessionId: sourceId, chunkId } });
	assert.match(host.printed.map((p) => p.text).join("\n"), /migration decision/);
	assert.equal(host.current?.id, SESSION.id);
});

test("/recall reports empty results without opening a window", async () => {
	const host = new FakeHost();
	host.responses["session.recall"] = { hits: [] };
	await run("/recall obscure", host);
	assert.match(host.last().text, /No transcript matches/);
	assert.equal(host.calls.length, 1);
});

test("/memory share sends an owned memory to a named agent", async () => {
	const host = new FakeHost();
	host.responses["memory.list"] = {
		memories: [{ id: "bbbb2222-3333-4444-5555-666666666666", content: "Fact", category: "fact", importance: 0.7 }],
	};
	host.responses["memory.share"] = { status: "accepted", targetMemoryId: "aaaaaaaa-3333-4444-5555-666666666666" };
	await run("/memory share bbbb2222 researcher", host);
	assert.deepEqual(host.calls.at(-1), {
		method: "memory.share",
		params: { sessionId: SESSION.id, id: "bbbb2222-3333-4444-5555-666666666666", recipientAgent: "researcher" },
	});
	assert.match(host.last().text, /accepted/);
});

test("/skills proposals lists staged learning and reviews one by id", async () => {
	const host = new FakeHost();
	const id = "11111111-aaaa-bbbb-cccc-eeeeeeeeeeee";
	host.responses["learning.list"] = { reviews: [{ id, name: "deployment-checks", description: "Check a deployment", status: "pending", content: "Check health and ready.", triggers: ["deploy"] }] };
	host.responses["learning.approve"] = { review: { id, status: "approved" } };
	await run("/skills proposals", host);
	assert.deepEqual(host.calls[0], { method: "learning.list", params: { sessionId: SESSION.id, limit: 50 } });
	assert.match(host.last().text, /deployment-checks/);
	await run("/skills approve 11111111", host);
	assert.deepEqual(host.calls.at(-1), { method: "learning.approve", params: { sessionId: SESSION.id, id } });
	assert.match(host.last().text, /approved/);
});

test("/jobs add schedules an interval job and reports it", async () => {
	const host = new FakeHost();
	host.responses["jobs.create"] = (p: Record<string, unknown>) => ({
		job: {
			id: "job-abcdef01",
			name: "interval job",
			kind: p.kind,
			sessionId: SESSION.id,
			agent: "harness",
			prompt: p.prompt,
			intervalSeconds: p.intervalSeconds,
			enabled: true,
			status: "idle",
			lastRunAt: null,
			nextRunAt: null,
			lastError: null,
			runCount: 0,
		},
	});
	await run("/jobs add 60 check the news", host);
	const call = host.calls.at(-1)!;
	assert.equal(call.method, "jobs.create");
	assert.equal(call.params.kind, "interval");
	assert.equal(call.params.intervalSeconds, 60);
	assert.equal(call.params.prompt, "check the news");
	assert.equal(host.last().kind, "success");
});

test("/jobs add can pin a model and provider", async () => {
	const host = new FakeHost();
	host.responses["jobs.create"] = { job: { id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", prompt: "Check deployment", intervalSeconds: 60 } };
	await run("/jobs add 60 Check deployment --model=openrouter/free --provider=openrouter", host);
	assert.deepEqual(host.calls.at(-1), {
		method: "jobs.create",
		params: { sessionId: SESSION.id, kind: "interval", prompt: "Check deployment", intervalSeconds: 60, model: "openrouter/free", provider: "openrouter" },
	});
});

test("/jobs script schedules a script-only job without a prompt", async () => {
	const host = new FakeHost();
	host.responses["jobs.create"] = { job: { id: "job-script01", scriptPath: "check.py", intervalSeconds: 60 } };
	await run("/jobs script 60 check.py", host);
	assert.deepEqual(host.calls.at(-1), {
		method: "jobs.create",
		params: { sessionId: SESSION.id, kind: "interval", intervalSeconds: 60, noAgent: true, scriptPath: "check.py" },
	});
	assert.match(host.last().text, /check.py/);
});

test("/jobs add rejects a non-numeric interval", async () => {
	const host = new FakeHost();
	await run("/jobs add soon do stuff", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /number of seconds/);
});

test("/jobs add rejects trailing text in an interval", async () => {
	const host = new FakeHost();
	await run("/jobs add 30abc do stuff", host);
	assert.equal(host.last().kind, "error");
	assert.equal(host.calls.length, 0);
});

test("/jobs heartbeat defaults to 300s and omits a prompt", async () => {
	const host = new FakeHost();
	host.responses["jobs.create"] = (p: Record<string, unknown>) => ({
		job: {
			id: "job-11112222",
			name: "heartbeat job",
			kind: "heartbeat",
			sessionId: SESSION.id,
			agent: "harness",
			prompt: "...",
			intervalSeconds: p.intervalSeconds,
			enabled: true,
			status: "idle",
			lastRunAt: null,
			nextRunAt: null,
			lastError: null,
			runCount: 0,
		},
	});
	await run("/jobs heartbeat", host);
	const call = host.calls.at(-1)!;
	assert.equal(call.params.kind, "heartbeat");
	assert.equal(call.params.intervalSeconds, 300);
	assert.equal(call.params.prompt, undefined);
});

test("/jobs cron sends a UTC expression and prompt", async () => {
	const host = new FakeHost();
	host.responses["jobs.create"] = (p: Record<string, unknown>) => ({
		job: {
			id: "job-cron1234",
			prompt: p.prompt,
			cronExpression: p.cronExpression,
		},
	});
	await run("/jobs cron */15 9-17 * * 1-5 :: check the queue", host);
	const call = host.calls.at(-1)!;
	assert.equal(call.params.kind, "cron");
	assert.equal(call.params.cronExpression, "*/15 9-17 * * 1-5");
	assert.equal(call.params.prompt, "check the queue");
	assert.match(host.last().text, /UTC/);
});

test("/jobs off resolves an id prefix and disables the job", async () => {
	const host = new FakeHost();
	host.responses["jobs.list"] = {
		jobs: [{ id: "job-abcdef01", intervalSeconds: 60, enabled: true, kind: "interval", runCount: 0, nextRunAt: null }],
	};
	host.responses["jobs.setEnabled"] = {};
	await run("/jobs off job-abc", host);
	const call = host.calls.at(-1)!;
	assert.equal(call.method, "jobs.setEnabled");
	assert.equal(call.params.id, "job-abcdef01");
	assert.equal(call.params.enabled, false);
	assert.equal(call.params.sessionId, SESSION.id);
});

test("every command has a description and unique name", () => {
	const names = SLASH_COMMANDS.map((c) => c.name);
	assert.equal(new Set(names).size, names.length);
	for (const c of SLASH_COMMANDS) assert.ok(c.description, c.name);
	for (const required of ["memory", "skills", "context", "compress", "export", "fork", "delete", "config", "status"]) {
		assert.ok(names.includes(required), required);
	}
});

test("unknown commands and subcommands are reported, not thrown", async () => {
	const host = new FakeHost();
	await run("/nope", host);
	assert.equal(host.last().kind, "warning");
	await run("/memory dance", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Unknown \/memory option/);
});

test("/memory add parses an optional category prefix", async () => {
	const host = new FakeHost();
	host.responses["memory.add"] = (p: Record<string, unknown>) => ({
		memory: { id: "abcdef12-0000", category: p.category, content: p.content },
	});
	await run("/memory add preference: tabs over spaces", host);
	assert.deepEqual(host.calls[0]!.params, { content: "tabs over spaces", category: "preference", sessionId: SESSION.id });
	await run("/memory add note: not a category", host);
	assert.deepEqual(host.calls[1]!.params.category, "fact");
	assert.equal(host.calls[1]!.params.content, "note: not a category");
});

test("/memory forget resolves an id prefix", async () => {
	const host = new FakeHost();
	host.responses["memory.list"] = { memories: [{ id: "aaaa1111-x" }, { id: "bbbb2222-y" }], total: 2 };
	host.responses["memory.forget"] = { deleted: true };
	await run("/memory forget bbbb", host);
	assert.deepEqual(host.calls.at(-1), { method: "memory.forget", params: { sessionId: SESSION.id, id: "bbbb2222-y" } });
	assert.equal(host.last().kind, "success");
});

test("/memory approve all uses the bulk method", async () => {
	const host = new FakeHost();
	host.responses["memory.approveAll"] = { count: 3 };
	await run("/memory approve all", host);
	assert.equal(host.calls[0]!.method, "memory.approveAll");
	assert.match(host.last().text, /Approved 3/);
});

test("/delete asks for confirmation and starts a new session after deleting the current one", async () => {
	const host = new FakeHost();
	host.responses["session.delete"] = { deleted: true };
	host.responses["session.create"] = { session: { ...SESSION, id: "99999999-0000-0000-0000-000000000000", title: "" } };

	host.picks = ["no"];
	await run(`/delete ${SESSION.id}`, host);
	assert.equal(host.calls.length, 0, "cancel sends nothing");

	host.picks = ["yes"];
	await run(`/delete ${SESSION.id}`, host);
	assert.deepEqual(host.calls.map((c) => c.method), ["session.delete", "session.create"]);
	assert.equal(host.current?.id, "99999999-0000-0000-0000-000000000000");
});

test("/delete lists every page, then deletes only checked sessions after confirmation", async () => {
	const host = new FakeHost();
	const older = { ...SESSION, id: "older-session", title: "Older session" };
	host.responses["session.list"] = (params: Record<string, unknown>) => params.cursor ? { sessions: [older] } : { sessions: [SESSION], nextCursor: "200" };
	host.manyQueue.push([older.id]);
	host.picks.push("yes");
	host.responses["session.delete"] = { deleted: true };
	await run("/delete", host);
	assert.deepEqual(host.manyItems[0]!.map(item => item.value), [SESSION.id, older.id]);
	assert.deepEqual(host.calls.map(call => call.method), ["session.list", "session.list", "session.delete"]);
	assert.deepEqual(host.calls[2]!.params, { sessionId: older.id });
	assert.equal(host.current?.id, SESSION.id);
	assert.match(host.last().text, /Deleted 1 session/);
});

test("/delete checkbox and confirmation cancellation never delete", async () => {
	for (const selection of [undefined, [], [SESSION.id]]) {
		const host = new FakeHost();
		host.responses["session.list"] = { sessions: [SESSION] };
		host.manyQueue.push(selection);
		host.picks.push("no");
		await run("/delete", host);
		assert.deepEqual(host.calls.map(call => call.method), ["session.list"]);
	}
});

test("/delete reports partial failures and keeps a failed current session", async () => {
	const host = new FakeHost();
	host.responses["session.list"] = { sessions: [SESSION, { ...SESSION, id: "other" }, { ...SESSION, id: "gone" }] };
	host.manyQueue.push([SESSION.id, "other", "gone"]);
	host.picks.push("yes");
	host.responses["session.delete"] = (params: Record<string, unknown>) => {
		if (params.sessionId === SESSION.id) throw new Error("Stop the running reply first");
		return { deleted: params.sessionId !== "gone" };
	};
	await run("/delete", host);
	assert.equal(host.current?.id, SESSION.id);
	assert.ok(!host.calls.some(call => call.method === "session.create"));
	assert.match(host.printed.map(item => item.text).join("\n"), /Deleted 1 session/);
	assert.match(host.printed.map(item => item.text).join("\n"), /Stop the running reply first/);
	assert.equal(host.calls.filter(call => call.method === "session.delete").length, 3);
});

test("/delete selected current session creates only one replacement", async () => {
	const host = new FakeHost();
	host.responses["session.list"] = { sessions: [SESSION, { ...SESSION, id: "other" }] };
	host.manyQueue.push([SESSION.id, "other"]);
	host.picks.push("yes");
	host.responses["session.delete"] = { deleted: true };
	host.responses["session.create"] = { session: { ...SESSION, id: "new" } };
	await run("/delete", host);
	assert.equal(host.current?.id, "new");
	assert.equal(host.calls.filter(call => call.method === "session.create").length, 1);
	assert.match(host.last().text, /Deleted 2 sessions/);
});

test("/delete runs at most three requests concurrently", async () => {
	const host = new FakeHost();
	const ids = ["a", "b", "c", "d"];
	host.responses["session.list"] = { sessions: ids.map(id => ({ ...SESSION, id })) };
	host.manyQueue.push(ids);
	host.picks.push("yes");
	const pending: Array<(value: { deleted: boolean }) => void> = [];
	host.responses["session.delete"] = () => new Promise(resolve => pending.push(resolve));
	const deleting = run("/delete", host);
	await new Promise(resolve => setImmediate(resolve));
	assert.equal(pending.length, 3);
	for (const resolve of pending.splice(0)) resolve({ deleted: true });
	await new Promise(resolve => setImmediate(resolve));
	assert.equal(pending.length, 1);
	pending[0]!({ deleted: true });
	await deleting;
	assert.match(host.last().text, /Deleted 4 sessions/);
});

test("/export writes Markdown to a default file name", async () => {
	const host = new FakeHost();
	host.responses["session.export"] = { markdown: "# Session: current" };
	await run("/export", host);
	assert.equal(host.files.get("session-11111111.md"), "# Session: current");
	assert.match(host.last().text, /Exported to \/abs\/session-11111111\.md/);
});

test("/config sets a value and --save persists it", async () => {
	const host = new FakeHost();
	host.responses["config.get"] = { config: { max_iterations: 10, model: "m", openrouter_api_key: true }, secrets: ["openrouter_api_key"] };
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "openrouter", key: p.key, value: 7 });
	await run("/config max_iterations 7 --save", host);
	assert.deepEqual(host.calls.at(-1)!.params, { key: "max_iterations", value: "7", persist: true });
	assert.match(host.last().text, /max_iterations = 7 \(saved\)/);

	await run("/config", host);
	assert.match(host.last().text, /openrouter_api_key\s+set \(in \.env\)/);
	await run("/config bogus 1", host);
	assert.match(host.last().text, /Unknown setting/);
});

test("/default-model sets a custom id and saves it", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = { model: "openai/gpt-4o", provider: "openrouter", key: "model", value: "openai/gpt-4o" };
	host.responses["session.setModel"] = { sessionId: SESSION.id, model: "openai/gpt-4o", provider: "openrouter" };
	await run("/default-model openai/gpt-4o", host);
	assert.equal(host.configured[0]!.model, "openai/gpt-4o");
	assert.deepEqual(host.calls.map((c) => [c.method, c.params]), [
		["config.set", { key: "model", value: "openai/gpt-4o", persist: true }],
		["session.setModel", { sessionId: SESSION.id, model: "openai/gpt-4o" }],
	]);
});

test("/default-model with no args opens the menu and saves the pick", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "p", key: p.key, value: p.value });
	host.responses["session.setModel"] = { sessionId: SESSION.id, model: "deepseek-chat", provider: "deepseek" };
	host.picks.push("deepseek::deepseek-chat");
	await run("/default-model", host);
	assert.deepEqual(
		host.calls.map((c) => [c.method, c.params]),
		[
			["config.set", { key: "provider", value: "deepseek", persist: true }],
			["config.set", { key: "model", value: "deepseek-chat", persist: true }],
			["session.setModel", { sessionId: SESSION.id, model: "deepseek-chat", provider: "deepseek" }],
		],
	);
	assert.match(host.last().text, /saved as default/);
});

test("/default-model resolves a curated id directly", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "p", key: p.key, value: p.value });
	host.responses["session.setModel"] = { sessionId: SESSION.id, model: "deepseek-chat", provider: "deepseek" };
	await run("/default-model deepseek-chat", host);
	assert.deepEqual(
		host.calls.map((c) => [c.method, c.params]),
		[
			["config.set", { key: "provider", value: "deepseek", persist: true }],
			["config.set", { key: "model", value: "deepseek-chat", persist: true }],
			["session.setModel", { sessionId: SESSION.id, model: "deepseek-chat", provider: "deepseek" }],
		],
	);
});

test("/model is an alias for /default-model", () => {
	assert.deepEqual(parseCommand("/model deepseek-chat"), { name: "default-model", args: "deepseek-chat" });
});

test("/provider re-pins the current session", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = { model: "m", provider: "deepseek", key: "provider", value: "deepseek" };
	host.responses["session.setModel"] = { sessionId: SESSION.id, model: "m", provider: "deepseek" };
	await run("/provider deepseek", host);
	assert.deepEqual(host.calls.map((c) => [c.method, c.params]), [
		["config.set", { key: "provider", value: "deepseek", persist: false }],
		["session.setModel", { sessionId: SESSION.id, provider: "deepseek" }],
	]);
});

test("commands that need a session say so", async () => {
	const host = new FakeHost();
	host.current = undefined;
	await run("/context", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /No active session/);
});

test("argument completions offer subcommands", async () => {
	const memory = SLASH_COMMANDS.find((c) => c.name === "memory")!;
	const items = await memory.getArgumentCompletions!("pe");
	assert.deepEqual(items?.map((i) => i.value), ["pending"]);
	assert.equal(await memory.getArgumentCompletions!("search foo"), null);
});

test("/agents list and show render agent definitions", async () => {
	const host = new FakeHost();
	host.responses["agents.list"] = {
		agents: [
			{ name: "harness", description: "general", systemPrompt: "", tools: [], model: null, provider: null, maxIterations: 10, source: "builtin" },
			{ name: "coder", description: "edits code", systemPrompt: "be careful", tools: ["read_file", "write_file"], model: null, provider: null, maxIterations: 10, source: "builtin" },
		],
	};
	await run("/agents", host);
	assert.match(host.last().text, /harness/);
	assert.match(host.last().text, /coder/);
	await run("/agents show coder", host);
	assert.match(host.last().text, /edits code/);
	assert.match(host.last().text, /read_file, write_file/);
});

test("/delegate runs one step through agents.run", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = {
		results: [{ agent: "researcher", task: "find X", response: "found X", sessionId: "s", tokens: 20, iterations: 1, status: "complete" }],
	};
	await run("/delegate researcher find X", host);
	assert.deepEqual(host.calls[0]!.params.steps, [{ agent: "researcher", task: "find X" }]);
	assert.match(host.markdown.at(-1)!, /found X/);
});

test("/delegate selects a live agent before asking for the task", async () => {
	const host = new FakeHost();
	host.responses["agents.list"] = { agents: [{ name: "coder", description: "Edits code" }, { name: "researcher", description: "Researches questions" }] };
	host.picks.push("researcher");
	host.textQueue.push("  Find the source\nwith evidence  ");
	host.responses["agents.run"] = { results: [{ status: "complete", response: "Found it", tokens: 10 }] };
	await run("/delegate", host);
	assert.deepEqual(host.pickTitles, ["Delegate to which agent?"]);
	assert.match(host.asked[0]!.title, /researcher/);
	assert.deepEqual(host.calls.map(c => c.method), ["agents.list", "agents.run"]);
	assert.deepEqual(host.calls[1]!.params, { sessionId: SESSION.id, steps: [{ agent: "researcher", task: "Find the source\nwith evidence" }] });
});

test("/delegate agent asks for a task without opening the picker", async () => {
	const host = new FakeHost();
	host.textQueue.push("Fix the bug");
	host.responses["agents.run"] = { results: [{ status: "complete", response: "Fixed", tokens: 10 }] };
	await run("/delegate coder", host);
	assert.equal(host.pickTitles.length, 0);
	assert.match(host.asked[0]!.title, /coder/);
	assert.deepEqual(host.calls[0]!.params.steps, [{ agent: "coder", task: "Fix the bug" }]);
});

test("/delegate picker cancellation does not ask for a task or launch", async () => {
	const host = new FakeHost();
	host.responses["agents.list"] = { agents: [{ name: "coder", description: "Edits code" }] };
	await run("/delegate", host);
	assert.equal(host.asked.length, 0);
	assert.deepEqual(host.calls.map(c => c.method), ["agents.list"]);
});

test("/delegate cancelled or blank task never launches", async () => {
	for (const task of [undefined, "   "]) {
		const host = new FakeHost();
		host.textQueue.push(task);
		await run("/delegate coder", host);
		assert.equal(host.calls.length, 0);
	}
});

test("/delegate with no available agents explains how to add one", async () => {
	const host = new FakeHost();
	host.responses["agents.list"] = { agents: [] };
	await run("/delegate", host);
	assert.equal(host.pickTitles.length, 0);
	assert.equal(host.asked.length, 0);
	assert.match(host.last().text, /No agents.*\/agents save/);
});

test("/resume with no args delegates to the sessions command (named reference)", async () => {
	const host = new FakeHost();
	host.responses["session.list"] = { sessions: [] };
	await run("/resume", host);
	// Should call session.list with limit=50 (sessions command), not limit=200 (resolveSessionId)
	const call = host.calls.at(-1)!;
	assert.equal(call.method, "session.list");
	assert.equal(call.params.limit, 50);
});

test("/resume with a prefix resolves via paginated session.list", async () => {
	const host = new FakeHost();
	const oldSessionId = "cccccccc-cccc-cccc-cccc-cccccccccccc";
	// First page: no match, has nextCursor
	host.responses["session.list"] = (p: Record<string, unknown>) => {
		if (p.cursor) {
			// Second page: contains the match
			return { sessions: [{ id: oldSessionId }] };
		}
		return { sessions: [{ id: "dddddddd-dddd-dddd-dddd-dddddddddddd" }], nextCursor: "page2" };
	};
	host.responses["session.resume"] = { session: { ...SESSION, id: oldSessionId }, history: [] };
	await run("/resume cccc", host);
	// Should have made two session.list calls (pagination)
	const listCalls = host.calls.filter((c) => c.method === "session.list");
	assert.equal(listCalls.length, 2, "paginates through all sessions");
	assert.equal(listCalls[0]!.params.cursor, undefined);
	assert.equal(listCalls[1]!.params.cursor, "page2");
	// Should have resolved the prefix and resumed
	const resumeCall = host.calls.find((c) => c.method === "session.resume")!;
	assert.equal(resumeCall.params.sessionId, oldSessionId);
});

test("format helpers", () => {
	assert.equal(resolvePrefix("ab", ["abc", "xyz"], "thing"), "abc");
	assert.throws(() => resolvePrefix("a", ["abc", "abd"], "thing"), /matches 2 things/);
	assert.throws(() => resolvePrefix("q", ["abc"], "thing"), /No thing starts with/);
	const t = table(["A", "B"], [["1", "two"], ["three", "4"]]);
	assert.equal(t.split("\n").length, 4);
});

test("/keys list shows status without ever echoing values", async () => {
	const host = new FakeHost();
	host.responses["secrets.list"] = {
		secrets: [
			{ key: "OPENROUTER_API_KEY", description: "OpenRouter", set: true, liveSet: true },
			{ key: "ANTHROPIC_API_KEY", description: "Anthropic", set: false, liveSet: false },
		],
		envFile: "/tmp/.env",
	};
	await run("/keys list", host);
	assert.match(host.last().text, /OPENROUTER_API_KEY/);
	assert.match(host.last().text, /not set/);
	assert.doesNotMatch(host.last().text, /sk-or/);
});

test("/keys set saves by default and --no-save stays session-only", async () => {
	const host = new FakeHost();
	host.responses["secrets.set"] = (p: Record<string, unknown>) => ({
		key: p.key,
		set: true,
		persisted: p.persist,
		envFile: p.persist ? "/tmp/.env" : null,
	});
	await run("/keys set openrouter_api_key sk-or-secret", host);
	assert.deepEqual(host.calls.at(-1), {
		method: "secrets.set",
		params: { key: "OPENROUTER_API_KEY", value: "sk-or-secret", persist: true },
	});
	assert.match(host.last().text, /OPENROUTER_API_KEY set/);
	assert.doesNotMatch(host.last().text, /sk-or-secret/, "value is never echoed");
	await run("/keys set groq_api_key gsk-x --no-save", host);
	assert.deepEqual(host.calls.at(-1)!.params, { key: "GROQ_API_KEY", value: "gsk-x", persist: false });
	assert.match(host.last().text, /session only/);
});

test("/keys clear resolves a prefix", async () => {
	const host = new FakeHost();
	host.responses["secrets.list"] = {
		secrets: [{ key: "COHERE_API_KEY", description: "Cohere", set: true, liveSet: true }],
		envFile: "/tmp/.env",
	};
	host.responses["secrets.clear"] = { key: "COHERE_API_KEY", set: false };
	await run("/keys clear cohere", host);
	assert.deepEqual(host.calls.at(-1), { method: "secrets.clear", params: { key: "COHERE_API_KEY" } });
	assert.match(host.last().text, /cleared/);
});

test("/keys menu Set flow saves once with persist:true and never echoes the value", async () => {
	const host = new FakeHost();
	host.responses["secrets.list"] = {
		secrets: [{ key: "OPENAI_API_KEY", description: "OpenAI", set: false, liveSet: false }],
		envFile: "/tmp/.env",
	};
	host.responses["secrets.set"] = { key: "OPENAI_API_KEY", set: true, persisted: true, envFile: "/tmp/.env" };
	host.picks.push("OPENAI_API_KEY", "set");
	host.textQueue.push("SENTINEL-s3cr3t-value");
	await run("/keys", host);
	const sets = host.calls.filter((c) => c.method === "secrets.set");
	assert.deepEqual(sets, [{ method: "secrets.set", params: { key: "OPENAI_API_KEY", value: "SENTINEL-s3cr3t-value", persist: true } }]);
	assert.match(host.last().text, /OPENAI_API_KEY set/);
	assert.ok(!host.printed.some((p) => p.text.includes("SENTINEL")), "secret never echoed in notices");
	assert.ok(!host.pickTitles.join("\n").includes("SENTINEL"), "secret never in menus");
	assert.ok(host.asked.some((a) => a.secret === true), "value collected through a masked editor");
});

test("/keys menu Clear confirms before a single clear", async () => {
	const host = new FakeHost();
	host.responses["secrets.list"] = {
		secrets: [{ key: "OPENAI_API_KEY", description: "OpenAI", set: true, liveSet: true }],
		envFile: "/tmp/.env",
	};
	host.responses["secrets.clear"] = { key: "OPENAI_API_KEY" };
	host.picks.push("OPENAI_API_KEY", "clear", "yes");
	await run("/keys", host);
	assert.equal(host.calls.filter((c) => c.method === "secrets.clear").length, 1);
	assert.match(host.last().text, /OPENAI_API_KEY cleared/);
});

test("/keys menu Back and Esc persist nothing", async () => {
	const host = new FakeHost();
	host.responses["secrets.list"] = {
		secrets: [{ key: "OPENAI_API_KEY", description: "OpenAI", set: false, liveSet: false }],
		envFile: "/tmp/.env",
	};
	host.picks.push("OPENAI_API_KEY", "back", undefined);
	await run("/keys", host);
	assert.ok(!host.calls.some((c) => c.method === "secrets.set" || c.method === "secrets.clear"));
});

test("/keys expert set failure redacts the value", async () => {
	const host = new FakeHost();
	host.responses["secrets.set"] = () => {
		throw new Error("store exploded on SENTINEL-s3cr3t-value boom");
	};
	await run("/keys set OPENAI_API_KEY SENTINEL-s3cr3t-value", host);
	assert.match(host.last().text, /Could not save OPENAI_API_KEY/);
	assert.ok(!host.last().text.includes("SENTINEL"), "failure text redacts the submitted value");
	assert.match(host.last().text, /\[redacted\]/);
});

test("/models picker applies the curated model and provider", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "p", key: p.key, value: p.value });
	host.responses["session.setModel"] = { sessionId: SESSION.id, model: "llama3.1", provider: "ollama" };
	host.picks.push("ollama::llama3.1");
	await run("/models", host);
	assert.deepEqual(
		host.calls.map((c) => [c.method, c.params]),
		[
			["config.set", { key: "provider", value: "ollama" }],
			["config.set", { key: "model", value: "llama3.1" }],
			["session.setModel", { sessionId: SESSION.id, model: "llama3.1", provider: "ollama" }],
		],
	);
	assert.match(host.last().text, /\/keys/);
});

test("/models filters by query and warns when nothing matches", async () => {
	const host = new FakeHost();
	await run("/models zekefake", host);
	assert.equal(host.last().kind, "warning");
	assert.equal(host.calls.length, 0);
});

test("/compact is an alias for /compress", () => {
	assert.deepEqual(parseCommand("/compact"), { name: "compress", args: "" });
});

test("/help groups commands by domain", async () => {
	const { helpText } = await import("../src/commands.ts");
	const { SLASH_COMMANDS } = await import("../src/features/index.ts");
	const help = stripTerminalSequences(helpText(SLASH_COMMANDS));
	for (const group of ["Session", "Memory", "Setup"]) assert.match(help, new RegExp(`^${group}$`, "m"));
	for (const command of SLASH_COMMANDS) assert.match(help, new RegExp(`/${command.name}\\b`));
});

test("/init scaffolds AGENTS.md", async () => {
	const host = new FakeHost();
	await run("/init", host);
	const content = host.files.get("AGENTS.md");
	assert.ok(content?.includes("# AGENTS.md"));
	assert.match(host.last().text, /AGENTS\.md/);
});

test("/review submits a review turn", async () => {
	const host = new FakeHost();
	await run("/review", host);
	assert.match(host.submitted.at(-1)!, /Review my current changes/);
	await run("/review focus on auth", host);
	assert.match(host.submitted.at(-1)!, /auth/);
});

test("/clear clears and starts a new chat", async () => {
	const host = new FakeHost();
	host.responses["session.create"] = { session: { ...SESSION, id: "99999999-0000-0000-0000-000000000000" } };
	await run("/clear", host);
	assert.equal(host.calls.at(-1)!.method, "session.create");
	assert.equal(host.current?.id, "99999999-0000-0000-0000-000000000000");
});

test("/theme switches skin and persists it", async () => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const host = new FakeHost();
	host.responses["config.set"] = { model: "m", provider: "p", key: "theme", value: "tokyonight" };
	const before = getSkin();
	try {
		await run("/theme tokyonight", host);
		assert.equal(getSkin(), "tokyonight");
		assert.deepEqual(host.calls.at(-1)!.params, { key: "theme", value: "tokyonight", persist: true });
		assert.match(host.last().text, /Skin: tokyonight/);
		assert.equal(host.cleared, 0, "switching skin keeps the transcript");
		assert.equal(host.restyled, 1);
		await run("/theme nope", host);
		assert.match(host.last().text, /Unknown skin/);
	} finally {
		setSkin(before);
	}
});

test("read-only reports use the plain kind", async () => {
	const host = new FakeHost();
	host.responses["session.list"] = { sessions: [] };
	await run("/sessions", host);
	assert.match(host.last().text, /No sessions yet/);
	assert.equal(host.last().kind, "plain");
	host.responses["session.search"] = { sessions: [] };
	await run("/search nothing-here", host);
	assert.equal(host.last().kind, "plain");
});

test("unknown modes and keys fail as errors, not warnings", async () => {
	const modeHost = new FakeHost();
	await run("/mode bogus", modeHost);
	assert.equal(modeHost.last().kind, "error");
	assert.match(modeHost.last().text, /Unknown mode/);
	assert.equal(modeHost.calls.length, 0);

	const keysHost = new FakeHost();
	await run("/keys frobnicate", keysHost);
	assert.equal(keysHost.last().kind, "error");
	assert.match(keysHost.last().text, /Unknown \/keys option/);
	assert.equal(keysHost.calls.length, 0);
});

test("/approvals renders one table and rejects bad ids as errors", async () => {
	const host = new FakeHost();
	host.responses["approvals.list"] = {
		approvals: [
			{ request_id: "abcdef12-0000", operation: "exec", target: "ls", agent_id: "harness", status: "pending" },
			{ request_id: "12345678-0000", operation: "write", target: "a.txt", agent_id: "harness", status: "pending" },
		],
	};
	await run("/approvals", host);
	assert.equal(host.printed.length, 1);
	assert.match(host.last().text, /abcdef12/);
	assert.match(host.last().text, /12345678/);
	assert.equal(host.last().kind, "plain");

	await run("/approvals allow zzzz", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /No pending approval/);

	await run("/approvals dance", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Unknown \/approvals option/);
});

test("/growth forget reports a miss as a warning", async () => {
	const host = new FakeHost();
	host.responses["growth.forget"] = { removed: false };
	await run("/growth forget style:missing", host);
	assert.equal(host.last().kind, "warning");
	assert.match(host.last().text, /No matching note/);
});

test("notices mark success, warning, and error with text cues", async () => {
	const { Notice } = await import("../src/components.ts");
	const line = (kind: "info" | "plain" | "success" | "warning" | "error") =>
		stripTerminalSequences(new Notice("hello", kind).render(40).join("\n"));
	assert.match(line("success"), /✓ hello/);
	assert.match(line("warning"), /! hello/);
	assert.match(line("error"), /✗ hello/);
	assert.doesNotMatch(line("plain"), /[✓!✗] hello/);
	assert.doesNotMatch(line("info"), /[✓!✗] hello/);
});

const PROFILE = {
	userId: "default",
	displayName: "",
	preferences: {},
	interactionCount: 3,
	topTopics: [],
	updatedAt: null,
};

test("bare /profile view shows the current profile without switching", async () => {
	const host = new FakeHost();
	host.responses["profile.get"] = { profile: PROFILE };
	host.picks.push("view");
	await run("/profile", host);
	assert.deepEqual(host.pickTitles, ["Profile"]);
	assert.match(host.last().text, /default/);
	assert.equal(host.profileId(), "default");
	assert.equal(host.submitted.length, 0);
});

test("bare /profile new creates a slug id and switches to it", async () => {
	const host = new FakeHost();
	host.responses["profile.get"] = { profile: { ...PROFILE, userId: "work" } };
	host.picks.push("new");
	host.textQueue.push("work");
	await run("/profile", host);
	assert.deepEqual(host.calls.at(-1), { method: "profile.get", params: { userId: "work" } });
	assert.equal(host.profileId(), "work");
	assert.match(host.last().text, /Switched to profile work/);
});

test("bare /profile new rejects non-slug ids without creating", async () => {
	const host = new FakeHost();
	host.picks.push("new");
	host.textQueue.push("My Work!", undefined);
	await run("/profile", host);
	assert.match(host.printed.map((p) => p.text).join("\n"), /lowercase letters, numbers, and dashes/);
	assert.equal(host.calls.length, 0);
	assert.equal(host.profileId(), "default");
});

test("bare /profile switch changes the active profile only", async () => {
	const host = new FakeHost();
	host.responses["profile.list"] = {
		profiles: [PROFILE, { ...PROFILE, userId: "work", interactionCount: 9 }],
	};
	host.picks.push("switch", "work");
	await run("/profile", host);
	assert.equal(host.profileId(), "work");
	assert.match(host.last().text, /existing chats keep theirs/);

	const same = new FakeHost();
	same.responses["profile.list"] = { profiles: [PROFILE] };
	same.picks.push("switch", "default");
	await run("/profile", same);
	assert.equal(same.profileId(), "default");
	assert.equal(same.printed.length, 0);
});

test("/profile set writes to the current profile", async () => {
	const host = new FakeHost();
	host.currentProfile = "work";
	host.responses["profile.set"] = { profile: { ...PROFILE, userId: "work" } };
	await run("/profile theme dark", host);
	assert.deepEqual(host.calls.at(-1), {
		method: "profile.set",
		params: { userId: "work", key: "theme", value: "dark" },
	});
});

test("/profiles marks the current profile", async () => {
	const host = new FakeHost();
	host.currentProfile = "work";
	host.responses["profile.list"] = {
		profiles: [PROFILE, { ...PROFILE, userId: "work", interactionCount: 9 }],
	};
	await run("/profiles", host);
	assert.match(host.last().text, /work \(current\)/);
});

test("new chats carry the profile agent scope only off-default", async () => {
	const fresh = new FakeHost();
	fresh.responses["session.create"] = { session: { ...SESSION } };
	await run("/new", fresh);
	assert.deepEqual(fresh.calls.at(-1), { method: "session.create", params: { title: "" } });

	const scoped = new FakeHost();
	scoped.currentProfile = "work";
	scoped.responses["session.create"] = { session: { ...SESSION } };
	await run("/new", scoped);
	assert.deepEqual(scoped.calls.at(-1), {
		method: "session.create",
		params: { title: "", agent: "harness-work" },
	});
});

test("H02 /theme menu commit persists exactly once with success", async () => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const host = new FakeHost();
	host.responses["config.set"] = { key: "theme", value: "dracula" };
	host.themeQueue.push("dracula");
	const before = getSkin();
	try {
		await run("/theme", host);
		assert.equal(getSkin(), "dracula");
		const sets = host.calls.filter((c) => c.method === "config.set");
		assert.deepEqual(sets, [{ method: "config.set", params: { key: "theme", value: "dracula", persist: true } }]);
		assert.match(host.last().text, /Skin: dracula/);
	} finally {
		setSkin(before);
	}
});

test("H02 /theme menu cancel restores and persists nothing", async () => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const host = new FakeHost();
	host.themeQueue.push(undefined);
	const before = getSkin();
	setSkin("mono");
	try {
		await run("/theme", host);
		assert.equal(getSkin(), "mono", "cancel restores the original theme");
		assert.ok(!host.calls.some((c) => c.method === "config.set"), "no persistence on cancel");
	} finally {
		setSkin(before);
	}
});

test("H02 /theme save failure keeps the temporary theme with a warning", async () => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const host = new FakeHost();
	host.responses["config.set"] = () => {
		throw new Error("database is down");
	};
	host.themeQueue.push("light");
	const before = getSkin();
	try {
		await run("/theme", host);
		assert.equal(getSkin(), "light", "chosen theme stays applied temporarily");
		assert.equal(host.calls.filter((c) => c.method === "config.set").length, 1, "persist attempted exactly once");
		assert.match(host.last().text, /applied for now/);
		assert.equal(host.last().kind, "warning", "failed save never claims saved");
	} finally {
		setSkin(before);
	}
});

test("/mcp lists real harness inventory and reports an honest empty state", async () => {
 const host = new FakeHost();
 host.responses["mcp.list"] = {servers:[]};
 await run("/mcp", host);
 assert.deepEqual(host.calls, [{method:"mcp.list", params:{}}]);
 assert.match(host.last().text, /No MCP servers registered/);
 assert.equal(host.pickTitles.length, 1);
 assert.ok(SLASH_COMMANDS.some(command => command.name === "mcp"));
});

test("/mcp opens a connection picker then fetches fresh status without connecting", async () => {
 const host = new FakeHost();
 const server = {id:"docs", display_name:"Docs", state:"connected", enabled:true, transport:"stdio", server_name:"fixture", server_version:"", protocol_version:"2025-11-25", error:null};
 host.responses["mcp.list"] = {servers:[server]};
 host.responses["mcp.status"] = {server};
 host.picks.push("docs");
 await run("/mcp", host);
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list", "mcp.status"]);
 assert.deepEqual(host.calls[1]!.params, {id:"docs"});
 assert.match(host.last().text, /Docs.*connected/s);
 assert.match(host.last().text, /2025-11-25/);
 assert.doesNotMatch(host.last().text, /connected.*tools available/i);
});

test("/mcp status and list inspect without launching; cancel and malformed commands are inert", async () => {
 const host = new FakeHost();
 host.responses["mcp.status"] = {server:{id:"docs", display_name:"Docs", state:"disconnected", enabled:false, transport:"stdio", error:null}};
 await run("/mcp status docs", host);
 assert.deepEqual(host.calls, [{method:"mcp.status", params:{id:"docs"}}]);
 assert.match(host.last().text, /disabled/);
 host.responses["mcp.list"] = {servers:[{id:"docs", display_name:"Docs", state:"disconnected", enabled:false, transport:"stdio"}]};
 await run("/mcp list", host);
 assert.match(host.last().text, /Docs.*disabled/);
 await run("/mcp", host);
 assert.equal(host.calls.at(-1)!.method, "mcp.list");
 const count = host.calls.length;
 await run("/mcp connect docs", host);
 assert.equal(host.calls.length, count);
 assert.match(host.last().text, /Usage:/);
});

const MCP_DOCS = { id: "docs", display_name: "Docs", enabled: true, transport: "stdio", state: "disconnected", error: null };
function mcpHost(): FakeHost {
 const host = new FakeHost();
 host.responses["mcp.list"] = { servers: [MCP_DOCS] };
 host.responses["mcp.status"] = { server: MCP_DOCS };
 host.responses["mcp.preview"] = { digest: "reviewed-digest", proposal: { argv: ["node", "server.js"], cwd: "/workspace", network: [], backend: "subprocess" } };
 return host;
}
test("/mcp adds a stdio server through forms without launching", async () => {
 const host = mcpHost();
 host.picks.push("__add__", "stdio");
 host.textQueue.push("local-docs", "Local docs", "node", '["server.js", "with spaces"]', "", "{}");
 host.responses["mcp.configure"] = { server: { ...MCP_DOCS, id: "local-docs" } };
 await run("/mcp", host);
 assert.deepEqual(host.calls.at(-1), { method: "mcp.configure", params: { config: {
  id: "local-docs", display_name: "Local docs", transport: "stdio", command: "node", argv: ["server.js", "with spaces"], trust: "untrusted",
 } } });
 assert.equal(host.last().kind, "success");
 assert.ok(host.asked.every(field => !field.secret));
});
test("/mcp cancelling an add form never saves configuration", async () => {
 const host = mcpHost();
 host.picks.push("__add__", "stdio");
 host.textQueue.push("docs", undefined);
 await run("/mcp", host);
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list"]);
});
test("/mcp connects only after confirmation with the active session", async () => {
 const host = mcpHost();
 host.picks.push("docs", "connect", "yes");
 host.responses["mcp.connect"] = { server: { ...MCP_DOCS, state: "connected" } };
 await run("/mcp", host);
 assert.deepEqual(host.calls.at(-1), { method: "mcp.connect", params: { id: "docs", sessionId: SESSION.id, digest: "reviewed-digest" } });
 assert.match(host.last().text, /connected/);
});
test("/mcp denied connection never launches a server", async () => {
 const host = mcpHost();
 host.picks.push("docs", "connect", "no");
 await run("/mcp", host);
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list", "mcp.status", "mcp.preview"]);
 assert.match(host.pickTitles.at(-1)!, /node.*server.js/s);
 assert.match(host.pickTitles.at(-1)!, /workspace/);
});
test("/mcp removal requires confirmation and reports the outcome", async () => {
 for (const choice of ["no", "yes"]) {
  const host = mcpHost();
  host.picks.push("docs", "remove", choice);
  host.responses["mcp.remove"] = { removed: true };
  await run("/mcp", host);
  assert.equal(host.calls.some(call => call.method === "mcp.remove"), choice === "yes");
  if (choice === "yes") assert.equal(host.last().kind, "success");
 }
});
test("/mcp connection failure reports an error without claiming success", async () => {
 const host = mcpHost();
 host.picks.push("docs", "connect", "yes");
 host.responses["mcp.connect"] = () => { throw new Error("Connection denied by policy"); };
 await run("/mcp", host);
 assert.equal(host.last().kind, "error");
 assert.match(host.last().text, /denied/);
});
test("/mcp shows cached tools without connecting and marks truncation", async () => {
 const host = mcpHost();
 host.picks.push("docs", "tools");
 host.responses["mcp.tools"] = { tools: [{ name: "lookup", description: "Look up documentation" }], truncated: true };
 await run("/mcp", host);
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list", "mcp.status", "mcp.tools"]);
 assert.match(host.last().text, /lookup/);
 assert.match(host.last().text, /truncated/i);
});
test("/mcp advanced configuration rejects secret values", async () => {
 const host = mcpHost();
 host.picks.push("__add__", "streamable_http", "none");
 host.textQueue.push("docs", "Docs", "https://example.com/mcp", '{"headers":{"Authorization":"secret"}}');
 await run("/mcp", host);
 assert.equal(host.last().kind, "error");
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list"]);
});

test("/mcp configures HTTP with reference names and allowlist without connecting", async () => {
 const host = mcpHost();
 host.picks.push("__add__", "streamable_http", "none");
 host.textQueue.push("remote", "Remote docs", "https://example.com/mcp", '{"secret_refs":["DOCS_TOKEN"],"tool_allowlist":["lookup"],"local_access":false}');
 host.responses["mcp.configure"] = { server: { ...MCP_DOCS, id: "remote", transport: "streamable_http" } };
 await run("/mcp", host);
 assert.deepEqual(host.calls.at(-1), { method: "mcp.configure", params: { config: { id: "remote", display_name: "Remote docs", transport: "streamable_http", endpoint: "https://example.com/mcp", trust: "untrusted", secret_refs: ["DOCS_TOKEN"], tool_allowlist: ["lookup"], local_access: false } } });
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list", "mcp.configure"]);
});
test("/mcp disconnects a connected server without starting it", async () => {
 const host = mcpHost();
 host.responses["mcp.status"] = { server: { ...MCP_DOCS, state: "connected" } };
 host.responses["mcp.disconnect"] = { server: MCP_DOCS };
 host.picks.push("docs", "disconnect");
 await run("/mcp", host);
 assert.deepEqual(host.calls.at(-1), { method: "mcp.disconnect", params: { id: "docs" } });
 assert.match(host.last().text, /disconnected/);
});
test("/mcp needs an active session before previewing or connecting", async () => {
 const host = mcpHost();
 host.current = undefined;
 host.picks.push("docs", "connect", "yes");
 await run("/mcp", host);
 assert.equal(host.last().kind, "error");
 assert.match(host.last().text, /No active session/);
 assert.deepEqual(host.calls.map(call => call.method), ["mcp.list", "mcp.status"]);
});
test("/mcp rejected argv and credential endpoints are never configured", async () => {
 for (const transport of ["stdio", "streamable_http"]) {
  const host = mcpHost();
  host.picks.push("__add__", transport);
  host.textQueue.push("docs", "Docs", ...(transport === "stdio" ? ["node", '["ok", 42]'] : ["https://user:password@example.com/mcp"]));
  await run("/mcp", host);
  assert.equal(host.last().kind, "error");
  assert.deepEqual(host.calls.map(call => call.method), ["mcp.list"]);
 }
});

test("/mcp supports trusted_read configuration without granting broader trust", async () => {
 const host = mcpHost();
 host.picks.push("__add__", "streamable_http", "none");
 host.textQueue.push("docs", "Docs", "https://example.com/mcp", '{"trust":"trusted_read"}');
 host.responses["mcp.configure"] = { server: MCP_DOCS };
 await run("/mcp", host);
 assert.equal((host.calls.at(-1)!.params.config as Record<string, unknown>).trust, "trusted_read");
 assert.equal(host.last().kind, "success");
});
test("/mcp cached tools report the policy verdict alongside the tool", async () => {
 const host = mcpHost();
 host.picks.push("docs", "tools");
 host.responses["mcp.tools"] = { tools: [{ name: "docs__lookup", remote_name: "lookup", description: "Read docs", verdict: "allow" }, { name: "docs__write", remote_name: "write", description: "Write docs", verdict: "deny" }], truncated: false };
 await run("/mcp", host);
 assert.match(host.last().text, /docs__lookup.*allow/);
 assert.match(host.last().text, /docs__write.*deny/);
});

test("/mcp rejects invalid server IDs before saving or collecting further fields", async () => {
 for (const id of ["Docs", "docs.local", "a".repeat(65), "_docs"]) {
  const host = mcpHost();
  host.picks.push("__add__", "stdio");
  host.textQueue.push(id);
  await run("/mcp", host);
  assert.equal(host.last().kind, "error", id);
  assert.equal(host.asked.length, 1, id);
  assert.deepEqual(host.calls.map(call => call.method), ["mcp.list"], id);
 }
});
test("/mcp connection confirmation shows the reviewed safe configuration", async () => {
 const host = mcpHost();
 host.picks.push("docs", "connect", "no");
 host.responses["mcp.preview"] = { digest: "config-digest", proposal: { argv: ["node", "server.js"], cwd: "/workspace", network: [], backend: "subprocess", contentPreview: '{"trust":"untrusted","tool_allowlist":["lookup"],"secret_refs":["DOCS_TOKEN"]}' } };
 await run("/mcp", host);
 assert.match(host.pickTitles.at(-1)!, /tool_allowlist.*lookup/);
 assert.match(host.pickTitles.at(-1)!, /secret_refs.*DOCS_TOKEN/);
 assert.match(host.pickTitles.at(-1)!, /trust.*untrusted/);
 assert.equal(host.calls.some(call => call.method === "mcp.connect"), false);
});
