// Shared types and helpers for feature commands.

import type { AutocompleteItem, SelectItem, SlashCommand } from "@earendil-works/pi-tui";
import type { ConfigResult, GatewayEvent, HistoryEntry, SessionInfo } from "../protocol.ts";
import type { NowPlayingState } from "../components/NowPlayingBar.ts";
import type { WorkspaceController } from "../workspace/controller.ts";
import type { UiPreferences } from "../workspace/preferences.ts";
import type { ChatSide, WorkspaceMode } from "../workspace/state.ts";

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
	/**
	 * Only meaningful with secret: true. Blank submits "" (explicit keyless
	 * path) instead of being rejected; undefined (Escape) still means
	 * cancelled. Defaults to false.
	 */
	allowEmpty?: boolean;
}

export type AskTextResult = string | undefined;

/** Profile ids double as agent-scope slugs: lowercase, dashes, max 40 chars. */
export const PROFILE_ID_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;

/** One session in the current session's agent family (the subagent rows). */
export interface FamilySession {
	session: SessionInfo;
	/** True for the family root — the main session the family is anchored to. */
	main: boolean;
	/** A turn is currently in flight for this session. */
	running: boolean;
}

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
	/**
	 * Observe every gateway event frame (feature-level flows like
	 * auth.chatgpt.* settle on their terminal event). Returns an unsubscribe
	 * function. Optional: hosts without streaming omit it and commands fall
	 * back to bounded polling.
	 */
	onGatewayEvent?(handler: (event: GatewayEvent) => void): () => void;
	/** Inspect locally retained tool output; never re-executes tools. */
	inspectOutput(toolId?: string): Promise<void>;
	/** Active user profile id (preferences bucket + new-chat agent scope). */
	profileId(): string;
	/** Switch the active profile (existing chats keep theirs). */
	setProfile(userId: string): void;
	/** Agent name for newly created sessions (undefined = shared default). */
	sessionAgent(): string | undefined;
	/**
	 * Sessions in the current agent family, main first (the subagent rows for
	 * the footer count and menus). Optional: hosts without family tracking
	 * omit it and commands fall back to filtering session.list client-side.
	 */
	familySessions?(): FamilySession[];
	/** Write a file relative to the working directory; returns the absolute path. */
	writeFile(path: string, text: string): Promise<string>;
	onConfig(result: ConfigResult): void;
	clear(): void;
	/** Re-render the startup banner. */
	banner(): void;
	/** Re-resolve theme colors without clearing the transcript (skin change). */
	restyle(): void;
	exit(): Promise<void>;
	/**
	 * Integrated workspace surface (T3/T6/T12). Optional: hosts without the
	 * feature omit it and the workspace commands no-op with one notice.
	 */
	workspace?(): WorkspaceHost | undefined;
	/**
	 * Global UI settings surface (closing pass): editor preferences, the
	 * workspace open/close passthroughs, LSP rows and the theme link for the
	 * `/settings` menu. Optional: hosts without it fall back to the text dump.
	 */
	uiSettings?(): UiSettingsHost | undefined;
	/**
	 * Music surface (pinned now-playing bar): playback control, library
	 * rescanning and folder/enabled persistence. Optional: hosts without it
	 * report one honest notice instead of failing.
	 */
	music?(): MusicHost | undefined;
}

/** Now-playing surface for the /music command (implemented by App). */
export interface MusicHost {
	status(): NowPlayingState | undefined;
	isEnabled(): boolean;
	/** Persist the bar on/off; returns whether it saved. */
	setEnabled(enabled: boolean): Promise<boolean>;
	/** Re-read what is playing right now. */
	refresh(): Promise<void>;
	toggle(): Promise<void>;
	next(): Promise<void>;
	previous(): Promise<void>;
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

// ─── integrated workspace (T3/T6/T12) ───────────────────────────────────────

/**
 * Integrated-workspace surface the app exposes to the workspace commands
 * (plan §8). Layout mutations go through the host so the shell geometry and
 * focus router stay in sync with the controller; reads use the controller's
 * public API directly.
 */
export interface WorkspaceHost {
	/** Orchestration hub (documents, layout intent, LSP, persistence). */
	controller: WorkspaceController;
	/** True while the feature flag is on and the backend serves workspace.*. */
	controllerAvailable(): boolean;
	setMode(mode: WorkspaceMode): void;
	toggleChatMaximize(): void;
	toggleEditorMaximize(): void;
	restoreSplit(): void;
	/** Toggle/open/close the explorer column (loads the listing on open). */
	setExplorer(open: boolean): void;
	/** Close the active editor tab through the dirty-guarded decision flow. */
	closeActiveTab(): void;
	/** Place the chat pane left/right of a split; persisted like the ratio. */
	setChatSide(side: ChatSide): void;
	/** Re-sync shell geometry after a direct controller layout call (setRatio). */
	layoutChanged(): void;
	/** Open (or focus) the Problems pane; app-owned layout and focus. */
	openProblems(): void;
	/** Open the Quick Open overlay; app-owned overlay and focus. */
	openQuickOpen(): void;
	/**
	 * Run the same explicit completion flow Ctrl+Space triggers (the
	 * `/editor complete` fallback for terminals that steal Ctrl+Space).
	 * Optional so other hosts stay compatible.
	 */
	completeAtCursor?(): void;
	/**
	 * Explicit user command to reopen the workspace at another folder
	 * (`/workspace open <path>`). Clean tabs from the previous root close;
	 * dirty buffers always stay. Optional so other hosts stay compatible.
	 */
	openRoot?(path: string): Promise<{ ok: boolean; message: string }>;
	/** Dirty local buffers for the close confirmation (bounded basenames). */
	dirtyBuffers(): { count: number; names: string[] };
}

export async function confirm(host: FeatureHost, question: string, action: string): Promise<boolean> {
	const choice = await host.pick(question, [
		{ value: "no", label: "Cancel" },
		{ value: "yes", label: action },
	]);
	return choice?.value === "yes";
}

// ─── global UI settings (closing pass) ──────────────────────────────────────

/** Preference keys the settings menu can toggle; values are boolean|2|4|8. */
export type UiPreferenceKey = "syntax" | "softWrap" | "tabWidth";

/** §snapshot: what the settings menu renders against right now. */
export interface UiSettingsSnapshot {
	preferences: UiPreferences;
	/** True while the integrated workspace can execute layout/file commands. */
	workspaceAvailable: boolean;
	/** True while the editor pane exists (editor preferences have a target). */
	editorAvailable: boolean;
}

/** One language-server row for the settings menu (profile id + honest state). */
export interface LspSettingsRow {
	profile: string;
	state: string;
	detail?: string;
}

/**
 * Minimal typed surface the `/settings` menu needs (closing pass). Reads are
 * cheap snapshots; `setPreference` persists through `ui_preferences` and
 * applies to the live view (it rejects when persistence fails, with the value
 * still applied for the session — the menu reports that honestly).
 */
export interface UiSettingsHost {
	snapshot(): UiSettingsSnapshot;
	setPreference(key: UiPreferenceKey, value: boolean | number): Promise<UiPreferences>;
	/** The `/workspace open <path>` flow (clean tabs close; dirty buffers stay). */
	openWorkspaceFolder(path: string): Promise<{ ok: boolean; message: string }>;
	/** Close the remote workspace; local buffers stay open. */
	closeWorkspace(): Promise<void>;
	/** One row per language-server profile from the cached snapshot. */
	lspRows(): LspSettingsRow[];
	/** Poll `workspace.lspStatus` so the rows are fresh before display. */
	refreshLsp(): Promise<void>;
}

export function formatValue(value: unknown): string {
	if (typeof value === "boolean") return value ? "on" : "off";
	return value === null || value === undefined || value === "" ? "—" : String(value);
}
