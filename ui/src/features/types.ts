// Shared types and helpers for feature commands.

import type { AutocompleteItem, SelectItem, SlashCommand } from "@earendil-works/pi-tui";
import type { ConfigResult, HistoryEntry, SessionInfo } from "../protocol.ts";

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

export interface Command extends SlashCommand {
	run(args: string, host: FeatureHost): Promise<void>;
}

/** Argument completions for a fixed list of first words. */
export function options(list: Array<[string, string]>): SlashCommand["getArgumentCompletions"] {
	return (prefix: string): AutocompleteItem[] | null => {
		const typed = prefix.trimStart().toLowerCase();
		if (/\s/.test(typed)) return null;
		const items = list
			.filter(([value]) => value.startsWith(typed))
			.map(([value, description]) => ({ value, label: value, description }));
		return items.length ? items : null;
	};
}

export function requireSession(host: FeatureHost): SessionInfo {
	const session = host.session();
	if (!session) throw new Error("No active session. Use /new.");
	return session;
}

export function requireArgs(args: string, usage: string): string {
	if (!args.trim()) throw new Error(`Usage: ${usage}`);
	return args.trim();
}

export async function confirm(host: FeatureHost, question: string, action: string): Promise<boolean> {
	const choice = await host.pick(question, [
		{ value: "no", label: "Cancel" },
		{ value: "yes", label: action },
	]);
	return choice?.value === "yes";
}

export function formatValue(value: unknown): string {
	if (typeof value === "boolean") return value ? "on" : "off";
	return value === null || value === undefined || value === "" ? "—" : String(value);
}
