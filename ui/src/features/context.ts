// Context commands: context (show what the agent remembers) and compress.

import { keyValues, table, when } from "../format.ts";
import type { CompressResult, ContextResult } from "../protocol.ts";
import { requireSession, type Command } from "./types.ts";

export const contextCommands: Command[] = [
	{
		name: "context",
		description: "Show what the agent remembers in this session",
		async run(_args, host) {
			const current = requireSession(host);
			const ctx = await host.request<ContextResult>("context.get", { sessionId: current.id, limit: 15 });
			const header = keyValues([
				["Tokens", `${ctx.totalTokens.toLocaleString("en-US")} / ${ctx.budget.toLocaleString("en-US")} budget`],
				["Goal", ctx.goal],
			]);
			const rows = ctx.chunks.map((c) => [c.type, String(c.tokens), when(c.createdAt), c.preview]);
			host.print(rows.length ? `${header}\n\n${table(["Type", "Tokens", "When", "Preview"], rows, 60)}` : `${header}\n\nNo context yet.`, "plain");
		},
	},
	{
		name: "compress",
		description: "Summarize older context to free up tokens",
		async run(_args, host) {
			const current = requireSession(host);
			host.print("Compressing context…");
			const r = await host.request<CompressResult>("context.compress", { sessionId: current.id });
			if (!r.compressed) host.print("Nothing to compress yet.");
			else
				host.print(
					`Compressed ${r.originalCount} chunks into ${r.newCount}: ${r.originalTokens} → ${r.compressedTokens} tokens (${r.method}).`,
					"success",
				);
		},
	},
];
