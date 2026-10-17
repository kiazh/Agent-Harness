// Quick Open overlay (plan §12): a bounded, overlay-style picker over an
// injected ranked-list provider.
//
// Contract:
// - The provider (`(query) => Promise<WorkspaceQuickOpenResult>`) is injected;
//   the controller binds it to the gateway `workspace.quickOpen` RPC and tests
//   bind fakes. This module never talks to the gateway itself.
// - Provider requests are debounced (~120 ms) and sequence-guarded: a response
//   that arrives after a newer request was scheduled is dropped, so an old
//   answer can never replace a newer one.
// - Every keystroke additionally filters the last provider result locally
//   (fuzzy subsequence on the basename), so typing stays responsive between
//   provider round-trips. Provider order is preserved: the pane never
//   re-sorts what the backend ranked.
// - Progress is disclosed, never hidden: the footer always carries the result
//   count and any `indexing` / `truncated` / `stale` state, and the declared
//   exclusion policy; empty states are distinct text (`searching`, `no
//   matches`, `no files`, `error`, `stale`).
// - Lists are bounded to `maxVisible` rows with exact `+N more` disclosure;
//   rows are sanitized and truncated so untrusted paths can never overflow
//   the pane or inject terminal controls.

import {
	Key,
	matchesKey,
	type Component,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { truncateDisplay } from "../design/text.ts";
import { sanitizeDisplay } from "./display.ts";
import { selectionBar, surfaceLine, theme } from "../theme.ts";
import type {
	WorkspaceQuickOpenEntry,
	WorkspaceQuickOpenResult,
} from "../workspace/protocol.ts";

/** Injected ranked provider; the controller binds it to `workspace.quickOpen`. */
export type QuickOpenProvider = (query: string) => Promise<WorkspaceQuickOpenResult>;

export type QuickOpenStatus = "idle" | "loading" | "ready" | "error";

/** Read-only snapshot of the pane state (host integration and tests). */
export interface QuickOpenView {
	query: string;
	/** Locally filtered entries, provider order preserved. */
	entries: WorkspaceQuickOpenEntry[];
	selected: WorkspaceQuickOpenEntry | null;
	total: number;
	indexing: boolean;
	truncated: boolean;
	reasons: string[];
	stale: boolean;
	scanned: number;
	exclusions: string[];
	status: QuickOpenStatus;
	error: string;
}

export interface QuickOpenPaneOptions {
	/** Provider-call debounce in ms; defaults to 120. */
	debounceMs?: number;
	/** Maximum visible result rows; defaults to 10. Values below 1 are raised to 1. */
	maxVisible?: number;
	/** Overlay title; defaults to the literal `Go to file` (finding 10). */
	title?: string;
	/** Called after any state change that should re-render the pane. */
	onChange?: () => void;
}

const SELECTED_PREFIX = `${ICONS.selectedMarker} `;
const PLAIN_PREFIX = "  ";
const FOOTER_SEPARATOR = " · ";
const DEFAULT_MAX_VISIBLE = 10;
const DEFAULT_DEBOUNCE_MS = 120;
/** Mirrors the backend `workspace.quickOpen` query bound (MAX_QUICK_OPEN_QUERY_CHARS). */
export const QUICK_OPEN_QUERY_MAX = 200;

/**
 * Fuzzy subsequence containment for the local as-you-type filter
 * (case-insensitive; an empty query matches everything).
 */
export function subsequenceMatches(name: string, query: string): boolean {
	const needle = query.toLowerCase();
	if (!needle) return true;
	const haystack = name.toLowerCase();
	if (haystack.includes(needle)) return true;
	let position = 0;
	for (const char of haystack) {
		if (char === needle[position]) {
			position += 1;
			if (position === needle.length) return true;
		}
	}
	return false;
}

type PaneRow =
	| { kind: "header"; query: string }
	| { kind: "entry"; entry: WorkspaceQuickOpenEntry; selected: boolean }
	| { kind: "more"; hidden: number }
	| { kind: "empty"; label: string }
	| { kind: "footer"; parts: string[] };

interface RowWindow {
	items: Array<{ entry: WorkspaceQuickOpenEntry }>;
	hiddenAbove: number;
	hiddenBelow: number;
}

/**
 * Overlay picker: keyboard and mouse map to the same actions; keys only act
 * while the pane is focused. The host calls `activate()` when opening the
 * overlay and owns focus; `onOpen`/`onClose` report the chosen entry and the
 * dismissal.
 */
export class QuickOpenPane implements Component {
	/** Set by the focus owner (TUI `Focusable`); unfocused keys are inert. */
	focused = false;
	onOpen: ((entry: WorkspaceQuickOpenEntry) => void) | undefined;
	onClose: (() => void) | undefined;

	private readonly provider: QuickOpenProvider;
	private readonly debounceMs: number;
	private readonly maxVisible: number;
	private readonly title: string;
	private readonly onChange: (() => void) | undefined;

	private query = "";
	private entries: WorkspaceQuickOpenEntry[] = [];
	private selectedPath: string | null = null;
	private total = 0;
	private truncated = false;
	private reasons: string[] = [];
	private indexing = false;
	private stale = false;
	private scanned = 0;
	private exclusions: string[] = [];
	private status: QuickOpenStatus = "idle";
	private errorText = "";
	/** Sequence of the newest scheduled request; older responses are dropped. */
	private requestSeq = 0;
	private timer: ReturnType<typeof setTimeout> | null = null;

	constructor(provider: QuickOpenProvider, options: QuickOpenPaneOptions = {}) {
		this.provider = provider;
		this.debounceMs = Math.max(0, options.debounceMs ?? DEFAULT_DEBOUNCE_MS);
		this.maxVisible = Math.max(1, Math.floor(options.maxVisible ?? DEFAULT_MAX_VISIBLE));
		this.title = options.title ?? "Go to file";
		this.onChange = options.onChange;
	}

	/** Open the overlay: reset, then immediately request the empty query. */
	activate(): void {
		this.clearTimer();
		this.query = "";
		this.entries = [];
		this.selectedPath = null;
		this.total = 0;
		this.truncated = false;
		this.reasons = [];
		this.indexing = false;
		this.stale = false;
		this.scanned = 0;
		this.exclusions = [];
		this.status = "idle";
		this.errorText = "";
		this.focused = true;
		this.schedule(true);
		this.emit();
	}

	/** Close the overlay; any in-flight response is dropped. */
	dismiss(): void {
		this.clearTimer();
		this.requestSeq += 1;
		this.focused = false;
		this.onClose?.();
	}

	getQuery(): string {
		return this.query;
	}

	/** Locally filtered entries in provider order. */
	visibleEntries(): WorkspaceQuickOpenEntry[] {
		return this.filtered();
	}

	selectedEntry(): WorkspaceQuickOpenEntry | null {
		return this.filtered().find((entry) => entry.path === this.selectedPath) ?? null;
	}

	getView(): QuickOpenView {
		return {
			query: this.query,
			entries: this.filtered(),
			selected: this.selectedEntry(),
			total: this.total,
			indexing: this.indexing,
			truncated: this.truncated,
			reasons: [...this.reasons],
			stale: this.stale,
			scanned: this.scanned,
			exclusions: [...this.exclusions],
			status: this.status,
			error: this.errorText,
		};
	}

	invalidate(): void {}

	render(width: number): string[] {
		const total = Math.max(0, width);
		return this.layout().map((row) => {
			const line = truncateDisplay(this.styleRow(row), total);
			if (row.kind === "entry" && row.selected) {
				return surfaceLine(selectionBar(line, total), total);
			}
			return surfaceLine(line, total);
		});
	}

	handleInput(data: string): void {
		if (!this.focused) return;
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
			this.dismiss();
			return;
		}
		if (matchesKey(data, Key.up)) {
			this.moveBy(-1);
			return;
		}
		if (matchesKey(data, Key.down)) {
			this.moveBy(1);
			return;
		}
		if (matchesKey(data, Key.enter) || matchesKey(data, Key.return)) {
			this.openSelected();
			return;
		}
		if (matchesKey(data, Key.backspace)) {
			if (this.query) this.setQuery(this.query.slice(0, -1));
			return;
		}
		// Printable filter input only: escape sequences and controls never
		// corrupt the query buffer, and the buffer is capped at the protocol
		// bound so a query can never be refused purely for length.
		if (data.includes("\x1b") || data.includes("\x9b")) return;
		if ([...data].some((ch) => {
			const code = ch.charCodeAt(0);
			return code < 32 || code === 0x7f || (code >= 0x80 && code <= 0x9f);
		})) {
			return;
		}
		if (!data) return;
		if (this.query.length >= QUICK_OPEN_QUERY_MAX) return;
		this.setQuery(this.query + data);
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		if (event.button !== "left") return undefined;
		const row = this.layout()[event.y];
		if (!row) return undefined;
		if (event.type === "press") return { handled: true, focus: true };
		if (event.type !== "click") return undefined;
		if (row.kind !== "entry") return { handled: true, focus: true };
		this.selectedPath = row.entry.path;
		if ((event.clickCount ?? 1) >= 2) {
			this.onOpen?.(row.entry);
			return { handled: true, focus: true, render: true };
		}
		this.emit();
		return { handled: true, focus: true, render: true };
	}

	// ─── query + provider plumbing ───────────────────────────────────────────
	private setQuery(query: string): void {
		this.query = query;
		this.reconcileSelection();
		this.schedule(false);
		this.emit();
	}

	private schedule(immediate: boolean): void {
		this.clearTimer();
		// Every schedule invalidates responses for older requests, including one
		// already in flight while the debounce timer is pending.
		const seq = ++this.requestSeq;
		const query = this.query;
		const run = (): void => {
			void this.runRequest(seq, query);
		};
		if (immediate) run();
		else this.timer = setTimeout(run, this.debounceMs);
	}

	private async runRequest(seq: number, query: string): Promise<void> {
		if (seq !== this.requestSeq) return;
		this.status = "loading";
		this.emit();
		try {
			const result = await this.provider(query);
			if (seq !== this.requestSeq) return; // a newer request superseded this one
			this.apply(result);
		} catch (error) {
			if (seq !== this.requestSeq) return;
			this.status = "error";
			this.errorText = sanitizeDisplay(error instanceof Error ? error.message : String(error));
			this.emit();
		}
	}

	private apply(result: WorkspaceQuickOpenResult): void {
		this.entries = Array.isArray(result.entries) ? result.entries : [];
		this.total = Number.isFinite(result.total) ? result.total : this.entries.length;
		this.truncated = result.truncated === true;
		this.reasons = Array.isArray(result.reasons) ? result.reasons : [];
		this.indexing = result.indexing === true;
		this.scanned = Number.isFinite(result.scanned) ? result.scanned : this.entries.length;
		this.stale = result.stale === true;
		this.exclusions = Array.isArray(result.exclusions) ? result.exclusions : [];
		this.status = result.refused ? "error" : "ready";
		if (result.refused) {
			this.errorText = sanitizeDisplay(result.refused.message || result.refused.reason);
		}
		this.reconcileSelection();
		this.emit();
	}

	private clearTimer(): void {
		if (this.timer !== null) {
			clearTimeout(this.timer);
			this.timer = null;
		}
	}

	private emit(): void {
		this.onChange?.();
	}

	// ─── selection ───────────────────────────────────────────────────────────
	private filtered(): WorkspaceQuickOpenEntry[] {
		if (!this.query) return [...this.entries];
		return this.entries.filter((entry) => subsequenceMatches(entry.name, this.query));
	}

	/** Keep the selected path when it survives; otherwise select the first row. */
	private reconcileSelection(): void {
		const list = this.filtered();
		if (this.selectedPath !== null && list.some((entry) => entry.path === this.selectedPath)) {
			return;
		}
		this.selectedPath = list[0]?.path ?? null;
	}

	private moveBy(delta: number): void {
		const list = this.filtered();
		if (list.length === 0) return;
		const current = list.findIndex((entry) => entry.path === this.selectedPath);
		const index = current < 0
			? (delta >= 0 ? 0 : list.length - 1)
			: Math.max(0, Math.min(list.length - 1, current + delta));
		this.selectedPath = list[index]!.path;
		this.emit();
	}

	private openSelected(): void {
		const entry = this.selectedEntry();
		if (entry) this.onOpen?.(entry);
	}

	// ─── rendering ───────────────────────────────────────────────────────────
	/** Bound the filtered list to `maxVisible` rows with exact `+N more` disclosure. */
	private windowEntries(list: WorkspaceQuickOpenEntry[]): RowWindow {
		if (list.length <= this.maxVisible) {
			return { items: list.map((entry) => ({ entry })), hiddenAbove: 0, hiddenBelow: 0 };
		}
		// Two rows may be reserved for the `+N more` disclosures; a budget below
		// three rows renders content only, so the pane is never overwide or empty.
		const reserved = this.maxVisible >= 3 ? 2 : 0;
		const content = Math.max(1, this.maxVisible - reserved);
		const selected = Math.max(
			0,
			list.findIndex((entry) => entry.path === this.selectedPath),
		);
		const start = Math.max(0, Math.min(selected - content + 1, list.length - content));
		const end = Math.min(list.length, start + content);
		return {
			items: list.slice(start, end).map((entry) => ({ entry })),
			hiddenAbove: reserved > 0 ? start : 0,
			hiddenBelow: reserved > 0 ? list.length - end : 0,
		};
	}

	private layout(): PaneRow[] {
		const rows: PaneRow[] = [{ kind: "header", query: this.query }];
		const visible = this.filtered();
		if (visible.length === 0) {
			rows.push({ kind: "empty", label: this.emptyLabel() });
		} else {
			const window = this.windowEntries(visible);
			if (window.hiddenAbove > 0) rows.push({ kind: "more", hidden: window.hiddenAbove });
			for (const item of window.items) {
				rows.push({
					kind: "entry",
					entry: item.entry,
					selected: item.entry.path === this.selectedPath,
				});
			}
			if (window.hiddenBelow > 0) rows.push({ kind: "more", hidden: window.hiddenBelow });
		}
		rows.push({ kind: "footer", parts: this.footerParts() });
		return rows;
	}

	private emptyLabel(): string {
		if (this.status === "error") {
			return this.errorText ? `(error: ${this.errorText})` : "(error)";
		}
		if (this.stale) return "(stale — workspace changed; results discarded)";
		if (this.indexing) return "(indexing…)";
		if (this.status === "loading") return "(searching…)";
		if (this.query) return "(no matches)";
		return "(no files)";
	}

	private footerParts(): string[] {
		const parts = [`${this.total} results`];
		if (this.indexing) parts.push(`indexing… (${this.scanned} scanned)`);
		if (this.truncated) {
			parts.push(this.reasons.length > 0 ? `truncated (${this.reasons.join(", ")})` : "truncated");
		}
		if (this.stale) parts.push("stale");
		if (this.exclusions.length > 0) parts.push(`excluded: ${this.exclusions.join(", ")}`);
		return parts;
	}

	private styleRow(row: PaneRow): string {
		switch (row.kind) {
			case "header": {
				// Finding 10: `Go to file: main` — a colon join, not a fragment.
				const query = row.query
					? theme.text(sanitizeDisplay(row.query))
					: theme.dim("type to search");
				return `${theme.accentBold(`${this.title}:`)} ${query}`;
			}
			case "entry": {
				const prefix = row.selected ? SELECTED_PREFIX : PLAIN_PREFIX;
				const name = theme.bold(sanitizeDisplay(row.entry.name));
				const directory = row.entry.directory
					? `  ${theme.dim(`${sanitizeDisplay(row.entry.directory)}/`)}`
					: "";
				return `${prefix}${name}${directory}`;
			}
			case "more":
				return theme.dim(`+${row.hidden} more`);
			case "empty":
				return theme.dim(row.label);
			case "footer":
				return theme.dim(row.parts.join(FOOTER_SEPARATOR));
		}
	}
}
