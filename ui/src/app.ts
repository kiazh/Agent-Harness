// The interactive app: transcript, editor and footer on a pi-tui alt screen,
// driven by the Python gateway. Slash commands live in features.ts.

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import {
	CombinedAutocompleteProvider,
	Container,
	type Editor,
	isViewportTUI,
	Key,
	Loader,
	matchesKey,
	type OverlayHandle,
	type SelectItem,
	type TUI,
} from "@earendil-works/pi-tui";
import { parseCommand } from "./commands.ts";
import { ComposerBox, createLayout, Footer, header, type LayoutRoot, Picker } from "./components.ts";
import { type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "./features/index.ts";
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
import { editorTheme, setSkin, theme } from "./theme.ts";
import { Transcript } from "./transcript.ts";

export interface AppOptions {
	model?: string;
	provider?: string;
	sessionId?: string;
	mode?: string;
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
	private readonly footer = new Footer();
	/**
	/**
	 * Codex-style bottom-docked layout: the transcript scrolls in the
	 * flexible area while the composer and footer stay pinned to the
	 * bottom rows. Built in the constructor once the children exist.
	 */
	private readonly layoutRoot: LayoutRoot;
	private lastPrompt = "";
	private lastEscAt = 0;
	private version = "";
	private current: SessionInfo | undefined;
	private running = false;
	/** Session ids of turns that are still in flight (prompt submitted, no message.complete yet). */
	private readonly inFlight = new Set<string>();
	/** Timers that auto-clear stale in-flight entries to prevent permanent locks. */
	private readonly inFlightTimers = new Map<string, NodeJS.Timeout>();
	/** The session ID of the most recent in-flight turn (for cancel). */
	private inFlightSessionId: string | undefined;
	private sessionTokens = 0;
	/** Preserved token counts per session so switchTo does not lose them. */
	private readonly sessionTokensById = new Map<string, number>();
	private overlay: OverlayHandle | undefined;
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

		const spinner = new Loader(tui, theme.accent, theme.muted, "Thinking…");
		spinner.setIndicator({ frames: ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"], intervalMs: 80 });
		spinner.stop();
		this.transcript = new Transcript(this.transcriptView, spinner);

		this.composer = new ComposerBox(tui, editorTheme, { paddingX: 1 });
		this.editor = this.composer.editor;
		this.layoutRoot = createLayout(this.transcriptView, this.composer, this.footer);
		this.editor.setAutocompleteProvider(new CombinedAutocompleteProvider(SLASH_COMMANDS, process.cwd()));
		this.editor.setAutocompleteMaxVisible(8);
		this.editor.onSubmit = (text) => void this.submit(text);
	}

	async start(): Promise<void> {
		// Clear vacated rows when content shrinks (closed menus, finished
		// turns). Without this, filled rows linger as ghost bands behind
		// newer output.
		this.tui.setClearOnShrink(true);
		if (isViewportTUI(this.tui)) {
			this.tui.setLayoutRoot(this.layoutRoot);
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
				if (typeof config.theme === "string" && config.theme) setSkin(config.theme);
			} catch {
				// Saved skin is best-effort; the default applies otherwise.
			}
			try {
				const { mode } = await this.client.request<{ mode: string }>("mode.get", {}, 30_000);
				if (mode === "full") {
					this.footer.model = `${this.footer.model} · FULL HOST`;
					this.transcript.addNotice("FULL HOST mode active — ordinary host operations proceed; credentials/elevation/destructive ops still ask.", "warning");
				}
			} catch {
				// Mode display is best-effort.
			}
			if (this.options.sessionId) {
				const { session, history } = await this.client.request<ResumeResult>("session.resume", {
					sessionId: this.options.sessionId,
				});
				this.switchTo(session, history);
			} else {
				const { session } = await this.client.request<SessionResult>("session.create", {});
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
	request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		return this.client.request<T>(method, params, 120_000);
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
		this.footer.tokens = this.sessionTokens;
		this.setRunning(this.inFlight.has(session.id));
		this.tui.requestRender();
	}

	updateSession(session: SessionInfo): void {
		this.current = session;
		this.tui.requestRender();
	}

	pick(title: string, items: SelectItem[]): Promise<SelectItem | undefined> {
		return new Promise((done) => {
			const close = (item?: SelectItem) => {
				this.overlay?.hide();
				this.overlay = undefined;
				this.tui.setFocus(this.editor);
				this.tui.requestRender();
				done(item);
			};
			const picker = new Picker(title, items, (item) => close(item), () => close());
			// Codex menu style: full-bleed sheet docked above the composer.
			this.overlay = this.tui.showOverlay(picker, {
				width: "100%",
				maxHeight: "70%",
				anchor: "bottom-center",
				margin: { bottom: 5 },
			});
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

	async exit(): Promise<void> {
		if (this.exiting) return;
		this.exiting = true;
		this.inputListenerDisposer?.();
		this.tui.stop();
		await this.client.stop();
		this.resolveExit();
	}

	// ─── input ────────────────────────────────────────────────────────────
	private onKey(data: string): { consume: boolean } | undefined {
		if (this.overlay) return undefined; // the picker handles its own keys

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
				this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
				this.transcript.addNotice(message(error), "error");
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
		this.startInFlightTimer(sessionId);
		this.tui.requestRender();
		try {
			await this.client.request("prompt.submit", { sessionId, text });
		} catch (error) {
			this.clearInFlight(sessionId);
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			throw error;
		}
	}

	/** Start a timer that auto-clears a stale in-flight entry to prevent permanent locks. */
	private startInFlightTimer(sessionId: string): void {
		this.inFlightTimers.get(sessionId)?.unref();
		const timer = setTimeout(() => {
			this.inFlightTimers.delete(sessionId);
			if (this.inFlight.has(sessionId)) {
				this.inFlight.delete(sessionId);
				this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
				this.transcript.addNotice("The previous turn timed out. You can submit again.", "warning");
				this.tui.requestRender();
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
		if (this.footer.status !== "offline") this.footer.status = running ? "working" : "ready";
	}

	// ─── gateway ────────────────────────────────────────────────────────────
	private onEvent(event: GatewayEvent): void {
		// A turn's events may arrive after the user switched sessions, so match
		// against in-flight turns, not just the current session.
		if (!this.inFlight.has(event.sessionId)) return;
		if ((event.type === "needs_approval" || event.type === "permission.required")) {
			// Approval card bound to this exact session/turn — switching
			// sessions never approves the wrong session (Phase 5.6).
			void this.promptApproval(event);
			this.tui.requestRender();
			return;
		}
		if ((event.type === "approval.resolved" || event.type === "permission.resolved")) {
			this.transcript.addNotice(`Approval ${event.requestId.slice(0, 8)} ${event.status}.`);
			this.tui.requestRender();
			return;
		}
		if (event.type === "turn.cleanup_pending" || event.type === "turn.ownership_lost") {
			this.transcript.addNotice(event.type === "turn.cleanup_pending"
				? "Stopping: waiting for the running action to finish cleanup."
				: "Turn ownership was lost; stopping the running action.", "warning");
			this.tui.requestRender();
			return;
		}
		if (event.sessionId !== this.current?.id) {
			if (event.type === "message.complete") {
				this.clearInFlight(event.sessionId);
			}
			// AH-032: never clear in-flight on "error" — the terminal
			// message.complete still follows with final accounting and
			// tool-card cleanup. Clearing early drops that completion.
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			this.tui.requestRender();
			return;
		}
		const summary = this.transcript.apply(event);
		if (event.type === "usage") this.footer.tokens = this.sessionTokens + event.tokens;
		if (event.type === "error") {
			// Keep turn state until terminal completion (AH-032). The error
			// notice is already rendered by transcript.apply(); final
			// accounting arrives with message.complete.
			this.setRunning(true);
		} else if (summary) {
			this.clearInFlight(event.sessionId);
			this.sessionTokens += summary.tokens;
			this.sessionTokensById.set(event.sessionId, this.sessionTokens);
			this.footer.tokens = this.sessionTokens;
			this.setRunning(this.inFlight.has(event.sessionId));
		}
		this.tui.requestRender();
	}

	/** Compact approval card: action, cwd/target, backend, grant choices. */
	private async promptApproval(event: Extract<GatewayEvent, { type: "needs_approval" | "permission.required" }>): Promise<void> {
		const short = event.sessionId.slice(0, 8);
		// AH-AUDIT-003/004: render the exact command (argv, unambiguous —
		// never a shell-looking string alone), cwd, backend, timeout, and
		// file-write content identity + diff. Secret values arrive redacted.
		const argv = (event.argv ?? []).map((a) => JSON.stringify(a)).join(" ");
		const command = argv ? `${event.operation} ${argv}` : `${event.operation} ${event.target}`;
		this.transcript.addNotice(
			`Approval needed [${short}]: ${command} (cwd: ${event.cwd}, backend: ${event.backend}, timeout: ${event.timeout ?? 60}s)`,
			"warning",
		);
		if (event.network?.length) {
			this.transcript.addNotice(`Destinations: ${event.network.join(", ")}`, "warning");
		}
		if (event.contentDigest) {
			this.transcript.addNotice(
				`Content sha256: ${event.contentDigest.slice(0, 16)}… (${event.contentLength ?? 0} chars)`,
				"warning",
			);
		}
		if (event.contentPreview) {
			this.transcript.addNotice(`Proposed content:\n${event.contentPreview}`, "warning");
		}
		if (event.fileDiff?.diff) {
			this.transcript.addNotice(
				`Diff:\n${event.fileDiff.diff}${event.fileDiff.truncated ? "\n… [truncated]" : ""}`,
				"warning",
			);
		}
		if (event.durable === false) {
			this.transcript.addNotice(
				"Approvals are local-only right now (database unavailable); cross-worker guarantees are degraded.",
				"warning",
			);
		}
		this.tui.requestRender();
		const choice = await this.pick(`Allow ${event.operation}?`, [
			{ label: "Allow once", value: "approved:once" },
			{ label: "Allow for this session", value: "approved:session" },
			{ label: "Deny", value: "denied:once" },
		]);
		const [verdict, grant] = (choice?.value ?? "denied:once").split(":");
		try {
			await this.client.request("approvals.resolve", {
				requestId: event.requestId,
				verdict,
				grant,
			});
		} catch (error) {
			this.transcript.addNotice(`Approval failed: ${error instanceof Error ? error.message : String(error)}`, "error");
		}
		this.tui.requestRender();
	}

	private onGatewayExit(code: number | null, stderr: string[]): void {
		if (this.exiting) return;
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
