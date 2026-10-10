// Slash-command parsing and help text. Command definitions live in features.ts.

import type { SlashCommand } from "@earendil-works/pi-tui";
import { theme } from "./theme.ts";

const ALIASES: Record<string, string> = {
	quit: "exit",
	q: "exit",
	h: "help",
	"?": "help",
	ls: "sessions",
	mem: "memory",
	skill: "skills",
	settings: "config",
	set: "config",
	compact: "compress",
	model: "default-model",
};

export interface ParsedCommand {
	name: string;
	args: string;
}

/** Parse `/name args...`; returns undefined for ordinary prompts. */
export function parseCommand(text: string): ParsedCommand | undefined {
	const trimmed = text.trim();
	if (!trimmed.startsWith("/") || trimmed.startsWith("//")) return undefined;
	const body = trimmed.slice(1);
	const space = body.search(/\s/);
	const head = (space === -1 ? body : body.slice(0, space)).toLowerCase();
	if (!head) return undefined;
	return { name: ALIASES[head] ?? head, args: space === -1 ? "" : body.slice(space).trim() };
}

/** Split `"sub rest of args"` into `["sub", "rest of args"]` (sub lowercased). */
export function splitSub(args: string): [string, string] {
	const trimmed = args.trim();
	const space = trimmed.search(/\s/);
	if (space === -1) return [trimmed.toLowerCase(), ""];
	return [trimmed.slice(0, space).toLowerCase(), trimmed.slice(space).trim()];
}

/**
 * Codex-style command groups for /help. Commands not listed fall into Other.
 * Order here is the presentation order.
 */
const HELP_GROUPS: Array<[group: string, names: string[]]> = [
	["Session", ["new", "resume", "sessions", "search", "recall", "rename", "fork", "delete", "export", "clear"]],
	["Task", ["goal", "review", "init", "delegate", "agents"]],
	["Memory", ["memory", "skills"]],
	["Context", ["context", "compress"]],
	["Automation", ["jobs"]],
	["Setup", ["keys", "models", "default-model", "provider", "effort", "config", "theme", "profile", "profiles", "mode", "approvals"]],
	["Info", ["status", "usage", "help", "exit"]],
];

export function helpText(commands: SlashCommand[]): string {
	const usage = (c: SlashCommand) => (c.argumentHint ? `${c.name} ${c.argumentHint}` : c.name);
	const width = Math.max(...commands.map((c) => usage(c).length));
	// Pad before styling so columns stay aligned; bold names act as headers.
	const line = (c: SlashCommand) => `${theme.bold(`/${usage(c).padEnd(width)}`)}  ${c.description ?? ""}`;
	const seen = new Set<string>();
	const out: string[] = [];
	for (const [group, names] of HELP_GROUPS) {
		const members = commands.filter((c) => names.includes(c.name));
		if (!members.length) continue;
		out.push(theme.bold(group));
		for (const c of members) {
			seen.add(c.name);
			out.push(`  ${line(c)}`);
		}
	}
	for (const c of commands.filter((c) => !seen.has(c.name))) out.push(line(c));
	return out.join("\n");
}
