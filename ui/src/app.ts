// The interactive app: transcript, editor and footer on a pi-tui main screen,
// driven by the Python gateway. Slash commands live in features.ts.

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import {
	CombinedAutocompleteProvider,
	Container,
	Editor,
	Key,
	Loader,
	matchesKey,
	type OverlayHandle,
	type SelectItem,
	type TUI,
} from "@earendil-works/pi-tui";
import { parseCommand } from "./commands.ts";
import { Footer, header, Picker } from "./components.ts";
import { type FeatureHost, type NoticeKind, runCommand, SLASH_COMMANDS } from "./features/index.ts";
import type { GatewayClient } from "./gateway.ts";
import type {
	ConfigResult,
	GatewayEvent,
	HistoryEntry,
	InitializeResult,
	ResumeResult,
	SessionInfo,
	SessionResult,
} from "./protocol.ts";
import { editorTheme, theme } from "./theme.ts";
import { Transcript } from "./transcript.ts";

export interface AppOptions {
	model?: string;
	provider?: string;
	sessionId?: string;
}

export class App implements FeatureHost {
	readonly exited: Promise<void>;

	private readonly tui: TUI;
	private readonly client: GatewayClient;
	private readonly options: AppOptions;
	private readonly transcriptView = new Container();
	private readonly transcript: Transcript;
	private readonly editor: Editor;
	private readonly footer = new Footer();
	private version = "";
	private current: SessionInfo | undefined;
	private running = false;
	/** Session ids of turns that are still in flight (prompt submitted, no message.complete yet). */
	private readonly inFlight = new Set<string>();
	private sessionTokens = 0;
	private overlay: OverlayHandle | undefined;
	private exiting = false;
	private resolveExit: () => void = () => {};

	constructor(tui: TUI, client: GatewayClient, options: AppOptions = {}) {
		this.tui = tui;
		this.client = client;
		this.options = options;
		this.exited = new Promise((done) => {
			this.resolveExit = done;
		});

		const spinner = new Loader(tui, theme.accent, theme.muted, "Thinking…");
		spinner.stop();
		this.transcript = new Transcript(this.transcriptView, spinner);

		this.editor = new Editor(tui, editorTheme, { paddingX: 1 });
		this.editor.setAutocompleteProvider(new CombinedAutocompleteProvider(SLASH_COMMANDS, process.cwd()));
		this.editor.onSubmit = (text) => void this.submit(text);
	}

	async start(): Promise<void> {
		this.tui.addChild(this.transcriptView);
		this.tui.addChild(this.editor);
		this.tui.addChild(this.footer);
		this.tui.setFocus(this.editor);
		this.tui.addInputListener((data) => this.onKey(data));

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
				{ model: this.options.model, provider: this.options.provider },
				60_000,
			);
			this.version = init.version;
			Object.assign(this.footer, { cwd: init.cwd, branch: init.branch, model: init.model, status: "ready" });
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

	switchTo(session: SessionInfo, history: HistoryEntry[]): void {
		this.updateSession(session);
		this.transcript.clear();
		this.transcriptView.addChild(header(this.version));
		this.transcript.replay(history);
		this.sessionTokens = 0;
		this.footer.tokens = 0;
		this.setRunning(this.inFlight.has(session.id));
		this.tui.requestRender();
	}

	updateSession(session: SessionInfo): void {
		this.current = session;
		this.footer.session = session.title || shortTitle(session.id);
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
			this.overlay = this.tui.showOverlay(picker, { width: "80%", minWidth: 40, maxHeight: "70%", anchor: "center" });
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

	async exit(): Promise<void> {
		if (this.exiting) return;
		this.exiting = true;
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
		return undefined;
	}

	private async submit(raw: string): Promise<void> {
		const text = raw.trim();
		this.editor.setText("");
		if (!text) return;
		this.editor.addToHistory(text);

		const command = parseCommand(text);
		if (command) {
			await runCommand(command, this);
		} else {
			try {
				await this.prompt(text);
			} catch (error) {
				this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
				this.transcript.addNotice(message(error), "error");
			}
		}
		this.tui.requestRender();
	}

	private async prompt(text: string): Promise<void> {
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
		this.tui.requestRender();
		try {
			await this.client.request("prompt.submit", { sessionId, text });
		} catch (error) {
			this.inFlight.delete(sessionId);
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			throw error;
		}
	}

	private cancel(): void {
		if (!this.current) return;
		this.setRunning(false);
		this.client.request("prompt.cancel", { sessionId: this.current.id }).catch(() => {});
	}

	private setRunning(running: boolean): void {
		this.running = running;
		this.editor.disableSubmit = running;
		if (this.footer.status !== "offline") this.footer.status = running ? "working" : "ready";
	}

	// ─── gateway ────────────────────────────────────────────────────────────
	private onEvent(event: GatewayEvent): void {
		// A turn's events may arrive after the user switched sessions, so match
		// against in-flight turns, not just the current session.
		if (!this.inFlight.has(event.sessionId)) return;
		if (event.sessionId !== this.current?.id) {
			if (event.type === "message.complete") this.inFlight.delete(event.sessionId);
			this.setRunning(this.current ? this.inFlight.has(this.current.id) : false);
			this.tui.requestRender();
			return;
		}
		const summary = this.transcript.apply(event);
		if (event.type === "usage") this.footer.tokens = this.sessionTokens + event.tokens;
		if (summary) {
			this.inFlight.delete(event.sessionId);
			this.sessionTokens += summary.tokens;
			this.footer.tokens = this.sessionTokens;
			this.setRunning(this.inFlight.has(event.sessionId));
		}
		this.tui.requestRender();
	}

	private onGatewayExit(code: number | null, stderr: string[]): void {
		if (this.exiting) return;
		this.setRunning(false);
		this.footer.status = "offline";
		this.transcript.addNotice(`The gateway stopped${code === null ? "" : ` (exit code ${code})`}.`, "error");
		const tail = stderr.slice(-8);
		if (tail.length) this.transcript.addNotice(tail.join("\n"));
		this.transcript.addNotice("Press Ctrl+C to exit, then run `ah doctor`.");
		this.tui.requestRender();
	}
}

function shortTitle(id: string): string {
	return id.slice(0, 8);
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}
