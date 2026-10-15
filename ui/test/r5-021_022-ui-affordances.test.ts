// R5-QR (findings 21 + 22) — UI affordances RED/GREEN.
// Q1: optional secret input has an explicit keyless path; Escape stays cancel.
// R1: full-UUID memory forget/share/approve/reject bypass the bounded newest-N
//     window via the ownership-checked backend call; abbreviations stay bounded
//     and the scope is disclosed.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, TuiAltScreen, type SelectItem, type Terminal } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { runCommand } from "../src/features/index.ts";
import type { AskTextOptions, FeatureHost, NoticeKind, OAuthProgressHandle, OAuthProgressOptions } from "../src/features/types.ts";
import { SecretForm } from "../src/components/Form.ts";
import type { HistoryEntry, SessionInfo } from "../src/protocol.ts";

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
	picks: Array<string | undefined> = [];
	textQueue: Array<string | undefined> = [];
	asked: AskTextOptions[] = [];
	current: SessionInfo | undefined = SESSION;
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
	printMarkdown(_text: string) {}
	session() {
		return this.current;
	}
	async submitTurn(_text: string) {}
	switchTo(_session: SessionInfo, _history: HistoryEntry[]) {}
	updateSession(_session: SessionInfo) {}
	async pick(_title: string, items: SelectItem[]) {
		const value = this.picks.shift();
		return items.find((i) => i.value === value);
	}
	async pickMany() {
		return undefined;
	}
	async pickTheme() {
		return undefined;
	}
	async askText(options: AskTextOptions): Promise<string | undefined> {
		this.asked.push(options);
		return this.textQueue.shift();
	}
	showOAuthProgress(_options: OAuthProgressOptions): OAuthProgressHandle {
		return { update() {}, dispose() {} };
	}
	profileId() {
		return "default";
	}
	setProfile() {}
	sessionAgent() {
		return undefined;
	}
	async inspectOutput() {}
	async writeFile(path: string, _text: string) {
		return path;
	}
	onConfig() {}
	clear() {}
	banner() {}
	restyle() {}
	async exit() {}
	called(method: string) {
		return this.calls.filter((c) => c.method === method);
	}
}

function terminal(columns = 80, rows = 24) {
	const term = {
		columns, rows, kittyProtocolActive: false,
		start() {}, stop() {}, async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	} as unknown as Terminal;
	return { term };
}

// Full UUIDs that are NOT in the newest-500 window fixtures below.
const OLD_MEMORY = "aaaaaaaa-1111-2222-3333-444444444444";
const OLD_PENDING = "bbbbbbbb-1111-2222-3333-444444444444";

function windowMemories(count: number) {
	return Array.from({ length: count }, (_, i) => ({
		id: `c0000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
		content: `memory ${i}`,
		category: "fact",
		importance: 0.5,
		access_count: 0,
		created_at: null,
		session_id: null,
	}));
}

// ─── Q1: optional secret field ─────────────────────────────────────────────

test("R5-021 optional SecretForm: blank Enter submits empty (explicit keyless path)", () => {
	const { term } = terminal();
	const tui = new TuiAltScreen(term);
	try {
		const saved: string[] = [];
		const form = new SecretForm(tui, { title: "API key for x (optional)", allowEmpty: true });
		form.onSubmit = (value) => void saved.push(value);
		form.handleInput("\r");
		assert.deepEqual(saved, [""], "blank submits as the keyless path");
	} finally {
		tui.stop();
	}
});

test("R5-021 optional SecretForm: blank Ctrl+S submits empty, Escape still cancels", () => {
	const { term } = terminal();
	const tui = new TuiAltScreen(term);
	try {
		const saved: string[] = [];
		let cancelled = 0;
		const form = new SecretForm(tui, { title: "API key for x (optional)", allowEmpty: true });
		form.onSubmit = (value) => void saved.push(value);
		form.onCancel = () => void cancelled++;
		form.handleInput("\x13");
		assert.deepEqual(saved, [""], "blank Ctrl+S submits as the keyless path");
		const cancelForm = new SecretForm(tui, { title: "API key for x (optional)", allowEmpty: true });
		let cancelSaved = 0;
		cancelForm.onSubmit = () => void cancelSaved++;
		cancelForm.onCancel = () => void cancelled++;
		cancelForm.handleInput("x");
		cancelForm.handleInput("\x1b");
		assert.equal(cancelSaved, 0, "Escape never submits");
		assert.equal(cancelled, 1, "Escape cancels");
		assert.equal(cancelForm.getValue(), "", "references cleared after cancel");
	} finally {
		tui.stop();
	}
});

test("R5-021 required SecretForm still rejects blank (no keyless path elsewhere)", () => {
	const { term } = terminal();
	const tui = new TuiAltScreen(term);
	try {
		const form = new SecretForm(tui, { title: "Set K" });
		let saved = 0;
		form.onSubmit = () => void saved++;
		form.handleInput("\r");
		assert.equal(saved, 0, "blank still rejected without allowEmpty");
		assert.match(form.render(80).map(stripTerminalSequences).join("\n"), /Value is required/);
	} finally {
		tui.stop();
	}
});

test("R5-021 /provider add: blank key proceeds keyless, Escape cancels without a request", async () => {
	const keyless = new FakeHost();
	keyless.textQueue = ["my-vllm", "http://127.0.0.1:8000/v1", ""];
	keyless.picks = ["yes"];
	keyless.responses = {
		"providers.add": { provider: "my-vllm", baseUrl: "http://127.0.0.1:8000/v1", models: [], keySaved: false, keySet: false },
	};
	await runCommand(parseCommand("/provider add")!, keyless);
	const add = keyless.called("providers.add");
	assert.equal(add.length, 1, "keyless submit reaches the backend");
	assert.equal(add[0]!.params["apiKey"], undefined, "no key is sent for the keyless path");

	const cancelled = new FakeHost();
	cancelled.textQueue = ["my-vllm", "http://127.0.0.1:8000/v1", undefined];
	cancelled.picks = ["yes"];
	cancelled.responses = {
		"providers.add": () => {
			throw new Error("must not be called after cancel");
		},
	};
	await runCommand(parseCommand("/provider add")!, cancelled);
	assert.equal(cancelled.called("providers.add").length, 0, "Escape cancels the whole add flow");
});

// ─── R1: full-UUID bypass of the bounded window ─────────────────────────────

test("R5-022 /memory forget <full-uuid> bypasses the newest-500 window", async () => {
	const host = new FakeHost();
	host.responses = {
		"memory.list": { memories: windowMemories(500), total: 501 },
		"memory.forget": { deleted: true },
	};
	await runCommand(parseCommand(`/memory forget ${OLD_MEMORY}`)!, host);
	const forgets = host.called("memory.forget");
	assert.equal(forgets.length, 1, "full UUID goes straight to the ownership-checked backend");
	assert.equal(forgets[0]!.params["id"], OLD_MEMORY);
	assert.equal(host.called("memory.list").length, 0, "bounded window is not consulted for a full UUID");
});

test("R5-022 /memory share <full-uuid> bypasses the newest-500 window", async () => {
	const host = new FakeHost();
	host.responses = {
		"memory.list": { memories: windowMemories(500), total: 501 },
		"memory.share": { status: "delivered" },
	};
	await runCommand(parseCommand(`/memory share ${OLD_MEMORY} teammate`)!, host);
	const shares = host.called("memory.share");
	assert.equal(shares.length, 1);
	assert.equal(shares[0]!.params["id"], OLD_MEMORY);
	assert.equal(shares[0]!.params["recipientAgent"], "teammate");
	assert.equal(host.called("memory.list").length, 0, "bounded window is not consulted for a full UUID");
});

test("R5-022 /memory approve <full-uuid> bypasses the pending window", async () => {
	const host = new FakeHost();
	host.responses = {
		"memory.pending": { pending: [] },
		"memory.approve": { memory: { id: OLD_PENDING } },
	};
	await runCommand(parseCommand(`/memory approve ${OLD_PENDING}`)!, host);
	const approvals = host.called("memory.approve");
	assert.equal(approvals.length, 1, "full UUID goes straight to the backend");
	assert.equal(approvals[0]!.params["id"], OLD_PENDING);
	assert.equal(host.called("memory.pending").length, 0, "bounded window is not consulted for a full UUID");
});

test("R5-022 /memory reject <full-uuid> bypasses the pending window", async () => {
	const host = new FakeHost();
	host.responses = {
		"memory.pending": { pending: [] },
		"memory.reject": { rejected: true },
	};
	await runCommand(parseCommand(`/memory reject ${OLD_PENDING}`)!, host);
	assert.equal(host.called("memory.reject").length, 1);
	assert.equal(host.called("memory.pending").length, 0);
});

test("R5-022 abbreviations stay bounded and misses disclose the full-ID path", async () => {
	const host = new FakeHost();
	host.responses = {
		"memory.list": { memories: windowMemories(500), total: 501 },
	};
	await runCommand(parseCommand("/memory forget deadbeef")!, host);
	assert.equal(host.called("memory.list").length, 1, "abbreviations still use the bounded lookup");
	assert.equal(host.called("memory.forget").length, 0, "miss never reaches the backend");
	const errors = host.printed.filter((p) => p.kind === "error").map((p) => p.text).join("\n");
	assert.match(errors, /newest 500/, "miss discloses the bounded scope");
	assert.match(errors, /full ID/i, "miss discloses the full-ID escape hatch");
});

// ─── R1 (learning): latest-100 bypass ───────────────────────────────────────

const OLD_PROPOSAL = "dddddddd-1111-2222-3333-444444444444";

test("R5-022 /skills approve <full-uuid> bypasses the latest-100 window", async () => {
	const host = new FakeHost();
	host.responses = {
		"learning.list": { reviews: windowProposals(100) },
		"learning.approve": { review: { id: OLD_PROPOSAL, status: "approved" } },
	};
	await runCommand(parseCommand(`/skills approve ${OLD_PROPOSAL}`)!, host);
	const approvals = host.called("learning.approve");
	assert.equal(approvals.length, 1, "full UUID goes straight to the ownership-checked backend");
	assert.equal(approvals[0]!.params["id"], OLD_PROPOSAL);
	assert.equal(host.called("learning.list").length, 0, "latest-100 window is not consulted for a full UUID");
});

test("R5-022 /skills revert <full-uuid> bypasses the latest-100 window", async () => {
	const host = new FakeHost();
	host.responses = {
		"learning.list": { reviews: windowProposals(100) },
		"learning.revert": { review: { id: OLD_PROPOSAL, status: "reverted" } },
	};
	await runCommand(parseCommand(`/skills revert ${OLD_PROPOSAL}`)!, host);
	assert.equal(host.called("learning.revert").length, 1);
	assert.equal(host.called("learning.list").length, 0, "latest-100 window is not consulted for a full UUID");
});

test("R5-022 /skills approve abbreviations stay bounded and disclose the full-ID path", async () => {
	const host = new FakeHost();
	host.responses = {
		"learning.list": { reviews: windowProposals(100) },
	};
	await runCommand(parseCommand("/skills approve deadbeef")!, host);
	assert.equal(host.called("learning.list").length, 1, "abbreviations still use the bounded lookup");
	assert.equal(host.called("learning.approve").length, 0, "miss never reaches the backend");
	const errors = host.printed.filter((p) => p.kind === "error").map((p) => p.text).join("\n");
	assert.match(errors, /latest 100/, "miss discloses the bounded scope");
	assert.match(errors, /full ID/i, "miss discloses the full-ID escape hatch");
});

function windowProposals(count: number) {
	return Array.from({ length: count }, (_, i) => ({
		id: `e0000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
		sessionId: SESSION.id,
		agentId: "agent",
		status: "pending",
		name: `skill ${i}`,
		description: null,
		triggers: [],
		content: null,
		reason: "auto",
	}));
}
