// Searchable menu: the filter buffer is owned here, matched
// case-insensitively against label, value and description. Stable values stay
// separate from displayed labels. The installed SelectList only prefix-matches
// on values, so this menu filters explicitly and renders its own rows.

import { Key, matchesKey, truncateToWidth, visibleWidth, type Component, type SelectItem, type TUI, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { isSelectionRow, selectionBar, theme } from "../theme.ts";

export type { SelectItem };

export interface MenuOptions {
	hint?: string;
	/** Explicit row cap; the live viewport still bounds it. Omit for the full viewport. */
	visibleRows?: number;
	/** Live viewport for row budgets (title + filter + hint rows reserved). */
	tui?: TUI;
	/** Initially selected value (defaults to the first item). */
	initialValue?: string;
	multiSelect?: boolean;
}

// Rendered chrome above and below the item rows (title, filter, hint) plus
// one slack row so the bottom-anchored overlay never claims the exact
// terminal height.
const MENU_RESERVED_ROWS = 4;

export class Menu implements Component {
	onSelect: ((item: SelectItem) => void) | undefined;
	onCancel: (() => void) | undefined;
	onSelectionChange: ((item: SelectItem) => void) | undefined;
	onConfirm: ((items: SelectItem[]) => void) | undefined;

	private readonly title: string;
	private readonly items: SelectItem[];
	private readonly hint: string;
	private visibleRows: number | undefined;
	private readonly tui: TUI | undefined;
	private filter = "";
	private selectedValue: string | undefined;
	private readonly multiSelect: boolean;
	private readonly checked = new Set<string>();

	constructor(title: string, items: SelectItem[], options: MenuOptions = {}) {
		this.title = title;
		this.items = items;
		this.multiSelect = options.multiSelect ?? false;
		this.hint = options.hint ?? (this.multiSelect ? "↑↓ move · space check · enter review · esc cancel" : "enter select · esc back");
		this.visibleRows = options.visibleRows === undefined ? undefined : Math.max(1, options.visibleRows);
		this.tui = options.tui;
		this.selectedValue = options.initialValue ?? items[0]?.value;
	}

	/** Currently applied filter text (for tests and budget restores). */
	getFilter(): string {
		return this.filter;
	}

	getSelectedItem(): SelectItem | null {
		return this.filtered().find((item) => item.value === this.selectedValue) ?? null;
	}

	/** Update the row budget cap (e.g. on resize), preserving the selection. */
	setVisibleRows(rows: number): void {
		this.visibleRows = Math.max(1, rows);
		this.clampSelection();
	}

	filtered(): SelectItem[] {
		const query = this.filter.toLowerCase();
		if (!query) return [...this.items];
		return this.items.filter((item) =>
			`${item.label ?? item.value} ${item.value} ${item.description ?? ""}`.toLowerCase().includes(query),
		);
	}

	private clampSelection(): void {
		const visible = this.filtered();
		if (!visible.some((item) => item.value === this.selectedValue)) {
			this.selectedValue = visible[0]?.value;
			const current = this.getSelectedItem();
			if (current) this.onSelectionChange?.(current);
		}
	}

	private select(value: string | undefined): void {
		if (value === this.selectedValue) return;
		this.selectedValue = value;
		const current = this.getSelectedItem();
		if (current) this.onSelectionChange?.(current);
	}

	private move(delta: number): void {
		const visible = this.filtered();
		if (visible.length === 0) return;
		const at = visible.findIndex((item) => item.value === this.selectedValue);
		const next = Math.max(0, Math.min(visible.length - 1, (at < 0 ? 0 : at) + delta));
		this.select(visible[next]!.value);
	}

	private jump(to: "start" | "end"): void {
		const visible = this.filtered();
		if (visible.length === 0) return;
		this.select((to === "start" ? visible[0] : visible.at(-1))!.value);
	}

	private toggle(value: string): void {
		if (this.checked.has(value)) this.checked.delete(value);
		else this.checked.add(value);
	}

	/** Effective row budget: the explicit cap when set, always bounded by the live viewport. */
	private rowBudget(): number {
		const cap = this.visibleRows;
		if (!this.tui) return cap ?? Number.MAX_SAFE_INTEGER;
		const live = Math.max(1, (this.tui.terminal.rows ?? 24) - MENU_RESERVED_ROWS);
		return cap === undefined ? live : Math.min(cap, live);
	}

	private visibleWindow(visible: SelectItem[]): SelectItem[] {
		const budget = this.rowBudget();
		const at = Math.max(0, visible.findIndex(item => item.value === this.selectedValue));
		const start = Math.max(0, Math.min(visible.length - budget, at - Math.floor(budget / 2)));
		return visible.slice(start, start + budget);
	}

	handleMouse(event: TuiMouseEvent): ReturnType<NonNullable<Component["handleMouse"]>> {
		if (!this.multiSelect || event.button !== "left") return undefined;
		const item = this.visibleWindow(this.filtered())[event.y - 2];
		if (!item) return undefined;
		if (event.type === "click") {
			this.select(item.value);
			this.toggle(item.value);
			return { handled: true, focus: true, render: true };
		}
		if (event.type === "press") return { handled: true, focus: true };
		return undefined;
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
			this.onCancel?.();
			return;
		}
		if (matchesKey(data, Key.enter)) {
			if (this.multiSelect) {
				if (this.checked.size) this.onConfirm?.(this.items.filter(item => this.checked.has(item.value)));
				return;
			}
			const item = this.getSelectedItem();
			if (item) this.onSelect?.(item);
			return;
		}
		if (this.multiSelect && data === " ") {
			const item = this.getSelectedItem();
			if (item) this.toggle(item.value);
			return;
		}
		if (matchesKey(data, Key.up)) {
			this.move(-1);
			return;
		}
		if (matchesKey(data, Key.down)) {
			this.move(1);
			return;
		}
		if (matchesKey(data, Key.home) || matchesKey(data, Key.pageUp)) {
			if (matchesKey(data, Key.pageUp)) this.move(-this.rowBudget());
			else this.jump("start");
			return;
		}
		if (matchesKey(data, Key.end) || matchesKey(data, Key.pageDown)) {
			if (matchesKey(data, Key.pageDown)) this.move(this.rowBudget());
			else this.jump("end");
			return;
		}
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.clampSelection();
			return;
		}
		// Printable filter input only: escape sequences and controls never
		// become a chat prompt or corrupt the buffer.
		if (data.includes("\x1b") || data.includes("\x9b")) return;
		if ([...data].some((ch) => {
			const code = ch.charCodeAt(0);
			return code < 32 || code === 0x7f || (code >= 0x80 && code <= 0x9f);
		})) {
			return;
		}
		if (!data) return;
		this.filter += data;
		this.clampSelection();
	}

	render(width: number): string[] {
		const content = Math.max(1, width);
		const total = Math.max(0, width);
		const visible = this.filtered();
		const selected = this.getSelectedItem()?.value;
		const count = theme.dim(`(${visible.length}/${this.items.length}${this.multiSelect ? ` · ${this.checked.size} checked` : ""})`);
		const out = [truncateToWidth(`${theme.dim(this.title)} ${count}`, content)];
		out.push(
			this.filter
				? truncateToWidth(`${theme.dim("Filter:")} ${this.filter}`, content)
				: truncateToWidth(theme.dim("Type to filter"), content),
		);
		if (visible.length === 0) {
			out.push(truncateToWidth(theme.dim("(no matches — esc to go back)"), content));
		} else {
			// Live budget: follow the constructed cap and the viewport so the
			// selection and cancel hint stay visible on short windows.
			for (const item of this.visibleWindow(visible)) {
				const label = `${this.multiSelect ? (this.checked.has(item.value) ? "[x] " : "[ ] ") : ""}${item.label ?? item.value}`;
				const head = item.value === selected
					? `${ICONS.selectedMarker} ${label}`
					: `  ${label}`;
				// Descriptions yield at narrow widths; primary labels carry actions.
				const room = Math.max(0, content - visibleWidth(head) - 2);
				const tail = item.description && room > 4 ? `  ${truncateToWidth(item.description, room)}` : "";
				const row = `${head}${tail}`;
				const padded = `${row}${" ".repeat(Math.max(0, content - visibleWidth(row)))}`;
				out.push(item.value === selected ? selectionBar(padded, content) : padded);
			}
		}
		out.push(truncateToWidth(theme.dim(this.hint), content));
		return out.map((line) => truncateToWidth(line, total));
	}

	invalidate(): void {}
}

/** A selected row is recognizable without color. */
export function isMenuSelection(line: string): boolean {
	return isSelectionRow(line);
}
