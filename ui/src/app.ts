// The interactive app: transcript, editor and footer on a pi-tui alt screen,
// driven by the Python gateway. Slash commands live in features.ts.

import { mkdir, writeFile } from "node:fs/promises";
import { userInfo } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
	Container,
	type Component,
	type Editor,
	isViewportTUI,
	Key,
	matchesKey,
	type OverlayHandle,
	type SelectItem,
	type TUI,
} from "@earendil-works/pi-tui";
import { ArgumentAwareAutocompleteProvider } from "./autocomplete.ts";
import { parseCommand, splitSub, type ParsedCommand } from "./commands.ts";
import { ComposerBox, Footer, header, Picker, SecretForm, TextForm, ThinkingIndicator } from "./components.ts";
import { DEFAULT_MENU_VISIBLE_ROWS, Menu } from "./components/Menu.ts";
import { OAuthProgress } from "./components/OAuthProgress.ts";
import { SkillsMenu, type SkillsCandidate, type SkillsDetailsSkill, type SkillsFilters, type SkillsPreviewResult, type SkillsSearchParams, type SkillsSearchResult, type SkillsSectionParams, type SkillsSectionResult } from "./components/SkillsMenu.ts";
import { HELP_KEYS, HelpMenu, helpMenuCommands } from "./components/HelpMenu.ts";
import { ApprovalCard, type ApprovalDecision } from "./components/ApprovalCard.ts";
import { AppShell } from "./components/AppShell.ts";
import { EditorPane, uriDisplayPath } from "./components/EditorPane.ts";
import { OutputStore, OutputViewer, type OutputRecord } from "./components/OutputViewer.ts";
import { ProblemsPane } from "./components/ProblemsPane.ts";
import {
	CompletionPopup,
	completionPopupModelFrom,
} from "./components/IntelligencePopups.ts";
import { AnimationCoordinator, CURSOR_BLINK_INTERVAL_MS, reducedMotionFromEnv } from "./design/motion.ts";
import type { AnimationSubscription } from "./design/motion.ts";
import { SHORTCUT_ROWS } from "./design/shortcuts.ts";
import { NowPlayingBar } from "./components/NowPlayingBar.ts";
import { NowPlayingController } from "./nowplaying/controller.ts";
import type { MusicHost } from "./features/types.ts";
import { type FamilySession, type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS, type WorkspaceHost } from "./features/index.ts";
import type { WorkspaceAttachmentHost } from "./features/workspace.ts";
import { guardSubmit } from "./commandDispatch.ts";
import { type AskTextOptions, PROFILE_ID_RE, type OAuthProgressHandle, type OAuthProgressOptions, type HelpPaletteHost, type SkillsDirectoryHost, type UiPreferenceKey, type UiSettingsHost } from "./features/types.ts";
import { FamilyStore, listFamilySessions } from "./family.ts";
import { agentSessionItems } from "./features/agents.ts";
import type { GatewayClient } from "./gateway.ts";
import { copyText as copyClipboardText } from "./clipboard.ts";
import { clipboardImage, clipboardText, ImageDraft, imagePaths, readImageFile, type ImageAttachment } from "./images.ts";
import type {
	ConfigGetResult,
	ConfigResult,
	GatewayEvent,
	HistoryEntry,
	InitializeResult,
	ResumeResult,
	SessionInfo,
	SessionResult,
} from "./protocol.ts";
import { RpcError } from "./protocol.ts";
import { EditorStore, type DocumentRecord } from "./editor/model.ts";
import { sanitizeDisplay } from "./editor/display.ts";
import { TabModel } from "./editor/tabs.ts";
import { EditorView } from "./editor/view.ts";
import { languageForId } from "./editor/syntax.ts";
import { ExplorerModel, ExplorerPane } from "./editor/explorer.ts";
import { QuickOpenPane } from "./editor/quickOpen.ts";
import {
	CompletionSession,
	LocationPicker,
	narrowLocationsPayload,
	ProblemsModel,
	RenameSession,
	type IntelligenceRequest,
	type LocationChoice,
	type ProblemRow,
} from "./editor/intelligence.ts";
import { offsetToPosition, positionToOffset } from "./editor/positions.ts";
import { SessionTailStore, tailEntries, trimSupersededHistory, type SessionTail, type TailBlock } from "./tail.ts";
import { editorTheme, getSkin, isOpaqueBackground, setOpaqueBackground, setSkin, SKIN_NAMES, skinDescription } from "./theme.ts";
import { Transcript } from "./transcript.ts";
import {
	WorkspaceController,
	type ConflictResolution,
	type DirtyCloseChoice,
	type IntelligenceOutcome,
	type SaveConflictDescriptor,
	type WorkspacePersistenceLike,
} from "./workspace/controller.ts";
import { WorkspaceGatewayAdapter, type WorkspaceRequestFn } from "./workspace/gateway.ts";
import { WorkspacePersistence } from "./workspace/persistence.ts";
import {
	defaultUiPreferences,
	UiPreferencesPersistence,
	type UiPreferences,
} from "./workspace/preferences.ts";
import { computeLayout, explorerTreeRowBudget } from "./workspace/layout.ts";
import type {
	FocusRegion,
	LayoutInput,
	LayoutResult,
	WorkspacePersistedState,
} from "./workspace/state.ts";
import type {
	LspPosition,
	WorkspaceEvent,
	WorkspaceQuickOpenResult,
} from "./workspace/protocol.ts";

export interface AppOptions {
	model?: string;
	provider?: string;
	sessionId?: string;
	mode?: string;
}

/** The default profile (first-touch identity); maps to the shared agent. */
export function defaultProfileId(): string {
	try {
		return userInfo().username || "default";
	} catch {
		return "default";
	}
}

/** Presentation record for one session's in-flight turn (section 4.2). */
interface SessionTurn {
	/** Accepted turn ID; undefined until the first bound event or submit result. */
	turnId: string | undefined;
	/** True while the binding is provisional (submit unresolved or start-only). */
	provisional: boolean;
	/** A message.start has been applied for the bound turn. */
	started: boolean;
	/** Any event has bound to this record (dispatch evidence). */
	hadEvents: boolean;
	/** The local watchdog has warned once for this turn. */
	warnedSlow: boolean;
	/** Latest cumulative usage for the active turn. */
	usageLatest: number;
	/** Bounded retired turn IDs; late duplicates are inert. */
	retired: string[];
}

/**
 * Integrated workspace wiring (T3/T6/T12): the controller plus the retained
 * views/panes it drives. Constructed once at start() when the feature is
 * enabled; absent forever otherwise (chat-only behavior stays identical).
 */
interface WorkspaceIntegration {
	gateway: WorkspaceGatewayAdapter;
	controller: WorkspaceController;
	store: EditorStore;
	tabs: TabModel;
	view: EditorView;
	pane: EditorPane;
	explorerModel: ExplorerModel;
	explorerPane: ExplorerPane;
	problemsModel: ProblemsModel;
	problemsPane: ProblemsPane;
}

/** Layout-intent + terminal identity for shell re-sync (cheap change key). */
function workspaceShellKey(input: LayoutInput, columns: number, rows: number): string {
	return [
		input.requestedMode,
		input.splitRatio,
		input.explorerRequested ? 1 : 0,
		input.preferredPane,
		input.chatSide ?? "left",
		columns,
		rows,
	].join("|");
}

/**
 * Legacy xterm sequences pi-tui 1.0.0 cannot express through `matchesKey`:
 * its f-key matcher requires modifier 0, so shift+F# never matches. The app
 * decodes these two explicitly for Shift+F6 (focus cycle) and Shift+F12
 * (references); `matchesKey` forms are kept alongside for a future toolkit.
 */
const SHIFT_F6_SEQUENCE = "\x1b[17;2~";
const SHIFT_F12_SEQUENCE = "\x1b[24;2~";

/**
 * Footer workspace hints (closing pass), shown only while a workspace
 * identity is live. Truthful to the editor-scoped Ctrl+←/→ semantics:
 * Ctrl+→ maximizes chat and Ctrl+← returns to the 50/50 split while the
 * editor region owns focus (the composer keeps its word motion).
 */
const WORKSPACE_FOOTER_HINTS = "Ctrl+T explorer · Ctrl+←/→ panes";

/**
 * Row cap for the keyboard-shortcuts menu. The documented list is long, so it
 * gets a couple more rows than the shared default
 * ({@link DEFAULT_MENU_VISIBLE_ROWS}) while still scrolling within the box.
 */
const SHORTCUTS_VISIBLE_ROWS = 14;

/** Basename for notices (scheme-stripped, percent-decoded; never the raw URI). */
function displayUriName(uri: string): string {
	const path = uriDisplayPath(uri);
	const parts = path.split("/").filter((segment) => segment.length > 0);
	return parts[parts.length - 1] ?? path;
}

/** Inline-safe display text: one line, no controls or backticks (header/notice surfaces). */
function inlineSafe(text: string): string {
	return sanitizeDisplay(text).replace(/`+/g, "").trim();
}

/** Fence long enough that no backtick run inside the text can close it. */
function fenceFor(text: string): string {
	let longest = 0;
	for (const run of text.match(/`+/g) ?? []) longest = Math.max(longest, run.length);
	return "`".repeat(Math.max(3, longest + 1));
}

/** Fence language token: identifier characters only (never markup). */
function fenceLanguage(languageId: string): string {
	return languageId.replace(/[^A-Za-z0-9_+.#-]/g, "");
}

/** Completion-prefix characters: the identifier run the list is filtered by. */
const COMPLETION_PREFIX_CHAR = /[A-Za-z0-9_$]/;

/**
 * Word prefix before the caret (finding 6): the trailing identifier run at
 * `cursorOffset`, used to refilter an open completion list after a typed
 * character. Punctuation/whitespace end the run, so the filter never spans
 * tokens.
 */
function completionPrefixBefore(text: string, cursorOffset: number): string {
	const offset = Math.max(0, Math.min(cursorOffset, text.length));
	let start = offset;
	while (start > 0 && COMPLETION_PREFIX_CHAR.test(text[start - 1] ?? "")) start -= 1;
	return text.slice(start, offset);
}

/** True when an untrusted `workspace.open` payload carries a usable binding. */
function isWorkspaceIdentity(value: unknown): boolean {
	if (typeof value !== "object" || value === null) return false;
	const identity = value as { workspaceId?: unknown; epoch?: unknown; resolvedRoot?: unknown };
	return (
		typeof identity.workspaceId === "string" &&
		identity.workspaceId.length > 0 &&
		typeof identity.epoch === "number" &&
		Number.isFinite(identity.epoch) &&
		typeof identity.resolvedRoot === "string" &&
		identity.resolvedRoot.length > 0
	);
}

/**
 * Typed failure for the IntelligenceRequest adapter: sessions catch it and
 * record an honest failure (including `superseded`, whose result is gone).
 */
class WorkspaceIntelligenceFailure extends Error {
	readonly reason: "refused" | "failed" | "no-workspace" | "superseded";

	constructor(reason: "refused" | "failed" | "no-workspace" | "superseded", message: string) {
		super(message);
		this.name = "WorkspaceIntelligenceFailure";
		this.reason = reason;
	}
}

function intelligenceOutcomeMessage(outcome: IntelligenceOutcome): string {
	switch (outcome.status) {
		case "refused":
		case "failed":
			return outcome.message;
		case "no-workspace":
			return "no workspace is open";
		case "superseded":
			return "superseded by a newer request";
		default:
			return "unavailable";
	}
}

/** One dialog title/row label sink: never render decoded or server text raw. */
function sanitizeSelectItems(items: SelectItem[]): SelectItem[] {
	return items.map((item) => ({
		value: item.value,
		label: sanitizeDisplay(item.label),
		...(item.description === undefined ? {} : { description: sanitizeDisplay(item.description) }),
	}));
}

function sanitizeAskTextOptions(options: AskTextOptions): AskTextOptions {
	return {
		...options,
		title: sanitizeDisplay(options.title),
		...(options.description === undefined ? {} : { description: sanitizeDisplay(options.description) }),
		...(options.placeholder === undefined ? {} : { placeholder: sanitizeDisplay(options.placeholder) }),
		...(options.example === undefined ? {} : { example: sanitizeDisplay(options.example) }),
	};
}

export class App implements FeatureHost {
	readonly exited: Promise<void>;

	private readonly tui: TUI;
	private readonly client: GatewayClient;
	private readonly options: AppOptions;
	private readonly transcriptView = new Container();
	private readonly transcript: Transcript;
	private readonly composer: ComposerBox;
	private readonly editor: Editor;
	private readonly coordinator: AnimationCoordinator;
	private readonly footer: Footer;
	/**
	 * Codex-style bottom-docked layout: the transcript scrolls in the
	 * flexible area while the composer and footer stay pinned to the
	 * bottom rows. The shell retains the primary ScrollView for life and
	 * recomputes density budgets from actual terminal dimensions.
	 */
	private readonly shell: AppShell;
	private readonly nowPlayingBar: NowPlayingBar;
	private nowPlayingController: NowPlayingController | undefined;
	private nowPlayingAnim: AnimationSubscription | undefined;
	/**
	 * Contract C3 / MCH-012: ONE authoritative workspace lifecycle fence.
	 * `workspaceGeneration` is the app-side epoch; every lifecycle entry point
	 * (background open, root switch, close, exit, gateway exit) bumps it, so a
	 * continuation that captured the previous epoch publishes nothing.
	 * `workspaceFenceController` is the controller generation that epoch is
	 * bound to (`undefined` until a mounted controller is captured). A fence is
	 * current only while BOTH sources agree: see `isWorkspaceCurrent`.
	 */
	private workspaceGeneration = 0;
	private workspaceFenceController: number | undefined;
	/**
	 * The single caret-blink subscription (key `cursor.blink`). Present only
	 * while a text surface owns the caret; its presence is the source of
	 * truth for "blinking is active" so focus changes can never stack loops.
	 */
	private cursorBlinkSub: AnimationSubscription | undefined;
	/** Wall-clock start of the current blink phase (kept across re-subscribes). */
	private cursorBlinkStartedAt = 0;
	private lastPrompt = "";
	private lastEscAt = 0;
	private version = "";
	private current: SessionInfo | undefined;
	private readonly defaultProfile = defaultProfileId();
	private currentProfile: string = this.defaultProfile;
	private running = false;
	/** Session ids of turns that are still in flight (prompt submitted, no message.complete yet). */
	private readonly inFlight = new Set<string>();
	/** Per-session turn binding, provisional pre-RPC state and retired IDs. */
	private readonly turns = new Map<string, SessionTurn>();
	/** Switch recency (oldest first) for bounded per-session state. */
	private readonly sessionRecency: string[] = [];
	/** Timers for the local watchdog (diagnostic deadline, not ownership). */
	private readonly inFlightTimers = new Map<string, NodeJS.Timeout>();
	private sessionTokens = 0;
	/** Preserved token counts per session so switchTo does not lose them. */
	private readonly sessionTokensById = new Map<string, number>();
	/**
	 * Hidden-session live tails: transcript work that arrived while a session
	 * was not visible. switchTo merges them (buffered tail wins over the
	 * fetched history) and consumes them. Bounded (see tail.ts).
	 */
	private readonly sessionTails = new SessionTailStore({
		visible: () => this.current?.id,
		running: (sessionId) => this.inFlight.has(sessionId),
		retained: (sessionId) =>
			sessionId === this.current?.id || this.inFlight.has(sessionId) || this.isFamilyMember(sessionId),
	});
	/** Family (agent family) state and actions; App owns rendering and keys. */
	private readonly family = new FamilyStore({
		current: () => this.current,
		isRunning: (sessionId) => this.inFlight.has(sessionId),
		request: <T>(method: string, params?: Record<string, unknown>) => this.request<T>(method, params ?? {}),
		switchTo: (session, history) => this.switchTo(session, history),
		changed: () => {
			this.pruneSessionTails();
			this.syncAgentCount();
			this.tui.requestRender();
		},
		report: (error) => {
			this.transcript.addNotice(`Could not switch session: ${message(error)}`, "error");
			this.tui.requestRender();
		},
	});
	/** Bounded retained tool outputs for /output inspection (all sessions). */
	private readonly outputStore = new OutputStore();
	/** Open approval reviews keyed by request ID (binding captured at open). */
	private readonly approvalDialogs = new Map<
		string,
		{
			requestId: string;
			sessionId: string;
			originTurnId: string | undefined;
			transportTurnId: string;
			card: ApprovalCard;
			evidenceAdded: boolean;
			close: () => void;
			dialog: Promise<void | undefined>;
		}
	>();
	/**
	 * Per-request approval evidence retained across session navigation
	 * (R15). The interactive card is presentation; this record survives
	 * leaving a session so the paused backend's review can be rebuilt on
	 * return, and is retired only by a decision or turn completion.
	 */
	private readonly pendingApprovals = new Map<
		string,
		Extract<GatewayEvent, { type: "needs_approval" | "permission.required" }>
	>();
	private readonly dialogs: Array<{ handle: OverlayHandle; cancel: () => void }> = [];
	private exiting = false;
	/** Re-entrancy guard: a Down pressed while the subagent list is still
	 *  being fetched must not stack a second picker (review round 4, F1). */
	private agentMenuOpening = false;
	/**
	 * Feature-level event subscribers (e.g. /auth waiting on
	 * auth.chatgpt.*). They observe every frame before turn bookkeeping,
	 * own their own filtering, and never mutate turn state.
	 */
	private readonly featureEventHandlers = new Set<(event: GatewayEvent) => void>();
	private resolveExit: () => void = () => {};
	private inputListenerDisposer: (() => void) | undefined;
    private imageDraft = new ImageDraft();
    private imageLoads = Promise.resolve();
    private imageEpoch = 0;
    private pendingImageLoads = 0;
    private imageLoadFailures = 0;
    private lastPromptImages: ImageAttachment[] = [];
    private lastPromptSessionId: string | undefined;

	// ─── integrated workspace (T3/T6/T12) ───────────────────────────────────
	/** MACHINA_WORKSPACE gate; captured at start() so tests can set it late. */
	private workspaceFlagOn = true;
	/** The backend answered METHOD_NOT_FOUND for workspace.* exactly once. */
	private workspaceUnsupported = false;
	private integration: WorkspaceIntegration | undefined;
	private workspaceHostSurface: WorkspaceHost | undefined;
	/** True once a successful open pushed geometry to the shell. */
	private workspaceLayoutActive = false;
	/** Latest computed workspace geometry (drives focus regions + pane rows). */
	private workspaceLayout: LayoutResult | undefined;
	private lastWorkspaceShellKey = "";
	/** v1 focus router state (plan §8): the last meaningfully focused region. */
	private activeRegion: FocusRegion = "chat-composer";
	private problemsOpen = false;
	private completionSession: CompletionSession | undefined;
	/** Mounted completion popup for the open session (refilter setModel seam). */
	private completionPopup: CompletionPopup | undefined;
	/** Orders deferred completion refilters; a newer pass supersedes older ones. */
	private completionRefilterSeq = 0;
	private quickOpenPane: QuickOpenPane | undefined;
	private quickOpenHandle: OverlayHandle | undefined;
	/** `/skills` Menu dialog (A5, spec §12.3): the mounted component (dedup + tests). */
	private skillsMenu: SkillsMenu | undefined;
	/** `/help` searchable command/keybinding Menu dialog. */
	private helpMenu: HelpMenu | undefined;
	/** Re-entrancy guard for the dirty-exit review. */
	private dirtyExitInProgress = false;
	/** Stored workspace_ui_state captured BEFORE open (open overwrites it). */
	private workspacePreOpenRaw: string | undefined;
	private workspacePreOpenCaptured = false;
	/** Global editor preferences (ui_preferences); defaults until loaded. */
	private uiPrefs: UiPreferences = defaultUiPreferences();

	// Match the server turn timeout (turn_timeout=300s) so the UI does not
	// time out and unlock while the server is still running the turn (which
	// would surface as a confusing TURN_IN_PROGRESS on the next submit).
	private static readonly IN_FLIGHT_TIMEOUT_MS = 300_000;

	// `/delegate` client budget: the orchestrator enforces its own
	// DELEGATION_TIMEOUT_SECONDS = 120s per child run (core/orchestrator.py),
	// so the client clock must outlast that budget; a timeout equal to the
	// server's aborts the RPC while the child is still winding down.
	private static readonly DELEGATION_TIMEOUT_MS = 150_000;

	// `/harnesses` wind-down grace: the gateway kills a delegated run's whole
	// process tree at the deadline it was given, then drains and redacts the
	// output; the RPC settles a few seconds later at most. The client clock
	// adds this margin on top of that deadline so a late-but-successful run is
	// never discarded (finding H22).
	private static readonly HARNESS_RUN_GRACE_MS = 15_000;

	/**
	 * Dormant per-session state (turn binding + token totals) for the last N
	 * sessions visited. Older sessions are forgotten so a long-lived UI with
	 * many sessions cannot grow its maps without bound.
	 */
	private static readonly MAX_RETAINED_SESSIONS = 50;

	constructor(tui: TUI, client: GatewayClient, options: AppOptions = {}) {
		this.tui = tui;
		this.client = client;
		this.options = options;
		this.exited = new Promise((done) => {
			this.resolveExit = done;
		});

		// F-23: one shared reader for the environment (the legacy AH_ name is
		// the documented fallback; an explicit MACHINA_ value always wins).
		const reducedMotion = reducedMotionFromEnv(process.env);
		this.coordinator = new AnimationCoordinator(() => this.tui.requestRender(), reducedMotion);
		this.footer = new Footer(this.coordinator);
		this.transcript = new Transcript(this.transcriptView, new ThinkingIndicator(this.coordinator), this.coordinator);

		this.composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
		this.editor = this.composer.editor;
		// Caret blink: the composer's block obeys the shared phase, and every
		// draft edit re-derives whether a caret is actually on screen (an empty
		// composer shows only the placeholder, so it must not burn renders).
		this.composer.editor.cursorBlockVisible = () => this.cursorBlinkOn();
		this.composer.editor.onChange = () => this.syncCursorBlink();
        this.composer.editor.onPaste = (text) => this.acceptImageDrop(text);
        this.composer.editor.onSubmitBlocked = () => {
            if (!this.pendingImageLoads) return false;
            const epoch = this.imageEpoch;
            const failures = this.imageLoadFailures;
            void this.imageLoads.then(() => {
                if (epoch === this.imageEpoch && failures === this.imageLoadFailures && !this.exiting) {
                    void this.submit(this.editor.getExpandedText());
                }
            });
            return true;
        };
		this.shell = new AppShell(tui, this.transcriptView, this.composer, this.footer);
		this.nowPlayingBar = new NowPlayingBar(this.coordinator);
		this.shell.setPinnedBar(this.nowPlayingBar);
		this.editor.setAutocompleteProvider(new ArgumentAwareAutocompleteProvider(SLASH_COMMANDS, process.cwd()));
		this.editor.setAutocompleteMaxVisible(8);
		this.editor.onSubmit = (text) => void this.submit(text);
	}

	async start(): Promise<void> {
		// Clear vacated rows when content shrinks (closed menus, finished
		// turns). Without this, filled rows linger as ghost bands behind
		// newer output.
		this.tui.setClearOnShrink(true);
		this.workspaceFlagOn = !/^(0|false|off)$/i.test((process.env.MACHINA_WORKSPACE ?? "").trim());
		this.shell.sync();
		if (isViewportTUI(this.tui)) {
			this.tui.setLayoutRoot(this.shell.root);
		} else {
			// Fallback for non-viewport TUIs (and test doubles): classic stacking.
			this.tui.addChild(this.transcriptView);
			this.tui.addChild(this.composer);
			this.tui.addChild(this.footer);
		}
		this.tui.setFocus(this.editor);
		this.syncCursorBlink();
		this.inputListenerDisposer = this.tui.addInputListener((data) => this.onKey(data));

		this.footer.cwd = process.cwd();
		this.footer.status = "offline";
		this.transcript.addNotice("Starting Machina…");
		// Transparent background: apply the override before the first frame so
		// there is never a flash of the opaque canvas. The launcher exports the
		// saved value as MACHINA_TRANSPARENT_BACKGROUND; the persisted file value
		// (read below) restyles with a full repaint if it differs.
		const transparentEnv = /^(1|true|on|yes)$/i.test((process.env.MACHINA_TRANSPARENT_BACKGROUND ?? "").trim());
		setOpaqueBackground(!transparentEnv);
		this.tui.start();

		this.client.onEvent = (event) => this.onEvent(event);
		this.client.onExit = (code, stderr) => this.onGatewayExit(code, stderr);
		this.client.start();

		let initializedCwd: string | undefined;
		let initializedCwdFull: string | undefined;
		// Music detection can stall for seconds (including a blocking macOS
		// Automation prompt); it is joined at the end of start() but MUST NOT
		// delay session creation (finding 8).
		let nowPlayingReady: Promise<void> | undefined;
		try {
			const init = await this.client.request<InitializeResult>(
				"initialize",
				{ protocolVersion: 2, model: this.options.model, provider: this.options.provider, mode: this.options.mode },
				60_000,
			);
			initializedCwd = init.cwd;
			initializedCwdFull = typeof init.cwdFull === "string" && init.cwdFull ? init.cwdFull : undefined;
			this.version = init.version;
			Object.assign(this.footer, { cwd: init.cwd, branch: init.branch, model: init.model, status: "ready" });
			try {
				const { config } = await this.client.request<ConfigGetResult>("config.get", {}, 30_000);
				if (typeof config.theme === "string" && config.theme) {
					setSkin(config.theme);
					// The startup frame is already on screen. Use the same
					// cache invalidation/full redraw as live theme selection.
					this.restyle();
				}
				if (typeof config.transparent_background === "boolean") {
					const opaque = !config.transparent_background;
					if (opaque !== isOpaqueBackground()) {
						setOpaqueBackground(opaque);
						// Same full redraw as a skin change: every painted cell
						// (including empty viewport rows) must drop the fill.
						this.restyle();
					}
				}
				// Kick off the pinned now-playing bar here, but never await it
				// before the session exists (finding 8). start() joins the
				// promise before returning, so the bar still lands on the first
				// frame while session creation stays unblocked.
				nowPlayingReady = this.startNowPlaying(config).catch(() => {
					// The music bar is optional; detection failures never block chat.
				});
			} catch {
				// Saved skin is best-effort; the default applies otherwise.
			}
			try {
				// Guarantee the default profile exists (first-touch RL/growth/memory identity).
				await this.client.request("profile.get", { userId: this.defaultProfile }, 30_000);
			} catch {
				// Profiles stay best-effort; commands surface real errors.
			}
			if (this.options.sessionId) {
				const { session, history } = await this.client.request<ResumeResult>("session.resume", {
					sessionId: this.options.sessionId,
				});
				this.switchTo(session, history);
			} else {
				const agent = this.sessionAgent();
				const { session } = await this.client.request<SessionResult>(
					"session.create",
					agent ? { agent } : {},
				);
				this.switchTo(session, []);
			}
			try {
				// Session-scoped authority for the initial session (finding 8):
				// resolve AFTER the session exists so the footer and the
				// notice below reflect the governing session, never the
				// global default. Later switches refresh via switchTo.
				const { mode } = await this.request<{ mode: string }>(
					"mode.get",
					this.current ? { sessionId: this.current.id } : {},
				);
				if (mode === "full") {
					this.transcript.addNotice("FULL HOST mode active for this session: host operations proceed without further approval, including credential access, elevation, and destructive system ops.", "warning");
				}
			} catch {
				// Mode display is best-effort.
			}
		} catch (error) {
			this.footer.status = "offline";
			this.transcript.clear();
			this.transcript.addNotice(`Could not start: ${message(error)}`, "error");
			const tail = this.client.stderr().slice(-8);
			if (tail.length) this.transcript.addNotice(tail.join("\n"));
			this.transcript.addNotice("Run `machina doctor` to check your setup. Press Ctrl+C to exit.");
		}
		// Integrated workspace: only after initialize + session setup, only when
		// the flag allows it, and never blocking chat. Prefer the absolute
		// launch directory (authenticated handshakes). The redacted basename is
		// a display value and must never be treated as a root: when only that
		// is available (older backend), stay chat-only.
		if (this.workspaceFlagOn) {
			const root = initializedCwdFull ?? (initializedCwd !== undefined && isAbsolute(initializedCwd) ? initializedCwd : undefined);
			if (root !== undefined) this.startWorkspace(root);
		}
		// Join the music-detection chain now that the session exists: the bar
		// belongs to the first frame, but must never block session creation.
		await nowPlayingReady;
		this.tui.requestRender();
	}

	// ─── FeatureHost ─────────────────────────────────────────────────────────
	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		const result = await this.client.request<T>(method, params, App.requestTimeoutMs(method, params));
		if (["mode.get", "mode.set", "mode.revoke"].includes(method)) {
			const mode = (result as { mode?: string }).mode;
			if (typeof mode === "string") this.footer.mode = mode;
			this.tui.requestRender();
		}
		if (method === "session.setModel") this.adoptSessionRoute(result);
		return result;
	}

	/**
	 * Client clock per method. A client timeout at or below its backend
	 * deadline aborts the RPC while the other side is still working, so both
	 * budgets below outlast their server-side counterpart (finding H22).
	 * - `agents.run`: the orchestrator enforces DELEGATION_TIMEOUT_SECONDS.
	 * - `interop.run`: `/harnesses` passes the gateway's own deadline
	 *   (`interop.list.runTimeoutSeconds`) as `timeout`; wait past it by the
	 *   wind-down grace. The feature refuses to start a run whose deadline the
	 *   gateway did not report, so this branch always has the authoritative
	 *   number when a harness actually runs.
	 */
	private static requestTimeoutMs(method: string, params: Record<string, unknown>): number {
		if (method === "agents.run") return App.DELEGATION_TIMEOUT_MS;
		if (method === "interop.run") {
			const seconds = params.timeout;
			if (typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0) {
				return seconds * 1000 + App.HARNESS_RUN_GRACE_MS;
			}
		}
		return 120_000;
	}

	/**
	 * Adopt the gateway's authoritative route for the current session after a
	 * `session.setModel`. Sessions pin model/provider at creation, so readers
	 * of `session()` (the `/provider remove` re-pin guard, `/models` grouping)
	 * must compare the live pin — never a snapshot taken before the last
	 * switch. All `/provider`/`/models` call sites route through `request`,
	 * so mirroring here keeps the snapshot faithful without re-fetching.
	 */
	private adoptSessionRoute(result: unknown): void {
		const current = this.current;
		if (!current) return;
		const route = result as { sessionId?: unknown; model?: unknown; provider?: unknown };
		if (typeof route.sessionId === "string" && route.sessionId !== current.id) return;
		const model = typeof route.model === "string" && route.model ? route.model : undefined;
		const provider = typeof route.provider === "string" && route.provider ? route.provider : undefined;
		if (!model && !provider) return;
		this.updateSession({
			...current,
			...(model ? { model } : {}),
			...(provider ? { provider } : {}),
		});
	}

	print(text: string, kind: NoticeKind = "info"): void {
		this.transcript.addNotice(text, kind);
		this.tui.requestRender();
	}

    copyText(text: string): Promise<boolean> { return copyClipboardText(text); }

	/**
	 * FeatureHost: place *text* in the composer draft without submitting
	 * (the `/help` palette resolves commands that need arguments this way).
	 * Focus moves to the composer with the cursor after the inserted text.
	 */
	prefillInput(text: string): void {
		this.editor.setText(text);
		this.setFocusForRegion("chat-composer");
		this.tui.requestRender();
	}

	onGatewayEvent(handler: (event: GatewayEvent) => void): () => void {
		this.featureEventHandlers.add(handler);
		return () => {
			this.featureEventHandlers.delete(handler);
		};
	}

	/**
	 * FeatureHost: the integrated-workspace surface. Present only when the
	 * feature was constructed at start(); commands check controllerAvailable()
	 * so a METHOD_NOT_FOUND backend disables them with one notice.
	 */
	workspace(): WorkspaceHost | undefined {
		return this.workspaceHostSurface;
	}

	/**
	 * FeatureHost: the interactive skills directory surface for `/skills`
	 * (A5, spec §12.3). Present whenever the app runs; the picker talks to
	 * `skills.search` / `skills.show` / `skills.preview_route` and falls back
	 * to the one honest error state when a method is unavailable.
	 */
	skillsDirectory(): SkillsDirectoryHost | undefined {
		return { openSkillsPicker: (query) => this.openSkillsPicker(query ?? "") };
	}

	/**
	 * FeatureHost: the searchable `/help` palette surface. Present whenever
	 * the app runs; the palette filters local command/key data and routes
	 * chosen commands back through the same dispatcher as typed input.
	 */
	helpPalette(): HelpPaletteHost | undefined {
		return { openHelpPalette: (query) => this.openHelpPalette(query ?? "") };
	}

	// ─── global UI settings (closing pass) ──────────────────────────────────

	/**
	 * FeatureHost: the global UI settings surface for `/settings` (and bare
	 * `/config`). Present whenever the app runs; the snapshot reports which
	 * categories have a live target (workspace / editor view).
	 */
	uiSettings(): UiSettingsHost | undefined {
		return {
			snapshot: () => ({
				preferences: { ...this.uiPrefs },
				workspaceAvailable: this.workspaceHostSurface?.controllerAvailable() ?? false,
				editorAvailable: this.integration !== undefined,
			}),
			setPreference: (key, value) => this.setUiPreference(key, value),
			openWorkspaceFolder: (path) => this.switchWorkspaceRoot(path),
			closeWorkspace: async () => {
				const integration = this.integration;
				if (!integration) return;
				// MCH-012: the close supersedes any in-flight open/switch, and
				// its own shell publish must not land on a newer workspace.
				const gen = this.beginWorkspaceFence();
				await integration.controller.closeWorkspace();
				// C3: the close advanced the controller generation; rebind this
				// epoch before publishing the shell state.
				this.armWorkspaceFence(gen);
				if (!this.isWorkspaceCurrent(gen)) return;
				this.syncWorkspaceShell();
			},
			lspRows: () => this.lspSettingsRows(),
			refreshLsp: async () => {
				await this.integration?.controller.lspStatus();
			},
		};
	}

	// ─── music (pinned now-playing bar) ─────────────────────────────────────

	/**
	 * FeatureHost: playback surface for `/music`. Undefined until the startup
	 * config loads (or when the host never received one), so commands report
	 * one honest notice instead of failing.
	 */
	music(): MusicHost | undefined {
		return this.nowPlayingController;
	}

	/**
	 * Start the pinned now-playing bar: config gate, first reading, and the
	 * shared-coordinator subscriptions (poll always; 250 ms animation only
	 * while audio is actually playing).
	 */
	private async startNowPlaying(config: Record<string, unknown>): Promise<void> {
		this.nowPlayingController = new NowPlayingController({
			bar: this.nowPlayingBar,
			request: (method, params) => this.client.request(method, params ?? {}, 30_000),
			onChange: () => {
				this.syncNowPlayingTicks();
				this.tui.requestRender();
			},
		});
		await this.nowPlayingController.initialize(config);
	}

	/**
	 * Subscribe/detach the coordinator demand for the bar. Polling is a slow
	 * 2 s heartbeat; the equalizer and interpolated progress need 250 ms
	 * frames, but only while audio is playing.
	 */
	private syncNowPlayingTicks(): void {
		// Data polling is owned by the controller (its own 2 s interval,
		// stopped on exit/disable); the coordinator here only drives render
		// demand: 250 ms frames for the equalizer and interpolated clock
		// while audio is actually playing.
		const controller = this.nowPlayingController;
		const state = controller?.status();
		const playing =
			(controller?.isEnabled() ?? false) &&
			state?.state === "playing" &&
			Boolean(state.title);
		if (playing && !this.nowPlayingAnim) {
			this.nowPlayingAnim = this.coordinator.subscribe("nowplaying.anim", 250);
		} else if (!playing && this.nowPlayingAnim) {
			this.nowPlayingAnim.stop();
			this.nowPlayingAnim = undefined;
		}
	}

	// ─── caret blink (one coordinator subscription) ──────────────────────────

	/**
	 * True while a typing caret is actually on screen: the workspace editor
	 * (focused, with an open document) or the composer (focused, with a
	 * non-empty draft — an empty composer shows only the placeholder, so
	 * blinking it would burn renders with nothing to blink).
	 */
	private cursorCaretVisible(): boolean {
		if (this.exiting) return false;
		const integration = this.integration;
		if (integration?.view.focused && integration.tabs.count() > 0) return true;
		return this.editor.focused && this.editor.getText().length > 0;
	}

	/**
	 * The caret's current blink phase. When no blink subscription is live
	 * there is no caret to toggle, so "on" keeps an unexpected render static;
	 * under reduced motion the caret is pinned on (documented policy: focus
	 * must stay visible) and no timer exists at all.
	 */
	cursorBlinkOn(): boolean {
		if (!this.cursorBlinkSub || this.coordinator.isReducedMotion) return true;
		return this.coordinator.frame(["on", "off"], CURSOR_BLINK_INTERVAL_MS, this.cursorBlinkStartedAt) === "on";
	}

	/**
	 * Reconcile the single blink subscription with the focused caret. The
	 * stored handle guards subscribing, so repeated focus changes can never
	 * stack loops (the coordinator also replaces by key). Retired whenever no
	 * text surface owns the caret: explorer/problems focus, an overlay/dialog,
	 * an empty composer or an empty editor.
	 */
	private syncCursorBlink(): void {
		const needed = this.cursorCaretVisible() && !this.coordinator.isReducedMotion;
		if (needed && !this.cursorBlinkSub) {
			this.cursorBlinkStartedAt = Date.now();
			this.cursorBlinkSub = this.coordinator.subscribe("cursor.blink", CURSOR_BLINK_INTERVAL_MS);
		} else if (!needed && this.cursorBlinkSub) {
			this.cursorBlinkSub.stop();
			this.cursorBlinkSub = undefined;
		}
	}

	/** Apply one preference, persist it, and hand back the effective set. */
	private async setUiPreference(key: UiPreferenceKey, value: boolean | number): Promise<UiPreferences> {
		const next: UiPreferences = { ...this.uiPrefs };
		if (key === "syntax") next.syntax = value === true;
		else if (key === "softWrap") next.softWrap = value === true;
		else next.tabWidth = value === 2 || value === 4 || value === 8 ? value : 4;
		this.applyUiPreferences(next);
		// Persistence failure keeps the value applied for this session; the
		// caller (settings menu) reports that honestly and never blocks editing.
		await this.uiPreferencesStore().save(next);
		return { ...next };
	}

	/** Push preferences into the live editor view (no view: kept for later). */
	private applyUiPreferences(preferences: UiPreferences): void {
		this.uiPrefs = { ...preferences };
		const view = this.integration?.view;
		if (!view) return;
		view.setSyntaxEnabled(preferences.syntax);
		view.setSoftWrap(preferences.softWrap);
		view.setTabWidth(preferences.tabWidth);
	}

	/** Best-effort load of `ui_preferences` after initialize; never blocks. */
	private async loadUiPreferences(): Promise<void> {
		try {
			const preferences = await this.uiPreferencesStore().load();
			this.applyUiPreferences(preferences);
		} catch {
			// Defaults stay; the editor keeps working.
		}
	}

	private uiPreferencesStore(): UiPreferencesPersistence {
		return new UiPreferencesPersistence({
			get: async () => {
				const { config } = await this.client.request<ConfigGetResult>("config.get", {}, 30_000);
				const raw = config["ui_preferences"];
				return typeof raw === "string" && raw.length > 0 ? raw : undefined;
			},
			set: async (value) => {
				await this.client.request("config.set", { key: "ui_preferences", value, persist: true }, 30_000);
			},
		});
	}

	/** One settings row per language-server profile (server rows win). */
	private lspSettingsRows(): Array<{ profile: string; state: string; detail?: string }> {
		const controller = this.integration?.controller;
		if (!controller || controller.phase !== "ready") return [];
		const snapshot = controller.lspStatusSnapshot();
		const rows = new Map<string, { profile: string; state: string; detail?: string }>();
		for (const server of snapshot.servers) {
			const profile = server.profile || server.languageId;
			if (!profile) continue;
			rows.set(profile, {
				profile,
				state: server.state,
				...(server.detail !== undefined ? { detail: server.detail } : {}),
			});
		}
		for (const [profile, entry] of Object.entries(snapshot.profiles)) {
			if (rows.has(profile)) continue;
			rows.set(profile, {
				profile,
				state: entry.state,
				...(entry.detail !== undefined ? { detail: entry.detail } : {}),
			});
		}
		return [...rows.values()];
	}

	printMarkdown(text: string): void {
		this.transcript.addMarkdown(text);
		this.tui.requestRender();
	}

	session(): SessionInfo | undefined {
		return this.current;
	}

	private headerInfo(session: SessionInfo | undefined): Parameters<typeof header>[1] {
		if (!session) return undefined;
		return {
			model: session.model || this.footer.model,
			provider: session.provider || "",
			cwd: this.footer.cwd,
			branch: this.footer.branch,
		};
	}

	switchTo(session: SessionInfo, history: HistoryEntry[]): void {
		if (this.current?.id === session.id) {
			// Same-id re-resume of the visible session (`/resume <current>`,
			// picking the current row in a session list): the fetched history
			// cannot contain the in-flight turn, so clearing and replaying here
			// would erase live output and strand running-tool completions
			// (review round 4, R4-094). Refresh the metadata only.
			this.updateSession(session);
			this.tui.requestRender();
			return;
		}
		if (this.current) this.sessionTokensById.set(this.current.id, this.sessionTokens);
		// Consume this session's hidden-session tail (buffered events that
		// arrived while it was not visible). Merge rule (PINNED, tail.ts):
		// the tail is the conversation suffix and WINS over the fetched
		// history; matching trailing history entries are trimmed first so a
		// completed turn or settled tool renders exactly once. Consuming the
		// tail here is the documented "clear when history is fetched"
		// boundary: the buffer only ever holds work from after the last
		// switch, so it cannot duplicate earlier history.
		const tail = this.takeSessionTail(session.id)?.blocks ?? [];
		this.updateSession(session);
		this.rememberSession(session.id);
		this.transcript.clear();
		this.transcript.addComponent(header(this.version, this.headerInfo(session)));
		this.transcript.replay(trimSupersededHistory(history, tail));
		this.replayTail(session, tail);
		this.sessionTokens = this.sessionTokensById.get(session.id) ?? 0;
		// Preserve the active turn's latest cumulative usage across switches
		// without mixing turns: completed total plus this session's turn total.
		this.footer.tokens = this.sessionTokens + (this.turns.get(session.id)?.usageLatest ?? 0);
		// The backend stays paused for this session's approvals; rebuild the
		// review cards retained while it was hidden (R15).
		this.restoreApprovals(session.id);
		// F-30: approvals the hidden session never saw are parked only in the
		// permission store; re-read the target session's pending rows so the
		// turn cannot sit stranded behind a card that was never re-opened.
		void this.rehydrateApprovals(session.id);
		this.setRunning(this.inFlight.has(session.id));
		// Family = current.parentSessionId ?? current.id plus that root's
		// children; refresh after the local re-seed keeps the subagent count
		// honest.
		this.family.reseed();
		this.syncAgentCount();
		this.refreshFamily();
		// Finding 8: the footer must render the newly visible session's
		// authority, never the previous session's (or the global default).
		this.refreshSessionMode(session.id);
		this.tui.requestRender();
	}

	/**
	 * Re-query the gateway for a session's effective mode and mirror it
	 * into the footer (via request, which syncs footer.mode on mode.get).
	 * Best-effort: a failed refresh keeps the previous indicator rather
	 * than blanking it, and never rejects into the switch path.
	 */
	private refreshSessionMode(sessionId: string): void {
		void this.request<{ mode: string }>("mode.get", { sessionId }).catch(() => {});
	}

	updateSession(session: SessionInfo): void {
		if (session.id !== this.current?.id) {
            this.clearImages();
            this.editor.setText("");
			this.footer.dispose();
			// A pending request may reopen only when the user returns to its
			// owning session; never carry a selection into the new session.
			this.invalidateApprovals(session.id);
		}
		this.current = session;
		this.syncAgentCount();
		this.tui.requestRender();
	}

	/**
	 * Mark a session as recently visited and prune dormant state once the
	 * retained set exceeds MAX_RETAINED_SESSIONS. The visible session and any
	 * session with a turn still in flight are never evicted: their events
	 * still need to route.
	 */
	private rememberSession(sessionId: string): void {
		const index = this.sessionRecency.indexOf(sessionId);
		if (index !== -1) this.sessionRecency.splice(index, 1);
		this.sessionRecency.push(sessionId);
		while (this.sessionRecency.length > App.MAX_RETAINED_SESSIONS) {
			const candidate = this.sessionRecency.find(
				(id) => id !== this.current?.id && !this.inFlight.has(id),
			);
			if (candidate === undefined) break;
			this.sessionRecency.splice(this.sessionRecency.indexOf(candidate), 1);
			this.turns.delete(candidate);
			this.sessionTokensById.delete(candidate);
		}
	}

	// ─── agent family (root + delegated subagents) ──────────────────────────

	/** Family rows, main first, with run/current state for menus. */
	private familyRows(): FamilySession[] {
		return this.family.rows();
	}

	/** FeatureHost: the current family (main first) for /agents and menus. */
	familySessions(): FamilySession[] {
		return this.familyRows();
	}

	/**
	 * Footer segment: the number of subagents = family rows minus the
	 * main session (the root is not a subagent). Zero hides the segment.
	 */
	private syncAgentCount(): void {
		this.footer.subagents = Math.max(0, this.familyRows().length - 1);
	}

	/**
	 * Down on an empty composer: the same family picker as /agents sessions.
	 * Selecting switches through FamilyStore.open (the one-resume-at-a-time
	 * path shared with Ctrl+Shift+←/→); choosing the visible session is a
	 * no-op. Listing failures are reported quietly.
	 */
	private async openSubagentMenu(): Promise<void> {
		if (this.agentMenuOpening || this.dialogs.length) return;
		const current = this.current;
		if (!current) return;
		this.agentMenuOpening = true;
		try {
			const running = new Map(this.familyRows().map((row) => [row.session.id, row.running]));
			let family: SessionInfo[];
			try {
				family = await listFamilySessions(this, current);
			} catch (error) {
				this.transcript.addNotice(`Could not list agent sessions: ${message(error)}`, "error");
				this.tui.requestRender();
				return;
			}
			// The visible session may have changed while the list was fetched,
			// and a dialog (e.g. help) may have opened: in either case the
			// menu is no longer appropriate.
			if (this.exiting || this.current?.id !== current.id || this.dialogs.length) return;
			if (family.length <= 1) return;
			const choice = await this.pick("Switch to an agent session", agentSessionItems(family, current, running));
			if (!choice || choice.value === current.id) return;
			await this.family.open(choice.value);
		} finally {
			this.agentMenuOpening = false;
		}
	}

	/** Bounded, best-effort family refresh (see family.ts). */
	private refreshFamily(): void {
		void this.family.refresh();
	}

	/** Refresh kick for turn-start frames from ids not yet known family members. */
	private noteUnknownSession(sessionId: string): void {
		this.family.noteUnknown(sessionId);
	}

	private isFamilyMember(sessionId: string): boolean {
		return this.family.isMember(sessionId);
	}

	/** Ctrl+Shift+←/→: cycle the family; wraps around. */
	private cycleFamily(direction: 1 | -1): Promise<void> {
		return this.family.cycle(direction);
	}

	// ─── hidden-session live tails ──────────────────────────────────────────

	/** Consume a session's buffered tail (see tail.ts for the merge rule). */
	private takeSessionTail(sessionId: string): SessionTail | undefined {
		return this.sessionTails.take(sessionId);
	}

	/**
	 * Replay a consumed hidden-session tail in rendered order. Historical
	 * blocks re-enter through the resume path; ACTIVE blocks (streaming text,
	 * a running tool) re-enter through the live event path carrying their
	 * turn/tool identity, so the completion events that follow reconcile them
	 * — instead of a historical copy duplicating the text and a "running…"
	 * notice stranding a tool whose completion can no longer find its card
	 * (review round 3, F12).
	 */
	private replayTail(session: SessionInfo, tail: TailBlock[]): void {
		for (const block of tail) {
			if (block.kind === "assistant" && !block.completed) {
				this.transcript.apply({
					protocolVersion: 2,
					sessionId: session.id,
					turnId: block.turnId,
					type: "message.delta",
					text: block.text,
				});
			} else if (block.kind === "tool" && !block.settled) {
				this.transcript.apply({
					protocolVersion: 2,
					sessionId: session.id,
					turnId: block.turnId,
					type: "tool.start",
					id: block.id,
					name: block.name,
					args: {},
				});
			} else {
				this.transcript.replay(tailEntries([block]));
			}
		}
	}

	/** Drop tails that no longer route and cap the retained session count. */
	private pruneSessionTails(): void {
		this.sessionTails.prune();
	}

	/** Buffer one hidden-session event into its live tail (see tail.ts). */
	private bufferHiddenEvent(event: GatewayEvent): void {
		this.sessionTails.buffer(event);
	}

	pick(title: string, items: SelectItem[]): Promise<SelectItem | undefined> {
		// The overlay is full-screen, but the menu itself is bounded to the
		// documented default so a long list scrolls instead of covering the
		// page; Menu reserves its own chrome rows within the live viewport.
		// Every dynamic title/label is display-sanitized at this one sink:
		// callers hand over file names, session titles and server strings.
		return this.openDialog<SelectItem>((close) =>
			new Picker(
				sanitizeDisplay(title),
				sanitizeSelectItems(items),
				close,
				() => close(),
				DEFAULT_MENU_VISIBLE_ROWS,
				undefined,
				this.tui,
			),
		);
	}

	pickMany(title: string, items: SelectItem[]): Promise<SelectItem[] | undefined> {
		return this.openDialog<SelectItem[]>((close) => {
			const menu = new Menu(sanitizeDisplay(title), sanitizeSelectItems(items), {
				multiSelect: true,
				visibleRows: DEFAULT_MENU_VISIBLE_ROWS,
				tui: this.tui,
			});
			menu.onConfirm = close;
			menu.onCancel = () => close();
			return menu;
		});
	}

	/**
	 * Live theme preview: moving the selection recolors the whole visible
	 * UI without config writes; Esc restores the original theme; Enter
	 * commits the selected ID. Persistence stays with the feature module.
	 */
	pickTheme(): Promise<string | undefined> {
		const previous = getSkin();
		return this.openDialog<string>((close) => {
			const menu = new Menu(
				`Skins (current: ${previous})`,
				SKIN_NAMES.map((skin) => ({
					value: skin,
					label: `${skin}${skin === previous ? " (current)" : ""}`,
					description: skinDescription(skin),
				})),
				{ hint: "↑↓ preview · enter apply · esc restore", visibleRows: DEFAULT_MENU_VISIBLE_ROWS, tui: this.tui, initialValue: previous },
			);
			menu.onSelectionChange = (item) => {
				setSkin(item.value);
				this.restyle();
			};
			menu.onSelect = (item) => close(item.value);
			menu.onCancel = () => {
				setSkin(previous);
				this.restyle();
				close();
			};
			return menu;
		});
	}
	askText(options: AskTextOptions): Promise<string | undefined> {
		// Titles/descriptions/placeholders are chrome: sanitize once here so no
		// form implementation can render a hostile label (or bidi override).
		const safe = sanitizeAskTextOptions(options);
		if (safe.secret) {
			return this.openDialog<string>((close) => {
				const form = new SecretForm(this.tui, {
					title: safe.title,
					description: safe.description,
					placeholder: safe.placeholder ?? "••••••••",
					allowEmpty: safe.allowEmpty,
				});
				form.onSubmit = close;
				form.onCancel = () => close();
				return form;
			});
		}
		return this.openDialog<string>((close) => {
			const form = new TextForm(this.tui, editorTheme, safe);
			form.onSubmit = close;
			form.onCancel = () => close();
			return form;
		});
	}

	/**
	 * Cancellable transient overlay for OAuth browser authorization.
	 * Escape invokes options.onCancel and closes the overlay; status
	 * updates and the login link never touch the transcript.
	 */
	showOAuthProgress(options: OAuthProgressOptions): OAuthProgressHandle {
		let component: OAuthProgress | undefined;
		let closeDialog: ((value?: string) => void) | undefined;
		let settled = false;
		void this.openDialog<string>((close) => {
			closeDialog = close;
			component = new OAuthProgress(options.title, options.status, {
				onOpen: options.onOpen,
                onCopy: options.onCopy,
				onCancel: () => {
					if (settled) return;
					settled = true;
					options.onCancel();
					close("cancel");
				},
            }, options.loginUrl, this.tui);
			return component;
		}).then(() => {
			settled = true;
		});
		return {
			update: (status: string, loginUrl?: string) => {
				component?.setStatus(status, loginUrl);
				this.tui.requestRender();
			},
			dispose: () => {
				if (settled) return;
				settled = true;
				closeDialog?.();
			},
		};
	}

	/**
	 * Inspect locally retained tool output. Bare form picks from recent
	 * outputs of the current session; an explicit ID resolves only within
	 * the current session. Opening preserves the chat draft and transcript
	 * scroll position; closing returns to the previous dialog or composer.
	 */
	async inspectOutput(toolId?: string): Promise<void> {
		const session = this.current;
		if (!session) {
			this.transcript.addNotice("No active session.", "warning");
			return;
		}
		if (toolId) {
			try {
				await this.openViewer(this.outputStore.resolve(session.id, toolId));
			} catch (error) {
				this.transcript.addNotice(error instanceof Error ? error.message : String(error), "error");
			}
			return;
		}
		const recent = this.outputStore.list(session.id);
		if (recent.length === 0) {
			this.transcript.addNotice("No retained tool output for this session.", "info");
			return;
		}
		const choice = await this.pick(
			"Recent tool output",
			recent.map((record) => ({
				value: record.toolId,
				label: `${record.name} (${record.toolId.slice(0, 8)})`,
				description: `${record.byteLength.toLocaleString("en-US")} bytes${record.backendTruncated ? ", server truncated" : ""}${record.locallyTruncated ? ", locally capped" : ""}`,
			})),
		);
		if (!choice) return;
		const record = this.outputStore.get(session.id, choice.value);
		if (!record) {
			this.transcript.addNotice("That output is no longer retained.", "warning");
			return;
		}
		await this.openViewer(record);
	}

	private openViewer(record: OutputRecord): Promise<void> {
		return this.openDialog<void>((close) => new OutputViewer(record, { tui: this.tui, onClose: () => close() })).then(
			() => undefined,
		);
	}

	profileId(): string {
		return this.currentProfile;
	}

	setProfile(userId: string): void {
		this.currentProfile = userId;
		this.tui.requestRender();
	}

	sessionAgent(): string | undefined {
		// The default profile shares the harness agent (existing data intact);
		// other profiles scope RL, growth notes, and memories via harness-<slug>.
		if (this.currentProfile === this.defaultProfile) return undefined;
		if (!PROFILE_ID_RE.test(this.currentProfile)) return undefined;
		return `harness-${this.currentProfile}`;
	}

	private openDialog<T>(create: (close: (value?: T) => void) => Component): Promise<T | undefined> {
		if (this.exiting) return Promise.resolve(undefined);
		return new Promise((done) => {
			let settled = false;
			const close = (value?: T) => {
				if (settled) return;
				settled = true;
				// P0-23: every close path (choose, Esc-onClose, exit's
				// dialog.cancel()) funnels here before the dialog leaves the
				// DOM; menus and forms own timers/RPC continuations that must
				// not outlive their overlay. Dialogs without the hook are
				// inert callers.
				const liveStop = (component as { stopLiveWork?: () => void } | undefined)?.stopLiveWork;
				if (liveStop) liveStop.call(component);
				dialog.handle.hide();
				this.dialogs.splice(this.dialogs.indexOf(dialog), 1);
				const previous = this.dialogs.at(-1);
				if (this.approvalDialogs.size) this.showApprovalDock();
				else if (previous) { previous.handle.setHidden(false); previous.handle.focus(); }
				// The composer owns focus after the last dialog (finding 3):
				// route through the region router so activeRegion agrees, or the
				// editor-scoped gates would still think the editor owns keys.
				else if (!this.exiting) this.setFocusForRegion("chat-composer");
				// Recompute the recorded region against the component that now
				// owns focus; a dialog close must never leave editor-scoped gates
				// armed behind the composer.
				this.reconcileFocus();
				this.syncCursorBlink();
				this.tui.requestRender();
				done(value);
			};
			const component = create(close);
			// Each dialog owns its handle, including when approvals interrupt a form.
			// Full-screen sheet: 100% height with no bottom margin, so the overlay
			// is bounded only by the terminal and fills whatever the component
			// renders up to the viewport.
			const handle = this.tui.showOverlay(component, {
				width: "100%",
				maxHeight: "100%",
				anchor: "bottom-center",
			});
			const dialog = { handle, cancel: () => close() };
			this.dialogs.push(dialog);
			// Late management RPCs may finish after an approval arrives. Preserve
			// their draft invisibly until the bound decision releases focus.
			if (this.approvalDialogs.size) { handle.setHidden(true); this.showApprovalDock(); }
			this.syncCursorBlink();
			this.tui.requestRender();
		});
	}

	async writeFile(path: string, text: string): Promise<string> {
		const full = resolve(process.cwd(), path);
		await mkdir(dirname(full), { recursive: true });
		try {
			// "wx" refuses to clobber: /init and /export must never silently
			// truncate an existing file (runCommand reports the refusal).
			await writeFile(full, text, { encoding: "utf8", flag: "wx" });
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code === "EEXIST") {
				throw new Error(`Refusing to overwrite existing file ${full}. Delete it first or choose another path.`);
			}
			throw error;
		}
		return full;
	}

	onConfig(result: ConfigResult): void {
		this.footer.model = result.model;
		this.tui.requestRender();
	}

	clear(): void {
		this.transcript.clear();
		this.tui.requestRender();
	}

	banner(): void {
		this.transcript.clear();
		this.transcript.addComponent(header(this.version, this.headerInfo(this.current)));
		this.tui.requestRender();
	}

	restyle(): void {
		this.transcript.restyle();
		// A theme changes empty viewport rows too, which do not otherwise
		// differ in the renderer's retained screen cache.
		this.tui.requestRender(true);
	}

	async exit(): Promise<void> {
		if (this.exiting) return;
		// Plan §9 dirty-quit review: one dialog before any teardown. The review
		// runs while the app is still alive (dialogs stay usable); a cancel or
		// a failed save leaves everything untouched.
		if (this.dirtyExitInProgress) return;
		this.dirtyExitInProgress = true;
		try {
			if (!(await this.reviewDirtyExit())) return;
		} finally {
			this.dirtyExitInProgress = false;
		}
		if (this.exiting) return;
		this.exiting = true;
		// P0-20: the Quick Open overlay is not an openDialog dialog, so the
		// cancel loop below never sees it; tear it down explicitly so its
		// pending provider timer dies with the app.
		this.closeQuickOpen();
		// MCH-012: every in-flight workspace open/switch continuation is stale
		// the moment the app is leaving: it must publish nothing.
		this.beginWorkspaceFence();
		this.footer.dispose();
		for (const timer of this.inFlightTimers.values()) clearTimeout(timer);
		this.inFlightTimers.clear();
		this.transcript.suspend();
		this.invalidateApprovals();
		this.nowPlayingController?.stop();
		this.nowPlayingAnim?.stop();
		this.nowPlayingAnim = undefined;
		this.cursorBlinkSub?.stop();
		this.cursorBlinkSub = undefined;
		this.coordinator.dispose();
		for (const dialog of [...this.dialogs].reverse()) dialog.cancel();
		this.inputListenerDisposer?.();
		this.tui.stop();
		await this.integration?.controller.dispose();
		await this.client.stop();
		this.resolveExit();
	}

	/**
	 * Dirty-quit review (plan §9): lists up to two dirty basenames plus a
	 * `+N` remainder (finding 6: `1 unsaved file: main.py`) and offers
	 * Save all & exit / Discard & exit / Cancel. Save-all drives sequential
	 * controller saves; the first failure (conflict, refusal, transport) keeps
	 * the app open with a notice naming the file.
	 */
	private async reviewDirtyExit(): Promise<boolean> {
		const integration = this.integration;
		if (!integration || this.exiting) return true;
		const dirty = integration.store.documents().filter((document) => document.dirty);
		if (dirty.length === 0) return true;
		const names = dirty.slice(0, 2).map((document) => displayUriName(document.uri));
		const more = dirty.length > names.length ? ` +${dirty.length - names.length}` : "";
		const label = `${dirty.length} unsaved file${dirty.length === 1 ? "" : "s"}: ${names.join(", ")}${more}`;
		const choice = await this.pick(label, [
			{ value: "save", label: "Save all & exit" },
			{ value: "discard", label: "Discard & exit" },
			{ value: "cancel", label: "Cancel" },
		]);
		if (!choice || choice.value === "cancel") return false;
		if (choice.value === "discard") return true;
		// MCH-012: a workspace lifecycle change during the review retires the
		// exit request (the buffers belong to a workspace that moved on).
		const gen = this.captureWorkspaceFence();
		if (!this.isWorkspaceCurrent(gen)) return false;
		for (const document of dirty) {
			const outcome = await integration.controller.save(document.id);
			if (!this.isWorkspaceCurrent(gen)) return false;
			if (outcome.status === "conflict") {
				// "Save all & exit" survives the conflict detour: an explicit
				// overwrite/reload leaves the file saved (or adopted), so the
				// requested exit is still the user's decision. "Keep editing"
				// (or a failed/stale resolution) cancels it honestly.
				const resolution = await this.resolveSaveConflict(outcome.conflict);
				if (!this.isWorkspaceCurrent(gen)) return false;
				if (resolution?.status === "saved" || resolution?.status === "reloaded") continue;
				this.print(`Exit cancelled: ${displayUriName(document.uri)} was not saved.`, "warning");
				return false;
			}
			if (outcome.status !== "saved") {
				this.print(`Exit cancelled: could not save ${displayUriName(document.uri)}.`, "error");
				return false;
			}
		}
		return true;
	}

	// ─── input ────────────────────────────────────────────────────────────
    private clearImages(): void {
        this.imageEpoch++;
        this.imageDraft = new ImageDraft();
        this.lastPromptImages = [];
        this.lastPromptSessionId = undefined;
    }

    private acceptImageDrop(text: string): boolean {
        const paths = imagePaths(text);
        if (!paths) return false;
        this.loadImages(() => {
            if (paths.length > 5) throw new Error("Attach at most 5 images per message.");
            return Promise.all(paths.map(readImageFile));
        });
        return true;
    }

    private loadImages(load: () => Promise<ImageAttachment[]>): void {
        const epoch = this.imageEpoch;
        this.pendingImageLoads++;
        this.imageLoads = this.imageLoads.then(async () => {
            if (epoch !== this.imageEpoch || this.exiting) return;
            const images = await load();
            if (epoch !== this.imageEpoch || this.exiting || !images.length) return;
            this.imageDraft.prune(this.editor.getExpandedText());
            const markers = this.imageDraft.addMany(images);
            this.editor.insertTextAtCursor(markers.join(" ") + " ");
        }).catch((error) => {
            this.imageLoadFailures++;
            if (epoch === this.imageEpoch && !this.exiting) this.print(message(error), "error");
        }).finally(() => {
            this.pendingImageLoads--;
            this.tui.requestRender();
        });
    }

	/**
	 * `?` / F1 shortcut picker rows, from the single source shared with
	 * `/help` (design/shortcuts.ts). Workspace rows only make sense while the
	 * feature is live, but they stay listed so the shortcuts remain
	 * discoverable; unavailable ones no-op with one notice.
	 */
	private shortcutItems(): SelectItem[] {
		return SHORTCUT_ROWS.map((row) => ({
			value: row.value,
			label: row.label,
			...(row.description === undefined ? {} : { description: row.description }),
		}));
	}

	private onKey(data: string): { consume: boolean } | undefined {
		this.shell.sync();
		this.syncWorkspaceShellIfChanged();
		// A key may only reach a component the current geometry still composes.
		// Layout changes normally reconcile already; this closes the window where
		// the TUI still targets a pane removed by a resize/visibility toggle.
		this.reconcileFocus();
		if (this.dialogs.length || this.approvalDialogs.size) return undefined; // focused dialogs own their keys

		// ── integrated workspace (plan §8 focus priority) ────────────────────
		// The Quick Open overlay owns every key except Esc/Ctrl+C (which would
		// otherwise reach the stop/exit branches below): returning early lets
		// the focused overlay component receive everything else.
		if (this.quickOpenPane) {
			if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
				this.quickOpenPane.dismiss();
				return { consume: true };
			}
			return undefined;
		}
		const integration = this.integration;
		if (integration && !this.workspaceUnsupported) {
			// An editor intelligence popup (completion) owns Enter/Tab/Esc and
			// arrows while it is open. This runs BEFORE the running-turn Esc
			// branch: Esc dismisses the top popup and must not cancel a chat turn.
			// Region-gated (finding 4b): a popup whose region lost focus must
			// never consume keys from whoever owns them now.
			if (this.activeRegion === "editor" && integration.pane.handleIntelligenceKey(data) === "consumed") {
				this.tui.requestRender();
				return { consume: true };
			}
			// Completion type-to-filter (finding 6): this key falls through to
			// the editor, which may edit the buffer under an open list. One
			// deferred pass re-syncs the session; without an edit (motion keys,
			// consumed workspace keys) it no-ops on the version check.
			if (this.completionSession && this.activeRegion === "editor") this.scheduleCompletionRefilter();
			// The Problems pane owns Esc while focused: it closes the section
			// instead of falling into the double-Esc prompt refill or stopping
			// the running turn.
			if (this.problemsOpen && this.activeRegion === "problems" && matchesKey(data, Key.escape)) {
				this.closeProblems();
				return { consume: true };
			}
			const workspaceKey = this.handleWorkspaceKey(data);
			if (workspaceKey) return workspaceKey;
		}

        // Region-aware Ctrl+V: with the editor focused, the key belongs to the
        // editor view's own clipboard path (the TUI focus is the EditorPane →
        // EditorView.handleInput, which pastes via the injected clipboard).
        // Returning undefined lets the focused component see it; the composer
        // (and forwarded ⌘V) keep the image-aware global handler below.
        if (matchesKey(data, Key.ctrl("v")) && this.activeRegion === "editor") return undefined;
        if (matchesKey(data, Key.ctrl("v")) || matchesKey(data, Key.super("v"))) {
            const epoch = this.imageEpoch;
            this.loadImages(async () => {
                const image = await clipboardImage();
                if (image) return [image];
                const text = await clipboardText();
                if (epoch !== this.imageEpoch || this.exiting) return [];
                const paths = imagePaths(text);
                if (paths) return Promise.all(paths.map(readImageFile));
                if (text) this.editor.insertTextAtCursor(text);
                else this.print("No image or text found in the clipboard. Drop an image file to attach it.", "info");
                return [];
            });
            return { consume: true };
        }

		const questionKey = matchesKey(data, Key.question) || matchesKey(data, Key.shift("?")) || matchesKey(data, Key.shift("/"));
		if (matchesKey(data, Key.f1) || (questionKey && !this.editor.getText() && !this.editor.isShowingAutocomplete())) {
			// Selecting the `/help — all commands` row dispatches the real
			// command, so with the palette surface it opens the searchable
			// overlay; other rows are documentation and just close.
			void this.openDialog((close) => new Picker("Keyboard shortcuts", this.shortcutItems(), (item) => {
				close();
				if (item.value === "commands") {
					const command = parseCommand("/help");
					if (command) void runCommand(command, this);
				}
			}, () => close(), SHORTCUTS_VISIBLE_ROWS, "↑↓ browse · enter/esc close", this.tui));
			return { consume: true };
		}

		if (matchesKey(data, Key.ctrl("c"))) {
			if (this.running) this.cancel();
            else if (this.editor.getText() || this.pendingImageLoads) { this.editor.setText(""); this.clearImages(); }
			else void this.exit();
			this.tui.requestRender();
			return { consume: true };
		}
		if (matchesKey(data, Key.ctrl("d")) && !this.running && !this.editor.getText()) {
			void this.exit();
			return { consume: true };
		}
		if (matchesKey(data, Key.escape) && this.running && !this.editor.isShowingAutocomplete()) {
			this.cancel();
			return { consume: true };
		}
		if (matchesKey(data, Key.ctrl("l")) && !this.running) {
			this.clear();
			return { consume: true };
		}
		// Agent family: Ctrl+Shift+←/→ cycles the current family (main session
		// plus delegated subagents). Chosen because nothing else in the app
		// binds them — the editor uses plain/alt/ctrl arrows for movement and
		// ctrl+pageUp/PageDown for paging; the alt screen uses ctrl+up/down.
		// Consumed only while a multi-member family exists.
		const cycle = matchesKey(data, Key.ctrlShift("right")) ? 1 : matchesKey(data, Key.ctrlShift("left")) ? -1 : 0;
		if (cycle !== 0 && this.familyRows().length > 1) {
			void this.cycleFamily(cycle as 1 | -1);
			return { consume: true };
		}
		// Down on an empty composer opens the subagent menu (the same picker
		// as /agents sessions). With text, with a completion popup, or with
		// no subagents it falls through untouched to the editor's own
		// Down handling (movement and history). Dialogs and approvals were
		// already given the key above.
		if (
			matchesKey(data, Key.down) &&
			!this.editor.getText() &&
			!this.editor.isShowingAutocomplete() &&
			this.familyRows().length > 1
		) {
			void this.openSubagentMenu();
			return { consume: true };
		}
		if (
			matchesKey(data, Key.escape) &&
			!this.running &&
			!this.editor.getText() &&
			!this.editor.isShowingAutocomplete()
		) {
			// Codex double-Esc: refill the previous prompt for editing.
			const now = Date.now();
			if (now - this.lastEscAt < 800 && this.lastPrompt) {
				this.editor.setText(this.lastPrompt);
				this.lastEscAt = 0;
			} else {
				this.lastEscAt = now;
			}
			return { consume: true };
		}
		return undefined;
	}

	// ─── integrated workspace (T3/T6/T12) ───────────────────────────────────

	/**
	 * Construct the integration and open the workspace in the background.
	 * Called once from start() after initialize + session setup. Nothing here
	 * touches the shell until the open succeeds, so a failure leaves chat-only
	 * behavior byte-identical.
	 */
	private startWorkspace(root: string): void {
		const request: WorkspaceRequestFn = <T>(method: string, params: Record<string, unknown>) => {
			if (this.workspaceUnsupported) {
				return Promise.reject(new Error("workspace is unavailable on this backend"));
			}
			return this.client.request<T>(method, params, 120_000).then((result) => {
				// Fail closed on a malformed open identity (a backend that does
				// not really serve workspace.* answers `{}`): binding a bogus
				// workspace would half-open the shell. One quiet disable notice.
				if (method === "workspace.open" && !isWorkspaceIdentity(result)) {
					this.noteWorkspaceUnsupported();
					throw new Error("workspace.open returned an invalid identity");
				}
				return result;
			}).catch((error: unknown) => {
				// A backend without workspace.* fails every call closed with
				// METHOD_NOT_FOUND: disable the feature once, quietly, and let
				// the error surface normally for this call.
				if (error instanceof RpcError && error.code === -32601) this.noteWorkspaceUnsupported();
				throw error;
			});
		};
		const gateway = new WorkspaceGatewayAdapter(request);
		const store = new EditorStore();
		const tabs = new TabModel({ isDirty: (documentId) => store.getDocument(documentId)?.dirty ?? false });
		const view = new EditorView({
			store,
			view: { documentId: "", cursorOffset: 0, scrollLine: 0, horizontalCellOffset: 0 },
			onChange: (documentId) => {
				this.integration?.controller.noteEdit(documentId);
				this.tui.requestRender();
			},
			requestRender: () => this.tui.requestRender(),
			// Editor caret shares the app's single blink phase (coordinator).
			isCursorBlockVisible: () => this.cursorBlinkOn(),
			// The view's clipboard seam: copy through the real system clipboard
			// (clipboard.ts), paste through the text reader (images.ts, "" when
			// empty or unavailable → null so the view never inserts "").
			clipboard: {
				copy: (text) => copyClipboardText(text),
				paste: async () => {
					try {
						const text = await clipboardText(process.platform);
						return text.length > 0 ? text : null;
					} catch {
						return null;
					}
				},
			},
		});
		view.onFocusRequested = () => this.noteRegionFocus("editor");
		const pane = new EditorPane({
			store,
			tabModel: tabs,
			view,
			requestRender: () => this.tui.requestRender(),
			onSelectTab: () => {
				// Tab focus refreshes the Problems projection for the new active file.
				void this.refreshProblemsForActiveDocument();
				this.refreshLspStatus();
				this.tui.requestRender();
			},
			onCloseTab: (documentId) => void this.closeWorkspaceTab(documentId),
			// Fallback when the pane owns the key directly; the app's Ctrl+S
			// handler runs first while the editor region is active.
			onSave: () => void this.saveActiveDocument(),
		});
		const controller = new WorkspaceController({
			gateway,
			persistence: this.workspacePersistence(),
			store,
			tabs,
			requestRender: () => this.tui.requestRender(),
			notify: (kind, text) => {
				// One quiet disable notice: the controller's own error for the
				// METHOD_NOT_FOUND open is suppressed (the info notice covers it),
				// and after that all controller errors stem from the same cause.
				if (this.workspaceUnsupported && kind === "error") return;
				this.transcript.addNotice(text, kind);
				this.tui.requestRender();
			},
			focus: (region) => this.setFocusForRegion(region),
		});
		const explorerModel = new ExplorerModel((path, cursor, listOptions) =>
			gateway.list(path, cursor, listOptions),
		);
		const explorerPane = new ExplorerPane(explorerModel, { onChange: () => this.tui.requestRender() });
		explorerPane.onFocusRequested = () => this.noteRegionFocus("explorer");
		explorerPane.onOpenFile = (entry) => {
			if (entry.kind === "file") void this.openWorkspacePath(entry.path, { preview: false });
		};
		explorerPane.onPreviewFile = (entry) => {
			if (entry.kind === "file") void this.openWorkspacePath(entry.path, { preview: true });
		};
		const problemsModel = new ProblemsModel({ request: this.intelligenceRequest() });
		const problemsPane = new ProblemsPane({
			model: problemsModel,
			requestRender: () => this.tui.requestRender(),
			onOpen: (row) => void this.openProblem(row),
			onClose: () => this.closeProblems(),
		});
		problemsPane.onFocusRequested = () => this.noteRegionFocus("problems");

		this.integration = {
			gateway,
			controller,
			store,
			tabs,
			view,
			pane,
			explorerModel,
			explorerPane,
			problemsModel,
			problemsPane,
		};
		const workspaceHost: WorkspaceHost & WorkspaceAttachmentHost = {
			controller,
			controllerAvailable: () =>
				this.workspaceFlagOn && !this.workspaceUnsupported && this.integration !== undefined,
			setMode: (mode) => {
				controller.setMode(mode);
				this.syncWorkspaceShell();
			},
			toggleChatMaximize: () => {
				controller.toggleChatMaximize();
				this.syncWorkspaceShell();
			},
			toggleEditorMaximize: () => {
				controller.toggleEditorMaximize();
				this.syncWorkspaceShell();
			},
			restoreSplit: () => {
				controller.restoreSplit();
				this.syncWorkspaceShell();
			},
			setExplorer: (open) => this.setExplorerRequested(open),
			closeActiveTab: () => this.closeActiveWorkspaceTab(),
			setChatSide: (side) => {
				controller.setChatSide(side);
				this.syncWorkspaceShell();
			},
			layoutChanged: () => this.syncWorkspaceShell(),
			openProblems: () => this.openProblems(),
			openQuickOpen: () => void this.openQuickOpen(),
			completeAtCursor: () => void this.startCompletion(),
			openRoot: (path) => this.switchWorkspaceRoot(path),
			// T10 explicit sharing: capture the editor selection into the
			// composer draft (plan §20). Never called by open/view paths.
			attachSelectionToDraft: () => this.attachSelectionToDraft(),
			dirtyBuffers: () => {
				const dirty = store.documents().filter((document) => document.dirty);
				return {
					count: dirty.length,
					names: dirty.slice(0, 8).map((document) => displayUriName(document.uri)),
				};
			},
		};
		this.workspaceHostSurface = workspaceHost;
		this.shell.setWorkspaceEditor(pane);
		this.shell.setWorkspaceExplorer(explorerPane);
		this.shell.onWorkspaceRatioChange = (ratio) => {
			controller.setRatio(ratio);
			this.syncWorkspaceShell();
		};
		// Far-right grip: flips the same explorer request the Ctrl+T / /explorer
		// path uses, so the listing load and persistence stay single-sourced.
		this.shell.onWorkspaceExplorerToggle = () => {
			const explorer = this.integration?.controller.getLayoutInput().explorerRequested ?? false;
			this.setExplorerRequested(!explorer);
		};
		this.client.onWorkspaceEvent = (event) => this.onWorkspaceEvent(event);
		// Global editor preferences (ui_preferences): best-effort, non-blocking.
		void this.loadUiPreferences();
		void this.openWorkspaceBackground(root);
	}

	/**
	 * Contract C3 / MCH-012: enter a new app-side lifecycle epoch. Call this
	 * BEFORE the first await of any open/switch/close/exit entry point so
	 * parked continuations of the previous epoch drop. The controller
	 * generation is re-bound after the entry's own controller call returns
	 * (`armWorkspaceFence`), because that call legitimately advances it.
	 */
	private beginWorkspaceFence(): number {
		this.workspaceFenceController = undefined;
		return ++this.workspaceGeneration;
	}

	/**
	 * Capture the live fence before an await. When a controller is mounted its
	 * generation is the authoritative half; during early startup/tests without
	 * a controller the app epoch is the only source.
	 */
	private captureWorkspaceFence(): number {
		this.workspaceFenceController = this.integration?.controller.generation;
		return this.workspaceGeneration;
	}

	/**
	 * Bind `gen` to the controller generation that exists now. Guarded so a
	 * latched continuation can never rebind (or adopt) a retired/newer epoch.
	 */
	private armWorkspaceFence(gen: number): void {
		if (gen !== this.workspaceGeneration || this.exiting) return;
		this.workspaceFenceController = this.integration?.controller.generation;
	}

	/**
	 * Contract C3 fence: true only while the captured epoch is still the live
	 * one AND (once a controller was captured) that controller still reports
	 * the captured generation current. A close/switch/dispose in EITHER source
	 * therefore denies the continuation, which must publish nothing.
	 */
	private isWorkspaceCurrent(gen: number): boolean {
		if (this.exiting || gen !== this.workspaceGeneration) return false;
		const integration = this.integration;
		if (this.workspaceFenceController === undefined) return true;
		return integration !== undefined && integration.controller.isCurrent(this.workspaceFenceController);
	}

	/**
	 * Background open. Failures notify once via the controller and leave the
	 * shell chat-only. On success the restored layout (or the default split)
	 * reaches the shell, remembered files reopen only when a stored state
	 * existed, cursors are re-applied, and the LSP/Problems feeds start.
	 */
	private async openWorkspaceBackground(root: string): Promise<void> {
		const integration = this.integration;
		if (!integration) return;
		// MCH-012: bump BEFORE the first await so a superseding entry point
		// (switch/close/exit) parked mid-flight drops this whole continuation.
		const gen = this.beginWorkspaceFence();
		// Capture the stored document BEFORE openWorkspace persists its own
		// intent over it (see workspacePersistence).
		// MCH-012: the snapshot itself is published state — a read that lands
		// after a superseding entry must not overwrite the live path's capture.
		let raw: string | undefined;
		try {
			raw = await this.readWorkspaceStateRaw();
		} catch {
			raw = undefined;
		}
		if (!this.isWorkspaceCurrent(gen)) return;
		this.workspacePreOpenRaw = raw;
		this.workspacePreOpenCaptured = false;
		const identity = await integration.controller.openWorkspace(root);
		if (!identity) return;
		// C3: this open legitimately advanced the controller generation; bind
		// the epoch to what it produced before the combined (both-source) check.
		this.armWorkspaceFence(gen);
		if (!this.isWorkspaceCurrent(gen)) return;
		this.workspaceLayoutActive = true;
		// Preferences are view-global; re-assert them once the editor exists so
		// a prefs load racing the background open can never be lost.
		this.applyUiPreferences(this.uiPrefs);
		const restored = await integration.controller.restore(identity.resolvedRoot);
		// MCH-012: restore can be slow; applying its layout/state after a
		// supersede would publish a stale root into the live shell.
		if (!this.isWorkspaceCurrent(gen)) return;
		// The controller's default intent is already the split; only a stored
		// state overrides it (and an explicit command issued during the open
		// window is never clobbered).
		if (restored) integration.controller.applyRestoredLayout(restored);
		this.syncWorkspaceShell();
		if (restored) {
			await integration.controller.reopenRecent(restored);
			if (!this.isWorkspaceCurrent(gen)) return;
			this.restoreCursorState(restored);
			const active = integration.tabs.activeDocumentId();
			if (active) integration.view.setDocument(active);
		}
		if (integration.controller.getLayoutInput().explorerRequested) {
			void integration.explorerModel.load();
		}
		integration.controller.setLspStatusListener(() => {
			integration.pane.setLspStatus(this.lspStatusText());
		});
		void integration.controller.lspStatus().then(() => {
			// MCH-012: a late status is only applied while this open is still
			// the live one — a superseded root never writes the pane.
			if (this.integration === integration && this.isWorkspaceCurrent(gen)) {
				integration.pane.setLspStatus(this.lspStatusText());
			}
		});
		void this.refreshProblemsForActiveDocument();
		this.syncWorkspaceShell();
	}

	/**
	 * Explicit user command to reopen the workspace at another folder
	 * (`/workspace open <path>`). Clean documents from the previous root close
	 * because the new workspace cannot save them (their URIs fall outside its
	 * root); dirty buffers always stay, with the count reported. The new
	 * root's persisted layout/files restore when a stored state exists.
	 */
	private async switchWorkspaceRoot(path: string): Promise<{ ok: boolean; message: string }> {
		const integration = this.integration;
		if (!integration) return { ok: false, message: "Workspace is not available." };
		const trimmed = path.trim();
		if (!trimmed) return { ok: false, message: "Usage: /workspace open <path>" };
		// MCH-012: this switch is now the live lifecycle entry — bump BEFORE the
		// first await so any in-flight open/switch continuation drops.
		const gen = this.beginWorkspaceFence();
		// Capture the stored document BEFORE open persists its intent (same
		// one-shot snapshot rule as the boot path).
		let raw: string | undefined;
		try {
			raw = await this.readWorkspaceStateRaw();
		} catch {
			raw = undefined;
		}
		if (!this.isWorkspaceCurrent(gen)) return { ok: false, message: "workspace switched" };
		this.workspacePreOpenRaw = raw;
		this.workspacePreOpenCaptured = false;
		const identity = await integration.controller.openWorkspace(trimmed);
		if (!identity) {
			return { ok: false, message: "Could not open that folder (see the notice above)." };
		}
		// C3: bind the epoch to the generation this open produced, then check
		// both sources (a concurrent close/switch/dispose still denies here).
		this.armWorkspaceFence(gen);
		if (!this.isWorkspaceCurrent(gen)) {
			return { ok: true, message: "workspace switched" };
		}
		this.workspaceLayoutActive = true;
		let closed = 0;
		let kept = 0;
		for (const document of integration.store.documents()) {
			if (document.dirty) {
				kept += 1;
				continue;
			}
			const outcome = await integration.controller.closeTab(document.id);
			if (!this.isWorkspaceCurrent(gen)) return { ok: true, message: "workspace switched" };
			if (outcome.status === "closed") closed += 1;
			else kept += 1;
		}
		const restored = await integration.controller.restore(identity.resolvedRoot);
		// MCH-012: never apply a restored layout to a root that has since lost
		// the lifecycle race.
		if (!this.isWorkspaceCurrent(gen)) return { ok: true, message: "workspace switched" };
		if (restored) integration.controller.applyRestoredLayout(restored);
		this.syncWorkspaceShell();
		if (restored) {
			await integration.controller.reopenRecent(restored);
			if (!this.isWorkspaceCurrent(gen)) return { ok: true, message: "workspace switched" };
			this.restoreCursorState(restored);
			const active = integration.tabs.activeDocumentId();
			if (active) integration.view.setDocument(active);
		}
		await integration.explorerModel.reload("");
		// MCH-012: the reload preceded the final shell publish; drop it when
		// another lifecycle entry took over while the read was parked.
		if (!this.isWorkspaceCurrent(gen)) return { ok: true, message: "workspace switched" };
		if (integration.controller.getLayoutInput().explorerRequested) void integration.explorerModel.load();
		void integration.controller.lspStatus();
		void this.refreshProblemsForActiveDocument();
		this.syncWorkspaceShell();
		const extras: string[] = [];
		if (closed > 0) extras.push(`closed ${closed} clean tab(s)`);
		if (kept > 0) extras.push(`kept ${kept} unsaved tab(s)`);
		const suffix = extras.length > 0 ? ` · ${extras.join(" · ")}` : "";
		return { ok: true, message: `workspace: ${identity.rootLabel} (${identity.resolvedRoot})${suffix}` };
	}

	/**
	 * Persistence bridge over `config.get`/`config.set("workspace_ui_state")`
	 * (plan §21). A one-shot snapshot captured before open serves the FIRST
	 * load: the controller persists its default intent during openWorkspace,
	 * which would otherwise overwrite the stored entry before `restore` reads
	 * it (verified by a first-run probe). The snapshot is parsed through
	 * WorkspacePersistence, so sanitization stays single-sourced.
	 */
	private workspacePersistence(): WorkspacePersistenceLike {
		const live = new WorkspacePersistence({
			get: () => this.readWorkspaceStateRaw(),
			set: async (value) => {
				await this.client.request("config.set", { key: "workspace_ui_state", value, persist: true }, 30_000);
			},
		});
		return {
			load: async (rootKey) => {
				if (!this.workspacePreOpenCaptured) {
					this.workspacePreOpenCaptured = true;
					const raw = this.workspacePreOpenRaw;
					if (raw !== undefined) {
						const snapshot = new WorkspacePersistence({ get: async () => raw, set: async () => {} });
						const parsed = await snapshot.load(rootKey);
						if (parsed) return parsed.state;
					}
				}
				return (await live.load(rootKey))?.state;
			},
			save: (rootKey, state) => live.save(rootKey, state),
		};
	}

	/** Read the stored workspace document ("" and failures mean "nothing"). */
	private async readWorkspaceStateRaw(): Promise<string | undefined> {
		const { config } = await this.client.request<ConfigGetResult>("config.get", {}, 30_000);
		const raw = config["workspace_ui_state"];
		return typeof raw === "string" && raw.length > 0 ? raw : undefined;
	}

	/** Apply remembered cursor/scroll positions after a successful reopen. */
	private restoreCursorState(state: WorkspacePersistedState): void {
		const integration = this.integration;
		if (!integration || !state.cursorByUri) return;
		for (const [uri, cursor] of Object.entries(state.cursorByUri)) {
			const document = integration.store.documents().find((candidate) => candidate.uri === uri);
			if (!document) continue;
			const offset = Math.max(0, Math.min(cursor.offset, document.text.length));
			const anchor =
				cursor.anchorOffset === undefined
					? undefined
					: Math.max(0, Math.min(cursor.anchorOffset, document.text.length));
			integration.store.setSelection(document.id, {
				cursorOffset: offset,
				...(anchor !== undefined ? { anchorOffset: anchor } : {}),
			});
			if (document.id === integration.tabs.activeDocumentId()) {
				integration.view.setView({
					documentId: document.id,
					cursorOffset: offset,
					...(anchor !== undefined ? { selectionAnchorOffset: anchor } : {}),
					scrollLine: cursor.scrollLine ?? 0,
					horizontalCellOffset: 0,
				});
			}
		}
	}

	/**
	 * Push current layout intent to the shell and size the editor pane. The
	 * split geometry itself always comes from computeLayout (single source);
	 * only the shell's dock/composer/footer chrome (+ density gaps) is
	 * subtracted before handing rows to the pane, so over-allocation can never
	 * clip its status row.
	 */
	private syncWorkspaceShell(): void {
		const integration = this.integration;
		if (!integration || !this.workspaceLayoutActive) {
			// No pane is composed: keys must belong to the composer, never to a
			// component retained from a previous workspace/layout.
			this.reconcileFocus();
			return;
		}
		const input = integration.controller.getLayoutInput();
		const columns = Math.max(0, this.tui.terminal.columns);
		const rows = Math.max(0, this.tui.terminal.rows);
		this.lastWorkspaceShellKey = workspaceShellKey(input, columns, rows);
		this.shell.setWorkspaceState(input);
		// Breadcrumb root (finding 2): pushed here so open/switch/close all
		// converge on the same single call site.
		const root = integration.controller.identity?.resolvedRoot;
		integration.pane.setWorkspaceRoot(root);
		integration.problemsPane.setWorkspaceRoot(root);
		// Footer workspace hints stay only while a workspace identity exists.
		this.footer.workspaceHints = integration.controller.identity ? WORKSPACE_FOOTER_HINTS : "";
		const layout = computeLayout({ ...input, columns, rows });
		this.workspaceLayout = layout;
		// Composer + optional approval dock + footer are full-width rows under
		// the panes row; chromeRows sums their heights and gaps from the same
		// budgets the root VStack uses.
		const chrome = this.shell.chromeRows();
		if (layout.editor) integration.pane.layout(Math.max(1, layout.editor.rows - chrome));
		if (layout.explorer) {
			// The docked tree owns its own windowing inside a bounded panel:
			// the drawer gets the rows the panes area can spare, capped so a
			// long tree never paints a full-page surface rectangle. Keep the
			// selection visible inside that budget.
			integration.explorerPane.setRowBudget(explorerTreeRowBudget(layout.explorer.rows - chrome - 2));
		}
		// Focus reconciliation (finding 1): the recomposed geometry may have
		// hidden the region that owns focus (e.g. chat maximized while the
		// editor was active). pi-tui would keep delivering keys to the now
		// unmounted pane — typing silently editing an invisible buffer — so
		// retarget the composer, the only always-visible region. The nested
		// sync from setFocusForRegion terminates immediately: the composer is
		// always in focusRegions(), so this never re-enters the branch.
		if (!this.focusRegions().includes(this.activeRegion)) {
			this.setFocusForRegion("chat-composer");
		}
		// Re-assert the TUI's real focus against the composed geometry: pi-tui
		// keeps delivering keys to a removed component, so a pane that was
		// unmounted after the check above (or a focus cleared by an overlay)
		// must be healed before the next keystroke, not on F6.
		this.reconcileFocus();
		this.tui.requestRender();
	}

	/** Cheap re-sync: only when the intent or terminal dimensions changed. */
	private syncWorkspaceShellIfChanged(): void {
		const integration = this.integration;
		if (!integration || !this.workspaceLayoutActive) return;
		const input = integration.controller.getLayoutInput();
		const key = workspaceShellKey(input, Math.max(0, this.tui.terminal.columns), Math.max(0, this.tui.terminal.rows));
		if (key === this.lastWorkspaceShellKey) return;
		this.syncWorkspaceShell();
	}

	// ─── workspace focus router (plan §8, v1) ───────────────────────────────

	/**
	 * Regions the current geometry makes visible, in cycle order. The editor
	 * is skipped while chat is maximized and the explorer while it is not
	 * docked; the Problems region exists only while its section is open.
	 */
	private focusRegions(): FocusRegion[] {
		const regions: FocusRegion[] = ["chat-composer"];
		const layout = this.workspaceLayout;
		if (layout?.editor) regions.push("editor");
		if (layout?.explorer) regions.push("explorer");
		if (this.problemsOpen && layout?.editor) regions.push("problems");
		return regions;
	}

	/** The mounted component that owns one visible focus region. */
	private regionComponent(region: FocusRegion): Component {
		const integration = this.integration;
		if (integration) {
			if (region === "editor") return integration.pane;
			if (region === "explorer") return integration.explorerPane;
			if (region === "problems") return integration.problemsPane;
		}
		return this.editor;
	}

	/** The region a mounted component stands for (undefined for non-regions). */
	private regionForComponent(component: Component | null): FocusRegion | undefined {
		if (component === this.editor) return "chat-composer";
		const integration = this.integration;
		if (!integration) return undefined;
		if (component === integration.pane) return "editor";
		if (component === integration.explorerPane) return "explorer";
		if (component === integration.problemsPane) return "problems";
		return undefined;
	}

	/**
	 * Deterministic focus reconciliation for every layout/visibility change and
	 * before each dispatched key. The TUI's real focused component wins when it
	 * is a known region that the current geometry still composes; otherwise the
	 * recorded region is re-validated. The composer is the always-live fallback.
	 * Overlays (dialogs, approvals, quick open) own their own focus and are
	 * never stolen from.
	 */
	private reconcileFocus(): void {
		if (this.exiting) return;
		if (this.dialogs.length > 0 || this.approvalDialogs.size > 0 || this.quickOpenPane) return;
		const regions = this.focusRegions();
		const probe = this.tui as TUI & { getFocusedComponent?(): Component | null; focused?: Component | null };
		const current =
			typeof probe.getFocusedComponent === "function" ? probe.getFocusedComponent() : (probe.focused ?? null);
		const focusedRegion = this.regionForComponent(current);
		const next =
			focusedRegion !== undefined && regions.includes(focusedRegion)
				? focusedRegion
				: regions.includes(this.activeRegion)
					? this.activeRegion
					: "chat-composer";
		const expected = this.regionComponent(next);
		if (next !== this.activeRegion) {
			this.activeRegion = next;
			if (next !== "editor" && this.completionSession) this.clearCompletion();
			this.integration?.controller.noteFocus(next);
		}
		if (current !== expected) this.tui.setFocus(expected);
		this.syncCursorBlink();
	}

	private cycleFocus(direction: 1 | -1): void {
		const regions = this.focusRegions();
		// A single visible region still takes focus: after a chat maximize F6
		// must hand the composer its focus instead of early-returning.
		if (regions.length <= 1) {
			this.setFocusForRegion(regions[0] ?? "chat-composer");
			return;
		}
		const index = regions.indexOf(this.activeRegion);
		const next =
			index < 0
				? (regions[0] ?? "chat-composer")
				: (regions[(index + direction + regions.length) % regions.length] ?? "chat-composer");
		this.setFocusForRegion(next);
	}

	/** Focus one region: retarget the TUI, record it, and re-sync narrow fallback. */
	private setFocusForRegion(region: FocusRegion): void {
		const integration = this.integration;
		// Only a region the current geometry actually composes may take TUI
		// focus (typing must never land in a hidden editor). The region intent
		// still reaches the controller so `preferredPane`/restore stay honest.
		const visible =
			region === "chat-composer" ||
			(region === "editor" && this.workspaceLayout?.editor !== undefined) ||
			(region === "explorer" && this.workspaceLayout?.explorer !== undefined) ||
			(region === "problems" && this.problemsOpen && this.workspaceLayout?.editor !== undefined);
		this.activeRegion = visible ? region : "chat-composer";
		// A completion list is editor-scoped: it must not stay mounted (and
		// keep consuming keys) after focus left the editor (finding 4a).
		if (this.activeRegion !== "editor" && this.completionSession) this.clearCompletion();
		if (!integration || !visible) {
			this.tui.setFocus(this.editor);
		} else {
			switch (region) {
				case "editor":
					this.tui.setFocus(integration.pane);
					break;
				case "explorer":
					this.tui.setFocus(integration.explorerPane);
					break;
				case "problems":
					this.tui.setFocus(integration.problemsPane);
					break;
				default:
					this.tui.setFocus(this.editor);
			}
		}
		integration?.controller.noteFocus(region);
		this.syncWorkspaceShellIfChanged();
		this.syncCursorBlink();
		this.tui.requestRender();
	}

	/** Mouse-driven region tracking (component onFocusRequested seam). */
	private noteRegionFocus(region: FocusRegion): void {
		// A mouse seam can report a region the layout no longer composes (the
		// pane was hidden between render and click). Clamp to the always-live
		// composer instead of leaving editor-scoped gates armed on a dead pane.
		const next = this.focusRegions().includes(region) ? region : "chat-composer";
		if (this.activeRegion === next) return;
		this.activeRegion = next;
		// A completion list is editor-scoped (finding 4a): mouse focus leaving
		// the editor unmounts it instead of leaving a visible orphan that could
		// later accept a stray Enter.
		if (next !== "editor" && this.completionSession) this.clearCompletion();
		this.integration?.controller.noteFocus(next);
		this.syncWorkspaceShellIfChanged();
		this.syncCursorBlink();
	}

	// ─── workspace keys (plan §8) ───────────────────────────────────────────

	/**
	 * Global workspace key routing. Keys are consumed only while the workspace
	 * is live and the feature was not disabled; editor-scoped keys additionally
	 * require the editor region so they can never fire while the composer (or a
	 * dialog, handled earlier) owns focus. Ctrl+W closes the active tab through
	 * the same dirty-guarded flow as the tab strip's ✕ (the terminal may
	 * intercept Ctrl+W; `/editor close` is the fallback).
	 */
	private handleWorkspaceKey(data: string): { consume: boolean } | undefined {
		const integration = this.integration;
		if (!integration) return undefined;
		const ready = integration.controller.phase === "ready";
		// Ctrl+P — Quick Open overlay (fallback: /quickopen; palette-safe).
		if (matchesKey(data, Key.ctrl("p")) && ready) {
			void this.openQuickOpen();
			return { consume: true };
		}
		if (!this.workspaceLayoutActive) return undefined;
		// Ctrl+T — explorer toggle (terminal new-tab risk documented; /explorer
		// and the palette remain the fallback).
		if (matchesKey(data, Key.ctrl("t"))) {
			this.setExplorerRequested(!integration.controller.getLayoutInput().explorerRequested);
			return { consume: true };
		}
		// Ctrl+W — close the active editor tab (dirty → Save/Discard/Cancel).
		// NOT in the chat composer: the composer's Editor owns Ctrl+W as
		// delete-word-backward, and stealing it there would break typing
		// (CodeRabbit pass-2 major; composer scoping per the ADR matrix).
		if (matchesKey(data, Key.ctrl("w")) && this.activeRegion !== "chat-composer") {
			this.closeActiveWorkspaceTab();
			return { consume: true };
		}
		// F6 / Shift+F6 — cycle focus. pi-tui 1.0.0's matchesKey cannot express
		// shift+F-keys (the f-key branch requires modifier 0), so the legacy
		// xterm sequence is decoded explicitly; the matchesKey form is kept for
		// a future toolkit that supports it.
		const shiftedCycle = data === SHIFT_F6_SEQUENCE;
		if (matchesKey(data, Key.f6) || shiftedCycle) {
			this.cycleFocus(shiftedCycle ? -1 : 1);
			return { consume: true };
		}
		if (!ready || this.activeRegion !== "editor") return undefined;
		// Ctrl+Right — toggle chat maximize; Ctrl+Left — always return to the
		// 50/50 split (never a second toggle: after mode hops the split must
		// stay reachable). These are EDITOR-scoped: while the composer owns
		// focus, Ctrl+←/→ keep their standard word-motion meaning. When the
		// editor region owns focus, the global bindings win there; editor word
		// motion stays alt+arrows (help text).
		if (matchesKey(data, Key.ctrl("right"))) {
			this.workspaceHostSurface?.toggleChatMaximize();
			return { consume: true };
		}
		if (matchesKey(data, Key.ctrl("left"))) {
			this.workspaceHostSurface?.restoreSplit();
			return { consume: true };
		}
		// Ctrl+Shift+A — attach the editor selection to the composer draft
		// (plan §20 explicit sharing). Delivered as a distinct CSI-u sequence
		// only under the Kitty keyboard protocol; legacy terminals send Ctrl+A,
		// which this never consumes (the editor's select-all stays intact).
		// /attach is the fallback everywhere.
		if (matchesKey(data, Key.ctrlShift("a"))) {
			const result = this.attachSelectionToDraft();
			this.print(result.notice, result.ok ? "info" : "warning");
			return { consume: true };
		}
		// Ctrl+Space — completion at the cursor (explicit-only in v1; the
		// legacy NUL byte is accepted when the Kitty protocol is inactive).
		if (matchesKey(data, Key.ctrl("space")) || data === "\x00") {
			void this.startCompletion();
			return { consume: true };
		}
		// F12 / Shift+F12 — go to definition / find references.
		const shiftedReferences = data === SHIFT_F12_SEQUENCE;
		if (matchesKey(data, Key.shift("f12")) || shiftedReferences) {
			void this.runDefinitionOrReferences("references");
			return { consume: true };
		}
		if (matchesKey(data, Key.f12)) {
			void this.runDefinitionOrReferences("definition");
			return { consume: true };
		}
		// F2 — rename preview (honest "unavailable" until the service offers it).
		if (matchesKey(data, Key.f2)) {
			void this.runRenamePreview();
			return { consume: true };
		}
		// Ctrl+S — compare-and-swap save of the active buffer.
		if (matchesKey(data, Key.ctrl("s"))) {
			void this.saveActiveDocument();
			return { consume: true };
		}
		return undefined;
	}

	// ─── workspace documents ────────────────────────────────────────────────

	/** Editor context for intelligence: active record + LSP cursor position. */
	private activeEditorContext(): { document: DocumentRecord; position: LspPosition } | undefined {
		const integration = this.integration;
		if (!integration || integration.controller.phase !== "ready") return undefined;
		const documentId = integration.tabs.activeDocumentId();
		if (!documentId) return undefined;
		const document = integration.store.getDocument(documentId);
		if (!document) return undefined;
		const state = integration.store.getViewState(documentId);
		return { document, position: offsetToPosition(document.text, state?.cursorOffset ?? 0) };
	}

	/** Root-relative display path for attachment banners (never a raw URI). */
	private workspaceRelativePath(uri: string): string {
		const display = uriDisplayPath(uri);
		const root = this.integration?.controller.identity?.resolvedRoot;
		if (root) {
			const prefix = root.endsWith("/") ? root : `${root}/`;
			if (display === root) return inlineSafe(display);
			if (display.startsWith(prefix)) return inlineSafe(display.slice(prefix.length));
		}
		const parts = display.split("/").filter((segment) => segment.length > 0);
		return inlineSafe(parts[parts.length - 1] ?? display);
	}

	/**
	 * T10 explicit selection attachment (plan §20): move the active editor
	 * selection into the composer draft with a visible scope header. This is
	 * the ONLY path that lets unsaved buffer text reach a message: the user
	 * invokes it directly (/attach or Ctrl+Shift+A in Kitty-protocol
	 * terminals), the notice names the file, line range, version and unsaved
	 * flag, and nothing is submitted — the user still writes and sends the
	 * message. Opening or viewing a file never calls this.
	 */
	private attachSelectionToDraft(): { ok: boolean; notice: string } {
		const integration = this.integration;
		if (!integration || integration.controller.phase !== "ready") {
			return { ok: false, notice: "Open a workspace first (/workspace)." };
		}
		const documentId = integration.tabs.activeDocumentId();
		const record = documentId ? integration.store.getDocument(documentId) : undefined;
		if (!documentId || !record) {
			return { ok: false, notice: "No file is open to attach a selection from." };
		}
		const state = integration.store.getViewState(documentId);
		const cursor = state?.cursorOffset ?? 0;
		const anchor = state?.selectionAnchorOffset;
		const start = anchor === undefined ? cursor : Math.min(anchor, cursor);
		const end = anchor === undefined ? cursor : Math.max(anchor, cursor);
		if (start >= end) return { ok: false, notice: "No selection to attach" };
		const selected = record.text.slice(start, end);
		// 1-based line range for display; a selection ending exactly at a line
		// start still belongs to the previous line's text in the shown span.
		const startLine = offsetToPosition(record.text, start).line + 1;
		const endPosition = offsetToPosition(record.text, end);
		let endLine = endPosition.line + 1;
		if (endPosition.character === 0 && endLine > startLine) endLine -= 1;
		const lineLabel = startLine === endLine ? `L${startLine}` : `L${startLine}–${endLine}`;
		const relativePath = this.workspaceRelativePath(record.uri);
		const versionLabel = record.dirty ? `unsaved v${record.version}` : "saved";
		const noticeLabel = record.dirty ? `unsaved, v${record.version}` : `saved, v${record.version}`;
		const banner = `<attached: ${relativePath} ${lineLabel} (${versionLabel})>`;
		const fence = fenceFor(selected);
		// A fence must close on its own line: add a trailing newline only when
		// the selection does not already end with one, so the fenced content
		// stays exactly the selected text.
		const body = selected.endsWith("\n") ? selected : `${selected}\n`;
		const block = `${banner}\n${fence}${fenceLanguage(record.languageId)}\n${body}${fence}`;
		this.editor.insertTextAtCursor(`${block}\n`);
		this.tui.requestRender();
		const rawUri = record.uri.length > 300 ? `${record.uri.slice(0, 299)}…` : record.uri;
		return {
			ok: true,
			notice: `Attached ${relativePath} ${lineLabel} (${noticeLabel}) — ${inlineSafe(rawUri)} — review and send when ready.`,
		};
	}

	/**
	 * Adapt the controller's IntelligenceOutcome union to the frozen
	 * IntelligenceRequest seam: ok → result; refused/failed/no-workspace (and
	 * superseded, whose result is gone) throw a typed error so sessions record
	 * an honest failure and never render a fabricated result.
	 */
	private intelligenceRequest(): IntelligenceRequest {
		return async (operation, uri, version, position) => {
			const integration = this.integration;
			if (!integration) {
				throw new WorkspaceIntelligenceFailure("no-workspace", "workspace is not available");
			}
			const outcome = await integration.controller.request(operation, uri, version, position);
			switch (outcome.status) {
				case "ok":
					return outcome.result;
				case "superseded":
					throw new WorkspaceIntelligenceFailure("superseded", "superseded by a newer request");
				case "refused":
					throw new WorkspaceIntelligenceFailure("refused", outcome.message);
				case "failed":
					throw new WorkspaceIntelligenceFailure("failed", outcome.message);
				case "no-workspace":
					throw new WorkspaceIntelligenceFailure("no-workspace", "no workspace is open");
			}
		};
	}

	/** Ctrl+Space: one explicit completion session mounted under the viewport. */
	private async startCompletion(): Promise<void> {
		const integration = this.integration;
		const context = this.activeEditorContext();
		if (!integration || !context) return; // no open file: do not fire (v1)
		const gen = this.captureWorkspaceFence();
		this.completionSession?.close("superseded");
		const session = new CompletionSession({
			store: integration.store,
			request: this.intelligenceRequest(),
			uri: context.document.uri,
			documentId: context.document.id,
			position: context.position,
		});
		this.completionSession = session;
		// Mouse acceptance (finding 5): a click selects the clicked row in the
		// machine first, then the popup's accept action fires. Keyboard and
		// dismiss hookups stay unchanged.
		const popup = new CompletionPopup({
			onSelect: (index) => {
				session.setSelectedIndex(index);
			},
		});
		this.completionPopup = popup;
		popup.onAction = (action) => {
			if (action.kind === "accept") this.acceptCompletion();
		};
		const state = await session.start();
		if (this.completionSession !== session || this.exiting || !this.isWorkspaceCurrent(gen)) return;
		if (state === "visible") {
			popup.setModel(completionPopupModelFrom(session));
			integration.pane.setIntelligence(popup, {
				accept: () => this.acceptCompletion(),
				dismiss: () => this.dismissCompletion(),
				move: (direction) => {
					session.moveSelection(direction);
					popup.setModel(completionPopupModelFrom(session));
					this.tui.requestRender();
				},
			});
			this.tui.requestRender();
			return;
		}
		// Nothing to show: one quiet honest notice per explicit invocation.
		if (session.lastDropReason === "unavailable") this.print("Completion is unavailable here.", "info");
		else if (session.lastDropReason === "error") this.print("Completion request failed.", "info");
		this.clearCompletion();
	}

	private acceptCompletion(): void {
		const session = this.completionSession;
		if (!session) return;
		const result = session.accept();
		this.clearCompletion();
		if (result.ok) {
			for (const application of result.applied) {
				this.integration?.controller.noteEdit(application.documentId);
			}
			this.refreshEditorView();
		} else if (result.reason !== "not-open") {
			this.print(`Completion not applied: ${result.message}`, "info");
		}
		this.tui.requestRender();
	}

	private dismissCompletion(): void {
		this.clearCompletion();
	}

	/**
	 * Deferred type-to-filter (finding 6): a key fell through to the editor and
	 * may have edited the buffer under an open completion list. The microtask
	 * runs after the focused view applied the edit, so the refilter sees the
	 * landed text/version instead of the stale pre-key state.
	 */
	private scheduleCompletionRefilter(): void {
		const session = this.completionSession;
		if (!session) return;
		queueMicrotask(() => this.refilterCompletion(session));
	}

	/**
	 * Re-sync an open completion session after the buffer moved on. A plain
	 * setPrefix() is not enough: it keeps the version captured when the list
	 * was requested, and accept() would refuse the (now) mismatched buffer as
	 * stale. rerun() re-captures the current version and refetches for the
	 * same caret, so the next accept passes the version gate. The existing
	 * snapshot is refiltered optimistically while that request is in flight.
	 */
	private refilterCompletion(session: CompletionSession): void {
		if (this.completionSession !== session || this.exiting) return;
		const integration = this.integration;
		const popup = this.completionPopup;
		if (!integration || !popup || this.activeRegion !== "editor") return;
		const record = integration.store.getDocument(session.documentId);
		if (!record || session.capturedVersion === record.version) return;
		const cursorOffset = integration.store.getViewState(session.documentId)?.cursorOffset ?? 0;
		const prefix = completionPrefixBefore(record.text, cursorOffset);
		const position = offsetToPosition(record.text, cursorOffset);
		const seq = ++this.completionRefilterSeq;
		const gen = this.captureWorkspaceFence();
		void session.rerun(prefix, position).then((state) => {
			// A newer refilter, a closed session, a superseded workspace or an
			// exit owns the surface now.
			if (this.completionSession !== session || this.exiting || seq !== this.completionRefilterSeq) return;
			if (!this.isWorkspaceCurrent(gen)) return;
			if (state === "visible") {
				popup.setModel(completionPopupModelFrom(session));
				this.tui.requestRender();
				return;
			}
			this.clearCompletion();
		}).catch(() => {
			if (this.completionSession === session && seq === this.completionRefilterSeq) this.clearCompletion();
		});
		popup.setModel(completionPopupModelFrom(session));
		this.tui.requestRender();
	}

	private clearCompletion(): void {
		this.completionSession?.close("dismissed");
		this.completionSession = undefined;
		this.completionPopup = undefined;
		this.completionRefilterSeq += 1;
		this.integration?.pane.setIntelligence(undefined);
		this.tui.requestRender();
	}

	/** F12 / Shift+F12: request, pick among multiple targets, then jump. */
	private async runDefinitionOrReferences(operation: "definition" | "references"): Promise<void> {
		const integration = this.integration;
		const context = this.activeEditorContext();
		if (!integration || !context) return;
		const gen = this.captureWorkspaceFence();
		const outcome = await integration.controller.request(
			operation,
			context.document.uri,
			context.document.version,
			context.position,
		);
		if (this.exiting || this.integration !== integration || !this.isWorkspaceCurrent(gen)) return;
		if (outcome.status !== "ok") {
			this.print(`${operation} unavailable: ${intelligenceOutcomeMessage(outcome)}`, "info");
			return;
		}
		const payload = narrowLocationsPayload(outcome.result.result);
		const picker = new LocationPicker(payload.locations, { omitted: payload.omitted });
		if (picker.size === 0) {
			this.print(`No ${operation} found.`, "info");
			return;
		}
		if (picker.size === 1) {
			const only = picker.current();
			if (only) await this.openLocation(only);
			return;
		}
		const targets = picker.targets();
		const choice = await this.pick(
			`${operation === "definition" ? "Definitions" : "References"} (${targets.length})`,
			targets.map((target, index) => ({
				value: String(index),
				label: `${uriDisplayPath(target.uri)}:${target.lineLabel}`,
			})),
		);
		if (!choice) return;
		if (!this.isWorkspaceCurrent(gen)) return;
		const target = targets[Number(choice.value)];
		if (target) await this.openLocation(target);
	}

	/** Open one jump target and place the caret (definition-style navigation). */
	private async openLocation(target: LocationChoice): Promise<void> {
		const integration = this.integration;
		if (!integration) return;
		const gen = this.captureWorkspaceFence();
		const outcome = await integration.controller.openUri(target.uri);
		if (!this.isWorkspaceCurrent(gen)) return;
		if (outcome.status !== "opened" && outcome.status !== "focused") return;
		const record = integration.store.getDocument(outcome.documentId);
		if (record) {
			const offset = positionToOffset(record.text, { line: target.line, character: target.character });
			integration.store.setSelection(outcome.documentId, { cursorOffset: offset });
			integration.view.setView({
				documentId: outcome.documentId,
				cursorOffset: offset,
				scrollLine: Math.max(0, target.line - 2),
				horizontalCellOffset: 0,
			});
		}
		this.tui.requestRender();
	}

	/** F2: prepare a rename preview; the honest v1 answer is "unavailable". */
	private async runRenamePreview(): Promise<void> {
		const integration = this.integration;
		const context = this.activeEditorContext();
		if (!integration || !context) return;
		const session = new RenameSession({
			store: integration.store,
			uri: context.document.uri,
			documentId: context.document.id,
			position: context.position,
		});
		const gen = this.captureWorkspaceFence();
		const result = await session.start();
		if (!this.isWorkspaceCurrent(gen)) return;
		if (!result.ok) {
			// Unavailable is the honest state until the service offers rename
			// (the operation is not in the frozen intelligence allowlist).
			this.print(result.reason, "info");
			return;
		}
		const choice = await this.pick(
			`Rename: ${result.preview.fileCount} file(s), ${result.preview.totalEdits} edit(s)`,
			[
				{ value: "cancel", label: "Cancel" },
				{ value: "apply", label: "Apply rename" },
			],
		);
		if (choice?.value !== "apply") return;
		if (!this.isWorkspaceCurrent(gen)) return;
		const applied = session.apply(true);
		if (!applied.ok) {
			this.print(`Rename not applied: ${applied.message}`, "error");
			return;
		}
		for (const application of applied.applied) {
			integration.controller.noteEdit(application.documentId);
			this.refreshEditorView(application.documentId);
		}
		this.print("Rename applied.", "info");
	}

	/** Ctrl+S: save the active buffer; conflicts open the explicit dialog. */
	private async saveActiveDocument(): Promise<void> {
		const integration = this.integration;
		if (!integration) return;
		const documentId = integration.tabs.activeDocumentId();
		// No open file: do not fire (consistent with completion/definition/rename).
		if (!documentId) return;
		await this.saveDocument(documentId);
	}

	private async saveDocument(documentId: string): Promise<boolean> {
		const integration = this.integration;
		if (!integration) return false;
		// MCH-012: a save (or its conflict detour) belongs to the workspace
		// generation that started it. A root switch/close/exit while the RPC is
		// parked must not publish into, or resolve against, the successor.
		const gen = this.captureWorkspaceFence();
		const outcome = await integration.controller.save(documentId);
		if (!this.isWorkspaceCurrent(gen)) return false;
		switch (outcome.status) {
			case "saved":
				this.tui.requestRender(); // status row: dirty marker cleared
				return true;
			case "conflict":
				await this.resolveSaveConflict(outcome.conflict);
				return false;
			case "not-found":
				this.print("No file open to save.", "info");
				return false;
			default:
				// refused/failed were notified by the controller with the reason.
				return false;
		}
	}

	/**
	 * Reload disk / Overwrite (explicit) / Keep editing, per plan §9. Returns
	 * the controller resolution so close-flow callers can continue the close
	 * after a conflict detour (finding 7).
	 */
	private async resolveSaveConflict(conflict: SaveConflictDescriptor): Promise<ConflictResolution | undefined> {
		const integration = this.integration;
		if (!integration) return undefined;
		// MCH-012: the picker can stay open across a root switch/close. The
		// resolution (and a possible overwrite write) must target the workspace
		// that raised the conflict, never its successor.
		const gen = this.captureWorkspaceFence();
		const choice = await this.pick(`Save conflict: ${displayUriName(conflict.uri)}`, [
			{ value: "reload", label: "Reload disk", description: "Discard buffer edits and adopt the file on disk" },
			{ value: "overwrite-explicit", label: "Overwrite (explicit)", description: "Write this buffer over the file on disk" },
			{ value: "keep", label: "Keep editing", description: "Nothing is written; the buffer stays dirty" },
		]);
		if (!this.isWorkspaceCurrent(gen)) return undefined;
		const resolution = await integration.controller.resolveConflict(
			conflict.documentId,
			(choice?.value as "keep" | "reload" | "overwrite-explicit" | undefined) ?? "keep",
		);
		if (!this.isWorkspaceCurrent(gen)) return undefined;
		if (resolution.status === "reloaded" || resolution.status === "saved" || resolution.status === "kept") {
			this.refreshEditorView(conflict.documentId);
		}
		this.tui.requestRender();
		return resolution;
	}

	/** Ctrl+W / `/editor close` path: close the active tab, dirty-guarded. */
	private closeActiveWorkspaceTab(): void {
		const documentId = this.integration?.tabs.activeDocumentId();
		if (!documentId) return;
		void this.closeWorkspaceTab(documentId);
	}

	/** Tab-strip close (middle-click / onCloseTab): decision dialog when dirty. */
	private async closeWorkspaceTab(documentId: string): Promise<void> {
		const integration = this.integration;
		if (!integration) return;
		// MCH-012: the dirty-close picker and the conflict detour are both
		// user-paced awaits; a root switch/close/exit in between retires the
		// whole request (the buffer itself is retained by the controller).
		const gen = this.captureWorkspaceFence();
		const outcome = await integration.controller.closeTab(documentId);
		if (!this.isWorkspaceCurrent(gen)) return;
		if (outcome.status === "needs-decision") {
			const record = integration.store.getDocument(documentId);
			const choice = await this.pick(`Close ${record ? displayUriName(record.uri) : "file"}?`, [
				{ value: "save", label: "Save and close" },
				{ value: "discard", label: "Discard changes" },
				{ value: "cancel", label: "Cancel" },
			]);
			if (!choice || !this.isWorkspaceCurrent(gen)) return;
			const resolved = await integration.controller.resolveClose(documentId, choice.value as DirtyCloseChoice);
			if (!this.isWorkspaceCurrent(gen)) return;
			if (resolved.status === "closed") this.afterTabClosed(documentId);
			else if (resolved.status === "save-conflict") {
				// The close decision survives the conflict detour (finding 7):
				// once the save/overwrite (or reload) cleared the buffer, run the
				// already-chosen close again — otherwise the tab silently stays
				// open after the user answered "Save and close". Keep editing
				// leaves the tab open (and dirty) on purpose.
				const conflict = await this.resolveSaveConflict(resolved.conflict);
				if (!this.isWorkspaceCurrent(gen)) return;
				if (conflict?.status === "saved" || conflict?.status === "reloaded") {
					const closed = await integration.controller.resolveClose(documentId, "discard");
					if (this.isWorkspaceCurrent(gen) && closed.status === "closed") this.afterTabClosed(documentId);
				}
			} else if (resolved.status === "needs-decision") {
				this.print("The file changed while saving; save again or discard.", "warning");
			}
			return;
		}
		if (outcome.status === "closed") this.afterTabClosed(documentId);
	}

	private afterTabClosed(documentId: string): void {
		const integration = this.integration;
		if (!integration) return;
		integration.view.releaseDocument(documentId);
		const active = integration.tabs.activeDocumentId();
		if (active) integration.view.setDocument(active);
		this.refreshLspStatus();
		this.syncCursorBlink();
		this.tui.requestRender();
	}

	/** Open a root-relative workspace path (explorer/quick open) as a file URI. */
	private async openWorkspacePath(path: string, options: { preview?: boolean } = {}): Promise<void> {
		const integration = this.integration;
		if (!integration) return;
		const identity = integration.controller.identity;
		if (!identity) {
			this.print("Workspace is not available.", "warning");
			return;
		}
		const gen = this.captureWorkspaceFence();
		const uri = pathToFileURL(isAbsolute(path) ? path : join(identity.resolvedRoot, path)).href;
		const outcome = await integration.controller.openUri(uri, options);
		// MCH-012: the read may outlive a root switch; a retired document id
		// must not be published into the successor's view.
		if (!this.isWorkspaceCurrent(gen)) return;
		if (outcome.status === "opened" || outcome.status === "focused") {
			integration.view.setDocument(outcome.documentId);
			void this.refreshProblemsForActiveDocument();
			this.refreshLspStatus();
			this.syncCursorBlink();
			this.tui.requestRender();
		}
	}

	/** Problems Enter/click: open the file and place the caret at the row. */
	private async openProblem(row: ProblemRow): Promise<void> {
		const integration = this.integration;
		if (!integration) return;
		const gen = this.captureWorkspaceFence();
		const outcome = await integration.controller.openUri(row.uri);
		if (!this.isWorkspaceCurrent(gen)) return;
		if (outcome.status !== "opened" && outcome.status !== "focused") return;
		const record = integration.store.getDocument(outcome.documentId);
		if (record) {
			const offset = positionToOffset(record.text, { line: row.line, character: row.character });
			integration.store.setSelection(outcome.documentId, { cursorOffset: offset });
			integration.view.setView({
				documentId: outcome.documentId,
				cursorOffset: offset,
				scrollLine: Math.max(0, row.line - 2),
				horizontalCellOffset: 0,
			});
		}
		this.tui.requestRender();
	}

	/** Pull cursor/selection from the store after an external store mutation. */
	private refreshEditorView(documentId?: string): void {
		const integration = this.integration;
		if (!integration) return;
		const id = documentId ?? integration.tabs.activeDocumentId();
		if (!id) return;
		const state = integration.store.getViewState(id);
		if (state) integration.view.setView(state);
		this.tui.requestRender();
	}

	// ─── workspace problems + LSP ───────────────────────────────────────────

	/** Feed the Problems pane: mounted on demand, refocused on request. */
	private openProblems(): void {
		const integration = this.integration;
		if (!integration || this.workspaceUnsupported) {
			this.print("Workspace is not available.", "warning");
			return;
		}
		if (!this.problemsOpen) {
			this.problemsOpen = true;
			integration.pane.setProblems(integration.problemsPane);
		}
		if (!this.workspaceLayout?.editor) {
			// The section lives inside the editor pane: bring the split back
			// when the geometry allows it, then focus; otherwise say so.
			this.workspaceHostSurface?.restoreSplit();
		}
		if (this.workspaceLayout?.editor) this.setFocusForRegion("problems");
		else this.print("The editor pane is not visible at this terminal size.", "warning");
		void this.refreshProblemsForActiveDocument();
	}

	/** Esc/Problems dismissal: unmount the section and return focus to the editor. */
	private closeProblems(): void {
		const integration = this.integration;
		if (!integration || !this.problemsOpen) return;
		this.problemsOpen = false;
		integration.pane.setProblems(undefined);
		if (this.activeRegion === "problems") this.setFocusForRegion("editor");
		else this.tui.requestRender();
	}

	/** Diagnostics pull for the active document (tab focus / after open). */
	private async refreshProblemsForActiveDocument(): Promise<void> {
		const integration = this.integration;
		if (!integration || integration.controller.phase !== "ready") return;
		const documentId = integration.tabs.activeDocumentId();
		const record = documentId ? integration.store.getDocument(documentId) : undefined;
		if (!record) return;
		const gen = this.captureWorkspaceFence();
		await integration.problemsModel.refresh(record.uri, record.version);
		// The model is URI/version-keyed; a superseded refresh must not repaint
		// the successor's pane from the retired root's diagnostics.
		if (this.isWorkspaceCurrent(gen)) this.tui.requestRender();
	}

	/**
	 * Right-aligned editor status text for the ACTIVE document's language
	 * (finding 3): the raw LSP state only ("ready", "starting…", "missing"),
	 * never repeating the language name shown on the left. No reported server
	 * means no segment — absence is not invented as a state.
	 */
	private lspStatusText(): string | undefined {
		const integration = this.integration;
		const controller = integration?.controller;
		if (!integration || !controller || controller.phase !== "ready") return undefined;
		const documentId = integration.tabs.activeDocumentId();
		const record = documentId ? integration.store.getDocument(documentId) : undefined;
		if (!record) return undefined;
		const target = languageForId(record.languageId);
		if (target === "plain") return undefined;
		const shown = (state: string): string => (state === "starting" ? "starting…" : state);
		const snapshot = controller.lspStatusSnapshot();
		for (const server of snapshot.servers) {
			if (languageForId(server.languageId || server.profile) === target) return shown(server.state);
		}
		for (const [profile, entry] of Object.entries(snapshot.profiles)) {
			if (languageForId(profile) === target) return shown(entry.state);
		}
		return undefined;
	}

	private refreshLspStatus(): void {
		this.integration?.pane.setLspStatus(this.lspStatusText());
	}

	// ─── workspace quick open ───────────────────────────────────────────────

	/** Ctrl+P /quickopen: bounded overlay picker over `workspace.quickOpen`. */
	private async openQuickOpen(): Promise<void> {
		const integration = this.integration;
		if (!integration || this.workspaceUnsupported || integration.controller.phase !== "ready") {
			this.print("Open a workspace first (/workspace).", "warning");
			return;
		}
		if (this.quickOpenPane) return;
		const pane = new QuickOpenPane((query) => this.workspaceQuickOpen(query), {
			onChange: () => this.tui.requestRender(),
		});
		pane.onOpen = (entry) => {
			this.closeQuickOpen();
			void this.openWorkspacePath(entry.path, { preview: true });
		};
		pane.onClose = () => this.closeQuickOpen();
		this.quickOpenPane = pane;
		this.quickOpenHandle = this.tui.showOverlay(pane, {
			width: "100%",
			maxHeight: "100%",
			// Same palette family as the skills directory: open above so the
			// list reads top-down and the composer stays visible.
			anchor: "top-center",
		});
		pane.activate();
		this.tui.setFocus(pane);
		this.syncCursorBlink();
		this.tui.requestRender();
	}

	private closeQuickOpen(): void {
		const pane = this.quickOpenPane;
		if (!pane) return;
		this.quickOpenPane = undefined;
		this.quickOpenHandle?.hide();
		this.quickOpenHandle = undefined;
		// P0-20: dismiss the pane too. Choice (onOpen) and exit paths close
		// without ever running dismiss(), leaving its provider timer alive:
		// a request scheduled for the dead overlay would still beat the
		// debounce window and hit the workspace with a query nobody asked
		// for. Clear the callback FIRST: dismiss() would otherwise re-enter
		// this funnel through onClose.
		pane.onClose = undefined;
		pane.dismiss();
		// Dismissing the overlay returns focus to the composer (finding 3);
		// the region router keeps the recorded region honest.
		if (!this.exiting) this.setFocusForRegion("chat-composer");
		this.tui.requestRender();
	}

	/** Ranking call for Quick Open, bound to the open workspace identity. */
	private workspaceQuickOpen(query: string): Promise<WorkspaceQuickOpenResult> {
		const integration = this.integration;
		const identity = integration?.controller.identity;
		if (!integration || !identity) return Promise.reject(new Error("workspace is not open"));
		return this.client.request<WorkspaceQuickOpenResult>(
			"workspace.quickOpen",
			{ workspaceId: identity.workspaceId, epoch: identity.epoch, query, limit: 50 },
			120_000,
		);
	}

	// ─── skills directory (A5, spec §12.3) ──────────────────────────────────

	/**
	 * `/skills`: a bounded, bottom-anchored Menu dialog over `skills.search`,
	 * with `skills.show` details and a call-free `skills.preview_route` view
	 * (the same `openDialog` + `Menu` pattern as the theme menu). The menu
	 * holds no authority: delete is routed back through the legacy
	 * `/skills delete` command (same confirmation/authorization), and preview
	 * cannot start a turn, call a provider, run tools, install skills or
	 * change approvals.
	 */
	private openSkillsPicker(initialQuery = ""): void {
		if (this.skillsMenu) return;
		void this.openDialog<{ delete: string }>((close) => {
			const menu = new SkillsMenu(
				{
					search: (params) => this.skillsSearch(params),
					catalog: () => this.skillsCatalog(),
					details: (skillId) => this.skillsDetails(skillId),
					preview: (request) => this.skillsPreview(request),
					section: (params) => this.skillsSection(params),
				},
				{
					initialQuery,
					title: "Skills",
					tui: this.tui,
					visibleRows: DEFAULT_MENU_VISIBLE_ROWS,
					onChange: () => this.tui.requestRender(),
					onClose: () => close(),
					onDelete: (skillName) => close({ delete: skillName }),
				},
			);
			this.skillsMenu = menu;
			menu.activate();
			return menu;
		}).then((result) => {
			this.skillsMenu = undefined;
			// Keep the legacy command as the only delete path: same
			// confirmation, same authorization, same RPC as `/skills delete`.
			if (result?.delete === undefined) return;
			const command = parseCommand(`/skills delete ${result.delete}`);
			if (command) void runCommand(command, this);
		});
	}

	// ─── help palette (searchable /help) ────────────────────────────────────

	/**
	 * `/help`: the searchable command/keybinding Menu (menu-shaped surface
	 * rule). Enter on a no-argument command closes the dialog and dispatches
	 * through the same parseCommand/runCommand path as typed input; a command
	 * with an argument hint closes and prefills `/name ` into the composer
	 * instead. Key rows are read-only reference; Esc closes.
	 */
	private openHelpPalette(initialQuery = ""): void {
		if (this.helpMenu) return;
		void this.openDialog<{ run?: string; prefill?: string }>((close) => {
			const menu = new HelpMenu({
				commands: helpMenuCommands(SLASH_COMMANDS),
				keys: HELP_KEYS,
				initialQuery,
				title: "Help",
				tui: this.tui,
				visibleRows: DEFAULT_MENU_VISIBLE_ROWS,
				onChange: () => this.tui.requestRender(),
				onClose: () => close(),
				onRun: (name) => close({ run: name }),
				onPrefill: (text) => close({ prefill: text }),
			});
			this.helpMenu = menu;
			menu.activate();
			return menu;
		}).then((result) => {
			this.helpMenu = undefined;
			if (result?.run !== undefined) {
				const parsed = parseCommand(`/${result.run}`);
				if (parsed) void runCommand(parsed, this);
			} else if (result?.prefill !== undefined) {
				this.prefillInput(result.prefill);
			}
		});
	}

	/** `skills.search` binding: the pane's closed filter object goes as-is. */
	private skillsSearch(params: SkillsSearchParams): Promise<SkillsSearchResult> {
		return this.request<SkillsSearchResult>("skills.search", {
			query: params.query,
			filters: params.filters,
			limit: params.limit,
			...(params.cursor ? { cursor: params.cursor } : {}),
		});
	}

	/**
	 * Empty-query browse for the skills menu: the full catalog (`skills.list`,
	 * the legacy authorization-guarded handler) shaped like a search result so
	 * opening `/skills` lists every skill immediately. Disabled skills are
	 * shown honestly (applicability `excluded`, reason `disabled`) — browsing
	 * intentionally surfaces them; the automatic selector never does.
	 */
	private async skillsCatalog(): Promise<SkillsSearchResult> {
		const { skills } = await this.request<{
			skills: Array<{ name: string; description: string; sourceType?: string; enabled?: boolean }>;
		}>("skills.list", {});
		const candidates: SkillsCandidate[] = (Array.isArray(skills) ? skills : []).map((skill) => {
			const enabled = skill.enabled !== false;
			return {
				skillId: skill.name,
				name: skill.name,
				contentRevision: "",
				score: 0,
				scoreComponents: {},
				matchedPhrases: [],
				matchedCapabilities: [],
				applicability: enabled ? "applicable" : "excluded",
				reasons: enabled ? [] : ["disabled"],
				workflowStages: [],
				explanation: "catalog browse",
				sourceType: skill.sourceType ?? "",
				trust: skill.sourceType === "builtin-ecc" ? "bundled" : "local",
				enabled,
			};
		});
		const filters: SkillsFilters = {
			task_family: "",
			capability: "",
			source: [],
			enabled: null,
			include_excluded: true,
		};
		return {
			query: "",
			filters,
			limit: candidates.length,
			browse: true,
			candidates,
			catalogRevision: "",
			queryDescriptorRevision: "",
			policyRevision: "",
			nextCursor: null,
			diagnostics: {},
		};
	}

	/** Details read: the legacy `skills.show` contract (case-insensitive id). */
	private async skillsDetails(skillId: string): Promise<SkillsDetailsSkill> {
		const { skill } = await this.request<{ skill: SkillsDetailsSkill }>("skills.show", { name: skillId });
		return skill;
	}

	/** Call-free route preview: no turn, provider, tool, install or approval. */
	private skillsPreview(request: string): Promise<SkillsPreviewResult> {
		return this.request<SkillsPreviewResult>("skills.preview_route", { request, context: {} });
	}

	/** Version-bound section read: exact passage for the shown revision (§12.2). */
	private skillsSection(params: SkillsSectionParams): Promise<SkillsSectionResult> {
		return this.request<SkillsSectionResult>("skills.section", {
			skillId: params.skillId,
			...(params.contentRevision ? { contentRevision: params.contentRevision } : {}),
			...(params.section ? { section: params.section } : {}),
			...(params.offset !== undefined ? { offset: params.offset } : {}),
			budget: params.budget ?? 800,
		});
	}

	// ─── workspace events + toggles ─────────────────────────────────────────

	/**
	 * Workspace notifications (plan §19): a family separate from chat, never
	 * entering turn bookkeeping. The controller identity/epoch-gates them;
	 * diagnostics additionally feed the Problems projection.
	 */
	private onWorkspaceEvent(event: WorkspaceEvent): void {
		if (this.exiting) return;
		const integration = this.integration;
		if (!integration) return;
		integration.controller.handleEvent(event);
		if (event.type === "workspace.diagnostics") {
			integration.problemsModel.push({
				uri: event.uri,
				version: event.version,
				source: event.source,
				diagnostics: event.diagnostics,
			});
		}
		this.syncWorkspaceShellIfChanged();
		this.tui.requestRender();
	}

	/** Toggle/open/close the explorer column; loads the listing on open. */
	private setExplorerRequested(open: boolean): void {
		const integration = this.integration;
		if (!integration) return;
		if (integration.controller.getLayoutInput().explorerRequested !== open) {
			integration.controller.toggleExplorer();
		}
		if (open) void integration.explorerModel.load();
		this.syncWorkspaceShell();
	}

	/** METHOD_NOT_FOUND once: quiet disable, one info notice, no retry spam. */
	private noteWorkspaceUnsupported(): void {
		if (this.workspaceUnsupported) return;
		this.workspaceUnsupported = true;
		this.footer.workspaceHints = "";
		this.print("workspace commands unavailable on this backend", "info");
	}

	private async submit(raw: string): Promise<void> {
        const waitingForImages = this.pendingImageLoads > 0;
        const epoch = this.imageEpoch;
        await this.imageLoads;
        if (epoch !== this.imageEpoch || this.exiting) return;
        const text = (waitingForImages ? this.editor.getExpandedText() : raw).trim();
		if (!text) return;

		// AH-031: parse FIRST, then guard only conflicting operations.
		// Read-only / navigation commands must work during a turn; only
		// new prompts and session-mutating commands on the in-flight
		// session are rejected.
		const command = parseCommand(text);
        if (command && /\[image \d+\]/.test(text)) {
            this.print("Send image attachments with a message, rather than a slash command.", "warning");
            return;
        }
		const inFlight = this.current && this.inFlight.has(this.current.id);
		switch (guardSubmit(command, Boolean(inFlight))) {
			case "turn-busy":
				this.transcript.addNotice("The previous turn is still finishing. Try again shortly.", "warning");
				this.tui.requestRender();
				return;
			case "stop-first":
				this.transcript.addNotice("Stop the running reply before changing this session.", "warning");
				this.tui.requestRender();
				return;
			case "allow":
				break;
		}

        let draft: ReturnType<ImageDraft["prepare"]>;
        try {
            if (text === this.lastPrompt && this.current?.id === this.lastPromptSessionId && this.lastPromptImages.length) {
                // Editor history contains markers only. Re-edit the original
                // inline bytes instead of reading a possibly changed file.
                this.imageDraft = new ImageDraft();
                this.imageDraft.addMany(this.lastPromptImages);
            }
            draft = this.imageDraft.prepare(text);
        }
        catch (error) { this.print(message(error), "error"); return; }
        const submittedImages = this.imageDraft;
        this.imageDraft = new ImageDraft();
        this.imageEpoch++;
        const submittedEpoch = this.imageEpoch;
        const submittedSessionId = this.current?.id;

		this.editor.setText("");

		// Never keep secrets in input history: `/keys set KEY VALUE` carries a
		// raw value, and the unsupported `/provider add <name> <url> <key>`
		// form carries one in its ignored third token (see commandCarriesSecret).
        if (!commandCarriesSecret(command)) this.editor.addToHistory(draft.text);
		if (command) {
			await runCommand(command, this);
		} else {
			this.lastPrompt = draft.text;
            this.lastPromptImages = draft.images;
            this.lastPromptSessionId = submittedSessionId;
			try {
				await this.submitTurn(draft.text, draft.images);
			} catch (error) {
				// Restore the draft on failure. When the guard was released
				// the failure preceded acceptance and retry is meaningful;
				// when it is retained the dispatch outcome is unknown, so say
				// so instead of implying a retry is safe.
                const unknownDispatch = submittedSessionId ? this.inFlight.has(submittedSessionId) : false;
                if (!this.editor.getText() && !unknownDispatch && this.current?.id === submittedSessionId && this.imageEpoch === submittedEpoch) {
                    this.imageDraft = submittedImages;
                    this.editor.setText(text);
                }
				this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
				this.transcript.addNotice(
					unknownDispatch
						? `${message(error)} (dispatch state unknown; retry may duplicate work)`
						: message(error),
					unknownDispatch ? "warning" : "error",
				);
			}
		}
		this.tui.requestRender();
	}

    async submitTurn(text: string, images: ImageAttachment[] = []): Promise<void> {
		if (!this.current) {
			this.transcript.addNotice("Not connected to a session. Try /new.", "warning");
			return;
		}
		const sessionId = this.current.id;
		if (this.inFlight.has(sessionId)) {
			this.transcript.addNotice("The previous turn is still finishing. Try again shortly.", "warning");
			this.tui.requestRender();
			return;
		}
		this.transcript.addUser(text);
		this.setRunning(true);
		this.inFlight.add(sessionId);
		// Register the submitting record before dispatch: gateway work is
		// asynchronous and events can arrive before the RPC result.
		const record = this.turnRecord(sessionId);
		record.turnId = undefined;
		record.provisional = true;
		record.started = false;
		record.hadEvents = false;
		record.warnedSlow = false;
		record.usageLatest = 0;
		this.startInFlightTimer(sessionId);
		this.tui.requestRender();
		try {
            const result = await this.client.request<{ turnId?: string }>("prompt.submit", {
                sessionId, text, ...(images.length ? { images } : {}),
            });
			this.confirmTurn(sessionId, result?.turnId);
		} catch (error) {
			// Rejection known to precede acceptance (no dispatch evidence)
			// retires the submitting record. After possible dispatch the
			// outcome is unknown: retain the guard, never auto-retry.
			const seen = this.turns.get(sessionId);
			if (!seen?.hadEvents) this.clearInFlight(sessionId);
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			throw error;
		}
	}

	/** Confirm a provisional binding against the submit result. */
	private confirmTurn(sessionId: string, turnId: string | undefined): void {
		const record = this.turns.get(sessionId);
		if (!record) return;
		if (turnId === undefined) return; // keep the event-bound provisional ID
		if (record.turnId === undefined) {
			record.turnId = turnId;
			record.provisional = false;
			return;
		}
		if (record.turnId !== turnId) {
			this.transcript.addNotice(
				`Protocol error: turn ${turnId.slice(0, 8)} does not match the running turn. Keeping the current turn guard.`,
				"error",
			);
			this.tui.requestRender();
			return;
		}
		record.provisional = false;
	}

	private turnRecord(sessionId: string): SessionTurn {
		let record = this.turns.get(sessionId);
		if (!record) {
			record = { turnId: undefined, provisional: true, started: false, hadEvents: false, warnedSlow: false, usageLatest: 0, retired: [] };
			this.turns.set(sessionId, record);
		}
		return record;
	}

	/** Retire a turn: release the guard once, keep a bounded retired-ID set. */
	private retireTurn(sessionId: string, turnId: string): void {
		const record = this.turnRecord(sessionId);
		if (!record.retired.includes(turnId)) {
			record.retired.push(turnId);
			if (record.retired.length > 20) record.retired.shift();
		}
		if (record.turnId === turnId) {
			record.turnId = undefined;
			record.provisional = true;
			record.started = false;
			record.hadEvents = false;
			record.warnedSlow = false;
			record.usageLatest = 0;
		}
		this.clearInFlight(sessionId);
	}

	/**
	 * Local watchdog: a UI diagnostic deadline (300s, matching the server
	 * turn timeout), never ownership authority. On expiry while connected it
	 * warns once, retains the in-flight guard, and leaves inspection, cancel
	 * and exit available. It never advertises resubmission.
	 */
	private startInFlightTimer(sessionId: string): void {
		const previous = this.inFlightTimers.get(sessionId);
		if (previous) clearTimeout(previous);
		const timer = setTimeout(() => {
			this.inFlightTimers.delete(sessionId);
			const record = this.turns.get(sessionId);
			if (this.inFlight.has(sessionId) && record && !record.warnedSlow) {
				record.warnedSlow = true;
				if (sessionId === this.current?.id) {
					this.transcript.addNotice("Taking longer than expected; waiting for completion.", "warning");
					this.tui.requestRender();
				}
			}
		}, App.IN_FLIGHT_TIMEOUT_MS);
		timer.unref();
		this.inFlightTimers.set(sessionId, timer);
	}

	/** Remove a session from in-flight tracking and cancel its stale timer. */
	private clearInFlight(sessionId: string): void {
		this.inFlight.delete(sessionId);
		const timer = this.inFlightTimers.get(sessionId);
		if (timer) {
			clearTimeout(timer);
			this.inFlightTimers.delete(sessionId);
		}
	}

	private cancel(): void {
		if (this.inFlight.size === 0) return;
		// Cancel every in-flight turn, not just the most recent: two sessions
		// can each have a turn running after a quick switch.
		for (const sessionId of [...this.inFlight]) {
			this.client.request("prompt.cancel", { sessionId }).catch(() => {});
		}
		// Don't setRunning(false) while any session is still in inFlight —
		// the completion event hasn't arrived yet and the guard will reject
		// the next submit if we clear running now.
		if (this.current && !this.inFlight.has(this.current.id)) {
			this.setRunning(false);
		}
	}

	private setRunning(running: boolean): void {
		this.running = running;
		// AH-031: do NOT disable the composer while a turn runs — safe
		// slash commands (/sessions, /new, /exit, /help, …) must stay
		// usable. Prompt submits are guarded in submit()/submitTurn().
		this.editor.disableSubmit = false;
		if (this.footer.status !== "offline") {
			if (!running) this.footer.status = "ready";
			else if (!["awaiting_approval", "stopping", "cleanup_pending"].includes(this.footer.status)) this.footer.status = "working";
		}
	}

	// ─── gateway ────────────────────────────────────────────────────────────
	private onEvent(event: GatewayEvent): void {
		if (this.exiting) return;
		for (const handler of [...this.featureEventHandlers]) handler(event);
		// Feature-level frames carry no session/turn: they settle command
		// flows above and must not enter turn bookkeeping or hidden tails.
		if (event.type === "auth.chatgpt.completed" || event.type === "auth.chatgpt.failed") return;
		this.shell.sync();
		if (event.type === "growth.status") {
			// Background training lands after the turn retires (and the
			// in-flight entry clears), so it bypasses turn tracking. Shown
			// only for the current session; never steals focus.
			if (event.sessionId !== this.current?.id) return;
			if (this.footer.pulseGrowth()) this.tui.requestRender();
			return;
		}
		const hidden = event.sessionId !== this.current?.id;
		// Family bookkeeping runs before the turn-state gate. Delegation is
		// non-streaming (agent.run), so a delegated child never emits frames
		// of its own; the child session is already in the catalog when the
		// parent's `delegate` tool.complete lands, so that completion is the
		// real discovery trigger (one bounded refresh of the current family).
		// A turn-start frame from an unfamiliar session is the only other
		// trigger worth a scan: later frames never rescan a foreign id
		// (negative cache in family.ts).
		if (event.type === "tool.complete" && event.name === "delegate") {
			this.refreshFamily();
		} else if (hidden && !this.isFamilyMember(event.sessionId) && isTurnStart(event)) {
			this.noteUnknownSession(event.sessionId);
		}
		if (!this.inFlight.has(event.sessionId)) {
			// A session without a local turn binding (e.g. an in-flight turn
			// from the server side): preview it in the hidden session's tail;
			// terminal dedupe lives in the tail itself.
			if (hidden) this.bufferHiddenEvent(event);
			return;
		}
		// A turn's events may arrive after the user switched sessions, so match
		// against in-flight turns, not just the current session. Non-visible
		// completion still retires and accounts; it never renders, opens
		// dialogs, or disturbs the visible session.
		const record = this.turns.get(event.sessionId);
		if (!record) return;
		const eventTurn = turnIdentity(event);
		if (event.type === "message.start") {
			// Late duplicates of a retired turn are inert. A start for the
			// bound turn applies once (whether bound by an earlier start or
			// confirmed by the submit result); repeats never reset it.
			if (record.retired.includes(event.turnId)) return;
			if (record.started) return;
			if (record.turnId !== undefined && event.turnId !== record.turnId) return;
			if (record.turnId === undefined) {
				record.turnId = event.turnId;
				record.provisional = true;
			}
			record.started = true;
			record.hadEvents = true;
		} else {
			if (record.turnId === undefined) {
				// Events arrived before any start: bind provisionally so
				// pre-RPC output and completion are retained once.
				record.turnId = eventTurn;
				record.provisional = true;
			} else if (eventTurn !== record.turnId && !this.approvalMatches(record, event)) {
				return; // stale turn: no state, text or usage mutation
			} else if (record.retired.includes(eventTurn)) {
				return; // late duplicate of a retired turn
			}
			record.hadEvents = true;
		}
		// Bound turn accepted: a hidden session keeps a live tail so switching
		// back shows the work done while it was invisible (switchTo merges it
		// with the fetched history — buffered tail wins, no duplicates).
		if (hidden) this.bufferHiddenEvent(event);
		// Retain reported output for local inspection (visible or not, but
		// only for the bound turn). Never refetched, never re-executed.
		if (event.type === "tool.complete") {
			this.outputStore.put(event.sessionId, event.id, event.name, event.result, event.truncated ?? false);
		}
		if ((event.type === "needs_approval" || event.type === "permission.required")) {
			// R4-065: a pause frame can carry a terminal status — the broker's
			// deadline (or another transport) already resolved the request.
			// That is a resolution, not a card: retire any open card and
			// retained evidence without issuing a decision the record refuses.
			if (event.status !== undefined && event.status !== "pending") {
				this.pendingApprovals.delete(event.requestId);
				const stale = this.approvalDialogs.get(event.requestId);
				stale?.card.setRecorded(`approval ${event.status}`);
				stale?.close();
				if (event.sessionId === this.current?.id) {
					this.transcript.addNotice(
						`Approval ${event.requestId.slice(0, 8)} ${event.status}.`,
						event.status === "approved" ? "success" : event.status === "denied" ? "error" : "warning",
					);
				}
				this.tui.requestRender();
				return;
			}
			// Retain the review evidence even while its session is hidden
			// (R15); the card itself opens only in the bound visible session
			// and never carries a dialog into another session (T7 owns the
			// review card/queue). A later return rebuilds it via
			// restoreApprovals.
			this.pendingApprovals.set(event.requestId, event);
			if (event.sessionId !== this.current?.id) return;
			// Approval card bound to this exact session/turn — switching
			// sessions never approves the wrong session (Phase 5.6).
			void this.promptApproval(event);
			this.tui.requestRender();
			return;
		}
		if ((event.type === "approval.resolved" || event.type === "permission.resolved")) {
			// The decision is durable regardless of which session is visible.
			this.pendingApprovals.delete(event.requestId);
			if (event.sessionId !== this.current?.id) return;
			const pending = this.approvalDialogs.get(event.requestId);
			pending?.card.setRecorded(`approval ${event.status}`);
			pending?.close();
			this.transcript.addNotice(
				`Approval ${event.requestId.slice(0, 8)} ${event.status}.`,
				event.status === "approved" ? "success" : event.status === "denied" ? "error" : "warning",
			);
			this.tui.requestRender();
			return;
		}
		if (event.type === "approval.resumed") {
			// The review is no longer pending; the approved action continues
			// under a fresh bound turn.
			this.pendingApprovals.delete(event.requestId);
			if (event.originTurnId === record.turnId) {
				if (record.turnId && !record.retired.includes(record.turnId)) {
					record.retired.push(record.turnId);
					if (record.retired.length > 20) record.retired.shift();
				}
				record.turnId = event.turnId;
				record.provisional = false;
				record.started = true;
				record.usageLatest = 0;
				if (event.sessionId === this.current?.id) this.transcript.apply({ ...event, type: "message.start" });
			}
			if (event.sessionId !== this.current?.id) return;
			this.transcript.addNotice(`Approval ${event.requestId.slice(0, 8)} resumed.`);
			this.tui.requestRender();
			return;
		}
		if (event.type === "turn.started") {
			this.tui.requestRender();
			return;
		}
		if (event.type === "turn.cleanup_pending" || event.type === "turn.ownership_lost") {
			if (event.sessionId !== this.current?.id) return;
			this.transcript.suspend("cleanup_pending");
			this.footer.status = "cleanup_pending";
			this.transcript.addNotice(event.type === "turn.cleanup_pending"
				? "Stopping: waiting for the running action to finish cleanup."
				: "Turn ownership was lost; stopping the running action.", "warning");
			this.tui.requestRender();
			return;
		}
		if (event.type === "usage") {
			record.usageLatest = event.tokens;
			if (event.sessionId === this.current?.id) this.footer.tokens = this.sessionTokens + event.tokens;
			this.tui.requestRender();
			return;
		}
		if (event.sessionId !== this.current?.id) {
			if (event.type === "message.complete") {
				this.accountCompletion(event.sessionId, event.tokens);
				this.retireTurn(event.sessionId, event.turnId);
				this.clearApprovals(event.sessionId);
			}
			// AH-032: never clear in-flight on "error" — the terminal
			// message.complete still follows with final accounting and
			// tool-card cleanup. Clearing early drops that completion.
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			this.tui.requestRender();
			return;
		}
		if (event.type === "message.stopping") this.footer.status = "stopping";
		if (event.type === "message.complete") this.clearApprovals(event.sessionId);
		const summary = this.transcript.apply(event);
		if (event.type === "error") {
			// Keep turn state until terminal completion (AH-032). The error
			// notice is already rendered by transcript.apply(); final
			// accounting arrives with message.complete.
			this.setRunning(true);
		} else if (summary) {
			this.accountCompletion(event.sessionId, summary.tokens);
			this.retireTurn(event.sessionId, event.turnId);
			this.footer.tokens = this.sessionTokens;
			this.setRunning(this.inFlight.has(event.sessionId));
		}
		this.tui.requestRender();
	}

	/**
	 * Approval events carry the reviewed origin turn alongside the transport
	 * turn; either may identify the bound record. needs_approval has no
	 * tool-call ID, so correlation stays at turn level only.
	 */
	private approvalMatches(record: SessionTurn, event: GatewayEvent): boolean {
		if (event.type !== "needs_approval" && event.type !== "permission.required" &&
			event.type !== "approval.resolved" && event.type !== "permission.resolved" && event.type !== "approval.resumed") return false;
		const origin = (event as { originTurnId?: string }).originTurnId;
		return origin !== undefined && origin === record.turnId;
	}

	/** Add a turn total once: duplicate or late completion adds zero. */
	private accountCompletion(sessionId: string, tokens: number): void {
		if (sessionId === this.current?.id) {
			this.sessionTokens += tokens;
			this.sessionTokensById.set(sessionId, this.sessionTokens);
			return;
		}
		this.sessionTokensById.set(sessionId, (this.sessionTokensById.get(sessionId) ?? 0) + tokens);
	}

	/**
	 * Exact approval review: capture the immutable event binding, open a
	 * dedicated review card for the bound visible session, and resolve once
	 * through approvals.resolve with the supported session binding. Repeat
	 * events for an open request are ignored; navigation, exit and
	 * disconnect invalidate the local prompt without any resolve RPC.
	 */
	private promptApproval(event: Extract<GatewayEvent, { type: "needs_approval" | "permission.required" }>): void {
		const binding = {
			requestId: event.requestId,
			sessionId: event.sessionId,
			originTurnId: event.originTurnId,
			transportTurnId: event.turnId,
		};
		if (this.approvalDialogs.has(binding.requestId)) return;
		const fileDiff = event.fileDiff && typeof event.fileDiff === "object"
			? event.fileDiff as { diff?: unknown; truncated?: unknown }
			: undefined;
		const card = new ApprovalCard(
			{
				requestId: event.requestId,
				sessionId: event.sessionId,
				originTurnId: event.originTurnId,
				transportTurnId: event.turnId,
				operation: event.operation,
				tool: event.tool,
				target: event.target,
				argv: event.argv ?? [],
				cwd: event.cwd,
				backend: event.backend,
				shell: event.shell,
				timeout: event.timeout,
				capabilities: event.capabilities ?? [],
				network: event.network,
				contentDigest: event.contentDigest,
				contentPreview: event.contentPreview,
				contentLength: event.contentLength,
				fileDiff: fileDiff && typeof fileDiff.diff === "string"
					? { diff: fileDiff.diff, truncated: fileDiff.truncated === true }
					: undefined,
				durable: event.durable,
				status: event.status,
			},
			{
				tui: this.tui,
				docked: true,
				onDecision: (decision) => void this.resolveApproval(binding.requestId, decision),
			},
		);
		this.transcript.pauseForApproval();
		let finish: () => void = () => {};
		const dialog = new Promise<void>((done) => { finish = done; });
		let settled = false;
		const close = () => {
			if (settled) return;
			settled = true;
			this.approvalDialogs.delete(binding.requestId);
			finish();
			this.showApprovalDock();
		};
		this.approvalDialogs.set(binding.requestId, { ...binding, card, evidenceAdded: false, close, dialog });
		this.showApprovalDock();
		this.tui.requestRender();
	}

	private showApprovalDock(): void {
		const next = this.approvalDialogs.values().next().value;
		if (next && !next.evidenceAdded) {
			this.transcript.addComponent(next.card.evidence);
			next.evidenceAdded = true;
		}
		this.shell.setDock(next?.card);
		if (!isViewportTUI(this.tui)) {
			// Classic test/embedded TUIs have no layout engine; retain their normal focus path.
			this.tui.setFocus(next?.card ?? this.editor);
		}
		if (next) {
			for (const dialog of this.dialogs) dialog.handle.setHidden(true);
			this.footer.status = "awaiting_approval";
			this.shell.transcriptScroll.scrollToEnd();
			this.tui.setFocus(next.card);
		} else {
			const previous = this.dialogs.at(-1);
			if (previous) { previous.handle.setHidden(false); previous.handle.focus(); }
			// Releasing the last approval returns focus to the composer
			// (finding 3) through the region router, so the editor-scoped gates
			// read the region that actually owns keys.
			else if (!this.exiting) this.setFocusForRegion("chat-composer");
			if (this.footer.status === "awaiting_approval") this.footer.status = this.running ? "working" : "ready";
		}
		this.syncCursorBlink();
		this.tui.requestRender();
	}

	/** Dispatch one deliberate decision with the captured session binding. */
	private async resolveApproval(requestId: string, decision: ApprovalDecision): Promise<void> {
		const entry = this.approvalDialogs.get(requestId);
		if (!entry) return;
		// Revalidate immediately before dispatch: wrong-session, retired-turn
		// and post-navigation decisions never leave this UI flow.
		const record = this.turns.get(entry.sessionId);
		const bound = record !== undefined
			&& this.inFlight.has(entry.sessionId)
			&& entry.sessionId === this.current?.id
			&& (record.turnId === entry.transportTurnId || (entry.originTurnId !== undefined && record.turnId === entry.originTurnId));
		if (!bound) {
			entry.close();
			this.pendingApprovals.delete(requestId);
			return;
		}
		entry.card.setResolving(true);
		this.tui.requestRender();
		try {
			await this.client.request("approvals.resolve", {
				requestId: entry.requestId,
				sessionId: entry.sessionId,
				verdict: decision.verdict,
				grant: decision.grant,
			});
		} catch (error) {
			entry.card.setError(error instanceof Error ? error.message : String(error));
			this.tui.requestRender();
			return;
		}
		// Decision recorded; execution is proven only by later tool events.
		entry.card.setRecorded(`approval ${decision.verdict}`);
		entry.close();
		this.pendingApprovals.delete(requestId);
		this.tui.requestRender();
	}

	/** Invalidate pending approval cards without resolving (navigation/exit/disconnect). */
	private invalidateApprovals(sessionId?: string): void {
		for (const [requestId, entry] of [...this.approvalDialogs]) {
			if (sessionId === undefined || entry.sessionId !== sessionId) {
				entry.close();
				this.approvalDialogs.delete(requestId);
			}
		}
		// Navigation retains evidence so the card can be rebuilt on return
		// (R15); exit and disconnect drop it: nothing can be decided after.
		if (sessionId === undefined) this.pendingApprovals.clear();
	}

	/** Retire approval evidence and cards for a session whose turn ended. */
	private clearApprovals(sessionId: string): void {
		for (const [requestId, event] of [...this.pendingApprovals]) {
			if (event.sessionId === sessionId) this.pendingApprovals.delete(requestId);
		}
		for (const entry of [...this.approvalDialogs.values()]) {
			if (entry.sessionId === sessionId) {
				entry.card.setRecorded("request ended");
				entry.close();
			}
		}
	}

	/** Rebuild review cards retained while their session was hidden (R15). */
	private restoreApprovals(sessionId: string): void {
		if (!this.inFlight.has(sessionId)) return;
		for (const [requestId, event] of [...this.pendingApprovals]) {
			if (event.sessionId !== sessionId || this.approvalDialogs.has(requestId)) continue;
			void this.promptApproval(event);
		}
	}

	/**
	 * F-30: pending approvals are parked server-side in the permission store;
	 * a session hidden while its approval arrived never received the event, so
	 * returning to it re-reads the pending rows and reopens decision-capable
	 * cards. Rows are the same redacted public view the wire event is shaped
	 * from (`machina/protocol.approval_fields`), and the synthesized event
	 * mirrors that mapping exactly. Best-effort: a failed or stale re-read
	 * keeps whatever retained prompts already show and never resolves
	 * anything by itself.
	 */
	private async rehydrateApprovals(sessionId: string): Promise<void> {
		if (this.exiting || this.current?.id !== sessionId || !this.inFlight.has(sessionId)) return;
		const result = await this.client
			.request<{ approvals?: unknown }>("approvals.list", { sessionId })
			.catch(() => undefined);
		// Bind the answer to the session, not the moment the read started: a
		// return that has since navigated away publishes nothing.
		if (result === undefined || this.exiting || this.current?.id !== sessionId || !this.inFlight.has(sessionId)) return;
		const rows = Array.isArray(result.approvals) ? result.approvals : [];
		for (const row of rows) {
			const event = approvalRowEvent(sessionId, row);
			if (!event) continue;
			// Already-live or retained prompts own their card; only rows this
			// frame never saw need synthesis.
			if (this.approvalDialogs.has(event.requestId) || this.pendingApprovals.has(event.requestId)) continue;
			this.pendingApprovals.set(event.requestId, event);
			this.promptApproval(event);
		}
	}

	private onGatewayExit(code: number | null, stderr: string[]): void {
		if (this.exiting) return;
		// MCH-012: the transport is gone — in-flight workspace opens publish
		// nothing (their teardown RPCs cannot be trusted either).
		this.beginWorkspaceFence();
		this.footer.dispose();
		this.transcript.suspend();
		// The gateway is gone: clear workspace timers/mirrors without RPC
		// expectations (every teardown call is bounded and fails fast).
		void this.integration?.controller.dispose();
		// Retire local approval prompts without resolving: navigation and
		// disconnect invalidation never issues surprise resolve RPCs.
		this.invalidateApprovals();
		// Clear all in-flight tracking so the session isn't permanently locked.
		for (const sessionId of [...this.inFlight]) {
			this.clearInFlight(sessionId);
		}
		this.setRunning(false);
		this.footer.status = "offline";
		this.transcript.addNotice(`The gateway stopped${code === null ? "" : ` (exit code ${code})`}.`, "error");
		const tail = stderr.slice(-8);
		if (tail.length) this.transcript.addNotice(tail.join("\n"));
		this.transcript.addNotice("Press Ctrl+C to exit, then run `machina doctor`.");
		this.tui.requestRender();
	}
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/**
 * True when a slash command embeds a raw secret that must never reach the
 * editor's input history.
 *
 * `/keys set KEY VALUE` is the documented secret form. `/provider add <name>
 * <url> <key>` is NOT: `addProvider` (features/settings.ts) reads only the
 * name and URL and always prompts for the key through a masked editor, so a
 * third token is ignored — but a user may still type it, and it would
 * otherwise be stashed in the session's recalled history. Both subcommands are
 * matched case-insensitively (splitSub lowercases the head).
 */
export function commandCarriesSecret(command: ParsedCommand | undefined): boolean {
	if (!command) return false;
	const [sub, rest] = splitSub(command.args);
	if (command.name === "keys") return sub === "set";
	return command.name === "provider" && sub === "add" && rest.split(/\s+/).filter(Boolean).length > 2;
}

/** Routing identity for an event: the transport turn (origin for approvals is matched separately). */
function turnIdentity(event: GatewayEvent): string {
	return event.turnId;
}

/** Turn-start frames: the first event of a turn in a session's stream. */
function isTurnStart(event: GatewayEvent): boolean {
	return event.type === "message.start" || event.type === "turn.started";
}

type ApprovalPromptEvent = Extract<GatewayEvent, { type: "needs_approval" | "permission.required" }>;

/**
 * Synthesize the wire `needs_approval` event for a stored pending row.
 *
 * `approvals.list` returns `permission_approvals.public_view` rows; the
 * gateway's live event is built from the same record by
 * `machina/protocol.approval_fields`, so the mapping below mirrors it:
 * top-level request/operation/target/status plus the proposal fields
 * (argv/shell/cwd/backend/capabilities/network/content_*). The row's
 * `turn_id` is the reviewed origin turn and doubles as the transport turn for
 * the synthesized frame, which is what makes the restored card resolvable
 * against the live turn binding.
 */
function approvalRowEvent(sessionId: string, row: unknown): ApprovalPromptEvent | undefined {
	if (!row || typeof row !== "object") return undefined;
	const record = row as Record<string, unknown>;
	const text = (value: unknown): string => (typeof value === "string" ? value : "");
	const list = (value: unknown): string[] => (
		Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []
	);
	const requestId = text(record.request_id);
	if (!requestId) return undefined;
	const proposal = record.proposal && typeof record.proposal === "object" && !Array.isArray(record.proposal)
		? record.proposal as Record<string, unknown>
		: {};
	const turnId = text(record.turn_id);
	return {
		protocolVersion: 2,
		type: "needs_approval",
		sessionId,
		turnId,
		requestId,
		operation: text(record.operation) || text(proposal.operation),
		tool: text(proposal.tool),
		target: text(record.target) || list(proposal.targets).join("|"),
		argv: list(proposal.argv),
		shell: text(proposal.shell),
		cwd: text(proposal.cwd),
		backend: text(proposal.backend),
		timeout: typeof proposal.timeout === "number" ? proposal.timeout : 60,
		network: list(proposal.network),
		capabilities: list(proposal.capabilities),
		contentDigest: text(proposal.content_digest),
		contentPreview: text(record.content_preview) || text(proposal.content_preview),
		contentLength: typeof proposal.content_length === "number" ? proposal.content_length : 0,
		fileDiff: record.file_diff && typeof record.file_diff === "object"
			? record.file_diff as Record<string, unknown>
			: {},
		durable: typeof record.durable === "boolean" || record.durable === null ? record.durable : undefined,
		status: text(record.status) || "pending",
		originTurnId: turnId,
	};
}
