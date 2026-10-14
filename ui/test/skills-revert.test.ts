// Skills proposals listing (pending + auto-applied) and post-hoc revert.
// Auto-applied learning rows stay visible so a human can undo them, and
// /skills revert publishes that decision through learning.revert.

import assert from "node:assert/strict";
import { test } from "node:test";
import type { SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { type FeatureHost, type NoticeKind, runCommand } from "../src/features/index.ts";
import type { AskTextOptions, OAuthProgressHandle, OAuthProgressOptions } from "../src/features/types.ts";
import type { SessionInfo } from "../src/protocol.ts";

const SESSION: SessionInfo = {
	id: "11111111-2222-3333-4444-555555555555",
	title: "current",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

class SkillsFakeHost implements FeatureHost {
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	responses: Record<string, unknown | ((params: Record<string, unknown>) => unknown)> = {};
	current: SessionInfo | undefined = SESSION;

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
	switchTo(_session: SessionInfo, _history: never[]) {}
	updateSession(_session: SessionInfo) {}
	async pick(_title: string, _items: SelectItem[]) {
		return undefined;
	}
	async pickMany(_title: string, _items: SelectItem[]) {
		return undefined;
	}
	async pickTheme() {
		return undefined;
	}
	async askText(_options: AskTextOptions): Promise<string | undefined> {
		return undefined;
	}
	async inspectOutput(_toolId?: string) {}
	profileId() {
		return "default";
	}
	setProfile(_userId: string) {}
	sessionAgent() {
		return undefined;
	}
	async writeFile(path: string, _text: string) {
		return `/abs/${path}`;
	}
	onConfig(_result: unknown) {}
	clear() {}
	banner() {}
	restyle() {}
	async exit() {}
	showOAuthProgress(_options: OAuthProgressOptions): OAuthProgressHandle {
		return { update() {}, dispose() {} };
	}
	last() {
		return this.printed.at(-1)!;
	}
}

const run = (text: string, host: SkillsFakeHost) => runCommand(parseCommand(text)!, host);

const PENDING_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const APPLIED_ID = "bbbbbbbb-1111-2222-3333-444444444444";
const REVERTED_ID = "cccccccc-1111-2222-3333-444444444444";

function review(over: Record<string, unknown>): Record<string, unknown> {
	return {
		sessionId: SESSION.id,
		agentId: "harness",
		name: "deploy-checks",
		description: "Check a deployment",
		triggers: ["deploy"],
		content: "Check health and ready.",
		reason: "",
		...over,
	};
}

test("/skills proposals lists pending and auto-applied rows, marking auto-applied", async () => {
	const host = new SkillsFakeHost();
	host.responses["learning.list"] = {
		reviews: [
			review({ id: APPLIED_ID, name: "auto-checks", status: "approved", autoApplied: true }),
			review({ id: PENDING_ID, name: "staged-checks", status: "pending", autoApplied: false }),
			review({ id: REVERTED_ID, name: "undone-checks", status: "reverted", autoApplied: true }),
		],
	};
	await run("/skills proposals", host);
	assert.deepEqual(host.calls[0], {
		method: "learning.list",
		params: { sessionId: SESSION.id, limit: 50 },
	});
	const text = host.last().text;
	assert.match(text, /auto-checks/);
	assert.match(text, /auto-applied/);
	assert.match(text, /staged-checks/);
	assert.match(text, /pending/);
	assert.doesNotMatch(text, /undone-checks/, "reverted rows are history, not proposals");
});

test("/skills proposals still accepts rows from servers without autoApplied", async () => {
	const host = new SkillsFakeHost();
	host.responses["learning.list"] = {
		reviews: [review({ id: PENDING_ID, name: "staged-checks", status: "pending" })],
	};
	await run("/skills proposals", host);
	assert.match(host.last().text, /staged-checks/);
});

test("/skills revert resolves an auto-applied row and calls learning.revert", async () => {
	const host = new SkillsFakeHost();
	host.responses["learning.list"] = {
		reviews: [
			review({ id: APPLIED_ID, name: "auto-checks", status: "approved", autoApplied: true }),
			review({ id: PENDING_ID, name: "staged-checks", status: "pending", autoApplied: false }),
		],
	};
	host.responses["learning.revert"] = { review: { id: APPLIED_ID, status: "reverted" } };
	await run(`/skills revert ${APPLIED_ID.slice(0, 8)}`, host);
	assert.deepEqual(host.calls.at(-1), {
		method: "learning.revert",
		params: { sessionId: SESSION.id, id: APPLIED_ID },
	});
	assert.match(host.last().text, /reverted/);
});

test("/skills revert does not resolve pending rows (approve/reject own those)", async () => {
	const host = new SkillsFakeHost();
	host.responses["learning.list"] = {
		reviews: [review({ id: PENDING_ID, name: "staged-checks", status: "pending", autoApplied: false })],
	};
	await run(`/skills revert ${PENDING_ID.slice(0, 8)}`, host);
	assert.match(host.last().text, /No learning proposal starts with/);
	assert.equal(host.calls.filter((c) => c.method === "learning.revert").length, 0);
});

test("/skills approve still resolves only pending rows", async () => {
	const host = new SkillsFakeHost();
	host.responses["learning.list"] = {
		reviews: [
			review({ id: APPLIED_ID, name: "auto-checks", status: "approved", autoApplied: true }),
			review({ id: PENDING_ID, name: "staged-checks", status: "pending", autoApplied: false }),
		],
	};
	host.responses["learning.approve"] = { review: { id: PENDING_ID, status: "approved" } };
	await run(`/skills approve ${PENDING_ID.slice(0, 8)}`, host);
	assert.deepEqual(host.calls.at(-1), {
		method: "learning.approve",
		params: { sessionId: SESSION.id, id: PENDING_ID },
	});
});

test("/skills argument completions offer revert", async () => {
	const { skillsCommand } = await import("../src/features/skills.ts");
	const completions = (await skillsCommand.getArgumentCompletions?.("rev")) ?? null;
	assert.deepEqual(completions?.map((item) => item.value), ["revert"]);
});
