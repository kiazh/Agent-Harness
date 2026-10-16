// Interactive `/help` command palette (product rule: menu-shaped surfaces are
// searchable overlays, never a printed wall of text).
//
// Contract:
// - Data is local and injected: commands (name / description / argumentHint
//   plus searchable argument-completion keywords) and keybinding reference
//   rows with their context ("Composer", "Workspace", "Global"). No provider
//   calls; typing filters locally.
// - Two grouped sections (Commands, Keys): an empty query shows everything,
//   a non-empty query hides empty sections and renders a distinct no-match
//   state — never a blank pane.
// - Selection is tracked by stable id (command name / source key index) so a
//   filter change can never re-route Enter to a different row.
// - Enter (or a click) on a no-argument command hands it to the host to run
//   through the existing dispatcher; a command with an argument hint hands
//   back `/name ` for the host to prefill into the composer; key rows are
//   reference-only and Enter is deliberately inert. The host owns the close
//   (mirrors the skills picker's delete flow).
// - Every rendered label is sanitized to a single physical row before layout
//   and rows are truncated, so a hostile label can neither add physical rows
//   nor misroute a click: the hit map is the layout index.

import {
	Key,
	matchesKey,
	type Component,
	type SlashCommand,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { sanitizeDisplay, truncateDisplay } from "../design/text.ts";
import { selectionBar, surfaceLine, theme } from "../theme.ts";

/** One searchable command row (built from `SLASH_COMMANDS`). */
export interface HelpCommandItem {
	name: string;
	description?: string;
	argumentHint?: string;
	/** Extra searchable terms (typically the command's argument completions). */
	keywords?: string[];
}

/** One keybinding reference row, grouped by its context. */
export interface HelpKeyItem {
	context: string;
	label: string;
	description?: string;
}

export interface HelpPaletteOptions {
	commands: readonly HelpCommandItem[];
	keys: readonly HelpKeyItem[];
	/** Prefilled filter text (e.g. `/help <query>`). */
	initialQuery?: string;
	/** Maximum visible item rows; defaults to 10. Values below 1 are raised to 1. */
	maxVisible?: number;
	/** Overlay title; defaults to the literal `Help`. */
	title?: string;
	/** Called after any state change that should re-render the pane. */
	onChange?: () => void;
}

export type HelpPaletteEntry =
	| { kind: "command"; id: string; command: HelpCommandItem }
	| { kind: "key"; id: string; key: HelpKeyItem };

/** Read-only snapshot for host integration and tests. */
export interface HelpPaletteView {
	query: string;
	/** Filtered commands, source order preserved. */
	commands: HelpCommandItem[];
	/** Filtered keys, source order preserved. */
	keys: HelpKeyItem[];
	selected: HelpPaletteEntry | null;
	/** Filtered entry count (`commands.length + keys.length`). */
	shown: number;
	/** Total entry count across both sections. */
	total: number;
}

/**
 * Default keybinding reference rows (the data the `?` menu and the legacy
 * help text carried), grouped by context. Kept next to the component because
 * app.ts's shortcut rows are private and lose the context labels the palette
 * needs.
 */
export const HELP_KEYS: readonly HelpKeyItem[] = [
	{ context: "Composer", label: "Enter — send", description: "Send a message or run a command" },
	{ context: "Composer", label: "Shift+Enter — new line" },
	{ context: "Composer", label: "Drop an image / Ctrl+V — paste image", description: "PNG, JPEG, GIF or WebP" },
	{ context: "Composer", label: "Esc — stop reply", description: "Twice on empty input edits the last prompt" },
	{ context: "Composer", label: "Ctrl+C — stop / clear / exit", description: "Stop a reply, clear a draft, or exit when empty" },
	{ context: "Composer", label: "Ctrl+D — exit", description: "When input is empty and idle" },
	{ context: "Composer", label: "Ctrl+L — clear chat", description: "When idle" },
	{ context: "Composer", label: "Tab — complete", description: "Commands and arguments" },
	{ context: "Composer", label: "↓ — subagent menu", description: "On empty input: switch between the main session and its subagents" },
	{ context: "Composer", label: "Ctrl+Shift+←/→ — switch agents", description: "Cycle the main session and its subagents" },
	{ context: "Workspace", label: "Ctrl+P — quick open", description: "Fuzzy file search in the workspace" },
	{ context: "Workspace", label: "Ctrl+S — save file", description: "Save the active editor buffer" },
	{ context: "Workspace", label: "Ctrl+W — close tab", description: "Close the active editor tab (dirty: save/discard/cancel); the terminal may intercept" },
	{ context: "Workspace", label: "Ctrl+T — file tree", description: "Toggle the right-edge explorer (/explorer)" },
	{ context: "Workspace", label: "Ctrl+Space — completion", description: "macOS may steal Ctrl+Space · fallback /editor complete" },
	{ context: "Workspace", label: "F12 — definition · Shift+F12 — references · F2 — rename", description: "Editor intelligence; /problems lists diagnostics" },
	{ context: "Workspace", label: "F6 / Shift+F6 — cycle focus", description: "Composer → editor → explorer → problems" },
	{ context: "Workspace", label: "Ctrl+→ maximize chat · Ctrl+← 50/50 split", description: "Editor focus; the composer keeps Ctrl+←/→ word motion" },
	{ context: "Workspace", label: "/workspace open <path> — open a folder", description: "Also split|chat|editor|ratio|side|status|close" },
	{ context: "Global", label: "? / F1 — shortcuts", description: "? on empty input, F1 with a draft" },
	{ context: "Global", label: "/help — all commands", description: "This palette; /growth shows learning details" },
	{ context: "Global", label: "□ / ▪ — activity and learning", description: "Pulses while working and briefly when learning is saved" },
];

const SELECTED_PREFIX = `${ICONS.selectedMarker} `;
const PLAIN_PREFIX = "  ";
const FOOTER_SEPARATOR = " · ";
const DEFAULT_MAX_VISIBLE = 10;
/** Filter bound; well above any real command/key name. */
export const HELP_QUERY_MAX = 200;

/** Single physical row per label: control sequences stripped, newlines collapse. */
function singleLine(text: string): string {
	return sanitizeDisplay(text).replace(/\n/g, " ");
}

/**
 * Build palette command rows from the registered slash commands, harvesting
 * each command's argument completions as searchable keywords (a completion
 * provider must never break the palette, so failures and async providers are
 * skipped).
 */
export function helpPaletteCommands(commands: readonly SlashCommand[]): HelpCommandItem[] {
	return commands.map((command) => {
		const keywords = new Set<string>();
		try {
			const completions = command.getArgumentCompletions?.("");
			if (Array.isArray(completions)) {
				for (const item of completions) {
					if (item.value) keywords.add(item.value);
					if (item.label && item.label !== item.value) keywords.add(item.label);
				}
			}
		} catch {
			// A throwing completion provider reduces keywords, never the palette.
		}
		return {
			name: command.name,
			description: command.description,
			argumentHint: command.argumentHint,
			keywords: [...keywords],
		};
	});
}

/** Case-insensitive substring match per whitespace-separated token. */
function matchesQuery(haystack: string, query: string): boolean {
	const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
	if (tokens.length === 0) return true;
	const hay = haystack.toLowerCase();
	return tokens.every((token) => hay.includes(token));
}

function commandHaystack(command: HelpCommandItem): string {
	return [
		`/${command.name}`,
		command.argumentHint ?? "",
		command.description ?? "",
		(command.keywords ?? []).join(" "),
	].join(" ");
}

function keyHaystack(key: HelpKeyItem): string {
	return [key.context, key.label, key.description ?? ""].join(" ");
}

interface KeyedCommand {
	id: string;
	command: HelpCommandItem;
}

interface KeyedKey {
	id: string;
	key: HelpKeyItem;
}

type PaneRow =
	| { kind: "header"; query: string }
	| { kind: "section"; label: string; detail: string }
	| { kind: "command"; id: string; command: HelpCommandItem; selected: boolean }
	| { kind: "key"; id: string; key: HelpKeyItem; selected: boolean }
	| { kind: "more"; hidden: number }
	| { kind: "empty"; text: string }
	| { kind: "footer"; parts: string[] };

/**
 * Overlay palette: keyboard and mouse map to the same actions; keys only act
 * while `focused`. The host calls `activate()` when opening the overlay and
 * owns focus; `onClose` reports the dismissal and `onRun` / `onPrefill` report
 * the chosen command (the host closes the overlay and dispatches).
 */
export class HelpPalette implements Component {
	/** Set by the focus owner (TUI `Focusable`); unfocused keys are inert. */
	focused = false;
	/** Dismissal callback (Esc). */
	onClose: (() => void) | undefined;
	/** No-argument command chosen: run it through the command dispatcher. */
	onRun: ((command: HelpCommandItem) => void) | undefined;
	/** Argument command chosen: prefill the composer with `/name `. */
	onPrefill: ((text: string) => void) | undefined;

	private readonly commands: KeyedCommand[];
	private readonly keys: KeyedKey[];
	private readonly initialQuery: string;
	private maxVisible: number;
	private readonly title: string;
	private readonly onChange: (() => void) | undefined;

	private query: string;
	private selectedId: string | null = null;

	constructor(options: HelpPaletteOptions) {
		this.commands = options.commands.map((command) => ({ id: `command:${command.name}`, command }));
		this.keys = options.keys.map((key, index) => ({ id: `key:${index}`, key }));
		this.initialQuery = options.initialQuery ?? "";
		this.maxVisible = Math.max(1, Math.floor(options.maxVisible ?? DEFAULT_MAX_VISIBLE));
		this.title = options.title ?? "Help";
		this.onChange = options.onChange;
		this.query = this.initialQuery.slice(0, HELP_QUERY_MAX);
	}

	/** Open the overlay: reset the filter to the initial query and select the first entry. */
	activate(): void {
		this.query = this.initialQuery.slice(0, HELP_QUERY_MAX);
		this.selectedId = null;
		this.reconcileSelection();
		this.focused = true;
		this.emit();
	}

	/** Close the overlay. */
	dismiss(): void {
		this.focused = false;
		this.onClose?.();
	}

	getQuery(): string {
		return this.query;
	}

	/** Update the row budget (e.g. on resize); selection stays by id. */
	setVisibleRows(rows: number): void {
		this.maxVisible = Math.max(1, Math.floor(rows));
		this.emit();
	}

	getView(): HelpPaletteView {
		const commands = this.filteredCommands();
		const keys = this.filteredKeys();
		return {
			query: this.query,
			commands: commands.map((entry) => entry.command),
			keys: keys.map((entry) => entry.key),
			selected: this.selectedEntry(),
			shown: commands.length + keys.length,
			total: this.commands.length + this.keys.length,
		};
	}

	selectedEntry(): HelpPaletteEntry | null {
		return this.entries().find((entry) => entry.id === this.selectedId) ?? null;
	}

	invalidate(): void {}

	// ─── rendering ─────────────────────────────────────────────────────────

	render(width: number): string[] {
		const total = Math.max(0, width);
		return this.layout().map((row) => {
			const line = truncateDisplay(this.styleRow(row), total);
			if ((row.kind === "command" || row.kind === "key") && row.selected) {
				return surfaceLine(selectionBar(line, total), total);
			}
			return surfaceLine(line, total);
		});
	}

	private layout(): PaneRow[] {
		const commands = this.filteredCommands();
		const keys = this.filteredKeys();
		const rows: PaneRow[] = [{ kind: "header", query: this.query }];
		const entries = this.entries(commands, keys);
		if (entries.length === 0) {
			rows.push({ kind: "empty", text: this.emptyLabel() });
			rows.push({ kind: "footer", parts: this.footerParts(0, this.commands.length + this.keys.length) });
			return rows;
		}
		const selected = Math.max(0, entries.findIndex((entry) => entry.id === this.selectedId));
		const window = this.windowFor(entries.length, selected);
		if (window.hiddenAbove > 0) rows.push({ kind: "more", hidden: window.hiddenAbove });
		const visibleCommands = commands.slice(window.start, Math.min(window.end, commands.length));
		if (visibleCommands.length > 0) {
			rows.push({ kind: "section", label: "Commands", detail: `${visibleCommands.length}/${commands.length}` });
			for (const entry of visibleCommands) {
				rows.push({ kind: "command", id: entry.id, command: entry.command, selected: entry.id === this.selectedId });
			}
		}
		const keyStart = Math.max(0, window.start - commands.length);
		const keyEnd = Math.max(0, window.end - commands.length);
		const visibleKeys = keys.slice(keyStart, keyEnd);
		if (visibleKeys.length > 0) {
			rows.push({ kind: "section", label: "Keys", detail: `${visibleKeys.length}/${keys.length}` });
			for (const entry of visibleKeys) {
				rows.push({ kind: "key", id: entry.id, key: entry.key, selected: entry.id === this.selectedId });
			}
		}
		if (window.hiddenBelow > 0) rows.push({ kind: "more", hidden: window.hiddenBelow });
		rows.push({
			kind: "footer",
			parts: this.footerParts(entries.length, this.commands.length + this.keys.length),
		});
		return rows;
	}

	private styleRow(row: PaneRow): string {
		switch (row.kind) {
			case "header": {
				if (row.query) return `${theme.accentBold(`${this.title}:`)} ${theme.text(singleLine(row.query))}`;
				return `${theme.accentBold(`${this.title}:`)} ${theme.dim("type to search")}`;
			}
			case "section":
				return `${theme.secondary(row.label)} ${theme.dim(`(${row.detail})`)}`;
			case "command": {
				const prefix = row.selected ? SELECTED_PREFIX : PLAIN_PREFIX;
				const hint = row.command.argumentHint ? ` ${singleLine(row.command.argumentHint)}` : "";
				const usage = theme.bold(singleLine(`/${row.command.name}${hint}`));
				const description = row.command.description ? `  ${theme.dim(singleLine(row.command.description))}` : "";
				return `${prefix}${usage}${description}`;
			}
			case "key": {
				const prefix = row.selected ? SELECTED_PREFIX : PLAIN_PREFIX;
				const description = row.key.description ? ` ${theme.dim(singleLine(row.key.description))}` : "";
				return `${prefix}${theme.bold(singleLine(row.key.label))}  ${theme.secondary(`${singleLine(row.key.context)}:`)}${description}`;
			}
			case "more":
				return theme.dim(`+${row.hidden} more`);
			case "empty":
				return theme.dim(row.text);
			case "footer":
				return theme.dim(row.parts.join(FOOTER_SEPARATOR));
		}
	}

	private windowFor(size: number, selected: number): { start: number; end: number; hiddenAbove: number; hiddenBelow: number } {
		if (size <= this.maxVisible) return { start: 0, end: size, hiddenAbove: 0, hiddenBelow: 0 };
		// Two rows may be reserved for the `+N more` disclosures; budgets below
		// three rows render content only, mirroring the skills picker.
		const reserved = this.maxVisible >= 3 ? 2 : 0;
		const content = Math.max(1, this.maxVisible - reserved);
		const start = Math.max(0, Math.min(selected - content + 1, size - content));
		const end = Math.min(size, start + content);
		return {
			start,
			end,
			hiddenAbove: reserved > 0 ? start : 0,
			hiddenBelow: reserved > 0 ? size - end : 0,
		};
	}

	private pageSize(): number {
		const reserved = this.maxVisible >= 3 ? 2 : 0;
		return Math.max(1, this.maxVisible - reserved);
	}

	private emptyLabel(): string {
		if (this.query) return `(no matches for "${singleLine(this.query)}")`;
		return "(no commands or keys registered)";
	}

	private footerParts(shown: number, total: number): string[] {
		const parts = [`${shown}/${total}`, "↑↓ move"];
		if (total > this.maxVisible) parts.push("ctrl+n/p page");
		const selected = this.selectedEntry();
		if (selected?.kind === "command") parts.push(this.needsArgs(selected.command) ? "enter insert" : "enter run");
		else if (selected?.kind === "key") parts.push("keys are reference-only");
		parts.push("type to filter", "esc close");
		return parts;
	}

	// ─── input ─────────────────────────────────────────────────────────────

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
		if (matchesKey(data, Key.ctrl("n"))) {
			this.moveBy(this.pageSize());
			return;
		}
		if (matchesKey(data, Key.ctrl("p"))) {
			this.moveBy(-this.pageSize());
			return;
		}
		if (matchesKey(data, Key.enter) || matchesKey(data, Key.return)) {
			this.activateSelected();
			return;
		}
		if (matchesKey(data, Key.backspace)) {
			if (this.query) this.setQuery(this.query.slice(0, -1));
			return;
		}
		this.appendPrintable(data, (text) => this.setQuery(this.query + text));
	}

	private activateSelected(): void {
		const selected = this.selectedEntry();
		if (!selected || selected.kind !== "command") return; // key rows are reference-only
		this.activateCommand(selected.command);
	}

	/** Enter/click on a command: run it, or hand back `/name ` for prefill. */
	private activateCommand(command: HelpCommandItem): void {
		if (this.needsArgs(command)) {
			this.onPrefill?.(`/${command.name} `);
			return;
		}
		this.onRun?.(command);
	}

	/** A command needs arguments when it declares a non-empty argument hint. */
	private needsArgs(command: HelpCommandItem): boolean {
		return typeof command.argumentHint === "string" && command.argumentHint.trim().length > 0;
	}

	/** Printable filter input only: escape sequences and controls are inert. */
	private appendPrintable(data: string, apply: (text: string) => void): void {
		if (!data || data.includes("\x1b") || data.includes("\x9b")) return;
		if ([...data].some((ch) => {
			const code = ch.charCodeAt(0);
			return code < 32 || code === 0x7f || (code >= 0x80 && code <= 0x9f);
		})) {
			return;
		}
		apply(data);
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		if (event.button !== "left") return undefined;
		// Every layout row renders as exactly one physical row (labels are
		// sanitized single-line and lines are truncated), so the pointer row is
		// the layout index — hostile labels can never shift a hit target.
		const layout = this.layout();
		const row = event.y >= 0 && event.y < layout.length ? layout[event.y] : undefined;
		if (!row) return undefined;
		if (event.type === "press") return { handled: true, focus: true };
		if (event.type !== "click") return undefined;
		if (row.kind === "command") {
			this.selectedId = row.id;
			this.activateCommand(row.command);
			this.emit();
			return { handled: true, focus: true, render: true };
		}
		if (row.kind === "key") {
			this.selectedId = row.id;
			this.emit();
			return { handled: true, focus: true, render: true };
		}
		return { handled: true, focus: true };
	}

	// ─── filtering + selection ─────────────────────────────────────────────

	private setQuery(query: string): void {
		this.query = query.slice(0, HELP_QUERY_MAX);
		this.reconcileSelection();
		this.emit();
	}

	private filteredCommands(): KeyedCommand[] {
		return this.commands.filter((entry) => matchesQuery(commandHaystack(entry.command), this.query));
	}

	private filteredKeys(): KeyedKey[] {
		return this.keys.filter((entry) => matchesQuery(keyHaystack(entry.key), this.query));
	}

	private entries(commands = this.filteredCommands(), keys = this.filteredKeys()): HelpPaletteEntry[] {
		return [
			...commands.map((entry): HelpPaletteEntry => ({ kind: "command", id: entry.id, command: entry.command })),
			...keys.map((entry): HelpPaletteEntry => ({ kind: "key", id: entry.id, key: entry.key })),
		];
	}

	/** Keep the selected id when it survives filtering; otherwise select the first row. */
	private reconcileSelection(): void {
		const list = this.entries();
		if (this.selectedId !== null && list.some((entry) => entry.id === this.selectedId)) return;
		this.selectedId = list[0]?.id ?? null;
	}

	private moveBy(delta: number): void {
		const list = this.entries();
		if (list.length === 0) return;
		const current = list.findIndex((entry) => entry.id === this.selectedId);
		const index = current < 0
			? (delta >= 0 ? 0 : list.length - 1)
			: Math.max(0, Math.min(list.length - 1, current + delta));
		this.selectedId = list[index]!.id;
		this.emit();
	}

	private emit(): void {
		this.onChange?.();
	}
}
