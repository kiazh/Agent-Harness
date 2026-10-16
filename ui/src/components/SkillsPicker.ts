// Interactive skills directory overlay (spec §12.3, §18 U01–U07): a bounded,
// search-first picker over the gateway `skills.search` / `skills.show` /
// `skills.preview_route` contracts.
//
// Contract:
// - Providers are injected; the App binds them to gateway RPCs and tests bind
//   fakes that mirror the real handler payloads field-for-field. This module
//   never talks to the gateway itself.
// - Search requests are debounced and sequence-guarded: a stale response can
//   never replace a newer one, and typed filter text survives failures (U06).
// - Selection is tracked by stable `skillId` (never array index) so provider
//   refreshes, filter changes and row-budget changes (terminal resize) keep
//   the same skill selected (U02).
// - Every rendered label is sanitized to a single physical row before layout;
//   mouse hit-testing reuses the same layout map, so a hostile label can never
//   corrupt rows or misroute a click (U03).
// - Detail actions exist only where the backend supports them: bundled
//   defaults never gain a delete control, and delete flows through the
//   existing `/skills delete` command with its existing confirmation (§12.3).
// - Preview is read-only telemetry: it calls `skills.preview_route` and never
//   starts a turn, provider call, tool run, install or approval change (U04).

import {
	Key,
	matchesKey,
	type Component,
	type TuiMouseEvent,
	type TuiMouseEventResult,
} from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { sanitizeDisplay, truncateDisplay, wrapDisplay } from "../design/text.ts";
import { selectionBar, surfaceLine, theme } from "../theme.ts";

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

export type SkillsPickerStatus = "idle" | "loading" | "ready" | "error";
export type SkillsPickerMode = "results" | "details" | "preview";
export type SkillsPickerFocus = "query" | "filters";

/** Read-only snapshot for host integration and tests. */
export interface SkillsPickerView {
	mode: SkillsPickerMode;
	query: string;
	focus: SkillsPickerFocus;
	filters: SkillsFilters;
	status: SkillsPickerStatus;
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
	preview: SkillsPreviewResult | null;
	previewStatus: "idle" | "loading" | "ready" | "error";
	previewError: string;
	notice: string;
}

export type SkillsSearchProvider = (params: SkillsSearchParams) => Promise<SkillsSearchResult>;
export type SkillsDetailsProvider = (skillId: string) => Promise<SkillsDetailsSkill>;
export type SkillsPreviewProvider = (request: string) => Promise<SkillsPreviewResult>;

export interface SkillsPickerProviders {
	search: SkillsSearchProvider;
	details: SkillsDetailsProvider;
	preview: SkillsPreviewProvider;
}

export interface SkillsPickerOptions {
	/** Prefilled search text (e.g. `/skills search <query>`). */
	initialQuery?: string;
	/** Provider-call debounce in ms; defaults to 120. */
	debounceMs?: number;
	/** Maximum visible candidate rows; defaults to 10. Values below 1 are raised to 1. */
	maxVisible?: number;
	/** Page size for `skills.search`; defaults to 20. */
	limit?: number;
	/** Overlay title; defaults to the literal `Skills`. */
	title?: string;
	/** Called after any state change that should re-render the pane. */
	onChange?: () => void;
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
/** Mirrors the gateway capability filter bound (`_str(..., max_len=80)`). */
const CAPABILITY_MAX = 80;

const SELECTED_PREFIX = `${ICONS.selectedMarker} `;
const PLAIN_PREFIX = "  ";
const FOOTER_SEPARATOR = " · ";
const DEFAULT_MAX_VISIBLE = 10;
const DEFAULT_DEBOUNCE_MS = 120;
const DEFAULT_LIMIT = 20;

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
 * empty string means "ok". Rendered parenthesized when a result set is empty
 * and prefixed `! ` when candidates exist but none are applicable.
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

/** One sanitized physical display row. */
function singleLine(text: string): string {
	return sanitizeDisplay(text).replace(/\n/g, " ");
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/** Cycle helper for the arrow-driven filter slots (clamped, never throws). */
function cycleValue<T>(values: readonly T[], current: T, delta: number): T {
	const index = values.indexOf(current);
	const base = index < 0 ? 0 : index;
	const next = ((base + delta) % values.length + values.length) % values.length;
	return values[next]!;
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

type PaneRow =
	| { kind: "header"; title: string; query: string }
	| { kind: "filters"; text: string }
	| { kind: "banner"; text: string }
	| { kind: "candidate"; candidate: SkillsCandidate; selected: boolean }
	| { kind: "more"; hidden: number }
	| { kind: "state"; text: string }
	| { kind: "line"; text: string }
	| { kind: "footer"; parts: string[] };

/**
 * Overlay picker over the injected skills providers. The host owns focus and
 * the overlay handle; keys only act while `focused`.
 */
export class SkillsPicker implements Component {
	/** Set by the focus owner (TUI `Focusable`); unfocused keys are inert. */
	focused = false;
	/** Dismissal callback (Esc in results mode). */
	onClose: (() => void) | undefined;
	/** Delete action: the host closes the overlay and runs the legacy command. */
	onDelete: ((skillName: string) => void) | undefined;

	private readonly providers: SkillsPickerProviders;
	private readonly debounceMs: number;
	private maxVisible: number;
	private readonly limit: number;
	private readonly title: string;
	private readonly onChange: (() => void) | undefined;

	private mode: SkillsPickerMode = "results";
	private query: string;
	private filters: SkillsFilters = {
		task_family: "",
		capability: "",
		source: [],
		enabled: null,
		include_excluded: true,
	};
	private focus: SkillsPickerFocus = "query";
	private filterIndex = 0;

	private candidates: SkillsCandidate[] = [];
	private selectedSkillId: string | null = null;
	private status: SkillsPickerStatus = "idle";
	private errorText = "";
	private classification = "";
	private retrieved = 0;
	private catalogRevision = "";
	private nextCursor: string | null = null;
	private diagnostics: SkillsSearchDiagnostics = {};
	private notice = "";

	private details: SkillsDetailsSkill | undefined;
	private detailsSkillId: string | null = null;
	private detailsStatus: "idle" | "loading" | "ready" | "error" = "idle";
	private detailsError = "";

	private preview: SkillsPreviewResult | undefined;
	private previewStatus: "idle" | "loading" | "ready" | "error" = "idle";
	private previewError = "";
	private previewReturn: "results" | "details" = "results";

	/** Sequence of the newest scheduled request; older responses are dropped. */
	private requestSeq = 0;
	private previewSeq = 0;
	private timer: ReturnType<typeof setTimeout> | null = null;

	constructor(providers: SkillsPickerProviders, options: SkillsPickerOptions = {}) {
		this.providers = providers;
		this.query = options.initialQuery ?? "";
		this.debounceMs = Math.max(0, options.debounceMs ?? DEFAULT_DEBOUNCE_MS);
		this.maxVisible = Math.max(1, Math.floor(options.maxVisible ?? DEFAULT_MAX_VISIBLE));
		this.limit = Math.max(1, Math.floor(options.limit ?? DEFAULT_LIMIT));
		this.title = options.title ?? "Skills";
		this.onChange = options.onChange;
	}

	/** Open the overlay: reset state, then immediately request the query. */
	activate(): void {
		this.clearTimer();
		this.mode = "results";
		this.focus = "query";
		this.filterIndex = 0;
		this.candidates = [];
		this.selectedSkillId = null;
		this.status = "idle";
		this.errorText = "";
		this.classification = "";
		this.retrieved = 0;
		this.catalogRevision = "";
		this.nextCursor = null;
		this.diagnostics = {};
		this.notice = "";
		this.details = undefined;
		this.detailsSkillId = null;
		this.detailsStatus = "idle";
		this.detailsError = "";
		this.preview = undefined;
		this.previewStatus = "idle";
		this.previewError = "";
		this.focused = true;
		this.schedule(true);
		this.emit();
	}

	/** Close the overlay; in-flight responses are dropped. */
	dismiss(): void {
		this.clearTimer();
		this.requestSeq += 1;
		this.previewSeq += 1;
		this.focused = false;
		this.onClose?.();
	}

	getQuery(): string {
		return this.query;
	}

	/** Effective visible-row budget (constructor option; clamped to >= 1). */
	getMaxVisible(): number {
		return this.maxVisible;
	}

	/** Update the row budget (terminal resize); selection stays by skill ID. */
	setVisibleRows(rows: number): void {
		this.maxVisible = Math.max(1, Math.floor(rows));
		this.emit();
	}

	/** Candidates in provider order (the backend ranked them; never re-sorted). */
	visibleCandidates(): SkillsCandidate[] {
		return [...this.candidates];
	}

	selectedCandidate(): SkillsCandidate | null {
		return this.candidates.find((candidate) => candidate.skillId === this.selectedSkillId) ?? null;
	}

	getView(): SkillsPickerView {
		return {
			mode: this.mode,
			query: this.query,
			focus: this.focus,
			filters: { ...this.filters, source: [...this.filters.source] },
			status: this.status,
			error: this.errorText,
			classification: this.classification,
			candidates: [...this.candidates],
			selected: this.selectedCandidate(),
			selectedSkillId: this.selectedSkillId,
			retrieved: this.retrieved,
			catalogRevision: this.catalogRevision,
			nextCursor: this.nextCursor,
			details: this.details ?? null,
			detailsStatus: this.detailsStatus,
			detailsError: this.detailsError,
			preview: this.preview ?? null,
			previewStatus: this.previewStatus,
			previewError: this.previewError,
			notice: this.notice,
		};
	}

	invalidate(): void {}

	// ─── rendering ─────────────────────────────────────────────────────────

	render(width: number): string[] {
		const total = Math.max(0, width);
		const out: string[] = [];
		for (const row of this.layout()) {
			// Content lines (details/preview prose) wrap so nothing is silently
			// cut mid-explanation; selectable rows stay exactly one physical row
			// so the mouse hit map can never drift.
			if (row.kind === "line") {
				for (const line of wrapDisplay(this.styleRow(row), total || 1)) {
					out.push(surfaceLine(line, total));
				}
				continue;
			}
			const line = truncateDisplay(this.styleRow(row), total);
			if (row.kind === "candidate" && row.selected) {
				out.push(surfaceLine(selectionBar(line, total), total));
			} else {
				out.push(surfaceLine(line, total));
			}
		}
		return out;
	}

	private layout(): PaneRow[] {
		switch (this.mode) {
			case "details":
				return this.detailsLayout();
			case "preview":
				return this.previewLayout();
			default:
				return this.resultsLayout();
		}
	}

	private resultsLayout(): PaneRow[] {
		const rows: PaneRow[] = [
			{ kind: "header", title: this.title, query: this.query },
			{ kind: "filters", text: this.filtersText() },
		];
		if (this.notice) rows.push({ kind: "line", text: theme.dim(`(${this.notice})`) });
		if (this.status === "error" && this.candidates.length > 0) {
			rows.push({ kind: "banner", text: `(search failed: ${this.errorText} — earlier results shown)` });
		} else {
			const banner = classificationLabel(this.classification, this.query);
			if (banner && this.candidates.length > 0) rows.push({ kind: "banner", text: `! ${banner}` });
		}
		if (this.candidates.length === 0) {
			rows.push({ kind: "state", text: this.emptyLabel() });
		} else {
			const window = this.windowCandidates();
			if (window.hiddenAbove > 0) rows.push({ kind: "more", hidden: window.hiddenAbove });
			for (const candidate of window.candidates) {
				rows.push({ kind: "candidate", candidate, selected: candidate.skillId === this.selectedSkillId });
			}
			if (window.hiddenBelow > 0) rows.push({ kind: "more", hidden: window.hiddenBelow });
		}
		rows.push({ kind: "footer", parts: this.footerParts() });
		return rows;
	}

	private detailsLayout(): PaneRow[] {
		const candidate = this.selectedCandidate();
		const details = this.details;
		const rows: PaneRow[] = [];
		if (!candidate) {
			rows.push({ kind: "state", text: "(no skill selected)" });
			rows.push({ kind: "line", text: theme.dim("esc back") });
			return rows;
		}
		const applicability = candidate.applicability || "unknown";
		rows.push({ kind: "header", title: this.title, query: "" });
		rows.push({ kind: "line", text: theme.accentBold(`${singleLine(details?.name ?? candidate.name)} — ${applicability}`)});
		if (this.detailsStatus === "loading") rows.push({ kind: "state", text: "(loading details…)" });
		else if (this.detailsStatus === "error") rows.push({ kind: "state", text: `(details unavailable: ${this.detailsError})` });
		else if (details) {
			rows.push({ kind: "line", text: `purpose: ${singleLine(details.description) || "—"}` });
			rows.push({
				kind: "line",
				text: `source: ${candidate.trust} (${details.sourceType}) · version ${singleLine(details.version) || "—"} · content ${candidate.contentRevision.slice(0, 8) || "—"}`,
			});
		}
		rows.push({ kind: "line", text: `reasons: ${singleLine(candidate.reasons.join("; ")) || "none — applicable"}` });
		const matched = [...candidate.matchedPhrases, ...candidate.matchedCapabilities];
		if (matched.length > 0) rows.push({ kind: "line", text: `matched: ${singleLine(matched.join(", "))}` });
		rows.push({ kind: "line", text: `stages: ${singleLine(candidate.workflowStages.join(", ")) || "—"}` });
		const derived = applicabilityDetails(candidate.reasons);
		rows.push({ kind: "line", text: `prerequisites: ${singleLine(derived.prerequisites.join("; ")) || "(none reported)"}` });
		rows.push({ kind: "line", text: `required tools: ${singleLine(derived.tools.join(", ")) || "(none reported)"}` });
		if (this.detailsStatus === "ready" && details) {
			const size = details.content?.length ?? 0;
			rows.push({
				kind: "line",
				text: `instructions: ${size} chars — read with skills.section (content ${candidate.contentRevision.slice(0, 8) || "—"})`,
			});
			rows.push({ kind: "line", text: `usage: ${details.usageCount} · enabled: ${details.enabled ? "yes" : "no"}` });
		}
		const actions = ["esc back", "ctrl+r preview"];
		if (skillDeletable(candidate) && (!details || skillDeletable(details))) actions.push("d delete");
		else actions.push("delete unavailable (bundled default)");
		rows.push({ kind: "line", text: theme.dim(`actions: ${actions.join(" · ")}`) });
		return rows;
	}

	private previewLayout(): PaneRow[] {
		const rows: PaneRow[] = [
			{ kind: "header", title: this.title, query: "" },
			{ kind: "line", text: theme.accentBold(`Route preview: ${singleLine(this.query)}`) },
		];
		if (this.previewStatus === "loading") {
			rows.push({ kind: "state", text: "(building preview…)" });
			rows.push({ kind: "line", text: theme.dim("esc back") });
			return rows;
		}
		if (this.previewStatus === "error") {
			rows.push({ kind: "state", text: `(preview failed: ${this.previewError})` });
			rows.push({ kind: "line", text: theme.dim("esc back") });
			return rows;
		}
		const preview = this.preview;
		if (!preview) {
			rows.push({ kind: "state", text: "(no preview yet)" });
			rows.push({ kind: "line", text: theme.dim("esc back") });
			return rows;
		}
		const descriptor = preview.descriptor;
		rows.push({
			kind: "line",
			text: `class: ${descriptor.task_class} · scope: ${descriptor.operation_scope} · families: ${descriptor.task_families.join(", ") || "—"}`,
		});
		rows.push({
			kind: "line",
			text: `stack: ${this.stackSummary(descriptor)} · provenance: ${descriptor.stack_provenance} · unresolved: ${descriptor.unresolved_fields.join(", ") || "—"}`,
		});
		rows.push({ kind: "line", text: `reasons: ${singleLine(preview.reasons.join("; ")) || "none"}` });
		rows.push({
			kind: "line",
			text: `selection: ${preview.selection.kind} · skills: ${preview.selection.skills.join(", ") || "—"}`,
		});
		const steps = preview.selection.steps;
		if (steps.length === 0) {
			rows.push({ kind: "line", text: "steps: —" });
		} else {
			steps.forEach((step, index) => {
				rows.push({
					kind: "line",
					text: `${index === 0 ? "steps: " : "       "}${index + 1}. ${singleLine(step.label)} (${step.skill_id}, stage ${step.stage || "—"}, ~${step.estimate_tokens} tok)`,
				});
			});
		}
		const disclosure = preview.disclosure;
		const chosen = disclosure.chosen
			.map((item) => `${item.skill_id} · ${singleLine(item.heading || item.section_id || "body")}`)
			.join(", ");
		rows.push({
			kind: "line",
			text: `disclosure: included ${disclosure.included_tokens}/${preview.budget} tok (${disclosure.token_basis}) · chosen: ${chosen || "—"}`,
		});
		if (disclosure.omitted.length > 0) {
			const reason = disclosure.omitted[0]!.reason;
			rows.push({ kind: "line", text: `omitted: ${disclosure.omitted.length} section(s) — ${singleLine(reason)}` });
		}
		if (disclosure.unresolved_prerequisites.length > 0) {
			rows.push({
				kind: "line",
				text: `unresolved prerequisites: ${disclosure.unresolved_prerequisites.map((item) => `${item.skill_id}·${item.section_id}`).join(", ")}`,
			});
		}
		rows.push({ kind: "line", text: `missing context: ${singleLine(disclosure.missing_context.join("; ")) || "(none)"}` });
		rows.push({ kind: "line", text: `task constraints: ${singleLine(disclosure.task_constraints.join("; ")) || "(none)"}` });
		rows.push({
			kind: "line",
			text: theme.dim("preview only — no turn, no provider call, no installation, no approval change · esc back"),
		});
		return rows;
	}

	private stackSummary(descriptor: SkillsPreviewDescriptor): string {
		const stack = descriptor.effective_stack ?? {};
		const categories = Object.keys(stack)
			.sort()
			.map((category) => `${category}: ${(stack[category] ?? []).join(", ")}`);
		return categories.join("; ") || "unknown";
	}

	private filtersText(): string {
		const slot = (index: number, text: string) =>
			this.focus === "filters" && this.filterIndex === index ? `[${text}]` : text;
		return `filters: ${slot(0, `family=${this.filters.task_family || "any"}`)}${FOOTER_SEPARATOR}${slot(1, `capability=${this.filters.capability || "any"}`)}${FOOTER_SEPARATOR}${slot(2, `source=${this.filters.source[0] ?? "any"}`)}${FOOTER_SEPARATOR}${slot(3, `state=${this.stateName()}`)}`;
	}

	private stateName(): string {
		if (this.filters.enabled === true) return "enabled";
		if (this.filters.enabled === false) return "disabled";
		return "any";
	}

	private emptyLabel(): string {
		if (this.status === "error") return `(search failed: ${this.errorText})`;
		if (this.status === "loading") return "(searching…)";
		const label = classificationLabel(this.classification, this.query);
		if (label) return `(${label})`;
		if (this.status === "idle") return "(type to search the skill catalog)";
		return "(no matches)";
	}

	private footerParts(): string[] {
		const parts = [
			`${this.candidates.length} shown`,
			`${this.retrieved} retrieved`,
		];
		if (this.nextCursor) parts.push("ctrl+n more");
		if (this.catalogRevision) parts.push(`catalog ${this.catalogRevision.slice(0, 8)}`);
		parts.push(
			`applicable ${this.diagnostics.applicable ?? 0} · conditional ${this.diagnostics.conditional ?? 0} · excluded ${this.diagnostics.excluded ?? 0}`,
		);
		parts.push("type to filter · enter details · ctrl+r preview · tab filters · esc close");
		return parts;
	}

	/** Bound the candidate list to `maxVisible` rows, centered on the selection. */
	private windowCandidates(): { candidates: SkillsCandidate[]; hiddenAbove: number; hiddenBelow: number } {
		const list = this.candidates;
		if (list.length <= this.maxVisible) {
			return { candidates: list, hiddenAbove: 0, hiddenBelow: 0 };
		}
		const reserved = this.maxVisible >= 3 ? 2 : 0;
		const content = Math.max(1, this.maxVisible - reserved);
		const selected = Math.max(0, list.findIndex((candidate) => candidate.skillId === this.selectedSkillId));
		const start = Math.max(0, Math.min(selected - content + 1, list.length - content));
		const end = Math.min(list.length, start + content);
		return {
			candidates: list.slice(start, end),
			hiddenAbove: reserved > 0 ? start : 0,
			hiddenBelow: reserved > 0 ? list.length - end : 0,
		};
	}

	private styleRow(row: PaneRow): string {
		switch (row.kind) {
			case "header": {
				if (row.query) return `${theme.accentBold(`${row.title}:`)} ${theme.text(singleLine(row.query))}`;
				return `${theme.accentBold(`${row.title}:`)} ${theme.dim("type to search")}`;
			}
			case "filters":
				return theme.dim(row.text);
			case "banner":
				return theme.warning(row.text);
			case "candidate": {
				const prefix = row.selected ? SELECTED_PREFIX : PLAIN_PREFIX;
				const name = theme.bold(singleLine(row.candidate.name));
				const state = row.candidate.applicability === "applicable"
					? theme.success(`[${row.candidate.applicability}]`)
					: theme.warning(`[${row.candidate.applicability}]`);
				const stages = row.candidate.workflowStages.length > 0
					? `  ${theme.dim(singleLine(row.candidate.workflowStages.join("/")))}`
					: "";
				return `${prefix}${name}  ${state}  ${theme.muted(singleLine(row.candidate.sourceType))}${stages}`;
			}
			case "more":
				return theme.dim(`+${row.hidden} more`);
			case "state":
				return theme.dim(row.text);
			case "line":
				return row.text;
			case "footer":
				return theme.dim(row.parts.join(FOOTER_SEPARATOR));
		}
	}

	// ─── input ─────────────────────────────────────────────────────────────

	handleInput(data: string): void {
		if (!this.focused) return;
		if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c"))) {
			this.onEscape();
			return;
		}
		if (this.mode === "details") {
			if (matchesKey(data, Key.ctrl("r")) || data === "p") {
				this.startPreview();
				return;
			}
			if (data === "d") {
				const candidate = this.selectedCandidate();
				if (candidate && skillDeletable(candidate) && (!this.details || skillDeletable(this.details))) {
					this.onDelete?.(candidate.name);
				}
				return;
			}
			if (matchesKey(data, Key.enter) || matchesKey(data, Key.return)) {
				this.mode = "results";
				this.emit();
			}
			return;
		}
		if (this.mode === "preview") {
			if (matchesKey(data, Key.enter) || matchesKey(data, Key.return)) {
				this.mode = this.previewReturn;
				this.emit();
			}
			return;
		}
		// results mode
		if (matchesKey(data, Key.tab)) {
			this.focus = this.focus === "query" ? "filters" : "query";
			this.emit();
			return;
		}
		if (matchesKey(data, Key.ctrl("r"))) {
			this.startPreview();
			return;
		}
		if (matchesKey(data, Key.ctrl("n"))) {
			void this.loadMore();
			return;
		}
		if (this.focus === "filters") {
			if (matchesKey(data, Key.left)) {
				this.filterIndex = Math.max(0, this.filterIndex - 1);
				this.emit();
				return;
			}
			if (matchesKey(data, Key.right)) {
				this.filterIndex = Math.min(3, this.filterIndex + 1);
				this.emit();
				return;
			}
			if (matchesKey(data, Key.up) || matchesKey(data, Key.down)) {
				const delta = matchesKey(data, Key.up) ? -1 : 1;
				this.cycleFilter(delta);
				return;
			}
			if (matchesKey(data, Key.enter) || matchesKey(data, Key.return)) {
				this.focus = "query";
				this.emit();
				return;
			}
			if (matchesKey(data, Key.backspace)) {
				if (this.filterIndex === 1 && this.filters.capability) {
					this.setCapability(this.filters.capability.slice(0, -1));
				}
				return;
			}
			if (data === " ") {
				this.cycleFilter(1);
				return;
			}
			this.appendPrintable(data, (text) => {
				if (this.filterIndex === 1) this.setCapability(this.filters.capability + text);
			});
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
			this.openDetails();
			return;
		}
		if (matchesKey(data, Key.backspace)) {
			if (this.query) this.setQuery(this.query.slice(0, -1));
			return;
		}
		this.appendPrintable(data, (text) => this.setQuery(this.query + text));
	}

	private onEscape(): void {
		if (this.mode === "preview") {
			this.mode = this.previewReturn;
			this.emit();
			return;
		}
		if (this.mode === "details") {
			this.mode = "results";
			this.emit();
			return;
		}
		if (this.focus === "filters") {
			this.focus = "query";
			this.emit();
			return;
		}
		this.dismiss();
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
		// Physical-row mapping: wrapped content lines ("line" rows) occupy more
		// than one physical row, so the pointer row must be resolved through the
		// same spans the renderer produced — a wrapped notice can never shift a
		// candidate's hit target (U03).
		const layout = this.layout();
		const spans = this.rowSpans(layout, event.width);
		let index = -1;
		let remaining = event.y;
		for (let position = 0; position < spans.length; position += 1) {
			if (remaining < spans[position]!) {
				index = position;
				break;
			}
			remaining -= spans[position]!;
		}
		const row = index >= 0 ? layout[index] : undefined;
		if (!row) return undefined;
		if (event.type === "press") return { handled: true, focus: true };
		if (event.type !== "click") return undefined;
		if (row.kind !== "candidate") return { handled: true, focus: true };
		this.selectedSkillId = row.candidate.skillId;
		if ((event.clickCount ?? 1) >= 2) {
			this.openDetails();
			return { handled: true, focus: true, render: true };
		}
		this.emit();
		return { handled: true, focus: true, render: true };
	}

	/** Physical rows per layout row at *width* (wrapped lines count individually). */
	private rowSpans(layout: PaneRow[], width: number): number[] {
		const total = Math.max(0, width);
		return layout.map((row) => {
			if (row.kind !== "line") return 1;
			return Math.max(1, wrapDisplay(this.styleRow(row), total || 1).length);
		});
	}

	// ─── search plumbing ───────────────────────────────────────────────────

	private setQuery(query: string): void {
		this.query = query.slice(0, SKILLS_QUERY_MAX);
		this.notice = "";
		this.reconcileSelection();
		this.schedule(false);
		this.emit();
	}

	private setCapability(capability: string): void {
		this.filters.capability = capability.slice(0, CAPABILITY_MAX);
		this.schedule(false);
		this.emit();
	}

	private cycleFilter(delta: number): void {
		switch (this.filterIndex) {
			case 0:
				this.filters.task_family = cycleValue(["", ...SKILLS_TASK_FAMILIES], this.filters.task_family, delta);
				break;
			case 2: {
				const current = this.filters.source[0] ?? "";
				const next = cycleValue(["", ...SKILLS_SOURCES], current, delta);
				this.filters.source = next ? [next] : [];
				break;
			}
			case 3: {
				const next = cycleValue([...SKILLS_STATES], this.stateName(), delta);
				this.filters.enabled = next === "enabled" ? true : next === "disabled" ? false : null;
				this.filters.include_excluded = next !== "enabled";
				break;
			}
			default:
				return; // capability is free text
		}
		this.schedule(false);
		this.emit();
	}

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
		this.emit();
		try {
			const result = await this.providers.search({ query, filters, limit: this.limit });
			if (seq !== this.requestSeq) return;
			this.apply(result, { append: false });
		} catch (error) {
			if (seq !== this.requestSeq) return;
			this.status = "error";
			this.errorText = singleLine(message(error));
			this.emit();
		}
	}

	/** Append the next page; `nextCursor` is the opaque cursor from the backend. */
	private async loadMore(): Promise<void> {
		const cursor = this.nextCursor;
		if (!cursor) return;
		this.clearTimer();
		const seq = ++this.requestSeq;
		this.status = "loading";
		this.emit();
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
			this.emit();
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
		this.retrieved = Number.isFinite(result.diagnostics?.retrieved)
			? Number(result.diagnostics.retrieved)
			: this.candidates.length;
		this.classification = typeof result.diagnostics?.classification === "string"
			? String(result.diagnostics.classification)
			: "";
		this.diagnostics = result.diagnostics ?? {};
		this.catalogRevision = typeof result.catalogRevision === "string" ? result.catalogRevision : "";
		this.nextCursor = typeof result.nextCursor === "string" && result.nextCursor ? result.nextCursor : null;
		this.status = "ready";
		this.errorText = "";
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

	// ─── selection ─────────────────────────────────────────────────────────

	/** Keep the selected skill ID when it survives; otherwise select the first row. */
	private reconcileSelection(): void {
		if (this.selectedSkillId !== null && this.candidates.some((candidate) => candidate.skillId === this.selectedSkillId)) {
			return;
		}
		this.selectedSkillId = this.candidates[0]?.skillId ?? null;
	}

	private moveBy(delta: number): void {
		if (this.candidates.length === 0) return;
		const current = this.candidates.findIndex((candidate) => candidate.skillId === this.selectedSkillId);
		const index = current < 0
			? (delta >= 0 ? 0 : this.candidates.length - 1)
			: Math.max(0, Math.min(this.candidates.length - 1, current + delta));
		this.selectedSkillId = this.candidates[index]!.skillId;
		this.emit();
	}

	// ─── details + preview ─────────────────────────────────────────────────

	private openDetails(): void {
		const candidate = this.selectedCandidate();
		if (!candidate) return;
		this.mode = "details";
		this.detailsSkillId = candidate.skillId;
		this.details = undefined;
		this.detailsStatus = "loading";
		this.detailsError = "";
		this.emit();
		void this.loadDetails(candidate.skillId);
	}

	private async loadDetails(skillId: string): Promise<void> {
		try {
			const skill = await this.providers.details(skillId);
			if (this.detailsSkillId !== skillId) return;
			this.details = skill;
			this.detailsStatus = "ready";
		} catch (error) {
			if (this.detailsSkillId !== skillId) return;
			this.detailsStatus = "error";
			this.detailsError = singleLine(message(error));
		}
		this.emit();
	}

	private startPreview(): void {
		const request = this.query.trim();
		if (!request) {
			this.notice = "type a request in the search box first";
			this.emit();
			return;
		}
		this.previewReturn = this.mode === "details" ? "details" : "results";
		this.mode = "preview";
		this.previewStatus = "loading";
		this.preview = undefined;
		this.previewError = "";
		this.emit();
		void this.loadPreview(request);
	}

	private async loadPreview(request: string): Promise<void> {
		const seq = ++this.previewSeq;
		try {
			const preview = await this.providers.preview(request);
			if (seq !== this.previewSeq || this.mode !== "preview") return;
			this.preview = preview;
			this.previewStatus = "ready";
		} catch (error) {
			if (seq !== this.previewSeq || this.mode !== "preview") return;
			this.previewStatus = "error";
			this.previewError = singleLine(message(error));
		}
		this.emit();
	}
}
