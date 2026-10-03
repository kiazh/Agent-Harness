// Memory commands: list, search, add, forget, pending, approve, reject, stats.

import { keyValues, resolvePrefix, shortId, table } from "../format.ts";
import type { MemoryInfo, PendingMemoryInfo } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { options, requireArgs, type Command, type FeatureHost } from "./types.ts";

const MEMORY_CATEGORIES = ["preference", "decision", "fact", "event", "transient"];

function memoryTable(memories: MemoryInfo[], withScore = false): string {
	const headers = withScore ? ["ID", "Category", "Score", "Content"] : ["ID", "Category", "Imp.", "Content"];
	return table(
		headers,
		memories.map((m) => [
			shortId(m.id),
			m.category,
			withScore ? (m.score ?? 0).toFixed(3) : m.importance.toFixed(2),
			m.content,
		]),
		70,
	);
}

async function resolvePending(host: FeatureHost, idOrPrefix: string): Promise<string> {
	const { pending } = await host.request<{ pending: PendingMemoryInfo[] }>("memory.pending", { limit: 500 });
	return resolvePrefix(idOrPrefix, pending.map((p) => p.id), "pending memory");
}

export const memoryCommand: Command = {
	name: "memory",
	description: "Long-term memory: list, search, add, forget, review",
	argumentHint: "[list|search|add|forget|pending|approve|reject|stats]",
	getArgumentCompletions: options([
		["list", "Recent memories [category]"],
		["search", "Find relevant memories <query>"],
		["add", "Remember something [category:] <text>"],
		["forget", "Delete a memory <id>"],
		["pending", "Memories waiting for approval"],
		["approve", "Approve <id|all>"],
		["reject", "Reject <id|all>"],
		["stats", "Counts by status"],
	]),
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "":
			case "list": {
				const category = rest && MEMORY_CATEGORIES.includes(rest.toLowerCase()) ? rest.toLowerCase() : undefined;
				const { memories, total } = await host.request<{ memories: MemoryInfo[]; total: number }>("memory.list", {
					category,
					limit: 20,
				});
				host.print(memories.length ? `${memoryTable(memories)}\n\n${total} memories in total.` : "No memories yet.", "plain");
				return;
			}
			case "search": {
				const query = requireArgs(rest, "/memory search <query>");
				const { results } = await host.request<{ results: MemoryInfo[] }>("memory.search", { query, limit: 10 });
				host.print(results.length ? memoryTable(results, true) : `Nothing found for "${query}".`, "plain");
				return;
			}
			case "add": {
				let text = requireArgs(rest, "/memory add [category:] <text>");
				let category = "fact";
				const match = /^(\w+):\s*(.+)$/s.exec(text);
				if (match && MEMORY_CATEGORIES.includes(match[1]!.toLowerCase())) {
					category = match[1]!.toLowerCase();
					text = match[2]!;
				}
				const { memory } = await host.request<{ memory: MemoryInfo }>("memory.add", {
					content: text,
					category,
					sessionId: host.session()?.id,
				});
				host.print(`Remembered (${memory.category}, ${shortId(memory.id)}).`, "success");
				return;
			}
			case "forget": {
				const target = requireArgs(rest, "/memory forget <id>");
				const { memories } = await host.request<{ memories: MemoryInfo[] }>("memory.list", { limit: 500 });
				const id = resolvePrefix(target, memories.map((m) => m.id), "memory");
				await host.request("memory.forget", { id });
				host.print(`Forgot memory ${shortId(id)}.`, "success");
				return;
			}
			case "pending": {
				const { pending } = await host.request<{ pending: PendingMemoryInfo[] }>("memory.pending", { limit: 50 });
				host.print(
					pending.length
						? table(
								["ID", "Category", "Redacted", "Content"],
								pending.map((p) => [shortId(p.id), p.category, p.redactions.join(", ") || "—", p.content]),
								70,
							)
						: "No memories waiting for approval.",
					"plain",
				);
				return;
			}
			case "approve":
			case "reject": {
				const target = requireArgs(rest, `/memory ${sub} <id|all>`);
				if (target.toLowerCase() === "all") {
					const { count } = await host.request<{ count: number }>(sub === "approve" ? "memory.approveAll" : "memory.rejectAll");
					host.print(`${sub === "approve" ? "Approved" : "Rejected"} ${count} pending memories.`, "success");
					return;
				}
				const id = await resolvePending(host, target);
				await host.request(sub === "approve" ? "memory.approve" : "memory.reject", { id });
				host.print(`${sub === "approve" ? "Approved" : "Rejected"} ${shortId(id)}.`, "success");
				return;
			}
			case "stats": {
				const { approval, total } = await host.request<{ approval: Record<string, number>; total: number }>("memory.stats");
				host.print(
					keyValues([
						["Stored", total],
						["Pending", approval.pending ?? 0],
						["Approved", approval.approved ?? 0],
						["Rejected", approval.rejected ?? 0],
					]),
					"plain",
				);
				return;
			}
			default:
				throw new Error(`Unknown /memory option "${sub}". Try: list, search, add, forget, pending, approve, reject, stats.`);
		}
	},
};
