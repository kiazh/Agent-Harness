// /interop: detection table, confirmation-gated enable/disable, honest
// failure surfacing, and argument completions. The gateway is faked; no
// platform CLI is ever spawned.

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

class InteropFakeHost implements FeatureHost {
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	picks: Array<string | undefined> = [];
	pickTitles: string[] = [];
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
		this.pickTitles.push(title);
		const value = this.picks.shift();
		return items.find((item) => item.value === value);
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
	text() {
		return this.printed.map((item) => item.text).join("\n");
	}
}

const run = (text: string, host: InteropFakeHost) => runCommand(parseCommand(text)!, host);

const STATUS_PAYLOAD = {
	platforms: [
		{
			platform: "opencode",
			label: "OpenCode",
			installed: true,
			version: "opencode v2.0.26",
			running: true,
			registered: true,
			enabled: null,
			scope: "project",
			notes: ["registered outside Machina's state (shows as manual)"],
		},
		{
			platform: "codex",
			label: "Codex",
			installed: true,
			version: "codex-cli 0.162.1",
			running: false,
			registered: false,
			enabled: null,
			scope: null,
			notes: [],
		},
		{
			platform: "hermes",
			label: "Hermes",
			installed: false,
			version: null,
			running: false,
			registered: false,
			enabled: null,
			scope: null,
			notes: ["not installed (hermes not on PATH)"],
		},
	],
};

test("/interop renders the detection table with notes and hints", async () => {
	const host = new InteropFakeHost();
	host.responses["interop.status"] = STATUS_PAYLOAD;
	await run("/interop", host);
	assert.deepEqual(host.calls, [{ method: "interop.status", params: {} }]);
	const notice = host.last();
	assert.equal(notice.kind, "plain");
	assert.match(notice.text, /OpenCode/);
	assert.match(notice.text, /opencode v2\.0\.26/);
	assert.match(notice.text, /manual/, "registered without state shows as manual");
	assert.match(notice.text, /not installed \(hermes not on PATH\)/);
	assert.match(notice.text, /\/interop enable <platform>/);

	// Explicit /interop status is the same read-only path.
	const explicit = new InteropFakeHost();
	explicit.responses["interop.status"] = STATUS_PAYLOAD;
	await run("/interop status", explicit);
	assert.deepEqual(explicit.calls.map((call) => call.method), ["interop.status"]);
});

test("/interop enable confirms before writing and cancel sends no RPC", async () => {
	const host = new InteropFakeHost();
	host.picks.push("no");
	await run("/interop enable codex --scope global", host);
	assert.equal(host.pickTitles.length, 1);
	assert.match(host.pickTitles[0]!, /Register Machina's MCP server with Codex/);
	assert.match(host.pickTitles[0]!, /writes Codex config outside this repository/);
	assert.equal(host.calls.length, 0, "cancel must not reach the gateway");
});

test("/interop enable registers after confirmation and prints steps and verification", async () => {
	const host = new InteropFakeHost();
	host.picks.push("yes");
	host.responses["interop.enable"] = {
		platform: "codex",
		action: "enable",
		ok: true,
		changed: true,
		message: "registered `machina` for Codex (global scope) and verified it.",
		steps: ["run: codex mcp add machina -- /usr/bin/python -m machina.mcp_server"],
		verify: { ok: true, detail: "present in `codex mcp list --json` (enabled)" },
		argv: [["/fake/bin/codex", "mcp", "add", "machina"]],
	};
	await run("/interop enable codex --scope global", host);
	assert.deepEqual(host.calls, [
		{ method: "interop.enable", params: { platform: "codex", scope: "global", dry_run: false } },
	]);
	assert.equal(host.printed[0]!.kind, "success");
	assert.match(host.printed[0]!.text, /registered `machina`/);
	assert.match(host.text(), /codex mcp add machina/);
	assert.match(host.text(), /verified: present in `codex mcp list --json`/);
});

test("/interop enable --dry-run previews without claiming a write", async () => {
	const host = new InteropFakeHost();
	host.picks.push("yes");
	host.responses["interop.enable"] = {
		platform: "codex",
		action: "enable",
		ok: true,
		changed: false,
		message: "dry run: would register `machina` for Codex (global scope).",
		steps: ["would run: codex mcp add machina -- /usr/bin/python -m machina.mcp_server"],
		verify: {},
		argv: [["/fake/bin/codex", "mcp", "add", "machina"]],
	};
	await run("/interop enable codex --dry-run", host);
	assert.match(host.pickTitles[0]!, /Nothing is written/);
	assert.deepEqual(host.calls.at(-1), {
		method: "interop.enable",
		params: { platform: "codex", dry_run: true },
	});
	assert.match(host.text(), /dry run/);
});

test("/interop disable confirms and surfaces manual-step failures honestly", async () => {
	const host = new InteropFakeHost();
	host.picks.push("yes");
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
	await run("/interop disable opencode", host);
	assert.deepEqual(host.calls, [{ method: "interop.disable", params: { platform: "opencode" } }]);
	assert.equal(host.printed[0]!.kind, "warning", "a failed action is never a success");
	assert.match(host.printed[0]!.text, /no MCP remove subcommand/);
	assert.match(host.text(), /edit opencode\.json/);

	// Cancelling the removal does not call the gateway.
	const cancelled = new InteropFakeHost();
	cancelled.picks.push("no");
	await run("/interop disable opencode", cancelled);
	assert.equal(cancelled.calls.length, 0);
});

test("/interop surfaces gateway errors instead of faking success", async () => {
	const host = new InteropFakeHost();
	host.picks.push("yes");
	host.responses["interop.enable"] = () => {
		throw new Error("Unknown platform 'nope'; choose one of: opencode, codex, hermes");
	};
	await run("/interop enable nope", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Unknown platform 'nope'/);
});

test("/interop usage errors are reported, not thrown at the app", async () => {
	const host = new InteropFakeHost();
	await run("/interop enable", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Usage: \/interop enable/);
	await run("/interop disable", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Usage: \/interop disable/);
	await run("/interop dance", host);
	assert.equal(host.last().kind, "error");
	assert.match(host.last().text, /Unknown \/interop option/);
	assert.equal(host.calls.length, 0);
});

test("/interop completions cover subcommands, platforms, flags and scopes", async () => {
	const { interopCommand } = await import("../src/features/interop.ts");
	const completions = interopCommand.getArgumentCompletions!;
	const values = async (prefix: string) => (await completions(prefix))?.map((item) => item.value) ?? null;
	assert.deepEqual(await values(""), ["status", "enable", "disable"]);
	assert.deepEqual(await values("en"), ["enable"]);
	assert.deepEqual(await values("enable "), ["opencode", "codex", "hermes"]);
	assert.deepEqual(await values("enable co"), ["codex"]);
	assert.deepEqual(await values("disable h"), ["hermes"]);
	assert.deepEqual(await values("enable codex --"), ["--scope", "--dry-run"]);
	assert.deepEqual(await values("enable codex --scope "), ["project", "global"]);
	assert.deepEqual(await values("enable codex --scope g"), ["global"]);
	assert.equal(await values("status foo"), null);
});
