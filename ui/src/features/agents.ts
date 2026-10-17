// Agent commands: agents (list, show, save, history, delete, sessions) and
// delegate (run a task on another agent).

import { keyValues, shortId, table, when } from "../format.ts";
import type { SelectItem } from "@earendil-works/pi-tui";
import type { AgentInfo, AgentHistoryResult, AgentSaveResult, DelegationResult, ResumeResult, SessionInfo } from "../protocol.ts";
import { listFamilySessions } from "../family.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";

/**
 * The family picker rows shared by `/agents sessions` and the composer's
 * Down-arrow menu: main marker, label, current marker, and a description
 * with the live run state. Pure presentation — callers own the pick/switch.
 */
export function agentSessionItems(
	family: SessionInfo[],
	current: SessionInfo,
	running: Map<string, boolean>,
): SelectItem[] {
	const rootId = current.parentSessionId ?? current.id;
	return family.map((session) => ({
		value: session.id,
		label: `${session.id === rootId ? "main · " : ""}${session.title || shortId(session.id)}${session.id === current.id ? " (current)" : ""}`,
		description: `${shortId(session.id)}  ${running.get(session.id) ? "running" : "idle"}  ${when(session.lastActivity)}`,
	}));
}

export const agentsCommand: Command = {
	name: "agents",
	description: "Agents: list, show, save, history, delete · sessions switches subagents (↓ on empty input, or Ctrl+Shift+←/→)",
	argumentHint: "[list|show|save|history|delete|sessions|switch]",
	getArgumentCompletions: options([
		["list", "All available agents"],
		["show", "Read an agent's definition <name>"],
		["save", "Save the current session as an agent <name>"],
		["history", "Recent agent activity"],
		["delete", "Remove a custom agent <name>"],
		["sessions", "Switch to a delegated subagent session"],
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
				// Live run state comes from the host's family tracking when the
				// host has it (App); other hosts report idle.
				const running = new Map((host.familySessions?.() ?? []).map((row) => [row.session.id, row.running]));
				const family = await listFamilySessions(host, current);
				if (family.length <= 1) {
					host.print("No delegated agent sessions in this family yet. Use /delegate to start one; Ctrl+Shift+←/→ cycles subagents.", "plain");
					return;
				}
				const choice = await host.pick("Switch to an agent session", agentSessionItems(family, current, running));
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
				const saveArgs = requireArgs(rest, "/agents save <name> [description]");
				// First token is the agent name; the remainder is the description.
				const [name, ...descWords] = saveArgs.split(/\s+/);
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
		// The reply is untrusted wire data: a child that fails before producing
		// a result, a server-side short-circuit or a malformed frame can leave
		// `results` empty or absent. Report that honestly instead of dereferencing
		// a non-existent result (P0-22).
		const reply = await host.request<{ results?: unknown } | undefined>("agents.run", {
			sessionId: host.session()?.id,
			steps: [{ agent, task }],
		});
		const results = Array.isArray(reply?.results) ? reply.results : [];
		if (!results.length) {
			host.print(
				`Delegation returned no result. ${agent} may have failed before responding; try again, or inspect it with /agents history.`,
				"error",
			);
			return;
		}
		const first: unknown = results[0];
		if (!isDelegationResult(first)) {
			host.print(
				`Delegation returned an invalid result (missing status). Nothing was applied; check /agents history.`,
				"error",
			);
			return;
		}
		const r = first;
		if (r.status === "error") host.print(`${agent} failed: ${r.response}`, "error");
		else if (r.status === "waiting_approval") await reportPausedDelegation(agent, r, host);
		else host.printMarkdown(`**${agent}** (${r.tokens} tokens):\n\n${r.response}`);
	},
};

/**
 * Runtime shape check for one `agents.run` step result. The gateway can answer
 * with a malformed frame; callers must not dereference an unvalidated object.
 */
function isDelegationResult(value: unknown): value is DelegationResult {
	return typeof value === "object" && value !== null && typeof (value as { status?: unknown }).status === "string";
}

/**
 * Distinct paused state for a delegated approval (H24).
 *
 * A child that pauses keeps its session alive with pending approval requests;
 * rendering its response like a finished answer hides both the requested
 * approvals and the child session that must be resumed after they are
 * resolved. The child session id travels with the delegation result and is
 * the continuation target here.
 */
async function reportPausedDelegation(agent: string, result: DelegationResult, host: FeatureHost): Promise<void> {
	const child = result.sessionId ?? "";
	const pauses = (result.needsApproval ?? []).filter((p) => typeof p.requestId === "string" && p.requestId);
	host.print(
		`${agent} is waiting for approval — it did not finish. Resolve the request, then resume the subagent session.`,
		"warning",
	);
	if (pauses.length) {
		host.print(
			table(
				["Request", "Operation", "Target"],
				pauses.map((p) => [shortId(p.requestId!), p.operation ?? "", p.target ?? ""]),
				40,
			),
			"plain",
		);
	}
	if (child) host.print(`Subagent session: ${shortId(child)} (also listed by /agents sessions)`, "plain");

	const choices: SelectItem[] = [];
	for (const pause of pauses) {
		const detail = [pause.operation, pause.target].filter(Boolean).join(" ");
		choices.push({ value: `allow:${pause.requestId}`, label: `Allow ${detail || pause.requestId}` });
		choices.push({ value: `deny:${pause.requestId}`, label: `Deny ${detail || pause.requestId}` });
	}
	if (child) {
		choices.push({
			value: "switch",
			label: "Switch to the subagent session",
			description: `${shortId(child)} — resolve the approval and resume it there`,
		});
	}
	choices.push({ value: "leave", label: "Leave it paused", description: "Continue later with /agents sessions" });
	const choice = await host.pick(`${agent} paused for approval`, choices);
	if (!choice || choice.value === "leave") {
		if (choice?.value === "leave") host.print(`Left ${shortId(child)} paused. Continue it later with /agents sessions.`, "plain");
		return;
	}
	if (choice.value === "switch" && child) {
		const { session, history } = await host.request<ResumeResult>("session.resume", { sessionId: child });
		host.switchTo(session, history);
		host.print(
			`Switched to "${session.title || "untitled"}" (${shortId(session.id)}). Resolve the approval with /approvals, then send a message to resume.`,
			"plain",
		);
		return;
	}
	const allowed = choice.value.startsWith("allow:");
	const requestId = choice.value.slice(allowed ? "allow:".length : "deny:".length);
	const pause = pauses.find((p) => p.requestId === requestId);
	const detail = [pause?.operation, pause?.target].filter(Boolean).join(" ");
	const params: Record<string, unknown> = {
		requestId,
		verdict: allowed ? "approved" : "denied",
	};
	// The approval belongs to the child session; binding the resolve to it
	// keeps the parent's own pending approvals untouched.
	if (child) params.sessionId = child;
	await host.request("approvals.resolve", params);
	host.print(
		`Approval ${allowed ? "allowed" : "denied"}: ${detail || requestId}. ` +
			`${agent} is still paused — switch to the subagent session (${shortId(child)}) and send a message to resume.`,
		allowed ? "success" : "warning",
	);
}
