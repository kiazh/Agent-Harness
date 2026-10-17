// Interactive skills directory built on the house searchable `Menu` (owner
// directive: do it like the theme menu — bounded size, info/hints in the
// bottom hint row — replacing the custom full-screen SkillsPicker).
//
// Contract:
// - Providers are injected; the App binds them to gateway RPCs and tests bind
//   fakes that mirror the real handler payloads field-for-field. This module
//   never talks to the gateway itself.
// - The Menu owns the filter line and the value-based selection. Typing drives
//   a debounced, sequence-guarded `skills.search` (a stale response can never
//   replace a newer one) and the result rows are rebuilt from the response, so
//   the shown rows are exactly what the server matched — never locally
//   re-filtered against the raw query. Selection is preserved by stable
//   `skillId` across result refreshes, filter changes and row-budget changes
//   (terminal resize).
// - Opening a row loads `skills.show` into a second Menu; a call-free
//   `skills.preview_route` view is read-only telemetry; `skills.section` reads
//   one exact versioned passage with an honest stale state.
// - Detail actions exist only where the backend supports them: bundled
//   defaults never gain a delete control, and delete flows through the
//   existing `/skills delete` command with its existing confirmation.

import {
	Key,
	matchesKey,
	type Component,
	type SelectItem,
	type TUI,
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import { singleLine, wrapDisplay } from "../design/text.ts";
import { Menu, type MenuOptions, type MenuSectionItem } from "./Menu.ts";

// ─── gateway contract types (mirror machina/gateway/features/skills.py) ─────

/** One `skills.search` candidate (handler `_search_candidate`). */
export interface SkillsCandidate {
	skillId: string;
	name: string;
	contentRevision: string;
	score: number;
	scoreComponents: Record<string, number>;
	matchedPhrases: string[];
	matchedCapabilities: string[];
	applicability: string;
	reasons: string[];
	workflowStages: string[];
	explanation: string;
	sourceType: string;
	trust: string;
	enabled: boolean;
}

/** `skills.search` diagnostics (search module `execute_search`). */
export interface SkillsSearchDiagnostics {
	classification?: string;
	catalog_size?: number;
	retrieved?: number;
	applicable?: number;
	conditional?: number;
	excluded?: number;
	corrections?: Array<[string, string]>;
	weights_applied?: boolean;
	[key: string]: unknown;
}

/** Closed `skills.search` filter object (handler `_search_filters`). */
export interface SkillsFilters {
	task_family: string;
	capability: string;
	source: string[];
	enabled: boolean | null;
	include_excluded: boolean;
}

export interface SkillsSearchParams {
	query: string;
	filters: SkillsFilters;
	limit: number;
	cursor?: string | null;
}

/** `skills.search` response. */
export interface SkillsSearchResult {
	query: string;
	filters: SkillsFilters;
	limit: number;
	browse: boolean;
	candidates: SkillsCandidate[];
	catalogRevision: string;
	queryDescriptorRevision: string;
	policyRevision: string;
	nextCursor: string | null;
	diagnostics: SkillsSearchDiagnostics;
}

/** `skills.show` skill payload (legacy handler, `_common._skill`). */
export interface SkillsDetailsSkill {
	name: string;
	description: string;
	triggers: string[];
	version: string;
	sourceType: string;
	usageCount: number;
	enabled: boolean;
	content?: string;
}

export interface SkillsPreviewDescriptor {
	task_class: string;
	task_families: string[];
	requested_capabilities: string[];
	explicit_stack: Record<string, string[]>;
	project_stack: Record<string, string[]>;
	inherited_stack: Record<string, string[]>;
	operation_scope: string;
	constraints: string[];
	unresolved_fields: string[];
	evidence: Array<{ field: string; value: string; source: string; detail: string }>;
	task_state_revision: number;
	effective_stack: Record<string, string[]>;
	stack_provenance: string;
}

export interface SkillsPreviewStep {
	step_id: string;
	label: string;
	stage: string;
	skill_id: string;
	section_ids: string[];
	provenance: string;
	estimate_tokens: number;
	estimate_basis: string;
	prerequisites: string[];
}

export interface SkillsPreviewChosen {
	step_id: string;
	skill_id: string;
	skill_revision: string;
	content_revision: string;
	section_id: string;
	heading: string;
	stage: string;
	text: string;
	estimate_tokens: number;
	required_by: string[];
}

export interface SkillsPreviewOmitted {
	locator: {
		skill_id: string;
		content_revision: string;
		section_id: string;
		heading: string;
		stage: string;
	};
	reason: string;
	required_by: string[];
}

export interface SkillsDisclosurePlan {
	task_revision: number;
	stage_revision: number;
	catalog_revision: string;
	stage_source: string;
	current_stage: string;
	chosen: SkillsPreviewChosen[];
	whole_body: Array<{ step_id: string; skill_id: string; content_revision: string; estimate_tokens: number }>;
	required_prerequisites: Array<{ skill_id: string; section_id: string; heading: string; stage: string }>;
	omitted: SkillsPreviewOmitted[];
	unresolved_prerequisites: Array<{ skill_id: string; section_id: string; required_by: string; reason: string }>;
	missing_context: string[];
	task_constraints: string[];
	included_tokens: number;
	token_basis: string;
	diagnostics: Array<{ code: string; message: string; skill_id: string }>;
}

/** `skills.preview_route` response (call-free; spec §12.2). */
export interface SkillsPreviewResult {
	descriptor: SkillsPreviewDescriptor;
	reasons: string[];
	selection: {
		kind: string;
		skills: string[];
		steps: SkillsPreviewStep[];
		catalogRevision: string;
		taskRevision: number;
	};
	disclosure: SkillsDisclosurePlan;
	budget: number;
	tokenBasis: string;
}

/** `skills.section` request (handler `skills_section`). */
export interface SkillsSectionParams {
	skillId: string;
	contentRevision?: string;
	section?: string;
	offset?: number;
	budget?: number;
}

/** One `skills.section` diagnostic (metadata `MetadataDiagnostic` wire shape). */
export interface SkillsSectionDiagnostic {
	code: string;
	message: string;
	skill_id: string;
}

/** `skills.section` response (handler `_section_payload`). */
export interface SkillsSectionResult {
	skillId: string;
	contentRevision: string;
	sectionId: string;
	heading: string;
	text: string;
	start: number;
	end: number;
	total: number;
	sectionTotal: number;
	line: number;
	continuation: number | null;
	stale: boolean;
	bound: boolean;
	anchorId: string;
	budget: number;
	diagnostics: SkillsSectionDiagnostic[];
}

export type SkillsMenuStatus = "idle" | "loading" | "ready" | "error";
export type SkillsMenuMode =
	| "results"
	| "details"
	| "preview"
	| "filters"
	| "filter:family"
	| "filter:capability"
	| "filter:source"
	| "filter:state";

/** Read-only snapshot for host integration and tests. */
export interface SkillsMenuView {
	mode: SkillsMenuMode;
	query: string;
	filters: SkillsFilters;
	status: SkillsMenuStatus;
	error: string;
	classification: string;
	candidates: SkillsCandidate[];
	selected: SkillsCandidate | null;
	selectedSkillId: string | null;
	retrieved: number;
	catalogRevision: string;
	nextCursor: string | null;
	details: SkillsDetailsSkill | null;
	detailsStatus: "idle" | "loading" | "ready" | "error";
	detailsError: string;
	section: SkillsSectionResult | null;
	sectionStatus: "idle" | "loading" | "ready" | "error";
	sectionError: string;
	preview: SkillsPreviewResult | null;
	previewStatus: "idle" | "loading" | "ready" | "error";
	previewError: string;
	notice: string;
}

export type SkillsSearchProvider = (params: SkillsSearchParams) => Promise<SkillsSearchResult>;
export type SkillsDetailsProvider = (skillId: string) => Promise<SkillsDetailsSkill>;
export type SkillsPreviewProvider = (request: string) => Promise<SkillsPreviewResult>;
export type SkillsSectionProvider = (params: SkillsSectionParams) => Promise<SkillsSectionResult>;

export interface SkillsMenuProviders {
	search: SkillsSearchProvider;
	details: SkillsDetailsProvider;
	preview: SkillsPreviewProvider;
	/** Version-bound passage read (`skills.section`, spec §12.2). */
	section: SkillsSectionProvider;
	/**
	 * Empty-query browse: the full catalog shaped like a search result (the
	 * host binds `skills.list`). When present, opening the menu with no query
	 * lists every skill instead of showing the "type to search" idle state;
	 * typing switches to ranked `skills.search`.
	 */
	catalog?: () => Promise<SkillsSearchResult>;
}

export interface SkillsMenuOptions {
	/** Prefilled search text (e.g. `/skills search <query>`). */
	initialQuery?: string;
	/** Provider-call debounce in ms; defaults to 120. */
	debounceMs?: number;
	/** Explicit row cap (defaults to the shared menu default). */
	visibleRows?: number | "full";
	/** Live viewport for row budgets. */
	tui?: TUI;
	/** Overlay title; defaults to the literal `Skills`. */
	title?: string;
	/** Called after any state change that should re-render the pane. */
	onChange?: () => void;
	/** No-match/blank dismissal (Esc at the results level). */
	onClose?: () => void;
	/** Delete action: the host closes the dialog and runs the legacy command. */
	onDelete?: (skillName: string) => void;
}

/** Filter vocabulary; families mirror the gateway's canonical fallback set. */
export const SKILLS_TASK_FAMILIES = [
	"debugging",
	"testing",
	"implementing",
	"reviewing",
	"researching",
	"deploying",
	"documenting",
	"optimizing",
	"securing",
	"planning",
] as const;
export const SKILLS_SOURCES = ["builtin-ecc", "local", "hub", "url", "learned"] as const;
export const SKILLS_STATES = ["any", "enabled", "disabled"] as const;

/** Mirrors the gateway `SKILLS_SEARCH_MAX_QUERY` bound. */
export const SKILLS_QUERY_MAX = 2000;
/** Requested section budget; mirrors the handler default (`SKILLS_SECTION_DEFAULT_BUDGET`). */
export const SKILLS_SECTION_BUDGET = 800;

const SECTION_VALUE = "__section__";
const DEFAULT_DEBOUNCE_MS = 120;
const DEFAULT_LIMIT = 20;
// Content rows (details/preview/section passages) are pre-wrapped to this
// width so a bounded Menu shows the whole explanation instead of clipping it;
// the live terminal still truncates anything narrower (like every Menu row).
const CONTENT_WRAP = 76;
const FILTER_SLOTS = ["family", "capability", "source", "state"] as const;
type FilterSlot = (typeof FILTER_SLOTS)[number];

/** Single physical row: control sequences stripped, newlines collapse. */
function sectionRow(label: string): MenuSectionItem {
	return { value: SECTION_VALUE, label, section: true };
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/**
 * Delete is supported only for non-bundled sources: the gateway refuses
 * `skills.delete` for `builtin-ecc`, so mandatory bundled skills must not
 * render a control that cannot work (§12.3).
 */
export function skillDeletable(skill: { sourceType: string }): boolean {
	return skill.sourceType !== "builtin-ecc";
}

/**
 * Distinct honest state label per `skills.search` classification (spec §9.4);
 * empty string means "ok".
 */
export function classificationLabel(classification: string, query: string): string {
	switch (classification) {
		case "invalid_catalog":
			return "catalog unavailable — no skills loaded";
		case "no_lexical_evidence":
			return query
				? `no lexical evidence for "${query}"`
				: "no query yet — type to search the skill catalog";
		case "missing_capability":
			return "candidates need capabilities that are not available here";
		case "conditional_only":
			return "conditional matches only — no applicable skill";
		case "all_excluded":
			return "all retrieved candidates are excluded";
		default:
			return "";
	}
}

const SECTION_HEADING = /^(?:#{1,6})\s+(.+?)\s*#*\s*$/m;

/**
 * Deterministic anchor of the first markdown heading, matching the server's
 * `slugify_heading` (casefold, non-alphanumerics to `-`, trimmed; empty
 * headings default to `section`). Empty content/headings return `""`, which
 * the reader treats as "no declared section" and continues by offset.
 */
export function firstSectionSlug(content: string): string {
	const match = SECTION_HEADING.exec(content ?? "");
	if (!match) return "";
	const slug = (match[1] ?? "")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "");
	return slug || "section";
}

/** Derive required tools and prerequisites from applicability reasons. */
export function applicabilityDetails(reasons: string[]): { tools: string[]; prerequisites: string[] } {
	const tools: string[] = [];
	const prerequisites: string[] = [];
	const toolPrefixes = ["tool availability unknown: ", "missing required tool: "];
	for (const reason of reasons) {
		const toolPrefix = toolPrefixes.find((prefix) => reason.startsWith(prefix));
		if (toolPrefix) {
			for (const tool of reason.slice(toolPrefix.length).split(", ")) {
				if (tool.trim() && !tools.includes(tool.trim())) tools.push(tool.trim());
			}
			continue;
		}
		const preconditionPrefix = "unverified precondition: ";
		if (reason.startsWith(preconditionPrefix)) {
			for (const item of reason.slice(preconditionPrefix.length).split("; ")) {
				if (item.trim() && !prerequisites.includes(item.trim())) prerequisites.push(item.trim());
			}
		}
	}
	return { tools, prerequisites };
}

/**
 * Menu whose row set is exactly the rows it was constructed with: the server
 * already narrowed the candidate set for the typed query, so the Menu's own
 * filter line drives the search and echoes the query but never re-filters the
 * server's matches against the raw query string (which would hide rows the
 * server matched through stemming, reasons or capabilities).
 */
class ResultMenu extends Menu {
	private readonly rows: SelectItem[];

	constructor(title: string, rows: SelectItem[], options: MenuOptions) {
		super(title, rows, options);
		this.rows = rows;
	}

	override filtered(): SelectItem[] {
		return this.rows;
	}
}

/**
 * Menu-shaped skills directory. The host owns focus and dismissal; keys only
 * act while `focused`.
 */
export class SkillsMenu implements Component {
	/** Set by the focus owner (TUI `Focusable`); unfocused keys are inert. */
	focused = false;
	/** Dismissal callback (Esc at the results level). */
	onClose: (() => void) | undefined;
	/** Delete action: the host closes the dialog and runs the legacy command. */
	onDelete: ((skillName: string) => void) | undefined;

	private readonly providers: SkillsMenuProviders;
	private readonly debounceMs: number;
	private readonly limit: number;
	private readonly title: string;
	private readonly onChange: (() => void) | undefined;
	private visibleRows: number | "full" | undefined;
	private readonly tui: TUI | undefined;

	private mode: SkillsMenuMode = "results";
	private query = "";
	private filters: SkillsFilters = { task_family: "", capability: "", source: [], enabled: null, include_excluded: true };
	private filterSlot: FilterSlot = "family";

	private candidates: SkillsCandidate[] = [];
	private selectedSkillId: string | null = null;
	private status: SkillsMenuStatus = "idle";
	private errorText = "";
	private classification = "";
	private retrieved = 0;
	private catalogRevision = "";
	private nextCursor: string | null = null;
	private notice = "";

	private details: SkillsDetailsSkill | undefined;
	private detailsSkillId: string | null = null;
	private detailsStatus: "idle" | "loading" | "ready" | "error" = "idle";
	private detailsError = "";
	/** Row value the details menu should keep selected (the read passage). */
	private detailsAnchor: string | undefined;

	private section: SkillsSectionResult | undefined;
	private sectionSkillId: string | null = null;
	private sectionStatus: "idle" | "loading" | "ready" | "error" = "idle";
	private sectionError = "";

	private preview: SkillsPreviewResult | undefined;
	private previewStatus: "idle" | "loading" | "ready" | "error" = "idle";
	private previewError = "";
	private previewReturn: "results" | "details" = "results";

	private requestSeq = 0;
	private previewSeq = 0;
	private sectionSeq = 0;
	private timer: ReturnType<typeof setTimeout> | null = null;
	/** P0-23: once stopLiveWork runs, late async continuations are dropped until activate. */
	private stopped = false;

	private menu: ResultMenu | Menu;

	constructor(providers: SkillsMenuProviders, options: SkillsMenuOptions = {}) {
		this.providers = providers;
		this.query = (options.initialQuery ?? "").slice(0, SKILLS_QUERY_MAX);
		this.debounceMs = Math.max(0, options.debounceMs ?? DEFAULT_DEBOUNCE_MS);
		this.limit = DEFAULT_LIMIT;
		this.title = options.title ?? "Skills";
		this.onChange = options.onChange;
		this.visibleRows = options.visibleRows;
		this.tui = options.tui;
		this.onClose = options.onClose;
		this.onDelete = options.onDelete;
		this.menu = this.buildMenu();
	}

	/** Open the dialog: reset state, then immediately request the query. */
	activate(): void {
		this.stopped = false;
		this.clearTimer();
		this.mode = "results";
		this.candidates = [];
		this.selectedSkillId = null;
		this.status = "idle";
		this.errorText = "";
		this.classification = "";
		this.retrieved = 0;
		this.catalogRevision = "";
		this.nextCursor = null;
		this.notice = "";
		this.details = undefined;
		this.detailsSkillId = null;
		this.detailsStatus = "idle";
		this.detailsError = "";
		this.section = undefined;
		this.sectionSkillId = null;
		this.sectionStatus = "idle";
		this.sectionError = "";
		this.preview = undefined;
		this.previewStatus = "idle";
		this.previewError = "";
		this.focused = true;
		this.sync();
		this.schedule(true);
	}

	/** Close the dialog; in-flight responses are dropped. */
	dismiss(): void {
		// P0-23: dismiss is the same funnel as close — stop live work first so
		// no pending provider timer or in-flight continuation survives the
		// dismissal, then report the close.
		this.stopLiveWork();
		this.onClose?.();
	}

	/**
	 * P0-23: kill every piece of live work ([the] debounce timer plus all
	 * request sequences: pending and in-flight search/preview/section
	 * continuations see a stale sequence or the stopped flag and drop).
	 * Idempotent; `activate()` revives. The host runs this from its one
	 * close funnel (including delete-close and exit `dialog.cancel()`),
	 * which do not reach `dismiss()`.
	 */
	stopLiveWork(): void {
		if (this.stopped) return;
		this.stopped = true;
		this.clearTimer();
		this.requestSeq += 1;
		this.previewSeq += 1;
		this.sectionSeq += 1;
		this.focused = false;
	}

	/** Update the row budget (terminal resize); selection stays by skill ID. */
	setVisibleRows(rows: number | "full"): void {
		this.visibleRows = rows;
		this.menu.setVisibleRows(rows);
		this.emit();
	}

	getView(): SkillsMenuView {
		return {
			mode: this.mode,
			query: this.query,
			filters: { ...this.filters, source: [...this.filters.source] },
			status: this.status,
			error: this.errorText,
			classification: this.classification,
			candidates: [...this.candidates],
			selected: this.currentCandidate(),
			selectedSkillId: this.selectedSkillId,
			retrieved: this.retrieved,
			catalogRevision: this.catalogRevision,
			nextCursor: this.nextCursor,
			details: this.details ?? null,
			detailsStatus: this.detailsStatus,
			detailsError: this.detailsError,
			section: this.section ?? null,
			sectionStatus: this.sectionStatus,
			sectionError: this.sectionError,
			preview: this.preview ?? null,
			previewStatus: this.previewStatus,
			previewError: this.previewError,
			notice: this.notice,
		};
	}

	render(width: number): string[] {
		return this.menu.render(width);
	}

	handleInput(data: string): void {
		if (!this.focused) return;
		switch (this.mode) {
			case "results":
				return this.handleResults(data);
			case "details":
				return this.handleDetails(data);
			default:
				// preview + filters + filter slots: the Menu handles navigation,
				// Enter (onSelect) and Esc (onCancel).
				this.menu.handleInput(data);
				this.emit();
		}
	}

	handleMouse(event: TuiMouseEvent): ReturnType<NonNullable<Component["handleMouse"]>> {
		return this.menu.handleMouse(event);
	}

	invalidate(): void {
		this.menu.invalidate();
	}

	// ─── menus ─────────────────────────────────────────────────────────────

	private buildMenu(): ResultMenu | Menu {
		const { title, rows, hint } = this.viewModel();
		const menu = this.mode === "results"
			? new ResultMenu(title, rows, { hint, visibleRows: this.visibleRows, tui: this.tui, initialValue: this.selectedSkillId ?? undefined })
			: new Menu(title, rows, {
				hint,
				visibleRows: this.visibleRows,
				tui: this.tui,
				initialValue: this.mode === "details" ? this.detailsAnchor : undefined,
			});
		menu.onCancel = () => this.onEscape();
		switch (this.mode) {
			case "results":
				menu.onSelect = () => this.openDetails(false);
				menu.onSelectionChange = (item) => {
					this.selectedSkillId = item.value;
					this.emit();
				};
				break;
			case "details":
				menu.onSelect = () => { this.mode = "results"; this.sync(); };
				break;
			case "preview":
				menu.onSelect = () => { this.mode = this.previewReturn; this.sync(); };
				break;
			case "filters":
				menu.onSelect = (item) => this.openFilterSlot(item.value);
				break;
			default:
				menu.onSelect = (item) => this.applyFilterSlot(item.value);
				break;
		}
		return menu;
	}

	/** Rebuild the current Menu from live state, preserving the results filter. */
	private sync(): void {
		const menu = this.buildMenu();
		this.menu = menu;
		if (this.mode === "results") {
			if (this.query) menu.handleInput(this.query);
			const selected = menu.getSelectedItem();
			if (selected) this.selectedSkillId = selected.value;
		}
		this.emit();
	}

	private onEscape(): void {
		switch (this.mode) {
			case "results":
				this.dismiss();
				return;
			case "details":
			case "filters":
				this.mode = "results";
				this.sync();
				return;
			case "preview":
				this.mode = this.previewReturn;
				this.sync();
				return;
			default:
				this.mode = "filters";
				this.sync();
		}
	}

	private viewModel(): { title: string; rows: SelectItem[]; hint: string } {
		switch (this.mode) {
			case "details": {
				const candidate = this.currentCandidate();
				return {
					title: candidate ? singleLine(candidate.name) : "Skill",
					rows: this.detailsItems(),
					hint: this.detailsHint(),
				};
			}
			case "preview":
				return {
					title: `Route preview: ${singleLine(this.query) || "—"}`,
					rows: this.previewItems(),
					hint: "esc back · preview only — no turn, no provider call, no installation, no approval change",
				};
			case "filters":
				return { title: "Filters", rows: this.filterSlotItems(), hint: "enter choose · esc back" };
			default:
				if (this.mode.startsWith("filter:")) {
					return { title: this.filterSlot, rows: this.slotChoiceItems(), hint: "enter apply · esc back" };
				}
				return { title: this.title, rows: this.resultsItems(), hint: this.resultsHint() };
		}
	}

	// ─── results ───────────────────────────────────────────────────────────

	private resultsItems(): SelectItem[] {
		const items: SelectItem[] = [];
		if (this.notice) items.push(sectionRow(`(${this.notice})`));
		if (this.status === "loading" && this.candidates.length === 0) {
			items.push(sectionRow("(searching…)"));
		} else if (this.status === "error" && this.candidates.length === 0) {
			items.push(sectionRow(`(search failed: ${this.errorText})`));
		} else if (this.candidates.length === 0) {
			items.push(sectionRow(`(${this.emptyLabel()})`));
		} else {
			if (this.status === "error") {
				items.push(sectionRow(`(search failed: ${this.errorText} — earlier results shown)`));
			} else {
				const banner = classificationLabel(this.classification, this.query);
				if (banner) items.push(sectionRow(`! ${banner}`));
			}
			for (const candidate of this.candidates) {
				items.push({
					value: candidate.skillId,
					label: singleLine(candidate.name),
					description: singleLine(this.candidateDescription(candidate)),
				});
			}
		}
		return items;
	}

	private candidateDescription(candidate: SkillsCandidate): string {
		const stages = candidate.workflowStages.length > 0 ? ` · ${candidate.workflowStages.join("/")}` : "";
		return `[${candidate.applicability}] ${candidate.trust} (${candidate.sourceType})${stages}`;
	}

	private emptyLabel(): string {
		if (this.status === "error") return `search failed: ${this.errorText}`;
		if (this.status === "loading") return "searching…";
		const label = classificationLabel(this.classification, this.query);
		if (label) return label;
		if (this.status === "idle") return "type to search the skill catalog";
		return "no matches";
	}

	private resultsHint(): string {
		const parts = [`${this.candidates.length} shown`];
		if (this.status === "loading") parts.push("searching…");
		parts.push("ctrl+f filters", "↑↓ move", "enter details", "ctrl+r preview", "ctrl+s section");
		if (this.nextCursor) parts.push("ctrl+n more");
		parts.push("esc close");
		return parts.join(" · ");
	}

	// ─── details ───────────────────────────────────────────────────────────

	private detailsItems(): SelectItem[] {
		const candidate = this.currentCandidate();
		if (!candidate) return [sectionRow("(no skill selected)")];
		const items: SelectItem[] = [];
		const push = (text: string) => {
			for (const line of wrapDisplay(text, CONTENT_WRAP)) {
				items.push({ value: `detail:${items.length}`, label: line });
			}
		};
		push(`applicability: ${candidate.applicability || "unknown"}`);
		const details = this.details;
		if (this.detailsStatus === "loading") {
			items.push(sectionRow("(loading details…)"));
		} else if (this.detailsStatus === "error") {
			items.push(sectionRow(`(details unavailable: ${this.detailsError})`));
		} else if (details) {
			push(`purpose: ${singleLine(details.description) || "—"}`);
			push(`source: ${candidate.trust} (${details.sourceType}) · version ${singleLine(details.version) || "—"} · content ${candidate.contentRevision.slice(0, 8) || "—"}`);
		}
		push(`reasons: ${singleLine(candidate.reasons.join("; ")) || "none — applicable"}`);
		const matched = [...candidate.matchedPhrases, ...candidate.matchedCapabilities];
		if (matched.length > 0) push(`matched: ${singleLine(matched.join(", "))}`);
		push(`stages: ${singleLine(candidate.workflowStages.join(", ")) || "—"}`);
		const derived = applicabilityDetails(candidate.reasons);
		push(`prerequisites: ${singleLine(derived.prerequisites.join("; ")) || "(none reported)"}`);
		push(`required tools: ${singleLine(derived.tools.join(", ")) || "(none reported)"}`);
		if (this.detailsStatus === "ready" && details) {
			const size = details.content?.length ?? 0;
			push(`instructions: ${size} chars — read with skills.section (content ${candidate.contentRevision.slice(0, 8) || "—"})`);
			push(`usage: ${details.usageCount} · enabled: ${details.enabled ? "yes" : "no"}`);
		}
		items.push(...this.sectionItems(candidate));
		return items;
	}

	private detailsHint(): string {
		const candidate = this.currentCandidate();
		if (!candidate) return "esc back";
		const actions = ["enter/esc back", "ctrl+r preview"];
		const pending = this.sectionSkillId === candidate.skillId ? this.section : undefined;
		actions.push(pending && pending.continuation !== null && !pending.stale ? "ctrl+s continue section" : "ctrl+s read section");
		if (skillDeletable(candidate) && (!this.details || skillDeletable(this.details))) actions.push("d delete");
		else actions.push("delete unavailable (bundled default)");
		return actions.join(" · ");
	}

	/**
	 * Version-bound section passage for the selected candidate (spec §10.4).
	 * A stale response renders the honest stale state with empty text.
	 */
	private sectionItems(candidate: SkillsCandidate): SelectItem[] {
		const build = (texts: string[]): SelectItem[] => {
			const rows: SelectItem[] = [];
			for (const text of texts) {
				for (const line of wrapDisplay(text, CONTENT_WRAP)) {
					rows.push({ value: `detail:section:${rows.length}`, label: line });
				}
			}
			return rows;
		};
		if (this.sectionStatus === "loading") return build(["(reading section…)"]);
		if (this.sectionStatus === "error" && this.sectionSkillId === candidate.skillId) {
			return build([`(section read failed: ${this.sectionError})`]);
		}
		if (this.sectionSkillId !== candidate.skillId) return [];
		const section = this.section;
		if (!section) return [];
		if (section.stale) {
			const current = section.contentRevision ? section.contentRevision.slice(0, 8) : "unknown";
			const detail = section.diagnostics[0]?.message ?? "";
			const rows = build([
				`section stale: ${section.sectionId || section.anchorId || "body"} was requested at content ${candidate.contentRevision.slice(0, 8) || "—"}, but the skill changed to ${current} — nothing from the edited text was served`,
				detail ? `${detail} · press s to re-read content ${current}` : `press s to re-read content ${current}`,
			]);
			// The stale notice is the passage's anchor: keep it in the bounded window.
			if (rows[0]) rows[0].value = "detail:section-stale";
			return rows;
		}
		const rows = build([
			`section: ${section.heading || section.sectionId || "body"} (${section.text.length} of ${section.sectionTotal} chars · ${section.bound ? "declared" : "parsed anchor"} · content ${section.contentRevision.slice(0, 8) || "—"})`,
		]);
		if (rows[0]) rows[0].value = "detail:section-head";
		rows.push(...build([section.text || "(empty section)"]));
		if (section.continuation !== null) {
			rows.push(...build([`[section continues at offset ${section.continuation} — press s]`]));
		}
		return rows;
	}

	// ─── preview ───────────────────────────────────────────────────────────

	private previewItems(): SelectItem[] {
		const rows: SelectItem[] = [];
		const push = (text: string) => {
			for (const line of wrapDisplay(text, CONTENT_WRAP)) {
				rows.push({ value: `preview:${rows.length}`, label: line });
			}
		};
		if (this.previewStatus === "loading") {
			push("(building preview…)");
			return rows;
		}
		if (this.previewStatus === "error") {
			push(`(preview failed: ${this.previewError})`);
			return rows;
		}
		const preview = this.preview;
		if (!preview) {
			push("(no preview yet)");
			return rows;
		}
		const descriptor = preview.descriptor;
		push(`class: ${descriptor.task_class} · scope: ${descriptor.operation_scope} · families: ${descriptor.task_families.join(", ") || "—"}`);
		push(`stack: ${this.stackSummary(descriptor)} · provenance: ${descriptor.stack_provenance} · unresolved: ${descriptor.unresolved_fields.join(", ") || "—"}`);
		push(`reasons: ${singleLine(preview.reasons.join("; ")) || "none"}`);
		push(`selection: ${preview.selection.kind} · skills: ${preview.selection.skills.join(", ") || "—"}`);
		const steps = preview.selection.steps;
		if (steps.length === 0) {
			push("steps: —");
		} else {
			steps.forEach((step, index) => {
				push(`${index === 0 ? "steps: " : "       "}${index + 1}. ${singleLine(step.label)} (${step.skill_id}, stage ${step.stage || "—"}, ~${step.estimate_tokens} tok)`);
			});
		}
		const disclosure = preview.disclosure;
		const chosen = disclosure.chosen.map((item) => `${item.skill_id} · ${singleLine(item.heading || item.section_id || "body")}`).join(", ");
		push(`disclosure: included ${disclosure.included_tokens}/${preview.budget} tok (${disclosure.token_basis}) · chosen: ${chosen || "—"}`);
		if (disclosure.omitted.length > 0) push(`omitted: ${disclosure.omitted.length} section(s) — ${singleLine(disclosure.omitted[0]!.reason)}`);
		if (disclosure.unresolved_prerequisites.length > 0) {
			push(`unresolved prerequisites: ${disclosure.unresolved_prerequisites.map((item) => `${item.skill_id}·${item.section_id}`).join(", ")}`);
		}
		push(`missing context: ${singleLine(disclosure.missing_context.join("; ")) || "(none)"}`);
		push(`task constraints: ${singleLine(disclosure.task_constraints.join("; ")) || "(none)"}`);
		return rows;
	}

	private stackSummary(descriptor: SkillsPreviewDescriptor): string {
		const stack = descriptor.effective_stack ?? {};
		const categories = Object.keys(stack).sort().map((category) => `${category}: ${(stack[category] ?? []).join(", ")}`);
		return categories.join("; ") || "unknown";
	}

	// ─── filters ───────────────────────────────────────────────────────────

	private filterSlotItems(): SelectItem[] {
		const slot = (value: FilterSlot, label: string, current: string) => ({ value, label, description: current || "any" });
		return [
			slot("family", "task family", this.filters.task_family),
			slot("capability", "capability", this.filters.capability),
			slot("source", "source", this.filters.source[0] ?? ""),
			slot("state", "enabled state", this.stateName()),
		];
	}

	private openFilterSlot(slot: string): void {
		const found = FILTER_SLOTS.find((candidate) => candidate === slot);
		if (!found) return;
		this.filterSlot = found;
		this.mode = `filter:${found}` as SkillsMenuMode;
		this.sync();
	}

	private slotChoiceItems(): SelectItem[] {
		const any: SelectItem = { value: "__any__", label: "any" };
		switch (this.filterSlot) {
			case "family":
				return [any, ...SKILLS_TASK_FAMILIES.map((value) => ({ value, label: value }))];
			case "source":
				return [any, ...SKILLS_SOURCES.map((value) => ({ value, label: value }))];
			case "state":
				return [any, ...SKILLS_STATES.filter((value) => value !== "any").map((value) => ({ value, label: value }))];
			default: {
				const capabilities = [...new Set(this.candidates.flatMap((candidate) => candidate.matchedCapabilities))].sort();
				return [any, ...capabilities.map((value) => ({ value, label: value }))];
			}
		}
	}

	private applyFilterSlot(value: string): void {
		const clear = value === "__any__";
		switch (this.filterSlot) {
			case "family":
				this.filters.task_family = clear ? "" : value;
				break;
			case "capability":
				this.filters.capability = clear ? "" : value;
				break;
			case "source":
				this.filters.source = clear ? [] : [value];
				break;
			case "state":
				this.filters.enabled = clear ? null : value === "enabled";
				this.filters.include_excluded = this.filters.enabled !== true;
				break;
		}
		this.mode = "results";
		this.schedule(false);
		this.sync();
	}

	private stateName(): string {
		if (this.filters.enabled === true) return "enabled";
		if (this.filters.enabled === false) return "disabled";
		return "any";
	}

	// ─── input ─────────────────────────────────────────────────────────────

	private handleResults(data: string): void {
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
			this.dismiss();
			return;
		}
		if (matchesKey(data, Key.ctrl("r"))) {
			this.startPreview();
			return;
		}
		if (matchesKey(data, Key.ctrl("f"))) {
			this.mode = "filters";
			this.sync();
			return;
		}
		if (matchesKey(data, Key.ctrl("s"))) {
			this.openDetails(true);
			return;
		}
		if (matchesKey(data, Key.ctrl("n"))) {
			void this.loadMore();
			return;
		}
		if (matchesKey(data, Key.enter)) {
			this.openDetails(false);
			return;
		}
		const before = this.menu.getFilter();
		this.menu.handleInput(data);
		const after = this.menu.getFilter();
		if (after !== before) {
			this.query = after;
			this.schedule(false);
		}
		this.emit();
	}

	private handleDetails(data: string): void {
		if (matchesKey(data, Key.ctrl("r"))) {
			this.startPreview();
			return;
		}
		if (data === "s" || matchesKey(data, Key.ctrl("s"))) {
			this.readSection();
			return;
		}
		if (data === "d") {
			this.deleteSelected();
			return;
		}
		this.menu.handleInput(data);
		this.emit();
	}

	private deleteSelected(): void {
		const candidate = this.currentCandidate();
		if (!candidate) return;
		if (skillDeletable(candidate) && (!this.details || skillDeletable(this.details))) {
			this.onDelete?.(candidate.name);
		}
	}

	// ─── search plumbing ───────────────────────────────────────────────────

	private schedule(immediate: boolean): void {
		this.clearTimer();
		const seq = ++this.requestSeq;
		const query = this.query;
		const filters: SkillsFilters = { ...this.filters, source: [...this.filters.source] };
		const run = (): void => {
			void this.runRequest(seq, query, filters);
		};
		if (immediate) run();
		else this.timer = setTimeout(run, this.debounceMs);
	}

	private async runRequest(seq: number, query: string, filters: SkillsFilters): Promise<void> {
		if (seq !== this.requestSeq) return;
		this.status = "loading";
		this.sync();
		try {
			// Empty query browses the whole catalog when the host binds one
			// (`skills.list`); any typed query uses ranked `skills.search`.
			const result =
				query.trim() === "" && this.providers.catalog
					? await this.providers.catalog()
					: await this.providers.search({ query, filters, limit: this.limit });
			if (seq !== this.requestSeq) return;
			this.apply(result, { append: false });
		} catch (error) {
			if (seq !== this.requestSeq) return;
			this.status = "error";
			this.errorText = singleLine(message(error));
			this.sync();
		}
	}

	/** Append the next page; `nextCursor` is the opaque cursor from the backend. */
	private async loadMore(): Promise<void> {
		const cursor = this.nextCursor;
		if (!cursor) return;
		this.clearTimer();
		const seq = ++this.requestSeq;
		this.status = "loading";
		this.sync();
		try {
			const result = await this.providers.search({
				query: this.query,
				filters: { ...this.filters, source: [...this.filters.source] },
				limit: this.limit,
				cursor,
			});
			if (seq !== this.requestSeq) return;
			this.apply(result, { append: true });
		} catch (error) {
			if (seq !== this.requestSeq) return;
			this.status = "error";
			this.errorText = singleLine(message(error));
			this.sync();
		}
	}

	private apply(result: SkillsSearchResult, options: { append: boolean }): void {
		const page = Array.isArray(result.candidates) ? result.candidates : [];
		if (options.append) {
			const known = new Set(this.candidates.map((candidate) => candidate.skillId));
			for (const candidate of page) {
				if (!known.has(candidate.skillId)) {
					known.add(candidate.skillId);
					this.candidates.push(candidate);
				}
			}
		} else {
			this.candidates = [...page];
		}
		this.retrieved = Number.isFinite(result.diagnostics?.retrieved) ? Number(result.diagnostics.retrieved) : this.candidates.length;
		this.classification = typeof result.diagnostics?.classification === "string" ? String(result.diagnostics.classification) : "";
		this.catalogRevision = typeof result.catalogRevision === "string" ? result.catalogRevision : "";
		this.nextCursor = typeof result.nextCursor === "string" && result.nextCursor ? result.nextCursor : null;
		this.status = "ready";
		this.errorText = "";
		this.reconcileSelection();
		this.sync();
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

	// ─── selection ─────────────────────────────────────────────────────────

	private currentCandidate(): SkillsCandidate | null {
		return this.candidates.find((candidate) => candidate.skillId === this.selectedSkillId) ?? null;
	}

	private reconcileSelection(): void {
		if (this.selectedSkillId !== null && this.candidates.some((candidate) => candidate.skillId === this.selectedSkillId)) return;
		this.selectedSkillId = this.candidates[0]?.skillId ?? null;
	}

	// ─── details + preview ─────────────────────────────────────────────────

	private openDetails(read: boolean): void {
		const candidate = this.currentCandidate();
		if (!candidate) return;
		this.mode = "details";
		if (this.detailsSkillId !== candidate.skillId) {
			this.details = undefined;
			this.detailsSkillId = candidate.skillId;
			this.detailsStatus = "loading";
			this.detailsError = "";
			this.section = undefined;
			this.sectionSkillId = null;
			this.sectionStatus = "idle";
			this.sectionError = "";
			this.detailsAnchor = undefined;
		}
		this.sync();
		void this.loadDetails(candidate.skillId);
		if (read) this.readSection();
	}

	private async loadDetails(skillId: string): Promise<void> {
		try {
			const skill = await this.providers.details(skillId);
			if (this.stopped || this.detailsSkillId !== skillId) return;
			this.details = skill;
			this.detailsStatus = "ready";
		} catch (error) {
			if (this.stopped || this.detailsSkillId !== skillId) return;
			this.detailsStatus = "error";
			this.detailsError = singleLine(message(error));
		}
		if (this.mode === "details") this.sync();
	}

	private startPreview(): void {
		const request = this.query.trim();
		if (!request) {
			this.notice = "type a request in the search box first";
			this.sync();
			return;
		}
		this.previewReturn = this.mode === "details" ? "details" : "results";
		this.mode = "preview";
		this.previewStatus = "loading";
		this.preview = undefined;
		this.previewError = "";
		this.sync();
		void this.loadPreview(request);
	}

	private async loadPreview(request: string): Promise<void> {
		const seq = ++this.previewSeq;
		try {
			const preview = await this.providers.preview(request);
			if (this.stopped || seq !== this.previewSeq || this.mode !== "preview") return;
			this.preview = preview;
			this.previewStatus = "ready";
		} catch (error) {
			if (this.stopped || seq !== this.previewSeq || this.mode !== "preview") return;
			this.previewStatus = "error";
			this.previewError = singleLine(message(error));
		}
		this.sync();
	}

	// ─── section reads (spec §10.4/§12.2, no new authority) ────────────────

	/**
	 * Read one exact versioned passage of the selected skill. The first press
	 * addresses the skill's first declared/parsed section (offset 0 when there
	 * is no heading). A pending continuation reads the next page of the same
	 * `contentRevision`; after a stale response the retry uses the revision the
	 * server reported as current. Read-only: no turn, tool run, install or
	 * approval change.
	 */
	private readSection(): void {
		const candidate = this.currentCandidate();
		if (!candidate) return;
		const skillId = candidate.skillId;
		const previous = this.sectionSkillId === skillId ? this.section : undefined;
		const revision = previous?.stale && previous.contentRevision ? previous.contentRevision : candidate.contentRevision;
		const params: SkillsSectionParams = { skillId, contentRevision: revision, budget: SKILLS_SECTION_BUDGET };
		if (previous?.sectionId) {
			params.section = previous.sectionId;
			if (previous.continuation !== null && !previous.stale) params.offset = previous.continuation;
		} else {
			const sectionId = previous && !previous.stale && previous.sectionId ? previous.sectionId : firstSectionSlug(this.details?.content ?? "");
			if (sectionId) params.section = sectionId;
			else if (previous && previous.continuation !== null && !previous.stale) params.offset = previous.continuation;
			else params.offset = 0;
		}
		this.sectionSkillId = skillId;
		this.sectionStatus = "loading";
		this.sectionError = "";
		this.sync();
		void this.loadSection(++this.sectionSeq, params);
	}

	private async loadSection(seq: number, params: SkillsSectionParams): Promise<void> {
		try {
			const result = await this.providers.section(params);
			if (this.stopped || seq !== this.sectionSeq || this.sectionSkillId !== params.skillId) return;
			this.section = result;
			this.sectionStatus = "ready";
			this.detailsAnchor = result.stale ? "detail:section-stale" : "detail:section-head";
		} catch (error) {
			if (this.stopped || seq !== this.sectionSeq || this.sectionSkillId !== params.skillId) return;
			this.sectionStatus = "error";
			this.sectionError = singleLine(message(error));
		}
		this.sync();
	}
}
