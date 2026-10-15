// The interactive app: transcript, editor and footer on a pi-tui alt screen,
// driven by the Python gateway. Slash commands live in features.ts.

import { mkdir, writeFile } from "node:fs/promises";
import { userInfo } from "node:os";
import { dirname, resolve } from "node:path";
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
import { Menu } from "./components/Menu.ts";
import { OAuthProgress } from "./components/OAuthProgress.ts";
import { ApprovalCard, type ApprovalDecision } from "./components/ApprovalCard.ts";
import { AppShell } from "./components/AppShell.ts";
import { OutputStore, OutputViewer, type OutputRecord } from "./components/OutputViewer.ts";
import { AnimationCoordinator } from "./design/motion.ts";
import { type FamilySession, type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "./features/index.ts";
import { guardSubmit } from "./commandDispatch.ts";
import { type AskTextOptions, PROFILE_ID_RE, type OAuthProgressHandle, type OAuthProgressOptions } from "./features/types.ts";
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
import { SessionTailStore, tailEntries, trimSupersededHistory, type SessionTail, type TailBlock } from "./tail.ts";
import { editorTheme, getSkin, setSkin, SKIN_NAMES, skinDescription } from "./theme.ts";
import { Transcript } from "./transcript.ts";

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

		const reducedMotion = /^(1|true)$/i.test(process.env.MACHINA_UI_REDUCED_MOTION ?? "");
		this.coordinator = new AnimationCoordinator(() => this.tui.requestRender(), reducedMotion);
		this.footer = new Footer(this.coordinator);
		this.transcript = new Transcript(this.transcriptView, new ThinkingIndicator(this.coordinator), this.coordinator);

		this.composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
		this.editor = this.composer.editor;
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
		this.editor.setAutocompleteProvider(new ArgumentAwareAutocompleteProvider(SLASH_COMMANDS, process.cwd()));
		this.editor.setAutocompleteMaxVisible(8);
		this.editor.onSubmit = (text) => void this.submit(text);
	}

	async start(): Promise<void> {
		// Clear vacated rows when content shrinks (closed menus, finished
		// turns). Without this, filled rows linger as ghost bands behind
		// newer output.
		this.tui.setClearOnShrink(true);
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
		this.inputListenerDisposer = this.tui.addInputListener((data) => this.onKey(data));

		this.footer.cwd = process.cwd();
		this.footer.status = "offline";
		this.transcript.addNotice("Starting Machina…");
		this.tui.start();

		this.client.onEvent = (event) => this.onEvent(event);
		this.client.onExit = (code, stderr) => this.onGatewayExit(code, stderr);
		this.client.start();

		try {
			const init = await this.client.request<InitializeResult>(
				"initialize",
				{ protocolVersion: 2, model: this.options.model, provider: this.options.provider, mode: this.options.mode },
				60_000,
			);
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

	onGatewayEvent(handler: (event: GatewayEvent) => void): () => void {
		this.featureEventHandlers.add(handler);
		return () => {
			this.featureEventHandlers.delete(handler);
		};
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
		// The overlay is full-screen: Menu derives its row budget from the
		// live viewport with its own chrome rows (plus one slack row) reserved.
		return this.openDialog<SelectItem>((close) => new Picker(title, items, close, () => close(), undefined, undefined, this.tui));
	}

	pickMany(title: string, items: SelectItem[]): Promise<SelectItem[] | undefined> {
		return this.openDialog<SelectItem[]>((close) => {
			const menu = new Menu(title, items, { multiSelect: true, tui: this.tui });
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
				{ hint: "↑↓ preview · enter apply · esc restore", tui: this.tui, initialValue: previous },
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
		if (options.secret) {
			return this.openDialog<string>((close) => {
				const form = new SecretForm(this.tui, {
					title: options.title,
					description: options.description,
					placeholder: options.placeholder ?? "••••••••",
					allowEmpty: options.allowEmpty,
				});
				form.onSubmit = close;
				form.onCancel = () => close();
				return form;
			});
		}
		return this.openDialog<string>((close) => {
			const form = new TextForm(this.tui, editorTheme, options);
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
				dialog.handle.hide();
				this.dialogs.splice(this.dialogs.indexOf(dialog), 1);
				const previous = this.dialogs.at(-1);
				if (this.approvalDialogs.size) this.showApprovalDock();
				else if (previous) { previous.handle.setHidden(false); previous.handle.focus(); }
				else if (!this.exiting) this.tui.setFocus(this.editor);
				this.tui.requestRender();
				done(value);
			};
			// Each dialog owns its handle, including when approvals interrupt a form.
			// Full-screen sheet: 100% height with no bottom margin, so the overlay
			// is bounded only by the terminal and fills whatever the component
			// renders up to the viewport.
			const handle = this.tui.showOverlay(create(close), {
				width: "100%",
				maxHeight: "100%",
				anchor: "bottom-center",
			});
			const dialog = { handle, cancel: () => close() };
			this.dialogs.push(dialog);
			// Late management RPCs may finish after an approval arrives. Preserve
			// their draft invisibly until the bound decision releases focus.
			if (this.approvalDialogs.size) { handle.setHidden(true); this.showApprovalDock(); }
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
		this.exiting = true;
		this.footer.dispose();
		for (const timer of this.inFlightTimers.values()) clearTimeout(timer);
		this.inFlightTimers.clear();
		this.transcript.suspend();
		this.invalidateApprovals();
		this.coordinator.dispose();
		for (const dialog of [...this.dialogs].reverse()) dialog.cancel();
		this.inputListenerDisposer?.();
		this.tui.stop();
		await this.client.stop();
		this.resolveExit();
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

	private onKey(data: string): { consume: boolean } | undefined {
		this.shell.sync();
		if (this.dialogs.length || this.approvalDialogs.size) return undefined; // focused dialogs own their keys

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
			void this.openDialog((close) => new Picker("Keyboard shortcuts", [
				{ value: "send", label: "Enter — send", description: "Send a message or run a command" },
				{ value: "newline", label: "Shift+Enter — new line" },
                { value: "image", label: "Drop an image / Ctrl+V — paste image", description: "PNG, JPEG, GIF or WebP · ⌘V where forwarded by your terminal" },
				{ value: "stop", label: "Esc — stop reply", description: "Twice on empty input edits the last prompt" },
				{ value: "cancel", label: "Ctrl+C — stop / clear / exit", description: "Stop a reply, clear a draft, or exit when empty" },
				{ value: "exit", label: "Ctrl+D — exit", description: "When input is empty and idle" },
				{ value: "clear", label: "Ctrl+L — clear chat", description: "When idle" },
				{ value: "complete", label: "Tab — complete", description: "Commands and arguments" },
				{ value: "subagents", label: "↓ — subagent menu", description: "On empty input: switch between the main session and its subagents" },
				{ value: "tabs", label: "Ctrl+Shift+←/→ — switch agents", description: "Cycle the main session and its subagents" },
				{ value: "help", label: "? / F1 — shortcuts", description: "? on empty input, F1 with a draft" },
				{ value: "commands", label: "/help — all commands", description: "/growth shows learning details" },
				{ value: "learning", label: "□ / ▪ — activity and learning", description: "Pulses while working and briefly when learning is saved" },
			], () => close(), () => close(), undefined, "↑↓ browse · enter/esc close", this.tui));
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
			else if (!this.exiting) this.tui.setFocus(this.editor);
			if (this.footer.status === "awaiting_approval") this.footer.status = this.running ? "working" : "ready";
		}
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

	private onGatewayExit(code: number | null, stderr: string[]): void {
		if (this.exiting) return;
		this.footer.dispose();
		this.transcript.suspend();
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
