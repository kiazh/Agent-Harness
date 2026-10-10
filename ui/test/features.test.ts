import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, type SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "../src/features/index.ts";
import { resolvePrefix, table } from "../src/format.ts";
import type { ConfigResult, HistoryEntry, SessionInfo } from "../src/protocol.ts";

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
	async pick(_title: string, items: SelectItem[]) {
		const value = this.picks.shift();
		return items.find((i) => i.value === value);
	}
	async writeFile(path: string, text: string) {
		this.files.set(path, text);
		return `/abs/${path}`;
	}
	onConfig(result: ConfigResult) {
		this.configured.push(result);
	}
	clear() {}
	banner() {}
	async exit() {}

	last(): { text: string; kind: NoticeKind } {
		return this.printed.at(-1)!;
	}
}

const run = (text: string, host: FakeHost) => runCommand(parseCommand(text)!, host);

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
	await run("/delete", host);
	assert.equal(host.calls.length, 0, "cancel sends nothing");

	host.picks = ["yes"];
	await run("/delete", host);
	assert.deepEqual(host.calls.map((c) => c.method), ["session.delete", "session.create"]);
	assert.equal(host.current?.id, "99999999-0000-0000-0000-000000000000");
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
	await run("/default-model openai/gpt-4o", host);
	assert.equal(host.configured[0]!.model, "openai/gpt-4o");
	assert.deepEqual(host.calls.at(-1)!.params, { key: "model", value: "openai/gpt-4o", persist: true });
});

test("/default-model with no args opens the menu and saves the pick", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "p", key: p.key, value: p.value });
	host.picks.push("deepseek::deepseek-chat");
	await run("/default-model", host);
	assert.deepEqual(
		host.calls.map((c) => [c.method, c.params]),
		[
			["config.set", { key: "provider", value: "deepseek", persist: true }],
			["config.set", { key: "model", value: "deepseek-chat", persist: true }],
		],
	);
	assert.match(host.last().text, /saved as default/);
});

test("/default-model resolves a curated id directly", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "p", key: p.key, value: p.value });
	await run("/default-model deepseek-chat", host);
	assert.deepEqual(
		host.calls.map((c) => [c.method, c.params]),
		[
			["config.set", { key: "provider", value: "deepseek", persist: true }],
			["config.set", { key: "model", value: "deepseek-chat", persist: true }],
		],
	);
});

test("/model is an alias for /default-model", () => {
	assert.deepEqual(parseCommand("/model deepseek-chat"), { name: "default-model", args: "deepseek-chat" });
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

test("/keys with no args opens the picker menu", async () => {
	const host = new FakeHost();
	host.responses["secrets.list"] = {
		secrets: [{ key: "OPENAI_API_KEY", description: "OpenAI", set: false, liveSet: false }],
		envFile: "/tmp/.env",
	};
	host.picks.push("OPENAI_API_KEY");
	await run("/keys", host);
	assert.match(host.last().text, /\/keys set OPENAI_API_KEY/);
});

test("/models picker applies the curated model and provider", async () => {
	const host = new FakeHost();
	host.responses["config.set"] = (p: Record<string, unknown>) => ({ model: "m", provider: "p", key: p.key, value: p.value });
	host.picks.push("ollama::llama3.1");
	await run("/models", host);
	assert.deepEqual(
		host.calls.map((c) => [c.method, c.params]),
		[
			["config.set", { key: "provider", value: "ollama" }],
			["config.set", { key: "model", value: "llama3.1" }],
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
		await run("/theme nope", host);
		assert.match(host.last().text, /Unknown skin/);
	} finally {
		setSkin(before);
	}
});
