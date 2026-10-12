// Turns gateway events into transcript widgets.
//
// Kept separate from the App so the event -> widget logic can be tested by
// rendering a plain Container, with no terminal involved.

import type { Component, Container } from "@earendil-works/pi-tui";
import { AssistantTurn } from "./components/AssistantTurn.ts";
import { Notice } from "./components/Notice.ts";
import { ToolTurn } from "./components/ToolTurn.ts";
import { UserTurn } from "./components/UserTurn.ts";
import { ACTIVITY_INTERVAL_MS, type ActivitySource, type AnimationCoordinator, type AnimationSubscription } from "./design/motion.ts";
import type { NoticeKind } from "./features/index.ts";
import type { GatewayEvent, HistoryEntry } from "./protocol.ts";

export interface TurnSummary {
	tokens: number;
	cancelled: boolean;
}

export class Transcript {
	private readonly container: Container;
	/** Shown while waiting for the model; removed as soon as output arrives. */
	private readonly spinner: Component & { start(): void; stop(): void; setMessage?(message: string): void };
	private readonly coordinator: AnimationCoordinator | undefined;
	private current: AssistantTurn | undefined;
	private wroteText = false;
	/** Text rendered via deltas this turn; reconciled with canonical final. */
	private displayed = "";
	private readonly tools = new Map<string, ToolTurn>();
	private readonly toolSubs = new Map<string, AnimationSubscription>();
	/** Terminal delivery guard: a repeated completion for this turn is inert. */
	private lastCompletedTurn: string | undefined;

	constructor(
		container: Container,
		spinner: Component & { start(): void; stop(): void },
		coordinator?: AnimationCoordinator,
	) {
		this.container = container;
		this.spinner = spinner;
		this.coordinator = coordinator;
	}

	/** Frame source for widgets: live coordinator or a static stub. */
	private get activity(): ActivitySource {
		return this.coordinator ?? { frame: (frames) => frames[0] ?? "" };
	}

	addComponent(component: Component): void {
		this.container.addChild(component);
	}

	pauseForApproval(): void {
		this.hideSpinner();
	}

	addUser(text: string): void {
		this.container.addChild(new UserTurn(text));
	}

	addNotice(text: string, kind: NoticeKind = "info"): void {
		this.container.addChild(new Notice(text, kind));
	}

	addMarkdown(text: string): void {
		this.container.addChild(new AssistantTurn(text));
	}

	clear(): void {
		this.hideSpinner();
		this.stopTools();
		this.container.clear();
		this.current = undefined;
		this.displayed = "";
		this.tools.clear();
		this.lastCompletedTurn = undefined;
	}

	/** Stop live tool subscriptions without clearing content. */
	private stopTools(): void {
		for (const sub of this.toolSubs.values()) sub.stop();
		this.toolSubs.clear();
	}

	/** Re-resolve theme colors without dropping transcript content (skin switch). */
	restyle(): void {
		for (const child of this.container.children) {
			if (child && typeof child === "object" && "refreshSkin" in child) {
				(child as { refreshSkin(): void }).refreshSkin();
			}
		}
	}

	/** Re-render a stored conversation (from session.resume). */
	replay(history: HistoryEntry[]): void {
		for (const entry of history) {
			if (entry.role === "user") this.addUser(entry.content);
			else if (entry.role === "assistant") this.container.addChild(new AssistantTurn(entry.content));
			else if (entry.role === "tool") {
				// Historical text carries no typed outcome metadata: neutral
				// recorded output, never an inferred success or failure.
				const card = new ToolTurn(entry.tool ?? "tool", {}, this.activity);
				card.settle({ text: entry.content, backendTruncated: false, locallyTruncated: false }, "historical");
				this.container.addChild(card);
			} else this.addNotice(entry.content);
		}
	}

	/** Apply one event. Returns a summary when the turn completes. */
	apply(event: GatewayEvent): TurnSummary | undefined {
		switch (event.type) {
		case "message.start":
			this.current = undefined;
			this.wroteText = false;
			this.displayed = "";
			this.lastCompletedTurn = undefined;
			this.showSpinner();
			return undefined;
		case "message.delta":
			this.hideSpinner();
			if (!this.current) {
				this.current = new AssistantTurn();
				this.container.addChild(this.current);
			}
			this.current.append(event.text);
			this.displayed += event.text;
			this.wroteText = true;
			return undefined;
		case "tool.start": {
			this.hideSpinner();
			this.current = undefined; // text after the tool starts a new block
			const card = new ToolTurn(event.name, event.args, this.activity);
			this.tools.set(event.id, card);
			this.container.addChild(card);
			if (this.coordinator) {
				this.toolSubs.get(event.id)?.stop();
				this.toolSubs.set(event.id, this.coordinator.subscribe(`tool:${event.id}`, ACTIVITY_INTERVAL_MS));
			}
			return undefined;
		}
		case "tool.complete": {
			// Unknown tool IDs never manufacture a tool card.
			const card = this.tools.get(event.id);
			card?.settle(
				{ text: event.result, backendTruncated: event.truncated ?? false, locallyTruncated: false },
				event.outcome === "outcome_unknown" ? "outcome_unknown" : event.isError ? "error" : "success",
			);
			this.toolSubs.get(event.id)?.stop();
			this.toolSubs.delete(event.id);
			this.tools.delete(event.id);
			this.showSpinner();
			return undefined;
		}
			case "usage":
				return undefined;
			case "error":
				this.hideSpinner();
				this.addNotice(event.message, "error");
				return undefined;
		case "message.stopping":
			// AH-AUDIT-028: cleanup state — the turn is stopping but
			// ownership is not yet released. Stay blocked; the terminal
			// message.complete still follows exactly once. Activity goes
			// static; the guard lives in the App.
			this.suspend();
			this.addNotice("Stopping…", "warning");
			return undefined;
			case "turn.started":
				return undefined;
			case "needs_approval":
			case "permission.required":
				return undefined;
			case "approval.resolved":
			case "permission.resolved":
				return undefined;
			case "approval.resumed":
				return undefined;
			case "turn.ownership_lost":
				this.hideSpinner();
				this.addNotice("Turn ownership was lost; stopping the running action.", "warning");
				return undefined;
		case "turn.cleanup_pending":
			this.hideSpinner();
			this.addNotice("Stopping: waiting for the running action to finish cleanup.", "warning");
			return undefined;
		case "message.complete": {
			this.hideSpinner();
			// Duplicate terminal delivery for an already-retired turn is inert.
			if (this.lastCompletedTurn !== undefined && this.lastCompletedTurn === event.turnId) return undefined;
			this.lastCompletedTurn = event.turnId;
			// AH-AUDIT-027: reconcile with the canonical final content.
			// Deltas alone may omit a withheld redaction tail: when the
			// canonical text extends what was displayed, append exactly
			// the missing suffix (never a duplicate). With no deltas,
			// render the canonical text once. When the canonical text
			// replaces streamed content instead of extending it, reconcile
			// the current text block to the canonical content so discarded
			// raw deltas are never exposed alongside it.
			if (!this.wroteText && event.text) {
				this.container.addChild(new AssistantTurn(event.text));
				this.displayed = event.text;
			} else if (event.text && event.text !== this.displayed) {
				if (event.text.startsWith(this.displayed)) {
					const missing = event.text.slice(this.displayed.length);
					if (missing) {
						this.current?.append(missing);
						this.displayed = event.text;
					}
				} else if (this.current) {
					this.current.setText(event.text);
					this.displayed = event.text;
				} else {
					this.container.addChild(new AssistantTurn(event.text));
					this.displayed = event.text;
				}
			}
			if (event.cancelled) {
				this.addNotice("Stopped.", "warning");
				for (const card of this.tools.values()) {
					card.settle({ text: "", backendTruncated: false, locallyTruncated: false }, "interrupted");
				}
			} else {
				// A missing tool completion is not evidence of success.
				for (const card of this.tools.values()) {
					card.settle({ text: "", backendTruncated: false, locallyTruncated: false }, "outcome_unknown");
				}
			}
			this.stopTools();
			this.tools.clear();
			this.current = undefined;
			return { tokens: event.tokens, cancelled: event.cancelled };
		}
	}
}

	private showSpinner(): void {
		if (this.container.children.includes(this.spinner)) return;
		this.container.addChild(this.spinner);
		this.spinner.start();
		// The shared animation coordinator owns render demand; elapsed text
		// (if shown) derives from timestamps at render. No private ticker here.
		this.spinner.setMessage?.("Thinking…");
	}

	private hideSpinner(): void {
		if (this.container.children.includes(this.spinner)) {
			this.spinner.stop();
			this.container.removeChild(this.spinner);
		}
	}

	/** Stop activity presentation without clearing content (disconnect/exit/switch). */
	suspend(phase: "stopping" | "cleanup_pending" = "stopping"): void {
		this.hideSpinner();
		this.stopTools();
		for (const card of this.tools.values()) card.setMotionPaused(phase);
	}
}
