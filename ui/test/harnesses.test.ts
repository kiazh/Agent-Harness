// /harnesses: picker over interop.list, per-harness actions (Status, Register,
// Unregister, Run a task), and bounded interop.run rendering. The gateway is
// faked; no harness CLI is ever spawned.

import assert from "node:assert/strict";
import { test } from "node:test";
import type { SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { harnessesCommand } from "../src/features/harnesses.ts";
import { type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "../src/features/index.ts";
import type { AskTextOptions, OAuthProgressHandle, OAuthProgressOptions } from "../src/features/types.ts";
import type { HarnessRow, SessionInfo } from "../src/protocol.ts";

const SESSION: SessionInfo = {
	id: "11111111-2222-3333-4444-555555555555",
	title: "current",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

const ROWS: HarnessRow[] = [
	{
		key: "opencode",
		label: "OpenCode",
		installed: true,
		version: "opencode v2.0.26",
		running: true,
		registered: true,
		scope: "project",
		runSupported: false,
		runHint: null,
	},
	{
		key: "codex",
		label: "Codex",
		installed: true,
		version: "codex-cli 0.162.1",
		running: false,
		registered: false,
		scope: null,
		runSupported: true,
		runHint: "codex exec <prompt>",
	},
];

class HarnessesFakeHost implements FeatureHost {
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	pickCalls: Array<{ title: string; values: string[]; labels: string[]; descriptions: Array<string | undefined> }> = [];
	answerQueue: Array<string | undefined> = [];
	textQueue: Array<string | undefined> = [];
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
		return SESSION;
	}
	async submitTurn(_text: string) {}
	switchTo(_session: SessionInfo, _history: never[]) {}
	updateSession(_session: SessionInfo) {}
	async pick(title: string, items: SelectItem[]) {
		this.pickCalls.push({
			title,
			values: items.map((item) => item.value),
			labels: items.map((item) => item.label),
			descriptions: items.map((item) => item.description),
		});
		const value = this.answerQueue.shift();
		return items.find((item) => item.value === value);
	}
	async pickMany(_title: string, _items: SelectItem[]) {
		return undefined;
	}
	async pickTheme() {
		return undefined;
	}
	async askText(_options: AskTextOptions): Promise<string | undefined> {
		return this.textQueue.shift();
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
	text() {
		return this.printed.map((item) => item.text).join("\n");
	}
}

const run = (text: string, host: HarnessesFakeHost) => runCommand(parseCommand(text)!, host);

function hostWithRows(): HarnessesFakeHost {
	const host = new HarnessesFakeHost();
	host.responses["interop.list"] = { harnesses: ROWS, stateFile: "/tmp/interop-state.json" };
	return host;
}

const RUN_RESULT = {
	ok: true,
	exitCode: 0,
	command: "codex exec --prompt-text",
	stdout: "done\n",
	stderr: "",
	unsupportedReason: null,
};

test("/harnesses opens the picker with a state summary per harness", async () => {
	const host = hostWithRows();
	host.answerQueue.push("codex");
	host.answerQueue.push("status");
	await run("/harnesses", host);

	assert.equal(host.calls[0]!.method, "interop.list");
	const menu = host.pickCalls[0]!;
	assert.equal(menu.title, "Harnesses");
	assert.deepEqual(menu.values, ["opencode", "codex"]);
	assert.match(menu.labels[0]!, /OpenCode — ✓ installed \(opencode v2\.0\.26\) · ● running · registered \(project\)/);
	assert.match(menu.labels[1]!, /Codex — ✓ installed \(codex-cli 0\.162\.1\) · not registered · runnable/);
});

test("/harnesses Status shows version, registration, scope and the state file", async () => {
	const host = hostWithRows();
	host.answerQueue.push("opencode", "status");
	await run("/harnesses", host);
	const status = host.last();
	assert.equal(status.kind, "plain");
	assert.match(status.text, /OpenCode/);
	assert.match(status.text, /opencode v2\.0\.26/);
	assert.match(status.text, /Registered\s+yes \(project\)/);
	assert.match(status.text, /State file\s+\/tmp\/interop-state\.json/);
	assert.match(status.text, /unsupported/);
});

test("/harnesses Register confirms, then calls interop.enable", async () => {
	const host = hostWithRows();
	host.answerQueue.push("codex", "register", "yes");
	host.responses["interop.enable"] = {
		platform: "codex",
		action: "enable",
		ok: true,
		changed: true,
		message: "registered `machina` for Codex (global scope) and verified it.",
		steps: ["run: codex mcp add machina -- /usr/bin/python -m machina.mcp_server"],
		verify: { ok: true, detail: "present in `codex mcp list --json` (enabled)" },
		argv: [],
	};
	await run("/harnesses", host);

	assert.match(host.pickCalls.at(-1)!.title, /Register Machina's MCP server with Codex\?/);
	assert.deepEqual(host.calls.at(-1), { method: "interop.enable", params: { platform: "codex" } });
	assert.equal(host.printed.at(-2)!.kind, "success");
	assert.match(host.text(), /registered `machina` for Codex/);
	assert.match(host.text(), /verified: present in `codex mcp list/);
});

test("/harnesses Register cancel sends no RPC", async () => {
	const host = hostWithRows();
	host.answerQueue.push("codex", "register", "no");
	await run("/harnesses", host);
	assert.equal(host.calls.length, 1, "only interop.list ran");
});

test("/harnesses Unregister confirms, then calls interop.disable and surfaces failures honestly", async () => {
	const host = hostWithRows();
	host.answerQueue.push("opencode", "unregister", "yes");
	host.responses["interop.disable"] = {
		platform: "opencode",
		action: "disable",
		ok: false,
		changed: false,
		message: "OpenCode's CLI has no MCP remove subcommand; delete the `machina` entry manually from: opencode.json",
		steps: ["edit opencode.json and remove the `machina` entry"],
		verify: {},
		argv: [],
	};
	await run("/harnesses", host);
	assert.deepEqual(host.calls.at(-1), { method: "interop.disable", params: { platform: "opencode" } });
	assert.equal(host.printed[0]!.kind, "warning", "a failed action is never a success");
	assert.match(host.text(), /no MCP remove subcommand/);
});

test("/harnesses Run a task prompts, warns, calls interop.run and renders bounded output", async () => {
	const host = hostWithRows();
	host.answerQueue.push("codex", "run", "yes");
	host.textQueue.push("summarize the repo");
	host.responses["interop.run"] = RUN_RESULT;
	await run("/harnesses", host);

	assert.deepEqual(host.calls.at(-1), {
		method: "interop.run",
		params: { platform: "codex", prompt: "summarize the repo" },
	});
	const confirmTitle = host.pickCalls.at(-1)!.title;
	assert.match(confirmTitle, /Run this prompt inside Codex\?/);
	assert.match(confirmTitle, /host access/);
	assert.match(host.text(), /Codex: completed · exit code 0/);
	assert.match(host.text(), /command: codex exec --prompt-text/);
	assert.match(host.text(), /done/);
});

test("/harnesses Run a task cancel (or empty prompt) never calls interop.run", async () => {
	const cancelling = hostWithRows();
	cancelling.answerQueue.push("codex", "run", "no");
	cancelling.textQueue.push("do something");
	await run("/harnesses", cancelling);
	assert.ok(!cancelling.calls.some((call) => call.method === "interop.run"));

	const empty = hostWithRows();
	empty.answerQueue.push("codex", "run");
	empty.textQueue.push("   ");
	await run("/harnesses", empty);
	assert.ok(!empty.calls.some((call) => call.method === "interop.run"));
	assert.match(empty.last().text, /Empty prompt/);
});

test("/harnesses hides the run action and shows the hint when unsupported", async () => {
	const host = hostWithRows();
	host.answerQueue.push("opencode");
	await run("/harnesses", host);
	const actions = host.pickCalls[1]!;
	assert.ok(!actions.values.includes("run"), "unsupported harness must not offer Run");
	assert.deepEqual(actions.values, ["status", "register", "unregister"]);
	assert.match(host.printed[0]!.text, /run unsupported/);
});

test("/harnesses <harness> jumps straight to the actions menu", async () => {
	const host = hostWithRows();
	host.answerQueue.push("status");
	await run("/harnesses codex", host);
	assert.equal(host.calls.length, 1);
	assert.equal(host.pickCalls[0]!.title, "Codex · actions");
});

test("/harnesses rejects an unknown harness without opening a menu", async () => {
	const host = hostWithRows();
	await run("/harnesses nope", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Unknown harness "nope"/);
	assert.equal(host.pickCalls.length, 0);
});

test("/harnesses is registered as a slash command", () => {
	assert.ok(SLASH_COMMANDS.some((command) => command.name === "harnesses"));
	assert.equal(harnessesCommand.name, "harnesses");
});
