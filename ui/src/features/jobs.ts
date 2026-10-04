// Job commands: list, add, heartbeat, cron, on, off, delete.

import { resolvePrefix, shortId, table, when } from "../format.ts";
import type { JobInfo } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { options, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";

async function resolveJobId(host: FeatureHost, sessionId: string, idOrPrefix: string): Promise<string> {
	const { jobs } = await host.request<{ jobs: JobInfo[] }>("jobs.list", { sessionId });
	return resolvePrefix(idOrPrefix, jobs.map((j) => j.id), "job");
}

function parseSeconds(value: string): number {
	if (!/^\d+$/.test(value)) throw new Error("Interval must be a whole number of seconds.");
	const seconds = Number(value);
	if (!Number.isSafeInteger(seconds) || seconds < 10 || seconds > 86_400) {
		throw new Error("Interval must be between 10 and 86400 seconds.");
	}
	return seconds;
}

export const jobsCommand: Command = {
	name: "jobs",
	description: "Scheduled jobs: list, add, heartbeat, cron, on, off, delete",
	argumentHint: "[list|add|heartbeat|cron|on|off|delete]",
	getArgumentCompletions: options([
		["list", "Jobs for this session"],
		["add", "Repeat a prompt: add <seconds> <prompt>"],
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
								["ID", "Kind", "Every", "On", "Runs", "Next"],
								jobs.map((j) => [
									shortId(j.id),
									j.kind,
									j.kind === "cron" ? (j.cronExpression ?? "") : `${j.intervalSeconds}s`,
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
				if (!prompt) throw new Error("Usage: /jobs add <seconds> <prompt>");
				const { job } = await host.request<{ job: JobInfo }>("jobs.create", {
					sessionId: current.id,
					kind: "interval",
					prompt,
					intervalSeconds: seconds,
				});
				host.print(`Scheduled "${job.prompt}" every ${job.intervalSeconds}s (${shortId(job.id)}).`, "success");
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
				const prompt = rest.slice(separator + 2).trim();
				if (cronExpression.split(/\s+/).length !== 5 || !prompt) {
					throw new Error("Usage: /jobs cron <5 fields> :: <prompt>");
				}
				const { job } = await host.request<{ job: JobInfo }>("jobs.create", {
					sessionId: current.id,
					kind: "cron",
					cronExpression,
					prompt,
				});
				host.print(`Scheduled "${job.prompt}" at ${job.cronExpression} UTC (${shortId(job.id)}).`, "success");
				return;
			}
			case "on":
			case "off": {
				const id = await resolveJobId(host, current.id, requireArgs(rest, `/jobs ${sub} <id>`));
				await host.request("jobs.setEnabled", { id, enabled: sub === "on" });
				host.print(`Job ${shortId(id)} ${sub === "on" ? "enabled" : "disabled"}.`, "success");
				return;
			}
			case "delete": {
				const id = await resolveJobId(host, current.id, requireArgs(rest, "/jobs delete <id>"));
				await host.request("jobs.delete", { id });
				host.print(`Deleted job ${shortId(id)}.`, "success");
				return;
			}
			default:
				throw new Error(`Unknown /jobs option "${sub}". Try: list, add, heartbeat, cron, on, off, delete.`);
		}
	},
};
