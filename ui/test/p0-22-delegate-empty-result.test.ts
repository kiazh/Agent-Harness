// P0-22: `/delegate` assumed `agents.run` always returns at least one result.
//
// RED before fix: `const r = results[0]!;` then `r.status` threw a TypeError on
// an empty `results` array (child failed before producing a result, malformed
// response, server-side short-circuit). The command surfaced a cryptic crash
// instead of an honest failure. GREEN: an empty/malformed reply reports a clear
// error notice and returns without touching the result.

import assert from "node:assert/strict";
import { test } from "node:test";
import type { SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { runCommand, type FeatureHost, type NoticeKind } from "../src/features/index.ts";
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
	current: SessionInfo | undefined = SESSION;
	responses: Record<string, unknown> = {};

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.calls.push({ method, params });
		const response = this.responses[method];
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		return response as T;
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
	switchTo(_session: SessionInfo, _history: HistoryEntry[]) {}
	updateSession() {}
	async pick(_title: string, _items: SelectItem[]) {
		return undefined;
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

test("P0-22 /delegate reports an empty agents.run result instead of crashing", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = { results: [] };
	await run("/delegate researcher find X", host);

	assert.equal(host.markdown.length, 0, "no result must not render as an answer");
	const notice = host.last();
	assert.equal(notice.kind, "error", "the failure is reported as an error");
	assert.match(notice.text, /delegation returned no result/i);
});

test("P0-22 /delegate reports a missing results field instead of crashing", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = {};
	await run("/delegate researcher find X", host);
	assert.equal(host.markdown.length, 0);
	assert.match(host.last().text, /delegation returned no result/i);
});

test("P0-22 /delegate rejects a non-object first result instead of dereferencing it", async () => {
	for (const bad of [null, "nope", 42, {}]) {
		const host = new FakeHost();
		host.responses["agents.run"] = { results: [bad] };
		await run("/delegate researcher find X", host);
		assert.equal(host.markdown.length, 0, `malformed result ${JSON.stringify(bad)} renders no answer`);
		assert.equal(host.last().kind, "error");
		assert.match(host.last().text, /invalid result/i);
	}
});

test("P0-22 a well-formed single result still renders normally", async () => {
	const host = new FakeHost();
	host.responses["agents.run"] = {
		results: [{ agent: "researcher", task: "find X", response: "found X", tokens: 3, status: "complete" }],
	};
	await run("/delegate researcher find X", host);
	assert.equal(host.printed.filter((p) => p.kind === "error").length, 0);
	assert.match(host.markdown.at(-1)!, /found X/);
});
