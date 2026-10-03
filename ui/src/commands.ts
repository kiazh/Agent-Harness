// Slash-command parsing and help text. Command definitions live in features.ts.

import type { SlashCommand } from "@earendil-works/pi-tui";

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

export function helpText(commands: SlashCommand[]): string {
	const usage = (c: SlashCommand) => (c.argumentHint ? `${c.name} ${c.argumentHint}` : c.name);
	const width = Math.max(...commands.map((c) => usage(c).length));
	return commands.map((c) => `/${usage(c).padEnd(width)}  ${c.description ?? ""}`).join("\n");
}
