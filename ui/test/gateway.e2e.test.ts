// End-to-end: GatewayClient <-> real `python -m ah.gateway` <-> test database.
// Runs only when AH_TEST_PYTHON and AGENT_HARNESS_TEST_DATABASE_URL are set.

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

const python = process.env.AH_TEST_PYTHON;
const dsn = process.env.AGENT_HARNESS_TEST_DATABASE_URL;
const skip = !python || !dsn ? "AH_TEST_PYTHON / AGENT_HARNESS_TEST_DATABASE_URL not set" : false;

function startClient() {
	const client = new GatewayClient({
		python: python!,
		env: { DATABASE_URL: dsn!, AH_GATEWAY_NO_SCHEDULER: "1" },
	});
	let crashed = false;
	client.onExit = () => {
		crashed = true;
	};
	client.start();
	return { client, crashed: () => crashed };
}

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
	const dir = await mkdtemp(join(tmpdir(), "ah-ui-e2e-"));
	const printed: Array<{ text: string; kind: NoticeKind }> = [];
	let current: SessionInfo | undefined;

	const host: FeatureHost = {
		request: (method, params) => client.request(method, params ?? {}, 60_000),
		print: (text, kind = "info") => void printed.push({ text, kind }),
		printMarkdown: (text) => void printed.push({ text, kind: "plain" }),
		session: () => current,
		switchTo: (session: SessionInfo, _history: HistoryEntry[]) => {
			current = session;
		},
		updateSession: (session) => {
			current = session;
		},
		pick: async (_title: string, items: SelectItem[]) => items.find((i) => i.value === "yes") ?? items[0],
		writeFile: async (path, text) => {
			const full = join(dir, path);
			const { writeFile } = await import("node:fs/promises");
			await writeFile(full, text, "utf8");
			return full;
		},
		onConfig: () => {},
		clear: () => {},
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
		assert.match(await run(`/memory search ${tag} green tea`), new RegExp(tag));
		assert.match(await run("/memory stats"), /Stored/);

		assert.match(await run("/context"), /Tokens/);
		assert.match(await run("/compress"), /Nothing to compress/);
		assert.match(await run("/status"), /PostgreSQL/);
		assert.match(await run("/config"), /max_iterations/);
		assert.match(await run("/skills"), /Name|No skills/);
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
		await run("/delete");
		assert.notEqual(current, undefined, "deleting the current session opens a new one");
	} finally {
		await client.stop();
	}
	assert.equal(crashed(), false, `gateway crashed:\n${client.stderr().join("\n")}`);
});
