// Turns gateway events into transcript widgets.
//
// Kept separate from the App so the event -> widget logic can be tested by
// rendering a plain Container, with no terminal involved.

import type { Component, Container } from "@earendil-works/pi-tui";
import { AssistantMessage, Notice, ToolCard, UserMessage } from "./components.ts";
import type { NoticeKind } from "./features.ts";
import type { GatewayEvent, HistoryEntry } from "./protocol.ts";

export interface TurnSummary {
	tokens: number;
	cancelled: boolean;
}

export class Transcript {
	private readonly container: Container;
	/** Shown while waiting for the model; removed as soon as output arrives. */
	private readonly spinner: Component & { start(): void; stop(): void };
	private current: AssistantMessage | undefined;
	private wroteText = false;
	private readonly tools = new Map<string, ToolCard>();

	constructor(container: Container, spinner: Component & { start(): void; stop(): void }) {
		this.container = container;
		this.spinner = spinner;
	}

	addUser(text: string): void {
		this.container.addChild(new UserMessage(text));
	}

	addNotice(text: string, kind: NoticeKind = "info"): void {
		this.container.addChild(new Notice(text, kind));
	}

	addMarkdown(text: string): void {
		this.container.addChild(new AssistantMessage(text));
	}

	clear(): void {
		this.hideSpinner();
		this.container.clear();
		this.current = undefined;
		this.tools.clear();
	}

	/** Re-render a stored conversation (from session.resume). */
	replay(history: HistoryEntry[]): void {
		for (const entry of history) {
			if (entry.role === "user") this.addUser(entry.content);
			else if (entry.role === "assistant") this.container.addChild(new AssistantMessage(entry.content));
			else if (entry.role === "tool") {
				const card = new ToolCard(entry.tool ?? "tool", {});
				card.complete(entry.content, entry.content.startsWith("Error:"));
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
				this.showSpinner();
				return undefined;
			case "message.delta":
				this.hideSpinner();
				if (!this.current) {
					this.current = new AssistantMessage();
					this.container.addChild(this.current);
				}
				this.current.append(event.text);
				this.wroteText = true;
				return undefined;
			case "tool.start": {
				this.hideSpinner();
				this.current = undefined; // text after the tool starts a new block
				const card = new ToolCard(event.name, event.args);
				this.tools.set(event.id, card);
				this.container.addChild(card);
				return undefined;
			}
			case "tool.complete":
				this.tools.get(event.id)?.complete(event.result, event.isError);
				this.tools.delete(event.id);
				this.showSpinner();
				return undefined;
			case "usage":
				return undefined;
			case "error":
				this.hideSpinner();
				this.addNotice(event.message, "error");
				return undefined;
			case "message.complete":
				this.hideSpinner();
				if (!this.wroteText && event.text) {
					this.container.addChild(new AssistantMessage(event.text));
				}
				if (event.cancelled) this.addNotice("Stopped.", "warning");
				for (const card of this.tools.values()) card.complete("(interrupted)", true);
				this.tools.clear();
				this.current = undefined;
				return { tokens: event.tokens, cancelled: event.cancelled };
		}
	}

	private showSpinner(): void {
		if (!this.container.children.includes(this.spinner)) {
			this.container.addChild(this.spinner);
			this.spinner.start();
		}
	}

	private hideSpinner(): void {
		if (this.container.children.includes(this.spinner)) {
			this.spinner.stop();
			this.container.removeChild(this.spinner);
		}
	}
}
