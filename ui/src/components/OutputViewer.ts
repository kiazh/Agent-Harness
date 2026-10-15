// Explicit retained-output inspection: a bounded local store plus a
// scrollable overlay viewer. UI-local only: no refetch, no re-execution.
// Bounds: 8 most recent outputs, 128 KiB UTF-8 each, 1 MiB total.

import {
	Key,
	matchesKey,
	ScrollView,
	Text,
	truncateToWidth,
	wrapTextWithAnsi,
	type Component,
	type TUI,
} from "@earendil-works/pi-tui";
import { LIMITS } from "../design/tokens.ts";
import { sanitizeDisplay, truncateUtf8ByBytes } from "../design/text.ts";
import { surfaceLine, theme } from "../theme.ts";

export interface OutputRecord {
	sessionId: string;
	toolId: string;
	name: string;
	text: string;
	byteLength: number;
	backendTruncated: boolean;
	locallyTruncated: boolean;
}

function utf8Length(text: string): number {
	return new TextEncoder().encode(text).length;
}

export class OutputStore {
	private readonly records = new Map<string, OutputRecord>();
	private order: string[] = [];
	private totalBytes = 0;

	private static key(sessionId: string, toolId: string): string {
		return JSON.stringify([sessionId, toolId]);
	}

	put(sessionId: string, toolId: string, name: string, result: string, backendTruncated: boolean): void {
		const capped = truncateUtf8ByBytes(result, LIMITS.outputMaxBytesPer);
		const key = OutputStore.key(sessionId, toolId);
		const existing = this.records.get(key);
		if (existing) {
			this.totalBytes -= existing.byteLength;
			this.order = this.order.filter((k) => k !== key);
		}
		const text = capped.text;
		const record: OutputRecord = {
			sessionId,
			toolId,
			name,
			text,
			byteLength: utf8Length(text),
			backendTruncated,
			locallyTruncated: capped.truncated,
		};
		this.records.set(key, record);
		this.order.push(key);
		this.totalBytes += record.byteLength;
		while (this.order.length > LIMITS.outputRetainedCount || this.totalBytes > LIMITS.outputMaxBytesTotal) {
			const oldest = this.order.shift();
			if (oldest === undefined) break;
			const dropped = this.records.get(oldest);
			if (dropped) this.totalBytes -= dropped.byteLength;
			this.records.delete(oldest);
		}
	}

	/** Retained outputs for a session, most recent first. */
	list(sessionId: string): OutputRecord[] {
		const out: OutputRecord[] = [];
		for (let i = this.order.length - 1; i >= 0; i--) {
			const record = this.records.get(this.order[i]!);
			if (record && record.sessionId === sessionId) out.push(record);
		}
		return out;
	}

	get(sessionId: string, toolId: string): OutputRecord | undefined {
		return this.records.get(OutputStore.key(sessionId, toolId));
	}

	/** Resolve an exact tool ID or unambiguous prefix within one session. */
	resolve(sessionId: string, idOrPrefix: string): OutputRecord {
		const wanted = idOrPrefix.trim();
		if (!wanted) throw new Error("Give a tool id. Use /output to browse retained output.");
		const direct = this.get(sessionId, wanted);
		if (direct) return direct;
		const matches = this.list(sessionId).filter((r) => r.toolId.startsWith(wanted));
		if (matches.length === 1) return matches[0]!;
		if (matches.length === 0) throw new Error(`No retained output for "${idOrPrefix}" in this session.`);
		throw new Error(`"${idOrPrefix}" matches ${matches.length} outputs; use more characters.`);
	}

	get size(): number {
		return this.records.size;
	}
}

export interface OutputViewerOptions {
	tui?: TUI;
	onClose?: () => void;
}

/**
 * Scrollable retained-output overlay with its own ScrollView.
 *
 * Installed-toolkit limitation (verified): overlay content never receives
 * viewport layout, so a nested ScrollView keeps viewportHeight 0 and cannot
 * clip on its own. The viewer therefore owns its ScrollView as the single
 * source of scroll truth (scrollBy/scrollToStart/scrollToEnd) and windows
 * the wrapped rows itself, mirroring App's full-screen overlay sheet exactly
 * like TextForm's cursor window does. Opening preserves the chat draft and
 * transcript scroll position; closing returns to the previous dialog.
 */
export class OutputViewer implements Component {
	private readonly record: OutputRecord;
	private readonly scroll: ScrollView;
	private readonly tui: TUI | undefined;
	private readonly onClose?: () => void;

	constructor(record: OutputRecord, options: OutputViewerOptions = {}) {
		this.record = record;
		this.tui = options.tui;
		this.onClose = options.onClose;
		this.scroll = new ScrollView(new Text(sanitizeDisplay(record.text || "(empty result)"), 0, 0));
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.escape)) {
			this.onClose?.();
			return;
		}
		if (matchesKey(data, Key.up)) this.scroll.scrollBy(-1);
		else if (matchesKey(data, Key.down)) this.scroll.scrollBy(1);
		else if (matchesKey(data, Key.pageUp)) this.scroll.scrollBy(-Math.max(1, this.scroll.viewportHeight - 1));
		else if (matchesKey(data, Key.pageDown)) this.scroll.scrollBy(Math.max(1, this.scroll.viewportHeight - 1));
		else if (matchesKey(data, Key.home)) this.scroll.scrollToStart();
		else if (matchesKey(data, Key.end)) this.scroll.scrollToEnd();
	}

	render(width: number): string[] {
		const total = Math.max(0, width);
		const content = Math.max(1, width);
		const rows = this.tui?.terminal.rows ?? 24;
		const wrapped = sanitizeDisplay(this.record.text || "(empty result)")
			.split("\n")
			.flatMap((line) => wrapTextWithAnsi(line, content));
		// Mirror App's full-screen overlay sheet (see openDialog): the viewer
		// owns title + status + hints rows, the ScrollView budget gets the rest.
		const height = Math.max(4, rows);
		const budget = Math.max(1, height - 3);
		this.scroll.updateLayout(wrapped.length, budget, () => {});
		const top = this.scroll.scrollTop;
		const flags = [
			this.record.backendTruncated ? "server truncated" : "",
			this.record.locallyTruncated ? "locally capped" : "",
		].filter(Boolean).join(" · ");
		// Untrusted provider tool name/id: sanitize like every other tool-name
		// surface. Newlines are flattened because this header is one physical row
		// (sanitizeDisplay preserves \n, which would otherwise break the sheet).
		const name = sanitizeDisplay(this.record.name).replace(/\n/g, " ");
		const toolId = sanitizeDisplay(this.record.toolId.slice(0, 8)).replace(/\n/g, " ");
		return [
			truncateToWidth(
				`${theme.accentBold("Output")}: ${name} ${theme.dim(`(${toolId})`)}`,
				content,
			),
			truncateToWidth(
				theme.dim(`${this.record.byteLength.toLocaleString("en-US")} bytes${flags ? ` · ${flags}` : ""}`),
				content,
			),
			...wrapped.slice(top, top + budget).map((line) => truncateToWidth(line, content)),
			truncateToWidth(theme.dim("esc close · ↑↓ scroll · pgup/pgdn · home/end"), content),
		].map((line) => surfaceLine(truncateToWidth(line, total), total));
	}

	invalidate(): void {
		this.scroll.invalidate();
	}
}
