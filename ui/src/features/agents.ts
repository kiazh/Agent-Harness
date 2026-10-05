// Agent commands: agents (list, show, delete) and delegate (run a task on another agent).

import { keyValues, table, when } from "../format.ts";
import type { AgentInfo, AgentSaveResult, AgentHistoryResult, DelegationResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, requireArgs, type Command } from "./types.ts";

export const agentsCommand: Command = {
	name: "agents",
	description: "Agents: list, show, save, history, delete",
	argumentHint: "[list|show|save|history|delete]",
	getArgumentCompletions: options([
		["list", "All available agents"],
		["show", "Read an agent's definition <name>"],
		["delete", "Remove a custom agent <name>"],
	]),
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		const { agents } = await host.request<{ agents: AgentInfo[] }>("agents.list");
		switch (sub) {
			case "":
			case "list":
				host.print(
					table(
						["Name", "Source", "Tools", "Description"],
						agents.map((a) => [a.name, a.source, a.tools.length ? String(a.tools.length) : "all", a.description]),
						60,
					),
					"plain",
				);
				return;
			case "show": {
				const name = requireArgs(rest, "/agents show <name>");
				const agent = agents.find((a) => a.name === name.toLowerCase());
				if (!agent) throw new Error(`No agent named "${name}".`);
				host.print(
					keyValues([
						["Name", agent.name],
						["Source", agent.source],
						["Model", agent.model ?? "(default)"],
						["Max iterations", agent.maxIterations],
						["Tools", agent.tools.join(", ") || "all"],
						["Description", agent.description],
						["Prompt", agent.systemPrompt || "(default)"],
					]),
					"plain",
				);
				return;
			}
			case "save": {
			const name = requireArgs(rest, "/agents save <name> [description]");
			const desc = rest.replace(/^\S+\s*/, "").trim() || `Custom agent: ${name}`;
			const { agent } = await host.request<AgentSaveResult>("agents.save", {
				name: name.toLowerCase(),
				description: desc,
				systemPrompt: "",
				tools: [],
				maxIterations: 10,
			});
			host.print(`Saved agent "${agent.name}".`, "success");
			return;
		}
		case "history": {
			const limit = parseInt(rest) || 50;
			const { messages } = await host.request<AgentHistoryResult>("agents.history", {
				sessionId: host.session()?.id,
				limit,
			});
			if (!messages.length) {
				host.print("No agent messages yet.", "plain");
				return;
			}
			host.print(
				table(
					["From", "To", "Status", "Tokens", "When"],
					messages.map((m) => [m.fromAgent, m.toAgent, m.status, String(m.tokens), when(m.createdAt)]),
					60,
				),
				"plain",
				);
			return;
		}
		case "delete": {
				const name = requireArgs(rest, "/agents delete <name>");
				if (!(await confirm(host, `Delete agent "${name}"?`, "Delete"))) {
					host.print("Kept the agent.");
					return;
				}
				await host.request("agents.delete", { name });
				host.print(`Deleted agent "${name}".`, "success");
				return;
			}
			default:
				throw new Error(`Unknown /agents option "${sub}". Try: list, show, save, history, delete.`);
		}
	},
};

export const delegateCommand: Command = {
	name: "delegate",
	description: "Run a task on another agent",
	argumentHint: "<agent> <task>",
	async run(args, host) {
		const [agent, task] = splitSub(requireArgs(args, "/delegate <agent> <task>"));
		if (!task) throw new Error("Usage: /delegate <agent> <task>");
		host.print(`Delegating to ${agent}…`);
		const { results } = await host.request<{ results: DelegationResult[] }>("agents.run", {
			sessionId: host.session()?.id,
			steps: [{ agent, task }],
		});
		const r = results[0]!;
		if (r.status === "error") host.print(`${agent} failed: ${r.response}`, "error");
		else host.printMarkdown(`**${agent}** (${r.tokens} tokens):\n\n${r.response}`);
	},
};
