// Cancellable transient overlay for OAuth browser authorization.
// Status updates and the login link live only in the overlay: they never
// reach the transcript, history, or notices. Escape triggers onCancel.

import { Key, matchesKey, truncateToWidth, type Component } from "@earendil-works/pi-tui";
import { sanitizeDisplay } from "../design/text.ts";
import { theme } from "../theme.ts";

export interface OAuthProgressCallbacks {
	onOpen(): void;
	onCancel(): void;
}

const MAX_STATUS_CHARS = 500;
const MAX_LINK_CHARS = 200;

// eslint-disable-next-line no-control-regex
const ANSI_SEQUENCE = /\[[0-9;]*[A-Za-z]/g;
// eslint-disable-next-line no-control-regex
const OSC_SEQUENCE = /\][^\x07]*\x07/g;
// eslint-disable-next-line no-control-regex
const CONTROLS = /[-]/g;

export function sanitizeStatus(text: string): string {
	// Strip ANSI/OSC escape sequences entirely, then leftover controls;
	// bound length for narrow terminals. Provider text is untrusted.
	const clean = sanitizeDisplay(text.replace(ANSI_SEQUENCE, "").replace(OSC_SEQUENCE, "")).replace(CONTROLS, "");
	return clean.length > MAX_STATUS_CHARS ? `${clean.slice(0, MAX_STATUS_CHARS)}…` : clean;
}

export class OAuthProgress implements Component {
	private title: string;
	private status: string;
	private loginUrl: string | undefined;
	private readonly callbacks: OAuthProgressCallbacks;
	private cancelled = false;

	constructor(title: string, status: string, callbacks: OAuthProgressCallbacks, loginUrl?: string) {
		this.title = sanitizeStatus(title);
		this.status = sanitizeStatus(status);
		this.callbacks = callbacks;
		if (loginUrl) this.loginUrl = loginUrl;
	}

	setStatus(status: string, loginUrl?: string): void {
		this.status = sanitizeStatus(status);
		if (loginUrl !== undefined) this.loginUrl = loginUrl || undefined;
	}

	isCancelled(): boolean {
		return this.cancelled;
	}

	invalidate(): void {}

	handleInput(data: string): void {
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
			this.cancelled = true;
			this.callbacks.onCancel();
			return;
		}
		if (data === "o" || data === "O") {
			this.callbacks.onOpen();
			return;
		}
		if (data === "c" || data === "C") {
			this.cancelled = true;
			this.callbacks.onCancel();
		}
	}

	render(width: number): string[] {
		const content = Math.max(1, width);
		const out = [truncateToWidth(theme.dim(this.title), content)];
		out.push(truncateToWidth(this.status, content));
		if (this.loginUrl) {
			const shown = this.loginUrl.length > MAX_LINK_CHARS
				? `${this.loginUrl.slice(0, MAX_LINK_CHARS)}…`
				: this.loginUrl;
			out.push(truncateToWidth(`${theme.dim("Link:")} ${shown}`, content));
		}
		const hint = this.loginUrl
			? "o open browser · c/esc cancel"
			: "waiting for browser authorization · c/esc cancel";
		out.push(truncateToWidth(theme.dim(hint), content));
		return out;
	}
}
