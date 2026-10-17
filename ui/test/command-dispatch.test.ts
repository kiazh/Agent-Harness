// F30: submit-time turn guard must block session-mutating slash commands and
// argument forms while a turn is in flight, without disabling read-only forms
// or the approvals allow/deny path that unblocks a paused turn.

import assert from "node:assert/strict";
import { test } from "node:test";
import { CONFLICTING_COMMANDS, guardSubmit } from "../src/commandDispatch.ts";
import { parseCommand } from "../src/commands.ts";

function guard(input: string, inFlight = true) {
	return guardSubmit(parseCommand(input), inFlight);
}

test("F30 idle turns allow every command, and plain prompts stay turn-busy", () => {
	assert.equal(guard("/models", false), "allow");
	assert.equal(guard("/default-model gpt-4o", false), "allow");
	assert.equal(guard("write a README", true), "turn-busy");
	assert.equal(guardSubmit(undefined, true), "turn-busy");
});

test("F30 always-mutating commands still stop first", () => {
	for (const name of ["compress", "delete", "rename", "goal", "delegate"]) {
		assert.ok(CONFLICTING_COMMANDS.has(name), name);
		assert.equal(guard(`/${name}`), "stop-first", name);
	}
});

test("F30 /models blocks the picker but allows catalog reads", () => {
	assert.equal(guard("/models"), "stop-first");
	assert.equal(guard("/models gpt"), "stop-first");
	assert.equal(guard("/models catalog"), "allow");
	assert.equal(guard("/models list"), "allow");
	assert.equal(guard("/models CATALOG"), "allow");
});

test("F30 /default-model always mutates (including its /model alias)", () => {
	assert.equal(guard("/default-model"), "stop-first");
	assert.equal(guard("/default-model gpt-4o"), "stop-first");
	assert.equal(guard("/model"), "stop-first");
	assert.equal(guard("/model claude-3-5-sonnet-20241022"), "stop-first");
});

test("F30 /provider reads are allowed; add/remove/switch stop first", () => {
	assert.equal(guard("/provider"), "allow");
	assert.equal(guard("/provider list"), "allow");
	assert.equal(guard("/providers"), "allow");
	assert.equal(guard("/providers list"), "allow");
	for (const args of ["add", "remove", "openrouter", "deepseek", "custom"]) {
		assert.equal(guard(`/provider ${args}`), "stop-first", args);
		assert.equal(guard(`/providers ${args}`), "stop-first", args);
	}
});

test("F30 /mode reads only when bare", () => {
	assert.equal(guard("/mode"), "allow");
	for (const mode of ["ask", "workspace", "sandbox", "full", "bogus"]) {
		assert.equal(guard(`/mode ${mode}`), "stop-first", mode);
	}
});

test("F30 approvals: list and allow/deny stay available, revoke stops first", () => {
	assert.equal(guard("/approvals"), "allow");
	assert.equal(guard("/approvals list"), "allow");
	assert.equal(guard("/approvals allow abc123"), "allow");
	assert.equal(guard("/approvals deny abc123"), "allow");
	assert.equal(guard("/approvals revoke"), "stop-first");
	// Unknown subcommands throw in the handler without mutating; they are not
	// turned into a misleading stop-first turn conflict.
	assert.equal(guard("/approvals nonsense"), "allow");
});

test("F30 growth: show reads, the menu and every write stop first", () => {
	assert.equal(guard("/growth show"), "allow");
	assert.equal(guard("/growth"), "stop-first");
	for (const args of ["teach", "feedback good", "forget style:verbosity", "rollback 2"]) {
		assert.equal(guard(`/growth ${args}`), "stop-first", args);
	}
	assert.equal(guard("/growth nonsense"), "allow");
});

test("F30 fork is a read-copy plus a supported session switch; it is allowed", () => {
	assert.equal(guard("/fork"), "allow");
	assert.equal(guard("/fork my copy"), "allow");
	assert.ok(!CONFLICTING_COMMANDS.has("fork"));
});

test("F30 read-only and navigation commands remain usable during a turn", () => {
	for (const input of ["/sessions", "/new", "/help", "/status", "/usage", "/mcp", "/config list", "/skills"]) {
		assert.equal(guard(input), "allow", input);
	}
});

test("settings menus stop first during a turn and are available when idle", () => {
	for (const input of ["/settings", "/config"]) {
		assert.equal(guard(input), "stop-first", input);
		assert.equal(guard(input, false), "allow", input);
	}
	assert.equal(guardSubmit({ name: "settings", args: "" }, true), "stop-first");
	assert.equal(guard("/config list"), "allow");
});
