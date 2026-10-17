// Searchable `/help` menu built on the house `Menu` (bounded rows, filter
// line, section headers, hint pinned last) — the same pattern as the theme
// menu. Replaces the custom full-screen HelpPalette.
//
// Contract:
// - Data is local and injected: commands (name / description / argumentHint
//   plus searchable argument-completion keywords) and keybinding reference
//   rows with their context ("Composer", "Workspace", "Global"). No provider
//   calls; typing filters locally through the Menu's own filter line.
// - Two grouped sections (Commands, Keys): an empty query shows everything,
//   a non-empty query hides empty sections and renders the Menu's distinct
//   no-match state — never a blank pane.
// - Selection is tracked by stable id (command name / source key index) by the
//   Menu itself (value-based), so a filter change can never re-route Enter.
// - Enter on a no-argument command hands it to the host to run through the
//   existing dispatcher; a command with an argument hint hands back `/name `
//   for the host to prefill into the composer; key rows are reference-only and
//   Enter is deliberately inert. The host owns the close.
// - A Menu subclass supplies a tokenized haystack (name + argument hint +
//   description + completion keywords) so multi-word queries keep working and
//   hidden keywords stay searchable without being displayed.
// - Labels are sanitized to a single physical row before layout, so a hostile
//   label can neither add physical rows nor misroute a selection.

import {
	type Component,
	type SelectItem,
	type TUI,
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import { Menu, type MenuOptions, type MenuSectionItem } from "./Menu.ts";
import { singleLine } from "../design/text.ts";
import { SHORTCUT_ROWS } from "../design/shortcuts.ts";

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

export interface HelpMenuOptions {
	commands: readonly HelpCommandItem[];
	keys: readonly HelpKeyItem[];
	/** Prefilled filter text (e.g. `/help <query>`). */
	initialQuery?: string;
	/** Explicit row cap (defaults to the shared menu default). */
	visibleRows?: number | "full";
	/** Live viewport for row budgets. */
	tui?: TUI;
	/** Overlay title; defaults to the literal `Help`. */
	title?: string;
	/** Called after any state change that should re-render the pane. */
	onChange?: () => void;
	/** Dismissal callback (Esc). */
	onClose?: () => void;
	/** No-argument command chosen: run it through the command dispatcher. */
	onRun?: (name: string) => void;
	/** Argument command chosen: prefill the composer with `/name `. */
	onPrefill?: (text: string) => void;
}

export interface HelpMenuSelected {
	kind: "command" | "key";
	/** Stable id: `command:<name>` or `key:<source index>`. */
	id: string;
	command?: HelpCommandItem;
	key?: HelpKeyItem;
}

/** Read-only snapshot for host integration and tests. */
export interface HelpMenuView {
	query: string;
	/** Filtered commands, source order preserved. */
	commands: HelpCommandItem[];
	/** Filtered keys, source order preserved. */
	keys: HelpKeyItem[];
	selected: HelpMenuSelected | null;
	/** Filtered entry count (`commands.length + keys.length`). */
	shown: number;
	/** Total entry count across both sections. */
	total: number;
}

/** Filter bound; well above any real command/key name. */
export const HELP_QUERY_MAX = 200;

const SECTION_VALUE = "__section__";
const COMMAND_PREFIX = "command:";
const KEY_PREFIX = "key:";

/**
 * Menu whose filter is a tokenized match over an injected haystack. The house
 * `Menu` only substring-matches label/value/description (with the whole query
 * as one substring), which would both miss multi-word queries and force
 * keywords into visible text; this subclass keeps the Menu's filter line and
 * value-based selection but applies a tokenized, keyword-aware predicate.
 */
class HelpResultMenu extends Menu {
	private readonly rows: SelectItem[];
	private readonly haystacks: Map<string, string>;

	constructor(title: string, rows: SelectItem[], haystacks: Map<string, string>, options: MenuOptions) {
		super(title, rows, options);
		this.rows = rows;
		this.haystacks = haystacks;
	}

	override filtered(): SelectItem[] {
		const tokens = this.getFilter().trim().toLowerCase().split(/\s+/).filter(Boolean);
		if (tokens.length === 0) return this.rows;
		return this.rows.filter((item) => {
			if ((item as { section?: unknown }).section === true) return false;
			const hay = this.haystacks.get(item.value) ?? "";
			return tokens.every((token) => hay.includes(token));
		});
	}
}

function commandLabel(command: HelpCommandItem): string {
	const hint = command.argumentHint ? ` ${command.argumentHint}` : "";
	return `/${command.name}${hint}`;
}

function commandHaystack(command: HelpCommandItem): string {
	return [
		command.name,
		`/${command.name}`,
		command.argumentHint ?? "",
		command.description ?? "",
		(command.keywords ?? []).join(" "),
	].join(" ");
}

function keyHaystack(key: HelpKeyItem): string {
	return [key.context, key.label, key.description ?? ""].join(" ");
}

/**
 * Build menu command rows from the registered slash commands, harvesting each
 * command's argument completions as searchable keywords (a completion provider
 * must never break the menu, so failures and async providers are skipped).
 */
export function helpMenuCommands(
	commands: readonly {
		name: string;
		description?: string;
		argumentHint?: string;
		getArgumentCompletions?(prefix: string): unknown;
	}[],
): HelpCommandItem[] {
	return commands.map((command) => {
		const keywords = new Set<string>();
		try {
			const completions = command.getArgumentCompletions?.("");
			if (Array.isArray(completions)) {
				for (const completion of completions as Array<{ value?: string; label?: string }>) {
					if (completion.value) keywords.add(completion.value);
					if (completion.label && completion.label !== completion.value) keywords.add(completion.label);
				}
			}
		} catch {
			// A throwing completion provider reduces keywords, never the menu.
		}
		return {
			name: command.name,
			description: command.description,
			argumentHint: command.argumentHint,
			keywords: [...keywords],
		};
	});
}

/**
 * Default keybinding reference rows, grouped by context. Same rows as the
 * `?`/F1 picker (design/shortcuts.ts); only the picker value is left behind.
 */
export const HELP_KEYS: readonly HelpKeyItem[] = SHORTCUT_ROWS.map(({ context, label, description }) => (
	description === undefined ? { context, label } : { context, label, description }
));

/**
 * Menu-shaped `/help`: keyboard and mouse map to the same actions. The host
 * calls `activate()` when opening the dialog and owns focus; `onClose` reports
 * the dismissal and `onRun` / `onPrefill` report the chosen command (the host
 * closes the dialog and dispatches).
 */
export class HelpMenu implements Component {
	/** Set by the focus owner (TUI `Focusable`); unfocused keys are inert. */
	focused = false;
	/** Dismissal callback (Esc). */
	onClose: (() => void) | undefined;
	/** No-argument command chosen: run it through the command dispatcher. */
	onRun: ((name: string) => void) | undefined;
	/** Argument command chosen: prefill the composer with `/name `. */
	onPrefill: ((text: string) => void) | undefined;

	private readonly commands: HelpCommandItem[];
	private readonly keys: HelpKeyItem[];
	private readonly initialQuery: string;
	private readonly title: string;
	private readonly onChange: (() => void) | undefined;
	private visibleRows: number | "full" | undefined;
	private readonly tui: TUI | undefined;

	private query = "";
	private menu: HelpResultMenu;

	constructor(options: HelpMenuOptions) {
		this.commands = options.commands.map((command) => ({ ...command }));
		this.keys = options.keys.map((key) => ({ ...key }));
		this.initialQuery = (options.initialQuery ?? "").slice(0, HELP_QUERY_MAX);
		this.title = options.title ?? "Help";
		this.onChange = options.onChange;
		this.visibleRows = options.visibleRows;
		this.tui = options.tui;
		this.onClose = options.onClose;
		this.onRun = options.onRun;
		this.onPrefill = options.onPrefill;
		this.menu = this.build();
	}

	/** Open the menu: reset the filter to the initial query, select the first entry. */
	activate(): void {
		this.query = this.initialQuery;
		this.focused = true;
		this.menu = this.build();
		if (this.query) this.menu.handleInput(this.query);
		this.emit();
	}

	/** Close the menu. */
	dismiss(): void {
		this.focused = false;
		this.onClose?.();
	}

	/** Update the row budget (e.g. on resize); selection stays by id. */
	setVisibleRows(rows: number | "full"): void {
		this.visibleRows = rows;
		this.menu.setVisibleRows(rows);
		this.emit();
	}

	getView(): HelpMenuView {
		const commands = this.filteredItems(COMMAND_PREFIX)
			.map((item) => this.commands.find((command) => `${COMMAND_PREFIX}${command.name}` === item.value))
			.filter((command): command is HelpCommandItem => command !== undefined);
		const keys = this.filteredItems(KEY_PREFIX)
			.map((item) => this.keys[Number(item.value.slice(KEY_PREFIX.length))])
			.filter((key): key is HelpKeyItem => key !== undefined);
		return {
			query: this.query,
			commands,
			keys,
			selected: this.selectedEntry(),
			shown: commands.length + keys.length,
			total: this.commands.length + this.keys.length,
		};
	}

	render(width: number): string[] {
		return this.menu.render(width);
	}

	handleInput(data: string): void {
		if (!this.focused) return;
		this.menu.handleInput(data);
		this.query = this.menu.getFilter();
		this.emit();
	}

	handleMouse(event: TuiMouseEvent): ReturnType<NonNullable<Component["handleMouse"]>> {
		return this.menu.handleMouse(event);
	}

	invalidate(): void {
		this.menu.invalidate();
	}

	// ─── internals ─────────────────────────────────────────────────────────

	private build(): HelpResultMenu {
		const rows: SelectItem[] = [];
		const haystacks = new Map<string, string>();
		const pushSection = (label: string) => rows.push({ value: SECTION_VALUE, label, section: true } as MenuSectionItem);
		pushSection("Commands");
		for (const command of this.commands) {
			const value = `${COMMAND_PREFIX}${command.name}`;
			rows.push({
				value,
				label: singleLine(commandLabel(command)),
				description: command.description ? singleLine(command.description) : undefined,
			});
			haystacks.set(value, commandHaystack(command).toLowerCase());
		}
		pushSection("Keys");
		this.keys.forEach((key, index) => {
			const value = `${KEY_PREFIX}${index}`;
			const context = key.description ? `${key.context}: ${key.description}` : key.context;
			rows.push({ value, label: singleLine(key.label), description: singleLine(context) });
			haystacks.set(value, keyHaystack(key).toLowerCase());
		});
		const menu = new HelpResultMenu(this.title, rows, haystacks, {
			hint: "↑↓ move · enter run/insert · type to filter · esc close",
			visibleRows: this.visibleRows,
			tui: this.tui,
		});
		menu.onSelect = (item) => this.activateItem(item);
		menu.onCancel = () => this.dismiss();
		return menu;
	}

	private filteredItems(prefix: string): SelectItem[] {
		return this.menu.filtered().filter((item) => item.value.startsWith(prefix));
	}

	private selectedEntry(): HelpMenuSelected | null {
		const item = this.menu.getSelectedItem();
		if (!item) return null;
		if (item.value.startsWith(COMMAND_PREFIX)) {
			const name = item.value.slice(COMMAND_PREFIX.length);
			const command = this.commands.find((entry) => entry.name === name);
			return command ? { kind: "command", id: item.value, command } : null;
		}
		const index = Number(item.value.slice(KEY_PREFIX.length));
		const key = this.keys[index];
		return key ? { kind: "key", id: item.value, key } : null;
	}

	private activateItem(item: SelectItem): void {
		if (!item.value.startsWith(COMMAND_PREFIX)) return; // key rows are reference-only
		const name = item.value.slice(COMMAND_PREFIX.length);
		const command = this.commands.find((entry) => entry.name === name);
		if (!command) return;
		if (this.needsArgs(command)) {
			this.onPrefill?.(`/${command.name} `);
			return;
		}
		this.onRun?.(command.name);
	}

	/** A command needs arguments when it declares a non-empty argument hint. */
	private needsArgs(command: HelpCommandItem): boolean {
		return typeof command.argumentHint === "string" && command.argumentHint.trim().length > 0;
	}

	private emit(): void {
		this.onChange?.();
	}
}
