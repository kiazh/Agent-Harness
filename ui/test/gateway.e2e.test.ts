// End-to-end: GatewayClient <-> real `python -m machina.gateway` <-> test database.
// Runs only when MACHINA_TEST_PYTHON and MACHINA_TEST_DATABASE_URL are set.

import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { type FeatureHost, type NoticeKind, runCommand } from "../src/features/index.ts";
import { GatewayClient } from "../src/gateway.ts";
import type { HistoryEntry, InitializeResult, SessionInfo, SessionListResult, SessionResult } from "../src/protocol.ts";

const python = process.env.MACHINA_TEST_PYTHON;
const dsn = process.env.MACHINA_TEST_DATABASE_URL;
const skip = !python || !dsn ? "MACHINA_TEST_PYTHON / MACHINA_TEST_DATABASE_URL not set" : false;

function startClient(extraEnv: NodeJS.ProcessEnv = {}) {
	const token = `ui-e2e-${Math.random().toString(36).slice(2)}`;
	const client = new GatewayClient({
		python: python!,
		token,
		env: { DATABASE_URL: dsn!, MACHINA_GATEWAY_NO_SCHEDULER: "1", MACHINA_GATEWAY_TOKEN: token, ...extraEnv },
	});
	let crashed = false;
	client.onExit = () => {
		crashed = true;
	};
	client.start();
	return { client, crashed: () => crashed };
}

test("persona menu persists its default across a gateway restart", { skip, timeout: 60_000 }, async () => {
	const home = await mkdtemp(join(tmpdir(), "machina-persona-e2e-"));
	const env = { HOME: home, MACHINA_ENV_FILE: join(home, ".env"), MACHINA_PERSONA_DESCRIPTION: undefined };
	const first = startClient(env).client;
	const printed: Array<{ text: string; kind: NoticeKind }> = [];
	const persona = "Give concise answers with practical examples.";
	let asked = false;
	const host = {
		request: (method: string, params: Record<string, unknown>) => first.request(method, params ?? {}),
		print: (text: string, kind: NoticeKind) => printed.push({ text, kind }),
		pick: async (_title: string, items: SelectItem[]) => items.find((item) => item.value === "add"),
		askText: async () => {
			if (asked) return undefined;
			asked = true;
			return persona;
		},
		onConfig: () => {},
	} as unknown as FeatureHost;
	try {
		await first.request("initialize", {}, 30_000);
		await runCommand(parseCommand("/persona")!, host);
		assert.equal(printed.some((notice) => notice.kind === "error"), false);
		assert.match(await readFile(join(home, ".machina", "config.yaml"), "utf8"), /Give concise answers/);
	} finally {
		await first.stop();
	}
	const restarted = startClient(env).client;
	try {
		await restarted.request("initialize", {}, 30_000);
		const result = await restarted.request<{ config: { persona_description: string } }>("config.get");
		assert.equal(result.config.persona_description, persona);
	} finally {
		await restarted.stop();
	}
});

test("personality survives a gateway restart and a new session", { skip, timeout: 60_000 }, async () => {
	const key = `e2e_${Math.random().toString(36).slice(2)}`;
	const first = startClient().client;
	let firstSession: SessionInfo | undefined;
    let retainedRevision: number;
	try {
		await first.request("initialize", {}, 30_000);
		firstSession = (await first.request<SessionResult>("session.create", { title: "growth e2e first" })).session;
		await first.request("growth.teach", { sessionId: firstSession.id, kind: "style", key, content: "Prefer concise examples" });
        const checkpoint = await first.request<{ rsi: { revision: number } }>("growth.get", { sessionId: firstSession.id });
        retainedRevision = checkpoint.rsi.revision;
        await first.request("growth.teach", { sessionId: firstSession.id, kind: "style", key, content: "Prefer detailed examples" });
	} finally {
		await first.stop();
	}
	const restarted = startClient().client;
	let secondSession: SessionInfo | undefined;
	try {
		await restarted.request("initialize", {}, 30_000);
		secondSession = (await restarted.request<SessionResult>("session.create", { title: "growth e2e second" })).session;
        const result = await restarted.request<{ growth: { notes: Record<string, string> }; rsi: { revision: number; history: Array<{ revision: number }> } }>("growth.get", { sessionId: secondSession.id });
        assert.equal(result.growth.notes[`style:${key}`], "Prefer detailed examples");
        assert.ok(result.rsi.history.some(item => item.revision === retainedRevision));
        const restored = await restarted.request<{ restored: boolean }>("growth.rollback", { sessionId: secondSession.id, revision: retainedRevision });
        assert.equal(restored.restored, true);
        const after = await restarted.request<{ growth: { notes: Record<string, string> }; rsi: { revision: number } }>("growth.get", { sessionId: secondSession.id });
        assert.equal(after.growth.notes[`style:${key}`], "Prefer concise examples");
        assert.ok(after.rsi.revision > result.rsi.revision);
	} finally {
		if (secondSession) {
			await restarted.request("growth.forget", { sessionId: secondSession.id, key: `style:${key}` });
			await restarted.request("session.delete", { sessionId: secondSession.id });
		}
		if (firstSession) await restarted.request("session.delete", { sessionId: firstSession.id });
		await restarted.stop();
	}
});

test("talks to the real gateway", { skip, timeout: 90_000 }, async () => {
	const { client, crashed } = startClient();
	try {
		const init = await client.request<InitializeResult>("initialize", {}, 60_000);
		assert.ok(init.version && init.model);
		const { session } = await client.request<SessionResult>("session.create", { title: "ui e2e" });
		assert.equal(session.title, "ui e2e");
		const { sessions } = await client.request<SessionListResult>("session.list", { limit: 20 });
		assert.ok(sessions.some((s) => s.id === session.id));
		await assert.rejects(client.request("nope"), /unknown method/);
	} finally {
		await client.stop();
	}
	assert.equal(crashed(), false, `gateway crashed:\n${client.stderr().join("\n")}`);
});

test("readline interfaces are closed after stop()", { skip, timeout: 90_000 }, async () => {
	const { client } = startClient();
	await client.request("initialize", {}, 60_000);
	await client.stop();
	const internal = client as unknown as {
		stdoutInterface?: { closed: boolean };
		stderrInterface?: { closed: boolean };
	};
	assert.equal(internal.stdoutInterface?.closed, true, "stdout readline interface closed");
	assert.equal(internal.stderrInterface?.closed, true, "stderr readline interface closed");
});

test("slash commands work against the real gateway and database", { skip, timeout: 120_000 }, async () => {
	const { client, crashed } = startClient();
	const dir = await mkdtemp(join(tmpdir(), "machina-ui-e2e-"));
	const printed: Array<{ text: string; kind: NoticeKind }> = [];
	let current: SessionInfo | undefined;

	const host: FeatureHost = {
		request: (method, params) => client.request(method, params ?? {}, 60_000),
		print: (text, kind = "info") => void printed.push({ text, kind }),
		printMarkdown: (text) => void printed.push({ text, kind: "plain" }),
		session: () => current,
		submitTurn: async () => {},
		switchTo: (session: SessionInfo, _history: HistoryEntry[]) => {
			current = session;
		},
		updateSession: (session) => {
			current = session;
		},
		pick: async (_title: string, items: SelectItem[]) => items.find((i) => i.value === "yes") ?? items[0],
		pickMany: async (_title, items) => items.filter(item => item.value === current?.id),
		askText: async () => undefined,
		showOAuthProgress: () => ({ update: () => {}, dispose: () => {} }),
		inspectOutput: async () => {},
		pickTheme: async () => undefined,
		writeFile: async (path, text) => {
			const full = join(dir, path);
			const { writeFile } = await import("node:fs/promises");
			await writeFile(full, text, "utf8");
			return full;
		},
		onConfig: () => {},
		clear: () => {},
		banner: () => {},
		restyle: () => {},
		profileId: () => "default",
		setProfile: () => {},
		sessionAgent: () => undefined,
		exit: async () => {},
	};
	const run = async (text: string) => {
		const before = printed.length;
		await runCommand(parseCommand(text)!, host);
		const errors = printed.slice(before).filter((p) => p.kind === "error");
		assert.deepEqual(errors, [], `${text} failed`);
		return printed.slice(before).map((p) => p.text).join("\n");
	};

	try {
		await client.request("initialize", {}, 60_000);
		const tag = Math.random().toString(36).slice(2, 10);

		await run(`/new e2e ${tag}`);
		assert.ok(current, "a session is current");
		assert.match(await run(`/rename renamed ${tag}`), /Renamed/);
		assert.match(await run("/goal finish the e2e test"), /Goal set/);
		assert.match(await run("/goal"), /finish the e2e test/);

		assert.match(await run(`/memory add preference: ${tag} likes green tea`), /Remembered/);
		assert.match(await run(`/memory search ${tag}`), new RegExp(tag));
		assert.match(await run("/memory stats"), /Stored/);

		assert.match(await run("/context"), /Tokens/);
		assert.match(await run("/compress"), /Nothing to compress/);
		assert.match(await run("/status"), /PostgreSQL/);
		assert.match(await run("/config"), /turn_timeout/);
		assert.match(await run("/theme dracula"), /Skin: dracula/);
		assert.match(await run("/config theme"), /dracula/);
		assert.match(await run("/skills"), /Name|No skills/);
		assert.match(await run("/mcp"), /No MCP servers registered in this harness/);
		assert.match(await run("/mcp list"), /No MCP servers registered in this harness/);
		assert.match(await run("/agents"), /harness/);
		assert.match(await run("/agents show researcher"), /research/i);

		assert.match(await run("/jobs"), /No scheduled jobs/);
		assert.match(await run("/jobs add 60 check the news"), /Scheduled/);
		const listed = await run("/jobs");
		assert.match(listed, /interval/);
		const jobId = (listed.match(/\b([0-9a-f]{8})\b/) ?? [])[1];
		assert.ok(jobId, "a job id is shown");
		assert.match(await run(`/jobs off ${jobId}`), /disabled/);
		assert.match(await run(`/jobs on ${jobId}`), /enabled/);
		assert.match(await run(`/jobs delete ${jobId}`), /Deleted/);

		const exported = await run(`/export ${tag}.md`);
		assert.match(exported, /Exported to/);
		assert.match(await readFile(join(dir, `${tag}.md`), "utf8"), new RegExp(`# Session: renamed ${tag}`));

		const original = current!.id;
		await run("/fork");
		assert.notEqual(current!.id, original, "fork switches to the copy");
		const forkId = current!.id;
		await run("/delete");
		assert.notEqual(current!.id, forkId, "checking and deleting the current session opens a new one");
	} finally {
		await client.stop();
	}
	assert.equal(crashed(), false, `gateway crashed:\n${client.stderr().join("\n")}`);
});
