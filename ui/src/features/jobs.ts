// Job commands: list, add, script, heartbeat, cron, on, off, delete.

import { resolvePrefix, shortId, table, when } from "../format.ts";
import type { JobInfo } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { options, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";

async function resolveJobId(host: FeatureHost, sessionId: string, idOrPrefix: string): Promise<string> {
	// Normalize braced/URN UUID forms like sessions (Python uuid.UUID accepts them).
	let wanted = idOrPrefix.trim().toLowerCase();
	if (wanted.startsWith("urn:uuid:")) wanted = wanted.slice("urn:uuid:".length);
	if (wanted.startsWith("{") && wanted.endsWith("}")) wanted = wanted.slice(1, -1).trim();
	if (/^[0-9a-f-]{36}$/.test(wanted)) return wanted;
	// Paginate through all jobs: a prefix must resolve even when the
	// matching job is older than the most recent page.
	let cursor: string | undefined;
	const ids: string[] = [];
	for (;;) {
		const { jobs, nextCursor } = await host.request<{ jobs: JobInfo[] } & { nextCursor?: string }>(
			"jobs.list",
			{ sessionId, limit: 500, ...(cursor ? { cursor } : {}) },
		);
		ids.push(...jobs.map((j) => j.id));
		if (!nextCursor) break;
		cursor = nextCursor;
	}
	return resolvePrefix(wanted, ids, "job");
}

function parseSeconds(value: string): number {
	if (!/^\d+$/.test(value)) throw new Error("Interval must be a whole number of seconds.");
	const seconds = Number(value);
	if (!Number.isSafeInteger(seconds) || seconds < 10 || seconds > 86_400) {
		throw new Error("Interval must be between 10 and 86400 seconds.");
	}
	return seconds;
}

function pinnedPrompt(value: string): { prompt: string; model?: string; provider?: string } {
	const words = value.trim().split(/\s+/);
	const pins: { model?: string; provider?: string } = {};
	while (words.length && /^--(model|provider)=/.test(words.at(-1)!)) {
		const option = words.pop()!;
		const match = /^--(model|provider)=(\S*)$/.exec(option)!;
		const key = match[1] as "model" | "provider";
		if (!match[2] || pins[key]) throw new Error(`Invalid --${key} value.`);
		pins[key] = match[2];
	}
	return { prompt: words.join(" "), ...pins };
}

export const jobsCommand: Command = {
	name: "jobs",
	description: "Scheduled jobs: list, add, script, heartbeat, cron, on, off, delete",
	argumentHint: "[list|add|script|heartbeat|cron|on|off|delete]",
	getArgumentCompletions: options([
		["list", "Jobs for this session"],
		["add", "Repeat a prompt: add <seconds> <prompt>"],
		["script", "Run a script without an LLM: script <seconds> <file>"],
		["heartbeat", "Nudge an idle session: heartbeat <seconds>"],
		["cron", "Run on a UTC schedule: cron <5 fields> :: <prompt>"],
		["on", "Enable a job <id>"],
		["off", "Disable a job <id>"],
		["delete", "Remove a job <id>"],
	]),
	async run(args, host) {
		const current = requireSession(host);
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "":
			case "list": {
				const { jobs } = await host.request<{ jobs: JobInfo[] }>("jobs.list", { sessionId: current.id });
				host.print(
					jobs.length
						? table(
								["ID", "Kind", "Every", "Model", "On", "Runs", "Next"],
								jobs.map((j) => [
									shortId(j.id),
									j.noAgent ? "script" : j.kind,
									j.kind === "cron" ? (j.cronExpression ?? "") : `${j.intervalSeconds}s`,
									j.model ?? "default",
									j.enabled ? "yes" : "no",
									String(j.runCount),
									when(j.nextRunAt),
								]),
								40,
							)
						: "No scheduled jobs. Add one with /jobs add <seconds> <prompt>.",
					"plain",
				);
				return;
			}
			case "add": {
				const [secondsText, prompt] = splitSub(requireArgs(rest, "/jobs add <seconds> <prompt>"));
				const seconds = parseSeconds(secondsText);
				const pinned = pinnedPrompt(prompt);
				if (!pinned.prompt) throw new Error("Usage: /jobs add <seconds> <prompt>");
				const { job } = await host.request<{ job: JobInfo }>("jobs.create", {
					sessionId: current.id,
					kind: "interval",
					...pinned,
					intervalSeconds: seconds,
				});
				host.print(`Scheduled "${job.prompt}" every ${job.intervalSeconds}s (${shortId(job.id)}).`, "success");
				return;
			}
			case "script": {
				const [secondsText, scriptPath] = splitSub(requireArgs(rest, "/jobs script <seconds> <file>"));
				const seconds = parseSeconds(secondsText);
				if (!scriptPath.trim()) throw new Error("Usage: /jobs script <seconds> <file>");
				const { job } = await host.request<{ job: JobInfo }>("jobs.create", {
					sessionId: current.id,
					kind: "interval",
					intervalSeconds: seconds,
					noAgent: true,
					scriptPath: scriptPath.trim(),
				});
				host.print(`Scheduled script ${job.scriptPath} every ${job.intervalSeconds}s (${shortId(job.id)}).`, "success");
				return;
			}
			case "heartbeat": {
				const seconds = parseSeconds(rest.trim() || "300");
				const { job } = await host.request<{ job: JobInfo }>("jobs.create", {
					sessionId: current.id,
					kind: "heartbeat",
					intervalSeconds: seconds,
				});
				host.print(`Heartbeat every ${job.intervalSeconds}s (${shortId(job.id)}).`, "success");
				return;
			}
			case "cron": {
				const separator = rest.indexOf("::");
				if (separator < 0) throw new Error("Usage: /jobs cron <5 fields> :: <prompt>");
				const cronExpression = rest.slice(0, separator).trim();
				const pinned = pinnedPrompt(rest.slice(separator + 2));
				if (cronExpression.split(/\s+/).length !== 5 || !pinned.prompt) {
					throw new Error("Usage: /jobs cron <5 fields> :: <prompt>");
				}
				const { job } = await host.request<{ job: JobInfo }>("jobs.create", {
					sessionId: current.id,
					kind: "cron",
					cronExpression,
					...pinned,
				});
				host.print(`Scheduled "${job.prompt}" at ${job.cronExpression} UTC (${shortId(job.id)}).`, "success");
				return;
			}
			case "on":
			case "off": {
				const id = await resolveJobId(host, current.id, requireArgs(rest, `/jobs ${sub} <id>`));
				await host.request("jobs.setEnabled", { sessionId: current.id, id, enabled: sub === "on" });
				host.print(`Job ${shortId(id)} ${sub === "on" ? "enabled" : "disabled"}.`, "success");
				return;
			}
			case "delete": {
				const id = await resolveJobId(host, current.id, requireArgs(rest, "/jobs delete <id>"));
				await host.request("jobs.delete", { sessionId: current.id, id });
				host.print(`Deleted job ${shortId(id)}.`, "success");
				return;
			}
			default:
				throw new Error(`Unknown /jobs option "${sub}". Try: list, add, script, heartbeat, cron, on, off, delete.`);
		}
	},
};
