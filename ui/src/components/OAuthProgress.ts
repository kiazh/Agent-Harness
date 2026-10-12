// Cancellable transient overlay for OAuth browser authorization.
// Status updates and the login link live only in the overlay: they never
// reach the transcript, history, or notices. Escape triggers onCancel.

import { Key, matchesKey, truncateToWidth, wrapTextWithAnsi, type Component, type TUI } from "@earendil-works/pi-tui";
import { sanitizeDisplay } from "../design/text.ts";
import { theme } from "../theme.ts";

export interface OAuthProgressCallbacks {
	onOpen(): void;
	onCopy?(): void;
	onCancel(): void;
}

const MAX_STATUS_CHARS = 500;

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
	private readonly tui?: TUI;
	private scrollTop = 0;
	private viewportRows = 1;

	constructor(title: string, status: string, callbacks: OAuthProgressCallbacks, loginUrl?: string, tui?: TUI) {
		this.title = sanitizeStatus(title);
		this.status = sanitizeStatus(status);
		this.callbacks = callbacks;
		this.tui = tui;
		if (loginUrl) this.loginUrl = loginUrl;
	}

	setStatus(status: string, loginUrl?: string): void {
		this.status = sanitizeStatus(status);
		if (loginUrl !== undefined) {
			if (loginUrl !== this.loginUrl) this.scrollTop = 0;
			this.loginUrl = loginUrl || undefined;
		}
	}

	isCancelled(): boolean {
		return this.cancelled;
	}

	invalidate(): void {}

	handleInput(data: string): void {
		if (this.cancelled) return;
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
			this.cancelled = true;
			this.callbacks.onCancel();
			return;
		}
		if (this.loginUrl && (data === "o" || data === "O")) {
			this.callbacks.onOpen();
			return;
		}
		if (this.loginUrl && (data === "y" || data === "Y")) {
			this.callbacks.onCopy?.();
			return;
		}
		if (data === "c" || data === "C") {
			this.cancelled = true;
			this.callbacks.onCancel();
		}
		if (matchesKey(data, Key.up)) this.scrollTop = Math.max(0, this.scrollTop - 1);
		else if (matchesKey(data, Key.down)) this.scrollTop++;
		else if (matchesKey(data, Key.pageUp)) this.scrollTop = Math.max(0, this.scrollTop - this.viewportRows);
		else if (matchesKey(data, Key.pageDown)) this.scrollTop += this.viewportRows;
		else if (matchesKey(data, Key.home)) this.scrollTop = 0;
		else if (matchesKey(data, Key.end)) this.scrollTop = Number.MAX_SAFE_INTEGER;
	}

	render(width: number): string[] {
		const content = Math.max(1, width);
		const out = [truncateToWidth(theme.dim(this.title), content)];
		out.push(truncateToWidth(this.status, content));
		const hint = this.loginUrl
			? `o open/retry${this.callbacks.onCopy ? " · y copy link" : ""} · c/esc cancel`
			: "waiting for browser authorization · c/esc cancel";
		const controls = wrapTextWithAnsi(theme.dim(hint), content);
		if (this.loginUrl) {
			controls.push(truncateToWidth(theme.dim("↑↓ scroll · home/end"), content));
			out.push(truncateToWidth(theme.dim("Login link:"), content));
			// Preserve the complete transient fallback even in narrow terminals.
			const shown = sanitizeDisplay(this.loginUrl.replace(ANSI_SEQUENCE, "").replace(OSC_SEQUENCE, "")).replace(CONTROLS, "");
			const wrapped = wrapTextWithAnsi(shown, content);
			const rows = this.tui?.terminal.rows ?? 24;
			const height = Math.max(4, Math.min(Math.floor(rows * 0.7), rows - 2));
			this.viewportRows = Math.max(1, height - out.length - controls.length);
			this.scrollTop = Math.max(0, Math.min(this.scrollTop, wrapped.length - this.viewportRows));
			out.push(...wrapped.slice(this.scrollTop, this.scrollTop + this.viewportRows));
		}
		out.push(...controls);
		return out;
	}
}
