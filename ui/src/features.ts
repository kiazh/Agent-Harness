// Every slash command, implemented against a small host interface so the
// logic can be tested without a terminal (see test/features.test.ts).

import { userInfo } from "node:os";
import type { AutocompleteItem, SelectItem, SlashCommand } from "@earendil-works/pi-tui";
import { helpText, type ParsedCommand, splitSub } from "./commands.ts";
import { keyValues, resolvePrefix, shortId, table, when } from "./format.ts";
import type {
	AgentInfo,
	CompressResult,
	ConfigGetResult,
	ConfigResult,
	ConfigSetResult,
	ContextResult,
	DelegationResult,
	HistoryEntry,
	MemoryInfo,
	PendingMemoryInfo,
	ProfileInfo,
	ResumeResult,
	SessionInfo,
	SessionListResult,
	SessionResult,
	SkillInfo,
	StatusResult,
} from "./protocol.ts";

export type NoticeKind = "info" | "plain" | "success" | "warning" | "error";

/** What a command can do to the app. Implemented by App. */
export interface FeatureHost {
	request<T>(method: string, params?: Record<string, unknown>): Promise<T>;
	print(text: string, kind?: NoticeKind): void;
	printMarkdown(text: string): void;
	session(): SessionInfo | undefined;
	/** Make *session* current and show *history*. */
	switchTo(session: SessionInfo, history: HistoryEntry[]): void;
	/** Update details (e.g. title) of the current session. */
	updateSession(session: SessionInfo): void;
	pick(title: string, items: SelectItem[]): Promise<SelectItem | undefined>;
	/** Write a file relative to the working directory; returns the absolute path. */
	writeFile(path: string, text: string): Promise<string>;
	onConfig(result: ConfigResult): void;
	clear(): void;
	exit(): Promise<void>;
}

interface Command extends SlashCommand {
	run(args: string, host: FeatureHost): Promise<void>;
}

const MEMORY_CATEGORIES = ["preference", "decision", "fact", "event", "transient"];

/** Argument completions for a fixed list of first words. */
function options(list: Array<[string, string]>): SlashCommand["getArgumentCompletions"] {
	return (prefix: string): AutocompleteItem[] | null => {
		const typed = prefix.trimStart().toLowerCase();
		if (/\s/.test(typed)) return null;
		const items = list
			.filter(([value]) => value.startsWith(typed))
			.map(([value, description]) => ({ value, label: value, description }));
		return items.length ? items : null;
	};
}

function requireSession(host: FeatureHost): SessionInfo {
	const session = host.session();
	if (!session) throw new Error("No active session. Use /new.");
	return session;
}

function requireArgs(args: string, usage: string): string {
	if (!args.trim()) throw new Error(`Usage: ${usage}`);
	return args.trim();
}

async function resolveSessionId(host: FeatureHost, idOrPrefix: string): Promise<string> {
	const wanted = idOrPrefix.trim().toLowerCase();
	if (/^[0-9a-f-]{36}$/.test(wanted)) return wanted;
	const { sessions } = await host.request<SessionListResult>("session.list", { limit: 200 });
	return resolvePrefix(wanted, sessions.map((s) => s.id), "session");
}

async function openSession(host: FeatureHost, id: string): Promise<void> {
	const { session, history } = await host.request<ResumeResult>("session.resume", { sessionId: id });
	host.switchTo(session, history);
	host.print(`Resumed “${session.title || "untitled"}” (${shortId(session.id)}).`);
}

function sessionItems(sessions: SessionInfo[]): SelectItem[] {
	return sessions.map((s) => ({
		value: s.id,
		label: s.title || "(untitled)",
		description: `${shortId(s.id)}  ${when(s.lastActivity)}`,
	}));
}

async function pickAndOpen(host: FeatureHost, title: string, sessions: SessionInfo[]): Promise<void> {
	const choice = await host.pick(title, sessionItems(sessions));
	if (choice) await openSession(host, choice.value);
}

async function confirm(host: FeatureHost, question: string, action: string): Promise<boolean> {
	const choice = await host.pick(question, [
		{ value: "no", label: "Cancel" },
		{ value: "yes", label: action },
	]);
	return choice?.value === "yes";
}

function formatValue(value: unknown): string {
	if (typeof value === "boolean") return value ? "on" : "off";
	return value === null || value === undefined || value === "" ? "—" : String(value);
}

// ─── sessions ─────────────────────────────────────────────────────────────────

const sessionCommands: Command[] = [
	{
		name: "new",
		description: "Start a new session",
		argumentHint: "[title]",
		async run(args, host) {
			const { session } = await host.request<SessionResult>("session.create", { title: args });
			host.switchTo(session, []);
		},
	},
	{
		name: "sessions",
		description: "Pick a recent session to resume",
		async run(_args, host) {
			const { sessions } = await host.request<SessionListResult>("session.list", { limit: 50 });
			if (!sessions.length) host.print("No sessions yet.");
			else await pickAndOpen(host, "Resume a session", sessions);
		},
	},
	{
		name: "resume",
		description: "Resume a session by id (prefix ok)",
		argumentHint: "<id>",
		async run(args, host) {
			if (!args) return sessionCommands[1]!.run("", host);
			await openSession(host, await resolveSessionId(host, args));
		},
	},
	{
		name: "search",
		description: "Find sessions by title",
		argumentHint: "<words>",
		async run(args, host) {
			const query = requireArgs(args, "/search <words>");
			const { sessions } = await host.request<SessionListResult>("session.search", { query, limit: 30 });
			if (!sessions.length) host.print(`No sessions match “${query}”.`);
			else await pickAndOpen(host, `Sessions matching “${query}”`, sessions);
		},
	},
	{
		name: "rename",
		description: "Rename the current session",
		argumentHint: "<title>",
		async run(args, host) {
			const current = requireSession(host);
			const title = requireArgs(args, "/rename <title>");
			const { session } = await host.request<SessionResult>("session.rename", { sessionId: current.id, title });
			host.updateSession(session);
			host.print(`Renamed to “${session.title}”.`, "success");
		},
	},
	{
		name: "goal",
		description: "Show or set the session goal",
		argumentHint: "[goal]",
		async run(args, host) {
			const current = requireSession(host);
			if (!args) {
				const ctx = await host.request<ContextResult>("context.get", { sessionId: current.id, limit: 1 });
				host.print(ctx.goal ? `Goal: ${ctx.goal}` : "No goal set. Use /goal <text>.");
				return;
			}
			const { goal } = await host.request<{ goal: string }>("session.setGoal", { sessionId: current.id, goal: args });
			host.print(`Goal set: ${goal}`, "success");
		},
	},
	{
		name: "fork",
		description: "Copy this session and continue in the copy",
		argumentHint: "[title]",
		async run(args, host) {
			const current = requireSession(host);
			const { session } = await host.request<SessionResult>("session.fork", { sessionId: current.id, title: args });
			await openSession(host, session.id);
		},
	},
	{
		name: "delete",
		description: "Delete a session (default: this one)",
		argumentHint: "[id]",
		async run(args, host) {
			const current = host.session();
			const id = args ? await resolveSessionId(host, args) : requireSession(host).id;
			if (!(await confirm(host, `Delete session ${shortId(id)}? This cannot be undone.`, "Delete"))) {
				host.print("Kept the session.");
				return;
			}
			await host.request("session.delete", { sessionId: id });
			host.print(`Deleted session ${shortId(id)}.`, "success");
			if (current?.id === id) {
				const { session } = await host.request<SessionResult>("session.create", {});
				host.switchTo(session, []);
			}
		},
	},
	{
		name: "export",
		description: "Save this conversation as Markdown",
		argumentHint: "[file.md]",
		async run(args, host) {
			const current = requireSession(host);
			const { markdown } = await host.request<{ markdown: string }>("session.export", { sessionId: current.id });
			const path = await host.writeFile(args || `session-${shortId(current.id)}.md`, markdown);
			host.print(`Exported to ${path}`, "success");
		},
	},
];

// ─── context ──────────────────────────────────────────────────────────────────

const contextCommands: Command[] = [
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

// ─── memory ───────────────────────────────────────────────────────────────────

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

const memoryCommand: Command = {
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
				host.print(results.length ? memoryTable(results, true) : `Nothing found for “${query}”.`, "plain");
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
				throw new Error(`Unknown /memory option “${sub}”. Try: list, search, add, forget, pending, approve, reject, stats.`);
		}
	},
};

// ─── skills ───────────────────────────────────────────────────────────────────

const skillsCommand: Command = {
	name: "skills",
	description: "Skills: list, show, learn, delete, health report",
	argumentHint: "[list|show|learn|delete|curator]",
	getArgumentCompletions: options([
		["list", "All installed skills"],
		["show", "Read a skill <name>"],
		["learn", "Create a skill from <file|url|skill>"],
		["delete", "Remove a skill <name>"],
		["curator", "Skill health report"],
	]),
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "":
			case "list": {
				const { skills } = await host.request<{ skills: SkillInfo[] }>("skills.list");
				host.print(
					skills.length
						? table(
								["Name", "Uses", "Triggers", "Description"],
								skills.map((s) => [s.name, String(s.usageCount), s.triggers.slice(0, 3).join(", ") || "—", s.description]),
								60,
							)
						: "No skills installed. Add one with /skills learn <file|url>.",
					"plain",
				);
				return;
			}
			case "show": {
				const { skill } = await host.request<{ skill: SkillInfo }>("skills.show", {
					name: requireArgs(rest, "/skills show <name>"),
				});
				const triggers = skill.triggers.length ? `\n\n*Triggers:* ${skill.triggers.join(", ")}` : "";
				host.printMarkdown(`## ${skill.name}\n\n${skill.description}${triggers}\n\n---\n\n${skill.content ?? ""}`);
				return;
			}
			case "learn": {
				const source = requireArgs(rest, "/skills learn <file|url|skill>");
				const { skill } = await host.request<{ skill: SkillInfo }>("skills.learn", { source });
				host.print(`Learned skill “${skill.name}”.`, "success");
				return;
			}
			case "delete": {
				const name = requireArgs(rest, "/skills delete <name>");
				if (!(await confirm(host, `Delete skill “${name}”?`, "Delete"))) {
					host.print("Kept the skill.");
					return;
				}
				await host.request("skills.delete", { name });
				host.print(`Deleted skill “${name}”.`, "success");
				return;
			}
			case "curator": {
				const report = await host.request<Record<string, unknown>>("skills.curator");
				const unused = (report.unused as string[] | undefined) ?? [];
				host.print(
					keyValues([
						["Skills", report.total_skills],
						["Enabled", report.enabled],
						["Never used", report.never_used],
						["Stale (30d)", report.stale_skills],
						["Total uses", report.total_uses],
						["Unused", unused.join(", ")],
					]),
					"plain",
				);
				return;
			}
			default:
				throw new Error(`Unknown /skills option “${sub}”. Try: list, show, learn, delete, curator.`);
		}
	},
};

// ─── settings ─────────────────────────────────────────────────────────────────

async function setConfig(host: FeatureHost, key: string, value: string, persist: boolean): Promise<void> {
	const result = await host.request<ConfigSetResult>("config.set", { key, value, persist });
	host.onConfig(result);
	host.print(`${key} = ${formatValue(result.value)}${persist ? " (saved)" : ""}`, "success");
}

const settingsCommands: Command[] = [
	{
		name: "model",
		description: "Show or set the model",
		argumentHint: "[model]",
		async run(args, host) {
			if (!args) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Model: ${config.model}`);
			} else await setConfig(host, "model", args, false);
		},
	},
	{
		name: "provider",
		description: "Show or set the provider",
		argumentHint: "[openrouter|ollama]",
		getArgumentCompletions: options([
			["openrouter", "OpenRouter (cloud models)"],
			["ollama", "Ollama (local models)"],
		]),
		async run(args, host) {
			if (!args) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Provider: ${config.provider}`);
			} else await setConfig(host, "provider", args.toLowerCase(), false);
		},
	},
	{
		name: "config",
		description: "Show settings, or set one (--save to keep it)",
		argumentHint: "[key [value] [--save]]",
		async run(args, host) {
			const persist = /(^|\s)--save(\s|$)/.test(args);
			const [key, value] = splitSub(args.replace(/(^|\s)--save(?=\s|$)/, " "));
			const { config, secrets } = await host.request<ConfigGetResult>("config.get");
			if (!key) {
				const keys = Object.keys(config).sort();
				host.print(
					keyValues(keys.map((k) => [k, secrets.includes(k) ? (config[k] ? "set (in .env)" : "not set") : formatValue(config[k])])),
					"plain",
				);
				return;
			}
			if (!(key in config)) throw new Error(`Unknown setting “${key}”. Use /config to list them.`);
			if (!value) host.print(`${key} = ${secrets.includes(key) ? (config[key] ? "set (in .env)" : "not set") : formatValue(config[key])}`);
			else await setConfig(host, key, value, persist);
		},
	},
	{
		name: "profile",
		description: "Show your profile, or set a preference",
		argumentHint: "[key value]",
		async run(args, host) {
			const userId = userInfo().username || "default";
			const [key, value] = splitSub(args);
			if (key && !value) throw new Error("Usage: /profile <key> <value>");
			const { profile } = key
				? await host.request<{ profile: ProfileInfo }>("profile.set", { userId, key, value })
				: await host.request<{ profile: ProfileInfo }>("profile.get", { userId });
			const prefs = Object.entries(profile.preferences);
			host.print(
				keyValues([
					["User", profile.userId],
					["Interactions", profile.interactionCount],
					["Top topics", profile.topTopics.map((t) => `${t.topic} (${t.count})`).join(", ")],
					...prefs.map(([k, v]): [string, unknown] => [`pref.${k}`, v]),
				]),
				"plain",
			);
		},
	},
	{
		name: "status",
		description: "Database, counts and configuration health",
		async run(_args, host) {
			const s = await host.request<StatusResult>("status");
			host.print(
				keyValues([
					["Database", s.postgres],
					["Model", `${s.model} (${s.provider})`],
					["OpenRouter key", s.openrouterKeySet ? "set" : "not set"],
					["Sessions", s.sessions],
					["Context chunks", s.contextChunks],
					["Memories", `${s.memories} (${s.pendingMemories} pending)`],
					["Tools", s.tools.join(", ")],
				]),
				"plain",
			);
		},
	},
];

// ─── agents (multi-agent) ───────────────────────────────────────────────────

const agentsCommand: Command = {
	name: "agents",
	description: "Agents: list, show, delete",
	argumentHint: "[list|show|delete]",
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
				if (!agent) throw new Error(`No agent named “${name}”.`);
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
			case "delete": {
				const name = requireArgs(rest, "/agents delete <name>");
				if (!(await confirm(host, `Delete agent “${name}”?`, "Delete"))) {
					host.print("Kept the agent.");
					return;
				}
				await host.request("agents.delete", { name });
				host.print(`Deleted agent “${name}”.`, "success");
				return;
			}
			default:
				throw new Error(`Unknown /agents option “${sub}”. Try: list, show, delete.`);
		}
	},
};

const delegateCommand: Command = {
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

// ─── app ──────────────────────────────────────────────────────────────────────

const appCommands: Command[] = [
	{
		name: "clear",
		description: "Clear the screen",
		async run(_args, host) {
			host.clear();
		},
	},
	{
		name: "exit",
		description: "Quit AgentHarness",
		async run(_args, host) {
			await host.exit();
		},
	},
];

const helpCommand: Command = {
	name: "help",
	description: "Show available commands",
	async run(_args, host) {
		host.print(
			`${helpText(COMMANDS)}\n\nKeys: Enter send · Shift+Enter newline · Esc stop reply · Tab complete · Ctrl+C exit`,
			"plain",
		);
	},
};

const COMMANDS: Command[] = [
	helpCommand,
	...sessionCommands,
	...contextCommands,
	memoryCommand,
	skillsCommand,
	agentsCommand,
	delegateCommand,
	...settingsCommands,
	...appCommands,
];

/** Commands offered by editor autocomplete. */
export const SLASH_COMMANDS: SlashCommand[] = COMMANDS;

/** Run a parsed slash command. Errors are reported through the host. */
export async function runCommand({ name, args }: ParsedCommand, host: FeatureHost): Promise<void> {
	const command = COMMANDS.find((c) => c.name === name);
	if (!command) {
		host.print(`Unknown command /${name}. Type /help for the list.`, "warning");
		return;
	}
	try {
		await command.run(args, host);
	} catch (error) {
		host.print(error instanceof Error ? error.message : String(error), "error");
	}
}
