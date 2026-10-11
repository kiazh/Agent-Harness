import { splitSub } from "../commands.ts";
import { type Command, options, requireArgs, requireSession } from "./types.ts";

interface GrowthResult {
	growth: { notes: Record<string, string>; interactions: number; feedback_count: number };
	episodes: Array<{ id: string; action: string; completed: boolean; reward: number | null }>;
}

export const growthCommand: Command = {
	name: "growth",
	description: "Show learned personality, teach a note, or rate memory use",
	argumentHint: "[show|teach|feedback|forget]",
	getArgumentCompletions: options([
		["show", "Learned preferences, style and lessons"],
		["teach", "<preference|style|lesson> <key> <note>"],
		["feedback", "<good|bad> [episode-id] — rate memory use"],
		["forget", "<kind:key> — remove a learned note"],
	]),
	async run(args, host) {
		const sessionId = requireSession(host).id;
		const [sub, rest] = splitSub(args);
		if (!sub || sub === "show") {
			const { growth, episodes } = await host.request<GrowthResult>("growth.get", { sessionId });
			const notes = Object.entries(growth.notes).map(([key, value]) => `${key}: ${value}`).join("\n");
			host.print(`${growth.interactions} completed conversations · ${growth.feedback_count} ratings\n${notes || "No learned notes yet. Share a preference or use /growth teach."}\n\nRecent memory decisions:\n${episodes.filter(e => e.completed).slice(0, 5).map(e => `${e.id} · ${e.action === "retrieve" ? "used memory search" : "skipped memory search"} · ${e.reward === null ? "unrated" : e.reward}`).join("\n") || "None yet"}`, "plain");
			return;
		}
		if (sub === "teach") {
			const match = rest.match(/^(preference|style|lesson)\s+(\S+)\s+([\s\S]+)$/);
			if (!match) throw new Error("Usage: /growth teach <preference|style|lesson> <key> <note>");
			await host.request("growth.teach", { sessionId, kind: match[1], key: match[2], content: match[3] });
			host.print("Learned. This note will carry into future conversations.", "success");
			return;
		}
		if (sub === "forget") {
			const { removed } = await host.request<{ removed: boolean }>("growth.forget", { sessionId, key: requireArgs(rest, "/growth forget <kind:key>") });
			host.print(removed ? "Learned note removed." : "No matching note.", removed ? "success" : "warning");
			return;
		}
		if (sub === "feedback") {
			const [rating, suppliedId] = rest.split(/\s+/);
			if (rating !== "good" && rating !== "bad") throw new Error("Usage: /growth feedback <good|bad> [episode-id]");
			let episodeId: string | undefined = suppliedId;
			if (!episodeId) {
				const { episodes } = await host.request<GrowthResult>("growth.get", { sessionId });
				episodeId = episodes.find(e => e.completed && e.reward === null)?.id;
			}
			if (!episodeId) throw new Error("No completed, unrated memory decision in this session.");
			const { accepted } = await host.request<{ accepted: boolean }>("growth.feedback", { sessionId, episodeId, reward: rating === "good" ? 1 : -1 });
			host.print(accepted ? "Memory policy learned from your rating." : "Decision unavailable or already rated.", accepted ? "success" : "warning");
			return;
		}
		throw new Error("Usage: /growth [show|teach|feedback|forget]");
	},
};
