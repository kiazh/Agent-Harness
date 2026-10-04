// Session commands: new, sessions, resume, search, recall, rename, goal, fork, delete, export.

import type { SelectItem } from "@earendil-works/pi-tui";
import { resolvePrefix, shortId, when } from "../format.ts";
import type { ContextResult, RecallResult, RecallWindowResult, ResumeResult, SessionListResult, SessionInfo, SessionResult } from "../protocol.ts";
import { confirm, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";

async function resolveSessionId(host: FeatureHost, idOrPrefix: string): Promise<string> {
	const wanted = idOrPrefix.trim().toLowerCase();
	if (/^[0-9a-f-]{36}$/.test(wanted)) return wanted;
	// Paginate through all sessions: a prefix must resolve even when the
	// matching session is older than the most recent page.
	let cursor: string | undefined;
	const ids: string[] = [];
	for (;;) {
		const { sessions, nextCursor } = await host.request<SessionListResult & { nextCursor?: string }>("session.list", {
			limit: 200,
			...(cursor ? { cursor } : {}),
		});
		ids.push(...sessions.map((s) => s.id));
		if (!nextCursor) break;
		cursor = nextCursor;
	}
	return resolvePrefix(wanted, ids, "session");
}

async function openSession(host: FeatureHost, id: string): Promise<void> {
	const { session, history } = await host.request<ResumeResult>("session.resume", { sessionId: id });
	host.switchTo(session, history);
	host.print(`Resumed "${session.title || "untitled"}" (${shortId(session.id)}).`);
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

const sessionsCommand: Command = {
	name: "sessions",
	description: "Pick a recent session to resume",
	async run(_args, host) {
		const { sessions } = await host.request<SessionListResult>("session.list", { limit: 50 });
		if (!sessions.length) host.print("No sessions yet.");
		else await pickAndOpen(host, "Resume a session", sessions);
	},
};

export const sessionCommands: Command[] = [
	{
		name: "new",
		description: "Start a new session",
		argumentHint: "[title]",
		async run(args, host) {
			const { session } = await host.request<SessionResult>("session.create", { title: args });
			host.switchTo(session, []);
		},
	},
	sessionsCommand,
	{
		name: "resume",
		description: "Resume a session by id (prefix ok)",
		argumentHint: "<id>",
		async run(args, host) {
			if (!args) return sessionsCommand.run("", host);
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
			if (!sessions.length) host.print(`No sessions match "${query}".`);
			else await pickAndOpen(host, `Sessions matching "${query}"`, sessions);
		},
	},
	{
		name: "recall",
		description: "Search past conversation text and inspect a match",
		argumentHint: "<words>",
		async run(args, host) {
			const current = requireSession(host);
			const query = requireArgs(args, "/recall <words>");
			const { hits } = await host.request<RecallResult>("session.recall", { sessionId: current.id, query, limit: 30 });
			if (!hits.length) {
				host.print(`No transcript matches for "${query}".`);
				return;
			}
			const choice = await host.pick(`Recall "${query}"`, hits.map((hit) => ({
				value: hit.chunkId ?? hit.sessionId,
				label: `${hit.title || "(untitled)"}: ${hit.preview.slice(0, 80)}`,
				description: `${hit.source}  ${shortId(hit.sessionId)}  ${when(hit.occurredAt)}`,
			})));
			if (!choice) return;
			const hit = hits.find((item) => (item.chunkId ?? item.sessionId) === choice.value);
			if (!hit) return;
			if (!hit.chunkId) {
				await openSession(host, hit.sessionId);
				return;
			}
			const { messages } = await host.request<RecallWindowResult>("session.recall.window", {
				sessionId: current.id, targetSessionId: hit.sessionId, chunkId: hit.chunkId,
			});
			host.print(`Transcript from ${hit.title || "(untitled)"} (${shortId(hit.sessionId)}):`);
			for (const message of messages) {
				const content = message.payload.content ?? message.payload.text ?? message.payload.result_preview ?? message.payload;
				const plain = typeof content === "string" ? content : JSON.stringify(content);
				host.print(`${message.type} [${message.source}]: ${plain.slice(0, 4000)}`, "plain");
			}
			host.print(`Use /resume ${hit.sessionId} to continue that conversation.`);
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
			host.print(`Renamed to "${session.title}".`, "success");
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
