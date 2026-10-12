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
import { parseCommand } from "./commands.ts";
import { ComposerBox, Footer, header, Picker, SecretForm, TextForm, ThinkingIndicator } from "./components.ts";
import { Menu } from "./components/Menu.ts";
import { OAuthProgress } from "./components/OAuthProgress.ts";
import { ApprovalCard, type ApprovalDecision } from "./components/ApprovalCard.ts";
import { AppShell } from "./components/AppShell.ts";
import { OutputStore, OutputViewer, type OutputRecord } from "./components/OutputViewer.ts";
import { AnimationCoordinator } from "./design/motion.ts";
import { type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "./features/index.ts";
import { type AskTextOptions, PROFILE_ID_RE, type OAuthProgressHandle, type OAuthProgressOptions } from "./features/types.ts";
import type { GatewayClient } from "./gateway.ts";
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
	/** Timers for the local watchdog (diagnostic deadline, not ownership). */
	private readonly inFlightTimers = new Map<string, NodeJS.Timeout>();
	/** The session ID of the most recent in-flight turn (for cancel). */
	private inFlightSessionId: string | undefined;
	private sessionTokens = 0;
	/** Preserved token counts per session so switchTo does not lose them. */
	private readonly sessionTokensById = new Map<string, number>();
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
	private readonly dialogs: Array<{ handle: OverlayHandle; cancel: () => void }> = [];
	private exiting = false;
	private resolveExit: () => void = () => {};
	private inputListenerDisposer: (() => void) | undefined;

	// Match the server turn timeout (turn_timeout=300s) so the UI does not
	// time out and unlock while the server is still running the turn (which
	// would surface as a confusing TURN_IN_PROGRESS on the next submit).
	private static readonly IN_FLIGHT_TIMEOUT_MS = 300_000;

	constructor(tui: TUI, client: GatewayClient, options: AppOptions = {}) {
		this.tui = tui;
		this.client = client;
		this.options = options;
		this.exited = new Promise((done) => {
			this.resolveExit = done;
		});

		const reducedMotion = /^(1|true)$/i.test(process.env.AH_UI_REDUCED_MOTION ?? "");
		this.coordinator = new AnimationCoordinator(() => this.tui.requestRender(), reducedMotion);
		this.footer = new Footer(this.coordinator);
		this.transcript = new Transcript(this.transcriptView, new ThinkingIndicator(this.coordinator), this.coordinator);

		this.composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
		this.editor = this.composer.editor;
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
		this.transcript.addNotice("Starting AgentHarness…");
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
				const { mode } = await this.client.request<{ mode: string }>("mode.get", {}, 30_000);
				if (typeof mode === "string") this.footer.mode = mode;
				if (mode === "full") {
					this.transcript.addNotice("FULL HOST mode active — ordinary host operations proceed; credentials/elevation/destructive ops still ask.", "warning");
				}
			} catch {
				// Mode display is best-effort.
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
		} catch (error) {
			this.footer.status = "offline";
			this.transcript.clear();
			this.transcript.addNotice(`Could not start: ${message(error)}`, "error");
			const tail = this.client.stderr().slice(-8);
			if (tail.length) this.transcript.addNotice(tail.join("\n"));
			this.transcript.addNotice("Run `ah doctor` to check your setup. Press Ctrl+C to exit.");
		}
		this.tui.requestRender();
	}

	// ─── FeatureHost ─────────────────────────────────────────────────────────
	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		const result = await this.client.request<T>(method, params, 120_000);
		if (["mode.get", "mode.set", "mode.revoke"].includes(method)) {
			const mode = (result as { mode?: string }).mode;
			if (typeof mode === "string") this.footer.mode = mode;
			this.tui.requestRender();
		}
		return result;
	}

	print(text: string, kind: NoticeKind = "info"): void {
		this.transcript.addNotice(text, kind);
		this.tui.requestRender();
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
		if (this.current) this.sessionTokensById.set(this.current.id, this.sessionTokens);
		this.updateSession(session);
		this.transcript.clear();
		this.transcriptView.addChild(header(this.version, this.headerInfo(session)));
		this.transcript.replay(history);
		this.sessionTokens = this.sessionTokensById.get(session.id) ?? 0;
		// Preserve the active turn's latest cumulative usage across switches
		// without mixing turns: completed total plus this session's turn total.
		this.footer.tokens = this.sessionTokens + (this.turns.get(session.id)?.usageLatest ?? 0);
		this.setRunning(this.inFlight.has(session.id));
		this.tui.requestRender();
	}

	updateSession(session: SessionInfo): void {
		if (session.id !== this.current?.id) {
			this.footer.dispose();
			// A pending request may reopen only when the user returns to its
			// owning session; never carry a selection into the new session.
			this.invalidateApprovals(session.id);
		}
		this.current = session;
		this.tui.requestRender();
	}

	pick(title: string, items: SelectItem[]): Promise<SelectItem | undefined> {		// Row budget follows the live viewport: title + filter + hint rows are
		// reserved so the selected row and cancel hint survive short windows.
		const rows = this.tui.terminal.rows ?? 24;
		const visibleRows = Math.max(1, Math.min(10, rows - 5 - 3));
		return this.openDialog<SelectItem>((close) => new Picker(title, items, close, () => close(), visibleRows, undefined, this.tui));
	}

	pickMany(title: string, items: SelectItem[]): Promise<SelectItem[] | undefined> {
		return this.openDialog<SelectItem[]>((close) => {
			const menu = new Menu(title, items, { multiSelect: true, tui: this.tui, visibleRows: 12 });
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
		const rows = this.tui.terminal.rows ?? 24;
		const visibleRows = Math.max(1, Math.min(10, rows - 5 - 4));
		return this.openDialog<string>((close) => {
			const menu = new Menu(
				`Skins (current: ${previous})`,
				SKIN_NAMES.map((skin) => ({
					value: skin,
					label: `${skin}${skin === previous ? " (current)" : ""}`,
					description: skinDescription(skin),
				})),
				{ visibleRows, hint: "↑↓ preview · enter apply · esc restore", tui: this.tui, initialValue: previous },
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
				onCancel: () => {
					if (settled) return;
					settled = true;
					options.onCancel();
					close("cancel");
				},
			}, options.loginUrl);
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
		return this.openDialog<void>((close) => new OutputViewer(record, { tui: this.tui, onClose: () => close() }), 2).then(
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

	private openDialog<T>(create: (close: (value?: T) => void) => Component, marginBottom = 5): Promise<T | undefined> {
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
			const handle = this.tui.showOverlay(create(close), {
				width: "100%",
				maxHeight: "70%",
				anchor: "bottom-center",
				margin: { bottom: marginBottom },
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
		await writeFile(full, text, "utf8");
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
		this.transcriptView.addChild(header(this.version, this.headerInfo(this.current)));
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
	private onKey(data: string): { consume: boolean } | undefined {
		this.shell.sync();
		if (this.dialogs.length || this.approvalDialogs.size) return undefined; // focused dialogs own their keys

		const questionKey = matchesKey(data, Key.question) || matchesKey(data, Key.shift("?")) || matchesKey(data, Key.shift("/"));
		if (matchesKey(data, Key.f1) || (questionKey && !this.editor.getText() && !this.editor.isShowingAutocomplete())) {
			void this.openDialog((close) => new Picker("Keyboard shortcuts", [
				{ value: "send", label: "Enter — send", description: "Send a message or run a command" },
				{ value: "newline", label: "Shift+Enter — new line" },
				{ value: "stop", label: "Esc — stop reply", description: "Twice on empty input edits the last prompt" },
				{ value: "cancel", label: "Ctrl+C — stop / clear / exit", description: "Stop a reply, clear a draft, or exit when empty" },
				{ value: "exit", label: "Ctrl+D — exit", description: "When input is empty and idle" },
				{ value: "clear", label: "Ctrl+L — clear chat", description: "When idle" },
				{ value: "complete", label: "Tab — complete", description: "Commands and arguments" },
				{ value: "help", label: "? / F1 — shortcuts", description: "? on empty input, F1 with a draft" },
				{ value: "commands", label: "/help — all commands", description: "/growth shows learning details" },
				{ value: "learning", label: "□ / ▪ — activity and learning", description: "Pulses while working and briefly when learning is saved" },
			], () => close(), () => close(), Math.max(1, Math.min(8, Math.floor((this.tui.terminal.rows ?? 24) * 0.7) - 3)), "↑↓ browse · enter/esc close", this.tui), 2);
			return { consume: true };
		}

		if (matchesKey(data, Key.ctrl("c"))) {
			if (this.running) this.cancel();
			else if (this.editor.getText()) this.editor.setText("");
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
		const text = raw.trim();
		if (!text) return;

		// AH-031: parse FIRST, then guard only conflicting operations.
		// Read-only / navigation commands must work during a turn; only
		// new prompts and session-mutating commands on the in-flight
		// session are rejected.
		const command = parseCommand(text);
		const inFlight = this.current && this.inFlight.has(this.current.id);
		if (inFlight && !command) {
			this.transcript.addNotice("The previous turn is still finishing. Try again shortly.", "warning");
			this.tui.requestRender();
			return;
		}
		if (inFlight && command && CONFLICTING_COMMANDS.has(command.name)) {
			this.transcript.addNotice("Stop the running reply before changing this session.", "warning");
			this.tui.requestRender();
			return;
		}

		this.editor.setText("");

		// Never keep secrets in input history: /keys set carries a raw value.
		const secret = command?.name === "keys" && command.args.startsWith("set");
		if (!secret) this.editor.addToHistory(text);
		if (command) {
			await runCommand(command, this);
		} else {
			this.lastPrompt = text;
			try {
				await this.submitTurn(text);
			} catch (error) {
				// Restore the draft on failure. When the guard was released
				// the failure preceded acceptance and retry is meaningful;
				// when it is retained the dispatch outcome is unknown, so say
				// so instead of implying a retry is safe.
				const sessionId = this.current?.id;
				const unknownDispatch = sessionId ? this.inFlight.has(sessionId) : false;
				if (!this.editor.getText()) this.editor.setText(text);
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

	async submitTurn(text: string): Promise<void> {
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
		this.inFlightSessionId = sessionId;
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
			const result = await this.client.request<{ turnId?: string }>("prompt.submit", { sessionId, text });
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
		if (this.inFlightSessionId === sessionId) {
			this.inFlightSessionId = undefined;
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
		this.shell.sync();
		if (event.type === "growth.status") {
			// Background training lands after the turn retires (and the
			// in-flight entry clears), so it bypasses turn tracking. Shown
			// only for the current session; never steals focus.
			if (event.sessionId !== this.current?.id) return;
			if (this.footer.pulseGrowth()) this.tui.requestRender();
			return;
		}
		// A turn's events may arrive after the user switched sessions, so match
		// against in-flight turns, not just the current session. Non-visible
		// completion still retires and accounts; it never renders, opens
		// dialogs, or disturbs the visible session.
		if (!this.inFlight.has(event.sessionId)) return;
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
		// Retain reported output for local inspection (visible or not, but
		// only for the bound turn). Never refetched, never re-executed.
		if (event.type === "tool.complete") {
			this.outputStore.put(event.sessionId, event.id, event.name, event.result, event.truncated ?? false);
		}
		if ((event.type === "needs_approval" || event.type === "permission.required")) {
			// Approval review only in the bound visible session; never carry
			// a dialog into another session (T7 owns the review card/queue).
			if (event.sessionId !== this.current?.id) return;
			// Approval card bound to this exact session/turn — switching
			// sessions never approves the wrong session (Phase 5.6).
			void this.promptApproval(event);
			this.tui.requestRender();
			return;
		}
		if ((event.type === "approval.resolved" || event.type === "permission.resolved")) {
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
			// The approved action continues under a fresh bound turn.
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
			}
			// AH-032: never clear in-flight on "error" — the terminal
			// message.complete still follows with final accounting and
			// tool-card cleanup. Clearing early drops that completion.
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			this.tui.requestRender();
			return;
		}
		if (event.type === "message.stopping") this.footer.status = "stopping";
		if (event.type === "message.complete") {
			for (const entry of [...this.approvalDialogs.values()]) {
				if (entry.sessionId === event.sessionId) {
					entry.card.setRecorded("request ended");
					entry.close();
				}
			}
		}
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
		this.transcript.addNotice("Press Ctrl+C to exit, then run `ah doctor`.");
		this.tui.requestRender();
	}
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/** Routing identity for an event: the transport turn (origin for approvals is matched separately). */
function turnIdentity(event: GatewayEvent): string {
	return event.turnId;
}

/**
 * Commands that mutate the in-flight session or start new work and must wait
 * for the running turn (AH-031). Everything else (/sessions, /new, /exit,
 * /help, /status, …) stays usable during a turn.
 */
const CONFLICTING_COMMANDS = new Set([
	"compress",
	"delete",
	"fork",
	"rename",
	"goal",
	"delegate",
]);
