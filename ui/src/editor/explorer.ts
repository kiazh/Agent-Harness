// Right-edge lazy workspace explorer (plan §12): a lazy directory tree over
// an injected listing function plus the `ExplorerPane` terminal component.
//
// Contract:
// - Listing is injected (`list(relPath, cursor?, options?)`); the controller
//   binds it to the gateway `workspace.list` RPC ({ path, cursor,
//   includeHidden }) and tests bind fakes. This module never talks to the
//   gateway itself.
// - The backend owns ordering (directories first, stable case-aware names
//   with deterministic ties); the model appends pages verbatim and never
//   re-sorts.
// - Selection is tracked by stable entry identity (the root-relative path),
//   never by rendered row index; a control-bearing or multiline name cannot
//   shift selection or click ownership to a neighboring file.
// - Hidden entries are off by default. Toggling re-requests the root and
//   every expanded directory with `includeHidden: true` (the flag is part of
//   listing identity, never a client-side filter).
// - Directory symlinks: the backend flags `symlink` and only reports
//   `kind: "directory"` for links it is willing to list (outside-root and
//   escaping targets are refused/never probed). Expansion therefore follows
//   `kind` alone: a `symlink` entry renders with the `@` marker, an
//   outside-root link arrives as a non-expandable file, and a structured
//   refusal renders `(denied)`.
// - Two truncation disclosures are kept distinct:
//   * a backend page past its bound keeps a selectable `load more` row and a
//     footer paging affordance;
//   * loaded rows hidden by the pane's optional row budget are disclosed as
//     `+N more` rows (N is the exact hidden-row count on that side).
//   The pane renders every loaded row when no row budget is set (the host
//   owns the viewport in that configuration).

import {
	Key,
	matchesKey,
	type Component,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { displayWidth, sanitizeDisplay, truncateDisplay } from "../design/text.ts";
import { selectionBar, surfaceLine, theme } from "../theme.ts";
import type { WorkspaceListEntry, WorkspaceListResult } from "../workspace/protocol.ts";

/** Disclosure and marker glyphs; every state also carries text, never color alone. */
export const EXPLORER_GLYPHS = {
	expanded: "▾",
	collapsed: "▸",
	file: "·",
	symlink: "@",
} as const;

/** Tree indentation per depth level. */
const INDENT = "  ";
const SELECTED_PREFIX = `${ICONS.selectedMarker} `;
const PLAIN_PREFIX = "  ";

/** Injected listing function; the controller binds it to `workspace.list`. */
export type ExplorerListFn = (
	relPath: string,
	cursor?: string,
	options?: { includeHidden?: boolean },
) => Promise<WorkspaceListResult>;

export type ExplorerDirectoryState = "unloaded" | "loading" | "loaded" | "error";

/** Read-only view of one directory record (do not mutate). */
export interface ExplorerDirectoryView {
	/** Root-relative POSIX path; "" is the workspace root. */
	path: string;
	state: ExplorerDirectoryState;
	expanded: boolean;
	/** Loaded entries, backend order, accumulated across pages. */
	entries: WorkspaceListEntry[];
	nextCursor: string | null;
	truncated: boolean;
	exclusions: string[];
	/** Structured refusal from the backend listing, if any. */
	refused?: { reason: string; message: string };
	/** Transport failure text when the listing threw instead of refusing. */
	error?: string;
	loadingMore: boolean;
}

export interface ExplorerEntryNode {
	kind: "entry";
	/** Stable identity: the entry path, unique across the tree. */
	id: string;
	entry: WorkspaceListEntry;
	parentPath: string;
	depth: number;
	expandable: boolean;
	expanded: boolean;
}

/** Selectable synthetic row for a directory whose backend page was truncated. */
export interface ExplorerLoadMoreNode {
	kind: "load-more";
	id: string;
	parentPath: string;
	depth: number;
}

export type ExplorerVisibleNode = ExplorerEntryNode | ExplorerLoadMoreNode;

interface DirectoryRecord extends ExplorerDirectoryView {
	generation: number;
}

function newRecord(path: string): DirectoryRecord {
	return {
		path,
		state: "unloaded",
		expanded: false,
		entries: [],
		nextCursor: null,
		truncated: false,
		exclusions: [],
		loadingMore: false,
		generation: 0,
	};
}

/** Stable identity for a directory's synthetic `load more` row (paths never contain NUL). */
function moreId(path: string): string {
	return `${path}\u0000more`;
}

/**
 * Lazy tree model. One record per visited directory; unvisited directories
 * cost nothing. Async listings are generation-guarded so a stale response
 * (hidden toggle, refresh, collapse/re-expand) can never overwrite newer
 * state.
 */
export class ExplorerModel {
	private readonly list: ExplorerListFn;
	private readonly records = new Map<string, DirectoryRecord>();
	private readonly listeners = new Set<() => void>();
	private includeHidden: boolean;
	private selectedEntryId: string | null = null;
	/** Last known visible index of the selection; the deterministic fallback. */
	private selectionHint = 0;
	private lastExclusions: string[] = [];

	constructor(list: ExplorerListFn, options: { includeHidden?: boolean } = {}) {
		this.list = list;
		this.includeHidden = options.includeHidden ?? false;
		this.records.set("", newRecord(""));
	}

	/** Subscribe to state changes (selection, expansion, load results). */
	subscribe(listener: () => void): () => void {
		this.listeners.add(listener);
		return () => {
			this.listeners.delete(listener);
		};
	}

	get hidden(): boolean {
		return this.includeHidden;
	}

	selectedId(): string | null {
		return this.selectedEntryId;
	}

	selectedNode(): ExplorerVisibleNode | null {
		if (this.selectedEntryId === null) return null;
		return this.visibleNodes().find((node) => node.id === this.selectedEntryId) ?? null;
	}

	/** Current state for *path*, or undefined when the directory was never visited. */
	directory(path: string): ExplorerDirectoryView | undefined {
		return this.records.get(path);
	}

	/** Active exclusion policy names from the most recent successful listing. */
	exclusions(): string[] {
		return [...this.lastExclusions];
	}

	/** True when a visible directory has a next backend page. */
	hasMorePages(): boolean {
		return this.visibleNodes().some((node) => node.kind === "load-more");
	}

	/** Visible nodes in tree order: loaded entries plus `load more` rows. */
	visibleNodes(): ExplorerVisibleNode[] {
		const out: ExplorerVisibleNode[] = [];
		this.walk("", 0, out);
		return out;
	}

	private walk(dirPath: string, depth: number, out: ExplorerVisibleNode[]): void {
		const record = this.records.get(dirPath);
		if (!record || record.state !== "loaded") return;
		for (const entry of record.entries) {
			const child = this.records.get(entry.path);
			const expanded = child?.expanded === true;
			out.push({
				kind: "entry",
				id: entry.path,
				entry,
				parentPath: dirPath,
				depth,
				expandable: entry.kind === "directory",
				expanded,
			});
			if (expanded) this.walk(entry.path, depth + 1, out);
		}
		if (record.nextCursor !== null) {
			out.push({ kind: "load-more", id: moreId(dirPath), parentPath: dirPath, depth });
		}
	}

	/** Load the root listing. */
	async load(): Promise<void> {
		await this.reload("");
	}

	/** Force a fresh listing for *path* (retryable after refusals/errors). */
	async reload(path: string): Promise<void> {
		const record = this.record(path);
		const generation = ++record.generation;
		record.state = "loading";
		record.entries = [];
		record.loadingMore = false;
		record.refused = undefined;
		record.error = undefined;
		this.reconcileSelection();
		this.emit();
		try {
			const result = await this.list(path, undefined, { includeHidden: this.includeHidden });
			if (record.generation !== generation) return;
			this.applyPage(record, result);
		} catch (error) {
			if (record.generation !== generation) return;
			record.state = "error";
			record.error = error instanceof Error ? error.message : String(error);
			this.reconcileSelection();
		}
		this.emit();
	}

	/** Expand *path*, loading its first page when it was never loaded (or failed). */
	async expand(path: string): Promise<void> {
		const record = this.record(path);
		record.expanded = true;
		this.reconcileSelection();
		this.emit();
		if (record.state === "unloaded" || record.state === "error") {
			await this.reload(path);
		}
	}

	/** Collapse *path*; a selection inside it returns to the directory row. */
	collapse(path: string): void {
		const record = this.records.get(path);
		if (!record || !record.expanded) return;
		record.expanded = false;
		if (this.selectedEntryId !== null && this.isDescendant(this.selectedEntryId, path)) {
			this.selectedEntryId = path;
		}
		this.reconcileSelection();
		this.emit();
	}

	async toggleDirectory(path: string): Promise<void> {
		if (this.record(path).expanded) this.collapse(path);
		else await this.expand(path);
	}

	/** Append the next backend page for *path* (no-op while paging or exhausted). */
	async loadMore(path: string): Promise<void> {
		const record = this.records.get(path);
		if (!record || record.state !== "loaded" || record.nextCursor === null || record.loadingMore) {
			return;
		}
		const cursor = record.nextCursor;
		const generation = record.generation;
		record.loadingMore = true;
		this.emit();
		try {
			const result = await this.list(path, cursor, { includeHidden: this.includeHidden });
			if (record.generation !== generation) return;
			if (result.refused) {
				// Keep the loaded page; stop paging and disclose the refusal.
				record.refused = result.refused;
				record.nextCursor = null;
				record.truncated = false;
			} else {
				record.entries = [...record.entries, ...result.entries];
				record.nextCursor = result.nextCursor ?? null;
				record.truncated = record.nextCursor !== null;
				record.exclusions = [...(result.exclusions ?? [])];
				if (record.exclusions.length > 0) this.lastExclusions = [...record.exclusions];
			}
		} catch {
			// A failed page keeps the loaded rows; the cursor stays for a retry.
		} finally {
			record.loadingMore = false;
			this.emit();
		}
	}

	/**
	 * Re-request listings with a different hidden-entry filter. The flag is
	 * part of listing identity, so every loaded page is invalidated; the root
	 * and every expanded directory are re-requested immediately, collapsed
	 * directories lazily on their next expansion.
	 */
	async setHidden(hidden: boolean): Promise<void> {
		if (this.includeHidden === hidden) return;
		this.includeHidden = hidden;
		const reloadPaths: string[] = [];
		for (const record of this.records.values()) {
			record.generation += 1; // in-flight responses belong to the old filter
			record.entries = [];
			record.nextCursor = null;
			record.truncated = false;
			record.exclusions = [];
			record.refused = undefined;
			record.error = undefined;
			record.loadingMore = false;
			record.state = "unloaded";
			if (record.path === "" || record.expanded) reloadPaths.push(record.path);
		}
		this.reconcileSelection();
		this.emit();
		await Promise.all(reloadPaths.map((path) => this.reload(path)));
	}

	async toggleHidden(): Promise<void> {
		await this.setHidden(!this.includeHidden);
	}

	/** Select by stable identity (null clears). Unknown ids are kept until reconcile. */
	select(id: string | null): void {
		if (this.selectedEntryId === id) return;
		this.selectedEntryId = id;
		if (id !== null) {
			const index = this.visibleNodes().findIndex((node) => node.id === id);
			if (index >= 0) this.selectionHint = index;
		}
		this.emit();
	}

	/** Move the selection among visible nodes; clamps at both ends. */
	move(delta: number): void {
		const nodes = this.visibleNodes();
		if (nodes.length === 0) return;
		const current = nodes.findIndex((node) => node.id === this.selectedEntryId);
		const index = current < 0
			? (delta >= 0 ? 0 : nodes.length - 1)
			: Math.max(0, Math.min(nodes.length - 1, current + delta));
		this.select(nodes[index]!.id);
	}

	/** Left arrow: collapse an expanded directory, else move to the parent. */
	moveToParent(): void {
		const node = this.selectedNode();
		if (!node || node.kind !== "entry") return;
		if (node.expandable && node.expanded) {
			this.collapse(node.entry.path);
			return;
		}
		if (node.parentPath === "") return;
		this.select(node.parentPath);
	}

	/** Right arrow: expand a directory, or move into its first visible child. */
	async moveInto(): Promise<void> {
		const node = this.selectedNode();
		if (!node || node.kind !== "entry" || !node.expandable) return;
		if (!node.expanded) {
			await this.expand(node.entry.path);
			return;
		}
		const nodes = this.visibleNodes();
		const index = nodes.findIndex((candidate) => candidate.id === node.id);
		const child = nodes[index + 1];
		if (child && child.depth === node.depth + 1) this.select(child.id);
	}

	private record(path: string): DirectoryRecord {
		let record = this.records.get(path);
		if (!record) {
			record = newRecord(path);
			this.records.set(path, record);
		}
		return record;
	}

	private applyPage(record: DirectoryRecord, result: WorkspaceListResult): void {
		record.entries = [...result.entries];
		record.nextCursor = result.nextCursor ?? null;
		record.truncated = record.nextCursor !== null;
		record.exclusions = [...(result.exclusions ?? [])];
		record.refused = result.refused;
		record.state = result.refused ? "error" : "loaded";
		if (record.exclusions.length > 0) this.lastExclusions = [...record.exclusions];
		this.reconcileSelection();
	}

	/**
	 * Keep the selection by identity. When the identity left the tree, fall
	 * back to the remembered visible index, clamped into range (never throws,
	 * never silently follows a same-named file elsewhere). While any directory
	 * is still loading, hold the identity: a refresh may bring it back, and
	 * every settled load re-runs this reconcile.
	 */
	private reconcileSelection(): void {
		const nodes = this.visibleNodes();
		if (nodes.length === 0) return; // keep the identity until something is visible
		const index = nodes.findIndex((node) => node.id === this.selectedEntryId);
		if (index >= 0) {
			this.selectionHint = index;
			return;
		}
		for (const record of this.records.values()) {
			if (record.state === "loading") return;
		}
		const fallback = Math.max(0, Math.min(this.selectionHint, nodes.length - 1));
		this.selectedEntryId = nodes[fallback]!.id;
		this.selectionHint = fallback;
	}

	private isDescendant(id: string, dirPath: string): boolean {
		if (dirPath === "") return id !== "";
		return id.startsWith(`${dirPath}/`);
	}

	private emit(): void {
		for (const listener of this.listeners) listener();
	}
}

// ─── pane ────────────────────────────────────────────────────────────────────

export interface ExplorerPaneOptions {
	/**
	 * Optional bound for the tree area rows (header/footer stay outside the
	 * budget). Unset renders every loaded row; the host owns the viewport.
	 * Values below 3 are raised to 3 so one row and two `+N more` markers fit.
	 */
	rowBudget?: number;
	/** Called after any state change that should re-render the pane. */
	onChange?: () => void;
	/** Header title; defaults to the literal `workspace` (plan §12). */
	title?: string;
}

type PaneRow =
	| { type: "header"; toggleStart: number; toggleEnd: number; hidden: boolean }
	| {
		type: "entry";
		node: ExplorerEntryNode;
		selected: boolean;
		glyphColumn: number;
		name: string;
		suffix: string;
		depth: number;
	}
	| { type: "load-more"; node: ExplorerLoadMoreNode; selected: boolean; depth: number; loading: boolean }
	| { type: "placeholder"; depth: number; label: string }
	| { type: "truncation"; hidden: number }
	| { type: "footer"; label: string; value: string };

/** Single physical row per name: control sequences are stripped, newlines collapse. */
function singleLineName(name: string): string {
	return sanitizeDisplay(name).replace(/\n/g, " ");
}

function placeholderLabel(record: ExplorerDirectoryView | undefined): string {
	if (!record || record.state === "loading" || record.state === "unloaded") {
		return ICONS.ellipsisMore;
	}
	if (record.state === "error") {
		if (record.refused) {
			return record.refused.reason === "denied"
				? "(denied)"
				: `(${singleLineName(record.refused.reason) || "refused"})`;
		}
		return "(error)";
	}
	if (record.entries.length === 0) return "(empty)";
	return "";
}

/**
 * Terminal explorer pane (plan §12). Keyboard and mouse map to the same
 * actions; keys only act while the pane is focused.
 */
export class ExplorerPane implements Component {
	/** Set by the focus owner (TUI `Focusable`); unfocused keys are inert. */
	focused = false;
	onOpenFile: ((entry: WorkspaceListEntry) => void) | undefined;
	onPreviewFile: ((entry: WorkspaceListEntry) => void) | undefined;

	private readonly model: ExplorerModel;
	private readonly onChange: (() => void) | undefined;
	private readonly title: string;
	private rowBudget: number | undefined;

	constructor(model: ExplorerModel, options: ExplorerPaneOptions = {}) {
		this.model = model;
		this.onChange = options.onChange;
		this.title = options.title ?? "workspace";
		this.rowBudget = options.rowBudget;
		model.subscribe(() => this.onChange?.());
	}

	setRowBudget(rows: number | undefined): void {
		this.rowBudget = rows;
	}

	getRowBudget(): number | undefined {
		return this.rowBudget;
	}

	invalidate(): void {}

	render(width: number): string[] {
		const total = Math.max(0, Math.floor(width));
		return this.layoutRows().map((row) => {
			const line = truncateDisplay(this.styleRow(row), total);
			if ((row.type === "entry" || row.type === "load-more") && row.selected) {
				return surfaceLine(selectionBar(line, total), total);
			}
			return surfaceLine(line, total);
		});
	}

	handleInput(data: string): void {
		if (!this.focused) return;
		if (matchesKey(data, Key.up)) {
			this.model.move(-1);
			return;
		}
		if (matchesKey(data, Key.down)) {
			this.model.move(1);
			return;
		}
		if (matchesKey(data, Key.left)) {
			this.model.moveToParent();
			return;
		}
		if (matchesKey(data, Key.right)) {
			void this.model.moveInto();
			return;
		}
		if (matchesKey(data, Key.enter) || matchesKey(data, Key.return)) {
			this.activate();
			return;
		}
		if (data === "h") {
			void this.model.toggleHidden();
		}
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult | undefined {
		if (event.button !== "left") return undefined;
		const rows = this.layoutRows();
		const row = rows[event.y];
		if (!row) return undefined;
		if (row.type === "header") {
			if (event.type === "click" && event.x >= row.toggleStart && event.x < row.toggleEnd) {
				void this.model.toggleHidden();
			}
			return { handled: true, focus: true };
		}
		if (event.type === "press") return { handled: true, focus: true };
		if (event.type !== "click") return undefined;
		switch (row.type) {
			case "entry": {
				const node = row.node;
				this.model.select(node.id);
				// The disclosure cell (glyph + its trailing space) toggles.
				if (node.expandable && event.x >= row.glyphColumn && event.x <= row.glyphColumn + 1) {
					void this.model.toggleDirectory(node.entry.path);
					return { handled: true, focus: true, render: true };
				}
				if ((event.clickCount ?? 1) >= 2) {
					if (node.expandable) void this.model.toggleDirectory(node.entry.path);
					else this.onOpenFile?.(node.entry);
					return { handled: true, focus: true, render: true };
				}
				if (!node.expandable) this.onPreviewFile?.(node.entry);
				return { handled: true, focus: true, render: true };
			}
			case "load-more": {
				this.model.select(row.node.id);
				void this.model.loadMore(row.node.parentPath);
				return { handled: true, focus: true, render: true };
			}
			default:
				// Header/footer/placeholder/truncation rows claim focus only.
				return { handled: true, focus: true };
		}
	}

	/** Enter: preview a file, toggle a directory, or page a truncated listing. */
	private activate(): void {
		const node = this.model.selectedNode();
		if (!node) return;
		if (node.kind === "load-more") {
			void this.model.loadMore(node.parentPath);
			return;
		}
		if (node.expandable) {
			void this.model.toggleDirectory(node.entry.path);
			return;
		}
		this.onPreviewFile?.(node.entry);
	}

	private layoutRows(): PaneRow[] {
		const tree = this.windowTree(this.buildTreeRows());
		const rows: PaneRow[] = [
			{
				type: "header",
				toggleStart: displayWidth(this.title) + 1,
				toggleEnd: displayWidth(this.title) + 4,
				hidden: this.model.hidden,
			},
			...tree,
		];
		const exclusions = this.model.exclusions();
		if (exclusions.length > 0) {
			rows.push({
				type: "footer",
				label: "excluded:",
				value: exclusions.map(singleLineName).join(" · "),
			});
		}
		if (this.model.hasMorePages()) {
			rows.push({ type: "footer", label: "paging:", value: 'more entries — select "load more"' });
		}
		return rows;
	}

	private buildTreeRows(): PaneRow[] {
		const selectedId = this.model.selectedId();
		const rows: PaneRow[] = [];
		const root = this.model.directory("");
		if (!root || root.state !== "loaded") {
			rows.push({ type: "placeholder", depth: 0, label: placeholderLabel(root) });
			return rows;
		}
		if (root.entries.length === 0 && root.nextCursor === null) {
			rows.push({ type: "placeholder", depth: 0, label: "(empty)" });
			return rows;
		}
		for (const node of this.model.visibleNodes()) {
			if (node.kind === "load-more") {
				rows.push({
					type: "load-more",
					node,
					selected: node.id === selectedId,
					depth: node.depth,
					loading: this.model.directory(node.parentPath)?.loadingMore === true,
				});
				continue;
			}
			const entry = node.entry;
			const suffix = `${entry.symlink ? ` ${EXPLORER_GLYPHS.symlink}` : ""}${
				entry.excluded ? " (excluded)" : ""
			}`;
			rows.push({
				type: "entry",
				node,
				selected: node.id === selectedId,
				glyphColumn: PLAIN_PREFIX.length + INDENT.length * node.depth,
				name: singleLineName(entry.name),
				suffix,
				depth: node.depth,
			});
			if (!node.expandable || !node.expanded) continue;
			const child = this.model.directory(entry.path);
			if (!child || child.state !== "loaded") {
				rows.push({ type: "placeholder", depth: node.depth + 1, label: placeholderLabel(child) });
			} else if (child.entries.length === 0 && child.nextCursor === null) {
				rows.push({ type: "placeholder", depth: node.depth + 1, label: "(empty)" });
			}
		}
		return rows;
	}

	/**
	 * Bound the tree area to the row budget. The selection stays visible with
	 * minimal scrolling; hidden rows on each side are disclosed as `+N more`.
	 */
	private windowTree(rows: PaneRow[]): PaneRow[] {
		const budget = this.rowBudget;
		if (budget === undefined) return rows;
		const limit = Math.max(3, Math.floor(budget));
		if (rows.length <= limit) return rows;
		const selectedId = this.model.selectedId();
		const selectedIndex = Math.max(0, rows.findIndex((row) => rowId(row) === selectedId));
		const content = Math.max(1, limit - 2);
		const start = Math.max(0, Math.min(selectedIndex - content + 1, rows.length - content));
		const end = Math.min(rows.length, start + content);
		const out: PaneRow[] = [];
		if (start > 0) out.push({ type: "truncation", hidden: start });
		out.push(...rows.slice(start, end));
		if (end < rows.length) out.push({ type: "truncation", hidden: rows.length - end });
		return out;
	}

	private styleRow(row: PaneRow): string {
		switch (row.type) {
			case "header": {
				const affordance = row.hidden ? "[h] hidden on" : "[h] hidden off";
				return `${theme.accentBold(this.title)} ${theme.dim(affordance)}`;
			}
			case "entry": {
				const prefix = row.selected ? SELECTED_PREFIX : PLAIN_PREFIX;
				const glyph = row.node.expandable
					? (row.node.expanded ? EXPLORER_GLYPHS.expanded : EXPLORER_GLYPHS.collapsed)
					: EXPLORER_GLYPHS.file;
				const indent = INDENT.repeat(row.depth);
				const suffix = row.suffix ? theme.dim(row.suffix) : "";
				return `${prefix}${indent}${theme.accent(glyph)} ${theme.text(row.name)}${suffix}`;
			}
			case "load-more": {
				const prefix = row.selected ? SELECTED_PREFIX : PLAIN_PREFIX;
				const tail = row.loading ? ` ${ICONS.ellipsisMore}` : "";
				return `${prefix}${INDENT.repeat(row.depth)}${theme.secondary(`load more${tail}`)}`;
			}
			case "placeholder":
				return `${PLAIN_PREFIX}${INDENT.repeat(row.depth)}${theme.dim(row.label)}`;
			case "truncation":
				return theme.dim(`+${row.hidden} more`);
			case "footer":
				return `${theme.secondary(row.label)} ${theme.dim(row.value)}`;
		}
	}
}

function rowId(row: PaneRow): string | null {
	if (row.type === "entry" || row.type === "load-more") return row.node.id;
	return null;
}
