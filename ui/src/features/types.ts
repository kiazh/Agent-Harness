// Shared types and helpers for feature commands.

import type { AutocompleteItem, SelectItem, SlashCommand } from "@earendil-works/pi-tui";
import type { ConfigResult, HistoryEntry, SessionInfo } from "../protocol.ts";

export type NoticeKind = "info" | "plain" | "success" | "warning" | "error";

/** Options for a single free-form text prompt shown as an overlay form. */
export interface AskTextOptions {
	title: string;
	description?: string;
	/** Prefilled draft (e.g. the current value being replaced). */
	initial?: string;
	placeholder?: string;
	example?: string;
	maxLength?: number;
	/**
	 * Masked single-line secret editor. The value is never rendered, never
	 * prefilled, and never echoed. Ordinary options keep multiline behavior.
	 */
	secret?: boolean;
}

export type AskTextResult = string | undefined;

/** Profile ids double as agent-scope slugs: lowercase, dashes, max 40 chars. */
export const PROFILE_ID_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;

/** What a command can do to the app. Implemented by App. */
export interface FeatureHost {
	request<T>(method: string, params?: Record<string, unknown>): Promise<T>;
	print(text: string, kind?: NoticeKind): void;
	printMarkdown(text: string): void;
	session(): SessionInfo | undefined;
	/** Submit a full agent turn (transcript echo + in-flight tracking). */
	submitTurn(text: string): Promise<void>;
	/** Make *session* current and show *history*. */
	switchTo(session: SessionInfo, history: HistoryEntry[]): void;
	/** Update details (e.g. title) of the current session. */
	updateSession(session: SessionInfo): void;
	pick(title: string, items: SelectItem[]): Promise<SelectItem | undefined>;
	/** Check multiple items; undefined means cancelled. Nothing starts here. */
	pickMany(title: string, items: SelectItem[]): Promise<SelectItem[] | undefined>;
	/**
	 * Live theme preview: selections recolor immediately, cancel restores,
	 * Enter commits. Returns the committed skin ID or undefined on cancel.
	 * Persistence stays with the caller.
	 */
	pickTheme(): Promise<string | undefined>;
	/** Collect free-form text in an overlay editor; undefined means cancelled. */
	askText(options: AskTextOptions): Promise<AskTextResult>;
	/**
	 * Cancellable transient progress overlay for browser authorization.
	 * Escape/cancel invokes options.onCancel; the overlay holds no secrets
	 * after dispose. Status updates never touch the transcript.
	 */
	showOAuthProgress(options: OAuthProgressOptions): OAuthProgressHandle;
	/** Copy a transient value using the local OS clipboard, if available. */
	copyText?(text: string): Promise<boolean>;
	/** Inspect locally retained tool output; never re-executes tools. */
	inspectOutput(toolId?: string): Promise<void>;
	/** Active user profile id (preferences bucket + new-chat agent scope). */
	profileId(): string;
	/** Switch the active profile (existing chats keep theirs). */
	setProfile(userId: string): void;
	/** Agent name for newly created sessions (undefined = shared default). */
	sessionAgent(): string | undefined;
	/** Write a file relative to the working directory; returns the absolute path. */
	writeFile(path: string, text: string): Promise<string>;
	onConfig(result: ConfigResult): void;
	clear(): void;
	/** Re-render the startup banner. */
	banner(): void;
	/** Re-resolve theme colors without clearing the transcript (skin change). */
	restyle(): void;
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

/** Cancellable transient progress overlay for OAuth browser authorization. */
export interface OAuthProgressOptions {
	title: string;
	status: string;
	/** Transient login link: shown only in the overlay viewer, never printed. */
	loginUrl?: string;
	onOpen(): void;
	onCopy?(): void;
	onCancel(): void;
}

export interface OAuthProgressHandle {
	/** Update the overlay status (and optional transient link). */
	update(status: string, loginUrl?: string): void;
	/** Dismiss after terminal completion. */
	dispose(): void;
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
