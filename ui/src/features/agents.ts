// Agent commands: agents (list, show, save, history, delete, sessions) and
// delegate (run a task on another agent).

import { keyValues, shortId, table, when } from "../format.ts";
import type { AgentInfo, AgentHistoryResult, AgentSaveResult, DelegationResult, ResumeResult, SessionInfo, SessionListResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";

/** Upper bound on cursor pages scanned for the family (sessions.ts pattern). */
const MAX_FAMILY_LIST_PAGES = 10;

/**
 * The current session's agent family: the family root (the current session's
 * parent, or the current session itself) plus every listed session whose
 * `parentSessionId` is that root. Paginated bound like /sessions; filtered
 * client-side. Children of a child stay out (single-level rule, see App).
 */
async function listFamilySessions(host: FeatureHost, current: SessionInfo): Promise<SessionInfo[]> {
	const rootId = current.parentSessionId ?? current.id;
	const members = new Map<string, SessionInfo>();
	let cursor: string | undefined;
	for (let page = 0; ; page++) {
		const result = await host.request<SessionListResult & { nextCursor?: string }>("session.list", {
			limit: 200,
			...(cursor ? { cursor } : {}),
		});
		for (const session of result.sessions) {
			if (session.id === rootId || session.parentSessionId === rootId) members.set(session.id, session);
		}
		if (!result.nextCursor) break;
		if (page + 1 >= MAX_FAMILY_LIST_PAGES) {
			throw new Error(`Too many sessions to scan (${MAX_FAMILY_LIST_PAGES} pages); try /resume <id>.`);
		}
		cursor = result.nextCursor;
	}
	members.set(current.id, current); // the visible session always wins
	const root = members.get(rootId) ?? current;
	const children = [...members.values()].filter((session) => session.id !== rootId);
	return [root, ...children];
}

export const agentsCommand: Command = {
	name: "agents",
	description: "Agents: list, show, save, history, delete · sessions switches delegated tabs (Ctrl+Shift+←/→)",
	argumentHint: "[list|show|save|history|delete|sessions|switch]",
	getArgumentCompletions: options([
		["list", "All available agents"],
		["show", "Read an agent's definition <name>"],
		["save", "Save the current session as an agent <name>"],
		["history", "Recent agent activity"],
		["delete", "Remove a custom agent <name>"],
		["sessions", "Switch to a delegated agent session (tab)"],
		["switch", "Alias for sessions"],
	]),
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "":
			case "list": {
				const { agents } = await host.request<{ agents: AgentInfo[] }>("agents.list");
				host.print(
					table(
						["Name", "Source", "Tools", "Description"],
						agents.map((a) => [a.name, a.source, a.tools.length ? String(a.tools.length) : "all", a.description]),
						60,
					),
					"plain",
				);
				return;
			}
			case "sessions":
			case "switch": {
				const current = requireSession(host);
				// Live run state comes from the host's tab tracking when the
				// host has it (App); other hosts report idle.
				const running = new Map((host.familySessions?.() ?? []).map((row) => [row.session.id, row.running]));
				const family = await listFamilySessions(host, current);
				const rootId = current.parentSessionId ?? current.id;
				if (family.length <= 1) {
					host.print("No delegated agent sessions in this family yet. Use /delegate to start one; Ctrl+Shift+←/→ cycles tabs.", "plain");
					return;
				}
				const choice = await host.pick("Switch to an agent session", family.map((session) => ({
					value: session.id,
					label: `${session.id === rootId ? "main · " : ""}${session.title || shortId(session.id)}${session.id === current.id ? " (current)" : ""}`,
					description: `${shortId(session.id)}  ${running.get(session.id) ? "running" : "idle"}  ${when(session.lastActivity)}`,
				})));
				if (!choice) return;
				if (choice.value === current.id) {
					host.print("Already on that session.", "plain");
					return;
				}
				const { session, history } = await host.request<ResumeResult>("session.resume", { sessionId: choice.value });
				host.switchTo(session, history);
				host.print(`Switched to "${session.title || "untitled"}" (${shortId(session.id)}).`, "plain");
				return;
			}
			case "show": {
				const name = requireArgs(rest, "/agents show <name>");
				const { agents } = await host.request<{ agents: AgentInfo[] }>("agents.list");
				const agent = agents.find((a) => a.name === name.toLowerCase());
				if (!agent) throw new Error(`No agent named "${name}".`);
				host.print(
					keyValues([
						["Name", agent.name],
						["Source", agent.source],
						["Model", agent.model ?? "(default)"],
						["Tools", agent.tools.join(", ") || "all"],
						["Description", agent.description],
						["Prompt", agent.systemPrompt || "(default)"],
					]),
					"plain",
				);
				return;
			}
			case "save": {
			const args = requireArgs(rest, "/agents save <name> [description]");
			// First token is the agent name; the remainder is the description.
			const [name, ...descWords] = args.split(/\s+/);
			const desc = descWords.join(" ").trim() || `Custom agent: ${name}`;
			const { agent } = await host.request<AgentSaveResult>("agents.save", {
				name: name!.toLowerCase(),
				description: desc,
				systemPrompt: "",
				tools: [],
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
					host.print("Kept the agent.", "plain");
					return;
				}
				await host.request("agents.delete", { name });
				host.print(`Deleted agent "${name}".`, "success");
				return;
			}
			default:
				throw new Error(`Unknown /agents option "${sub}". Try: list, show, save, history, delete, sessions.`);
		}
	},
};

export const delegateCommand: Command = {
	name: "delegate",
	description: "Choose an agent and delegate a task",
	argumentHint: "[agent] [task]",
	async run(args, host) {
		let [agent, task] = splitSub(args.trim());
		if (!agent) {
			const { agents } = await host.request<{ agents: AgentInfo[] }>("agents.list");
			if (!agents.length) {
				host.print("No agents available. Create one with /agents save <name>.", "plain");
				return;
			}
			const selected = await host.pick("Delegate to which agent?", agents.map(a => ({
				value: a.name,
				label: a.name,
				description: a.description,
			})));
			if (!selected) return;
			agent = selected.value;
		}
		if (!task) {
			const prompt = await host.askText({
				title: `Task for ${agent}`,
				description: "Describe the task and the result you want.",
				placeholder: "What should this agent do?",
			});
			if (prompt === undefined) return;
			task = prompt.trim();
			if (!task) {
				host.print("Task is empty. Use /delegate to try again.", "warning");
				return;
			}
		}
		host.print(`Delegating to ${agent}…`, "plain");
		const { results } = await host.request<{ results: DelegationResult[] }>("agents.run", {
			sessionId: host.session()?.id,
			steps: [{ agent, task }],
		});
		const r = results[0]!;
		if (r.status === "error") host.print(`${agent} failed: ${r.response}`, "error");
		else host.printMarkdown(`**${agent}** (${r.tokens} tokens):\n\n${r.response}`);
	},
};
