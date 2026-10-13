// Session commands: new, sessions, resume, search, recall, rename, goal, fork, delete, export.

import type { SelectItem } from "@earendil-works/pi-tui";
import { resolvePrefix, shortId, when } from "../format.ts";
import type { ContextResult, RecallResult, RecallWindowResult, ResumeResult, SessionListResult, SessionInfo, SessionResult } from "../protocol.ts";
import { confirm, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";

/**
 * Upper bound on cursor pages scanned by prefix resolution and pickers.
 * A nextCursor that never ends (buggy or hostile gateway) must fail fast
 * instead of looping forever.
 */
const MAX_LIST_PAGES = 100;

async function resolveSessionId(host: FeatureHost, idOrPrefix: string): Promise<string> {
	let wanted = idOrPrefix.trim().toLowerCase();
	// Accept braced ({...}) and URN (urn:uuid:...) forms like Python's uuid.UUID.
	if (wanted.startsWith("urn:uuid:")) wanted = wanted.slice("urn:uuid:".length);
	if (wanted.startsWith("{") && wanted.endsWith("}")) wanted = wanted.slice(1, -1).trim();
	if (/^[0-9a-f-]{36}$/.test(wanted)) return wanted;
	// Paginate through sessions: a prefix must resolve even when the
	// matching session is older than the most recent page (bounded).
	let cursor: string | undefined;
	const ids: string[] = [];
	for (let page = 0; ; page++) {
		const { sessions, nextCursor } = await host.request<SessionListResult & { nextCursor?: string }>("session.list", {
			limit: 200,
			...(cursor ? { cursor } : {}),
		});
		ids.push(...sessions.map((s) => s.id));
		if (!nextCursor) break;
		if (page + 1 >= MAX_LIST_PAGES) {
			throw new Error(`Too many sessions to scan (${MAX_LIST_PAGES} pages); use a full id.`);
		}
		cursor = nextCursor;
	}
	return resolvePrefix(wanted, ids, "session");
}

async function openSession(host: FeatureHost, id: string): Promise<void> {
	const { session, history } = await host.request<ResumeResult>("session.resume", { sessionId: id });
	host.switchTo(session, history);
	host.print(`Resumed "${session.title || "untitled"}" (${shortId(session.id)}).`, "plain");
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
	description: "Resume a session (menu when empty)",
	async run(_args, host) {
		const { sessions } = await host.request<SessionListResult>("session.list", { limit: 50 });
		if (!sessions.length) host.print("No sessions yet.", "plain");
		else await pickAndOpen(host, "Resume a session", sessions);
	},
};

export const sessionCommands: Command[] = [
	{
		name: "new",
		description: "Start a new session",
		argumentHint: "[title]",
		async run(args, host) {
			const agent = host.sessionAgent();
			const { session } = await host.request<SessionResult>(
				"session.create",
				agent ? { title: args, agent } : { title: args },
			);
			host.switchTo(session, []);
		},
	},
	sessionsCommand,
	{
		name: "resume",
		description: "Resume a session (menu when empty)",
		argumentHint: "[id]",
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
			if (!sessions.length) host.print(`No sessions match "${query}".`, "plain");
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
				host.print(`No transcript matches for "${query}".`, "plain");
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
			host.print(`Transcript from ${hit.title || "(untitled)"} (${shortId(hit.sessionId)}):`, "plain");
			for (const message of messages) {
				const content = message.payload.content ?? message.payload.text ?? message.payload.result_preview ?? message.payload;
				const plain = typeof content === "string" ? content : JSON.stringify(content);
				host.print(`${message.type} [${message.source}]: ${plain.slice(0, 4000)}`, "plain");
			}
			host.print(`Use /resume ${hit.sessionId} to continue that conversation.`, "plain");
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
				host.print(ctx.goal ? `Goal: ${ctx.goal}` : "No goal set. Use /goal <text>.", "plain");
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
		description: "Select sessions to delete",
		argumentHint: "[id]",
		async run(args, host) {
			const current = host.session();
			let ids: string[];
			if (args) {
				ids = [await resolveSessionId(host, args)];
			} else {
				const sessions: SessionInfo[] = [];
				let cursor: string | undefined;
				host.print("Loading sessions…", "plain");
				for (let page = 0; ; page++) {
					const result = await host.request<SessionListResult & { nextCursor?: string }>("session.list", { limit: 200, ...(cursor ? { cursor } : {}) });
					sessions.push(...result.sessions);
					cursor = result.nextCursor;
					if (!cursor) break;
					if (page + 1 >= MAX_LIST_PAGES) {
						throw new Error(`Too many sessions to scan (${MAX_LIST_PAGES} pages); use /delete <id>.`);
					}
				}
				if (!sessions.length) {
					host.print("No sessions to delete.", "plain");
					return;
				}
				const selected = await host.pickMany("Select sessions to delete", sessionItems(sessions).map(item => ({
					...item, description: `${item.value === current?.id ? "current · " : ""}${item.description}`,
				})));
				if (!selected?.length) return;
				ids = [...new Set(selected.map(item => item.value))];
			}
			const label = ids.length === 1 ? `session ${shortId(ids[0]!)}` : `${ids.length} sessions`;
			if (!(await confirm(host, `Delete ${label}? This cannot be undone.`, `Delete ${ids.length === 1 ? "session" : `${ids.length} sessions`}`))) {
				host.print("Kept the sessions.", "plain");
				return;
			}
			host.print(`Deleting ${ids.length} session${ids.length === 1 ? "" : "s"}…`, "plain");
			const deleted = new Set<string>();
			const failures: string[] = [];
			// Keep existing per-session authorization/live-turn guards, with
			// bounded concurrency instead of one confirmation per session.
			for (let i = 0; i < ids.length; i += 3) {
				const batch = ids.slice(i, i + 3);
				const results = await Promise.allSettled(batch.map(sessionId => host.request<{ deleted: boolean }>("session.delete", { sessionId })));
				for (const [index, result] of results.entries()) {
					const id = batch[index]!;
					if (result.status === "fulfilled" && result.value.deleted) deleted.add(id);
					else failures.push(`${shortId(id)}: ${result.status === "rejected" ? (result.reason instanceof Error ? result.reason.message : String(result.reason)) : "Session was not deleted."}`);
				}
			}
			if (current && deleted.has(current.id)) {
				const agent = host.sessionAgent();
				const { session } = await host.request<SessionResult>(
					"session.create",
					agent ? { agent } : {},
				);
				host.switchTo(session, []);
			}
			host.print(`Deleted ${deleted.size} session${deleted.size === 1 ? "" : "s"}.${failures.length ? ` ${failures.length} could not be deleted.` : ""}`, failures.length ? "warning" : "success");
			for (const failure of failures) host.print(failure, "error");
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
