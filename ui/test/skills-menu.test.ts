// A5 interactive skills directory rebuilt on the house searchable `Menu`
// (owner directive: do it like the theme menu — bounded size, info/hints in
// the bottom hint row — replacing the custom full-screen SkillsPicker).
//
// Part A drives SkillsMenu directly (real Menu layout, real row budget, real
// key handling) over scripted providers whose payloads mirror the gateway
// `skills.search` / `skills.show` / `skills.preview_route` / `skills.section`
// handlers field-for-field (machina/gateway/features/skills.py).
//
// Part B boots the real App on a TuiAltScreen with a scripted terminal and a
// recording client: `/skills` opens the Menu dialog through the real command
// path, typing searches over `skills.search`, ctrl+r previews over
// `skills.preview_route`, and the preview path is proven to record zero
// non-preview RPCs (no turn, no provider, no install, no approval change).

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	stripTerminalSequences,
	TuiAltScreen,
	visibleWidth,
	type Terminal,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { DEFAULT_MENU_VISIBLE_ROWS } from "../src/components/Menu.ts";
import {
	SKILLS_SECTION_BUDGET,
	SKILLS_SOURCES,
	SKILLS_TASK_FAMILIES,
	SkillsMenu,
	classificationLabel,
	firstSectionSlug,
	skillDeletable,
	type SkillsCandidate,
	type SkillsDetailsSkill,
	type SkillsMenuProviders,
	type SkillsPreviewResult,
	type SkillsSearchParams,
	type SkillsSearchResult,
	type SkillsSectionParams,
	type SkillsSectionResult,
} from "../src/components/SkillsMenu.ts";
import type { GatewayClient } from "../src/gateway.ts";
import type { SessionInfo } from "../src/protocol.ts";

// Keep the app chat-only in these tests (workspace is covered elsewhere).
process.env.MACHINA_WORKSPACE = "0";
process.env.MACHINA_UI_REDUCED_MOTION = "1";

const SESSION: SessionInfo = {
	id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
	title: "test",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

// ─── payload builders (exact gateway handler shapes) ────────────────────────

function candidate(overrides: Partial<SkillsCandidate> = {}): SkillsCandidate {
	return {
		skillId: "web-build",
		name: "web-build",
		contentRevision: "rev0000000000",
		score: 12.5,
		scoreComponents: { lexical: 9.5, trigger: 3 },
		matchedPhrases: ["build a website"],
		matchedCapabilities: ["layout"],
		applicability: "applicable",
		reasons: [],
		workflowStages: ["inspect", "implement"],
		explanation: "applicable · lexical=9.5 · trigger=3",
		sourceType: "builtin-ecc",
		trust: "bundled",
		enabled: true,
		...overrides,
	};
}

function searchResult(overrides: Partial<SkillsSearchResult> = {}): SkillsSearchResult {
	const candidates = overrides.candidates ?? [];
	const classification = overrides.diagnostics?.classification
		?? (candidates.some((row) => row.applicability === "applicable")
			? "ok"
			: candidates.some((row) => row.applicability === "conditional")
				? "conditional_only"
				: candidates.length
					? "all_excluded"
					: "no_lexical_evidence");
	return {
		query: "",
		filters: { task_family: "", capability: "", source: [], enabled: null, include_excluded: true },
		limit: 10,
		browse: true,
		candidates,
		catalogRevision: "cafebabe00000000",
		queryDescriptorRevision: "qdr-1",
		policyRevision: "pol-1",
		nextCursor: null,
		diagnostics: {
			classification,
			catalog_size: 3,
			retrieved: candidates.length,
			applicable: candidates.filter((row) => row.applicability === "applicable").length,
			conditional: candidates.filter((row) => row.applicability === "conditional").length,
			excluded: candidates.filter((row) => row.applicability === "excluded").length,
			corrections: [],
			weights_applied: false,
		},
		...overrides,
	};
}

function detailsSkill(overrides: Partial<SkillsDetailsSkill> = {}): SkillsDetailsSkill {
	return {
		name: "web-build",
		description: "Build a website from a plain request",
		triggers: ["website"],
		version: "1.2.0",
		sourceType: "builtin-ecc",
		usageCount: 4,
		enabled: true,
		content: "## Orientation\nStart here.\n\n## Implementation\nBuild it.",
		...overrides,
	};
}

function sectionResult(overrides: Partial<SkillsSectionResult> = {}): SkillsSectionResult {
	return {
		skillId: "web-build",
		contentRevision: "rev0000000000",
		sectionId: "orientation",
		heading: "Orientation",
		text: "Start here.",
		start: 14,
		end: 26,
		total: 56,
		sectionTotal: 12,
		line: 2,
		continuation: null,
		stale: false,
		bound: true,
		anchorId: "orientation",
		budget: 800,
		diagnostics: [],
		...overrides,
	};
}

function previewResult(overrides: Partial<SkillsPreviewResult> = {}): SkillsPreviewResult {
	return {
		descriptor: {
			task_class: "coding",
			task_families: ["web-build"],
			requested_capabilities: [],
			explicit_stack: {},
			project_stack: {},
			inherited_stack: {},
			operation_scope: "create",
			constraints: [],
			unresolved_fields: ["stack"],
			evidence: [
				{ field: "task_class", value: "coding", source: "intent:action-verb", detail: "make me a website" },
			],
			task_state_revision: 1,
			effective_stack: {},
			stack_provenance: "unknown",
		},
		reasons: ["task_class=coding [intent:action-verb] (make me a website)"],
		selection: {
			kind: "coding",
			skills: ["web-build"],
			steps: [
				{
					step_id: "web-build-step",
					label: "web build",
					stage: "implement",
					skill_id: "web-build",
					section_ids: ["implementation"],
					provenance: "policy",
					estimate_tokens: 120,
					estimate_basis: "char/4",
					prerequisites: [],
				},
			],
			catalogRevision: "cafebabe00000000",
			taskRevision: 1,
		},
		disclosure: {
			task_revision: 1,
			stage_revision: 0,
			catalog_revision: "cafebabe00000000",
			stage_source: "",
			current_stage: "",
			chosen: [
				{
					step_id: "web-build-step",
					skill_id: "web-build",
					skill_revision: "rev0000000000",
					content_revision: "rev0000000000",
					section_id: "implementation",
					heading: "Implementation",
					stage: "implement",
					text: "Build it.",
					estimate_tokens: 40,
					required_by: [],
				},
			],
			whole_body: [],
			required_prerequisites: [],
			omitted: [
				{
					locator: {
						skill_id: "web-build",
						content_revision: "rev0000000000",
						section_id: "orientation",
						heading: "Orientation",
						stage: "inspect",
					},
					reason: "budget exhausted",
					required_by: [],
				},
			],
			unresolved_prerequisites: [],
			missing_context: [],
			task_constraints: [],
			included_tokens: 340,
			token_basis: "char/4",
			diagnostics: [],
		},
		budget: 1200,
		tokenBasis: "char/4",
		...overrides,
	};
}

// ─── component harness ──────────────────────────────────────────────────────

interface Recorder {
	searches: SkillsSearchParams[];
	details: string[];
	previews: string[];
	sections: SkillsSectionParams[];
	providers: SkillsMenuProviders;
}

function recorder(
	search: (params: SkillsSearchParams) => Promise<SkillsSearchResult> = async (params) =>
		searchResult({ query: params.query, candidates: [] }),
	details: (skillId: string) => Promise<SkillsDetailsSkill> = async () => detailsSkill(),
	preview: (request: string) => Promise<SkillsPreviewResult> = async () => previewResult(),
	section: (params: SkillsSectionParams) => Promise<SkillsSectionResult> = async () => sectionResult(),
): Recorder {
	const searches: SkillsSearchParams[] = [];
	const detailCalls: string[] = [];
	const previewCalls: string[] = [];
	const sectionCalls: SkillsSectionParams[] = [];
	return {
		searches,
		details: detailCalls,
		previews: previewCalls,
		sections: sectionCalls,
		providers: {
			search: (params) => {
				searches.push(params);
				return search(params);
			},
			details: (skillId) => {
				detailCalls.push(skillId);
				return details(skillId);
			},
			preview: (request) => {
				previewCalls.push(request);
				return preview(request);
			},
			section: (params) => {
				sectionCalls.push(params);
				return section(params);
			},
		},
	};
}

function lines(menu: SkillsMenu, width = 200): string[] {
	return menu.render(width).map((line) => stripTerminalSequences(line).trimEnd());
}

function plain(menu: SkillsMenu, width = 200): string {
	return lines(menu, width).join("\n");
}

/** Wrapped content lines joined into one normalized stream (details/preview). */
function flow(menu: SkillsMenu, width = 200): string {
	return lines(menu, width).join(" ").replace(/\s+/g, " ");
}

function type(menu: SkillsMenu, text: string): void {
	for (const char of text) menu.handleInput(char);
}

function delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── Part A: component ──────────────────────────────────────────────────────

test("SM-01 bounded: ≤ DEFAULT_MENU_VISIBLE_ROWS item rows + chrome, hint pinned last", async () => {
	const many = Array.from({ length: 40 }, (_v, index) =>
		candidate({ skillId: `skill-${index}`, name: `skill-${index}` }));
	const rec = recorder(async (params) => searchResult({ query: params.query, candidates: many }));
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	const frame = lines(menu, 100);
	assert.equal(frame.length, DEFAULT_MENU_VISIBLE_ROWS + 3, "bounded to the default cap plus title/filter/hint");
	assert.ok(frame.length < 24, "the dialog is shorter than a 24-row page");
	assert.match(frame[0]!, /Skills/, "title stays on top");
	assert.match(frame[1]!, /Type to filter/, "filter row sits under the title");
	assert.match(frame.at(-1)!, /enter details/, "actions live in the bottom hint row");
	assert.match(frame.at(-1)!, /esc close/);
	assert.doesNotMatch(frame.join("\n"), /skill-12\b/, "rows past the cap scroll out of view");
});

test("SM-02 activate searches immediately; loading first row, then results", async () => {
	const rec = recorder(async (params) =>
		searchResult({ query: params.query, candidates: [candidate(), candidate({ skillId: "deploy", name: "deploy", applicability: "conditional", reasons: ["declared stack not confirmed: kubernetes (query does not mention it)"] })] }));
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	assert.equal(rec.searches.length, 1, "activate requests the query immediately");
	assert.equal(rec.searches[0]!.query, "");
	assert.equal(menu.getView().status, "loading");
	assert.match(plain(menu), /\(searching…\)/, "loading is a distinct first row, never a blank box");
	await delay(0);
	assert.equal(menu.getView().status, "ready");
	const screen = plain(menu);
	assert.match(screen, /web-build/);
	assert.match(screen, /\[applicable\]/);
	assert.match(screen, /\[conditional\]/);
});

test("SM-02 empty/unavailable/invalid/conditional states are distinct", async () => {
	const empty = recorder(async (params) => searchResult({ query: params.query, candidates: [] }));
	const menu = new SkillsMenu(empty.providers);
	menu.activate();
	await delay(0);
	assert.match(plain(menu), /\(no query yet — type to search the skill catalog\)/);

	type(menu, "z");
	await delay(200);
	assert.match(plain(menu), /\(no lexical evidence for "z"\)/);
	assert.notEqual(classificationLabel("no_lexical_evidence", "z"), classificationLabel("invalid_catalog", "z"));

	const invalid = recorder(async () => searchResult({
		candidates: [],
		diagnostics: { classification: "invalid_catalog", catalog_size: 0, retrieved: 0, applicable: 0, conditional: 0, excluded: 0, corrections: [], weights_applied: false },
	}));
	const invalidMenu = new SkillsMenu(invalid.providers);
	invalidMenu.activate();
	await delay(0);
	assert.match(plain(invalidMenu), /\(catalog unavailable — no skills loaded\)/);

	const conditional = recorder(async () => searchResult({ candidates: [candidate({ applicability: "conditional", reasons: ["unverified precondition: tests run"] })] }));
	const conditionalMenu = new SkillsMenu(conditional.providers);
	conditionalMenu.activate();
	await delay(0);
	const screen = plain(conditionalMenu);
	assert.match(screen, /! conditional matches only — no applicable skill/);
	assert.match(screen, /\[conditional\]/);
});

test("SM-02 unavailable gateway is an honest error state, never a fake success", async () => {
	const rec = recorder(async () => {
		throw new Error("Method not found: skills.search");
	});
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	assert.equal(menu.getView().status, "error");
	assert.match(plain(menu), /\(search failed: Method not found: skills\.search\)/);
	assert.equal(menu.getView().candidates.length, 0);
});

test("SM-02 typing debounces skills.search; filter narrows the rows", async () => {
	const rec = recorder(async (params) => {
		const all = [candidate({ skillId: "web-build", name: "web-build" }), candidate({ skillId: "deploy", name: "deploy" })];
		const matched = params.query ? all.filter((row) => row.name.includes(params.query)) : all;
		return searchResult({ query: params.query, candidates: matched });
	});
	const menu = new SkillsMenu(rec.providers, { debounceMs: 15 });
	menu.activate();
	assert.equal(rec.searches.length, 1, "no provider call while typing");
	type(menu, "web");
	assert.equal(rec.searches.length, 1);
	await delay(40);
	assert.deepEqual(rec.searches.map((call) => call.query), ["", "web"]);
	assert.equal(menu.getView().query, "web");
	const screen = plain(menu);
	assert.match(screen, /web-build/);
	assert.doesNotMatch(screen, /deploy/, "the server-narrowed result set is what shows");
	assert.match(plain(menu), /Filter: web/, "the filter line echoes the query (Menu's own filter)");
});

test("SM-03 selection follows the stable skill ID across a reorder and a resize", async () => {
	let call = 0;
	const rec = recorder(async () => {
		call += 1;
		return call === 1
			? searchResult({ candidates: [candidate({ skillId: "a", name: "alpha" }), candidate({ skillId: "b", name: "beta" }), candidate({ skillId: "c", name: "gamma" })] })
			: searchResult({ candidates: [candidate({ skillId: "c", name: "gamma" }), candidate({ skillId: "b", name: "beta" }), candidate({ skillId: "a", name: "alpha" })] });
	});
	const menu = new SkillsMenu(rec.providers, { debounceMs: 5 });
	menu.activate();
	await delay(10);
	menu.handleInput("\x1b[B");
	assert.equal(menu.getView().selectedSkillId, "b");

	menu.handleInput("x");
	await delay(25);
	assert.deepEqual(menu.getView().candidates.map((row) => row.skillId), ["c", "b", "a"]);
	assert.equal(menu.getView().selectedSkillId, "b", "selection follows the ID, not the row index");

	menu.setVisibleRows(2);
	assert.equal(menu.getView().selectedSkillId, "b");
	const narrow = lines(menu, 30);
	assert.ok(narrow.some((row) => row.includes("beta")), "the selected candidate stays in the viewport");
	for (const line of menu.render(30)) {
		assert.ok(visibleWidth(line) <= 30, `row wider than 30: ${JSON.stringify(stripTerminalSequences(line))}`);
	}
});

test("SM-04 details is a second Menu: purpose, provenance, revisions, prereqs, tools, stages, actions", async () => {
	const rec = recorder(
		async () => searchResult({
			candidates: [candidate({
				applicability: "conditional",
				reasons: ["tool availability unknown: node, git", "unverified precondition: repo has a test runner"],
				matchedPhrases: ["build a website"],
				matchedCapabilities: ["layout"],
			})],
		}),
		async () => detailsSkill(),
	);
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	menu.handleInput("\r");
	await delay(0);
	assert.deepEqual(rec.details, ["web-build"]);
	const screen = flow(menu);
	assert.match(screen, /web-build/);
	assert.match(screen, /applicability: conditional/);
	assert.match(screen, /purpose: Build a website from a plain request/);
	assert.match(screen, /source: bundled \(builtin-ecc\) · version 1\.2\.0 · content rev00000/);
	assert.match(screen, /reasons: tool availability unknown: node, git; unverified precondition: repo has a test runner/);
	assert.match(screen, /prerequisites: repo has a test runner/);
	assert.match(screen, /required tools: node, git/);
	assert.match(screen, /instructions: \d+ chars — read with skills\.section \(content rev00000\)/);
	assert.match(screen, /usage: 4 · enabled: yes/);
	assert.match(screen, /matched: build a website, layout/);
	assert.match(screen, /stages: inspect, implement/);
	assert.match(screen, /delete unavailable \(bundled default\)/, "bundled defaults expose no unsupported destructive action");
	assert.doesNotMatch(screen, /d delete/);
	assert.equal(skillDeletable({ sourceType: "builtin-ecc" }), false);
	assert.equal(skillDeletable({ sourceType: "learned" }), true);
});

test("SM-04 a deletable skill exposes the delete action and routes through the host", async () => {
	const rec = recorder(
		async () => searchResult({
			candidates: [candidate({ skillId: "local-skill", name: "local-skill", sourceType: "learned", trust: "external", applicability: "excluded", enabled: false, reasons: ["disabled: skill 'local-skill' is disabled"] })],
		}),
		async () => detailsSkill({ name: "local-skill", sourceType: "learned", enabled: false }),
	);
	const menu = new SkillsMenu(rec.providers);
	const deletions: string[] = [];
	menu.onDelete = (name) => deletions.push(name);
	menu.activate();
	await delay(0);
	menu.handleInput("\r");
	await delay(0);
	const screen = plain(menu);
	assert.match(screen, /disabled: skill 'local-skill' is disabled/);
	assert.match(screen, /d delete/);
	assert.match(screen, /enabled: no/);
	menu.handleInput("d");
	assert.deepEqual(deletions, ["local-skill"]);
});

test("SM-04 preview is read-only: one preview call, zero search/details/section calls", async () => {
	const rec = recorder(async (params) => searchResult({ query: params.query, candidates: [candidate()] }));
	const menu = new SkillsMenu(rec.providers, { debounceMs: 5 });
	menu.activate();
	menu.handleInput("m");
	await delay(25);
	const searchesBefore = rec.searches.length;
	menu.handleInput("\x12"); // ctrl+r → preview this request
	await delay(0);
	assert.deepEqual(rec.previews, ["m"]);
	assert.equal(rec.searches.length, searchesBefore, "the preview action issues no search RPC");
	assert.equal(rec.details.length, 0, "the preview action issues no details RPC");
	assert.equal(rec.sections.length, 0, "the preview action issues no section RPC");
	assert.equal(menu.getView().mode, "preview");
	const screen = flow(menu);
	assert.match(screen, /Route preview: m/);
	assert.match(screen, /class: coding · scope: create · families: web-build/);
	assert.match(screen, /reasons: task_class=coding \[intent:action-verb\]/);
	assert.match(screen, /selection: coding · skills: web-build/);
	assert.match(screen, /steps: 1\. web build \(web-build, stage implement, ~120 tok\)/);
	assert.match(screen, /disclosure: included 340\/1200 tok \(char\/4\) · chosen: web-build · Implementation/);
	assert.match(screen, /omitted: 1 section\(s\) — budget exhausted/);
	assert.match(screen, /preview only — no turn, no provider call, no installation, no approval change/);
	menu.handleInput("\x1b");
	assert.equal(menu.getView().mode, "results");
});

test("SM-04 a provider failure keeps the draft/filter and never fakes success", async () => {
	let fail = true;
	const rec = recorder(async (params) => {
		if (fail) throw new Error("gateway disconnected");
		return searchResult({ query: params.query, candidates: [candidate()] });
	});
	const menu = new SkillsMenu(rec.providers, { debounceMs: 5 });
	menu.activate();
	await delay(10);
	menu.handleInput("w");
	await delay(25);
	assert.equal(menu.getView().status, "error");
	assert.equal(menu.getView().query, "w", "the draft filter text survives the failure");
	assert.match(plain(menu), /search failed: gateway disconnected/);

	fail = false;
	menu.handleInput("e");
	await delay(25);
	assert.equal(menu.getView().status, "ready");
	assert.equal(menu.getView().query, "we");
	assert.match(plain(menu), /web-build/);
});

test("SM-05 the filters submenu drives the exact wire filter object", async () => {
	const rec = recorder(async (params) => searchResult({ query: params.query, filters: params.filters, candidates: [candidate()] }));
	const menu = new SkillsMenu(rec.providers, { debounceMs: 5 });
	menu.activate();
	await delay(10);
	menu.handleInput("\x06"); // ctrl+f
	assert.equal(menu.getView().mode, "filters");
	menu.handleInput("\r"); // choose the first slot (task family)
	assert.equal(menu.getView().mode, "filter:family");
	menu.handleInput("\x1b[B"); // "any" → first family
	menu.handleInput("\r"); // apply
	await delay(25);
	assert.equal(menu.getView().mode, "results");
	const last = rec.searches.at(-1)!;
	assert.equal(last.filters.task_family, SKILLS_TASK_FAMILIES[0]);
	assert.deepEqual(last.filters.source, []);

	// source + enabled-state slots + include_excluded semantics stay identical
	menu.handleInput("\x06");
	menu.handleInput("\x1b[B"); // capability slot
	menu.handleInput("\x1b[B"); // source slot
	menu.handleInput("\r");
	menu.handleInput("\x1b[B"); // "any" → first source
	menu.handleInput("\r");
	await delay(25);
	assert.deepEqual(rec.searches.at(-1)!.filters.source, [SKILLS_SOURCES[0]]);

	menu.handleInput("\x06");
	menu.handleInput("\x1b[B");
	menu.handleInput("\x1b[B");
	menu.handleInput("\x1b[B"); // enabled state slot
	menu.handleInput("\r");
	menu.handleInput("\x1b[B"); // "any" → enabled
	menu.handleInput("\r");
	await delay(25);
	assert.equal(rec.searches.at(-1)!.filters.enabled, true);
	assert.equal(rec.searches.at(-1)!.filters.include_excluded, false);
});

// ─── A6: Menu-based versioned section reader (§10.4/§12.2) ──────────────────

test("SM-06 details read fetches the first declared section with the shown revision and pages its continuation", async () => {
	assert.equal(firstSectionSlug("## Orientation\nStart here."), "orientation");
	assert.equal(firstSectionSlug("no headings here"), "", "no heading means the reader falls back to a body offset");
	let call = 0;
	const rec = recorder(
		async (params) => searchResult({ query: params.query, candidates: [candidate()] }),
		async () => detailsSkill(),
		async () => previewResult(),
		async (params) => {
			call += 1;
			if (call === 1) {
				assert.deepEqual(params, {
					skillId: "web-build",
					contentRevision: "rev0000000000",
					section: "orientation",
					budget: SKILLS_SECTION_BUDGET,
				});
				return sectionResult({ text: "Start here.", sectionTotal: 26, continuation: 12 });
			}
			assert.equal(params.section, "orientation");
			assert.equal(params.offset, 12);
			assert.equal(params.contentRevision, "rev0000000000", "continuations keep the exact revision");
			return sectionResult({ text: "Then build it.", start: 26, end: 40, sectionTotal: 26, continuation: null });
		},
	);
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	menu.handleInput("\r"); // details
	await delay(0);
	assert.equal(rec.sections.length, 0, "opening details does not read the section");
	assert.match(flow(menu), /ctrl\+s read section/);

	menu.handleInput("s");
	await delay(0);
	assert.equal(rec.sections.length, 1);
	const first = flow(menu);
	assert.match(first, /section: Orientation \(11 of 26 chars · declared · content rev00000\)/);
	assert.match(first, /Start here\./);
	assert.match(first, /\[section continues at offset 12 — press s\]/);
	assert.match(first, /ctrl\+s continue section/);

	menu.handleInput("s");
	await delay(0);
	assert.equal(rec.sections.length, 2);
	const second = flow(menu);
	assert.match(second, /Then build it\./);
	assert.doesNotMatch(second, /continues at offset/);
	assert.match(second, /ctrl\+s read section/);
});

test("SM-06 a stale section read is honest and the retry uses the reported revision", async () => {
	const rec = recorder(
		async (params) => searchResult({ query: params.query, candidates: [candidate()] }),
		async () => detailsSkill(),
		async () => previewResult(),
		async (params) => {
			if (rec.sections.length === 1) {
				return sectionResult({
					stale: true,
					text: "",
					contentRevision: "89abcdef01234567",
					diagnostics: [{
						code: "section-stale-content",
						message: "section 'orientation' was read against revision rev0000000000; the requested revision rev0000000000 no longer matches the current content",
						skill_id: "web-build",
					}],
				});
			}
			assert.equal(params.contentRevision, "89abcdef01234567", "the retry binds to the current revision, not the stale one");
			return sectionResult({ contentRevision: "89abcdef01234567", text: "Fresh orientation." });
		},
	);
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	menu.handleInput("\r");
	await delay(0);
	menu.handleInput("s");
	await delay(0);
	const staleScreen = flow(menu);
	assert.match(staleScreen, /section stale: orientation was requested at content rev00000, but the skill changed to 89abcdef — nothing from the edited text was served/);
	assert.match(staleScreen, /press s to re-read content 89abcdef/);
	assert.doesNotMatch(staleScreen, /Start here\./, "no passage is served from the edited content");
	menu.handleInput("s");
	await delay(0);
	assert.match(flow(menu), /Fresh orientation\./);
});

test("SM-06 a failing section read is an honest error state, never fabricated text", async () => {
	const rec = recorder(
		async (params) => searchResult({ query: params.query, candidates: [candidate()] }),
		async () => detailsSkill(),
		async () => previewResult(),
		async () => {
			throw new Error("skills.section unavailable");
		},
	);
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	menu.handleInput("\r");
	await delay(0);
	menu.handleInput("s");
	await delay(0);
	assert.equal(menu.getView().sectionStatus, "error");
	assert.match(flow(menu), /\(section read failed: skills\.section unavailable\)/);
	assert.equal(menu.getView().section, null, "no passage is invented on failure");
});

test("SM-07 hostile labels stay on one physical row and never exceed the width", async () => {
	const hostile = candidate({ skillId: "evil", name: "evil\n\u001b[31mname\u001b[0m\ttwo" });
	const rec = recorder(async () => searchResult({ candidates: [hostile, candidate({ skillId: "beta", name: "beta-skill" })] }));
	const menu = new SkillsMenu(rec.providers);
	menu.activate();
	await delay(0);
	const rendered = menu.render(70);
	assert.equal(rendered.length, 5, "title + filter + 2 rows + hint: a hostile label adds no physical rows");
	for (const line of rendered) {
		assert.ok(visibleWidth(line) <= 70, `row wider than 70: ${JSON.stringify(stripTerminalSequences(line))}`);
	}
	const flat = plain(menu);
	assert.match(flat, /evil/);
	assert.match(flat, /beta-skill/);
	assert.ok(!flat.includes("\x1b"), "control sequences never survive");
});

// ─── Part B: real App command path ──────────────────────────────────────────

class ScriptedSkillsClient {
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	candidates: SkillsCandidate[] = [];
	searchError: Error | undefined;
	previewError: Error | undefined;
	sectionError: Error | undefined;
	showSkill: SkillsDetailsSkill = detailsSkill();
	sectionRevision = "rev0000000000";

	start(): void {}
	stderr(): string[] { return []; }
	async stop(): Promise<void> {}

	sectionResponse(params: Record<string, unknown>): SkillsSectionResult {
		const content = this.showSkill.content ?? "";
		const requested = params.contentRevision === undefined ? undefined : String(params.contentRevision);
		const wanted = typeof params.section === "string" && params.section ? params.section : firstSectionSlug(content);
		if (requested !== undefined && requested !== this.sectionRevision) {
			return sectionResult({
				skillId: String(params.skillId ?? ""), sectionId: wanted, heading: "", text: "",
				stale: true, contentRevision: this.sectionRevision,
				diagnostics: [{
					code: "section-stale-content",
					message: `section '${wanted}' was read against revision ${requested}; the requested revision ${requested} no longer matches the current content`,
					skill_id: String(params.skillId ?? ""),
				}],
			});
		}
		const sections: Array<{ id: string; heading: string; text: string }> = [];
		let current: { id: string; heading: string; body: string[] } | null = null;
		for (const line of content.split("\n")) {
			const heading = /^(?:#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
			if (heading) {
				if (current) sections.push({ id: current.id, heading: current.heading, text: current.body.join("\n").trim() });
				current = { id: firstSectionSlug(line), heading: heading[1] ?? "", body: [] };
			} else if (current) current.body.push(line);
		}
		if (current) sections.push({ id: current.id, heading: current.heading, text: current.body.join("\n").trim() });
		const found = sections.find((entry) => entry.id === wanted) ?? sections[0];
		if (!found) {
			return sectionResult({
				skillId: String(params.skillId ?? ""), sectionId: wanted, heading: "", text: "",
				stale: false, contentRevision: this.sectionRevision,
				diagnostics: [{ code: "section-unknown", message: `parsed headings contain no section '${wanted}'`, skill_id: String(params.skillId ?? "") }],
			});
		}
		return sectionResult({
			skillId: String(params.skillId ?? ""), sectionId: found.id, heading: found.heading,
			text: found.text, sectionTotal: found.text.length, contentRevision: this.sectionRevision,
		});
	}

	searchResponse(params: Record<string, unknown>): SkillsSearchResult {
		const query = String(params.query ?? "");
		const filters = (params.filters ?? {}) as SkillsSearchResult["filters"];
		const matched = query ? this.candidates.filter((row) => row.name.toLowerCase().includes(query.toLowerCase())) : this.candidates;
		return searchResult({
			query, filters, candidates: matched,
			diagnostics: {
				classification: matched.some((row) => row.applicability === "applicable") ? "ok" : matched.some((row) => row.applicability === "conditional") ? "conditional_only" : matched.length ? "all_excluded" : "no_lexical_evidence",
				catalog_size: this.candidates.length,
				retrieved: matched.length,
				applicable: matched.filter((row) => row.applicability === "applicable").length,
				conditional: matched.filter((row) => row.applicability === "conditional").length,
				excluded: matched.filter((row) => row.applicability === "excluded").length,
				corrections: [], weights_applied: false,
			},
		});
	}

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.requests.push({ method, params });
		switch (method) {
			case "initialize":
				return { version: "test", model: "m", provider: "openrouter", cwd: "/tmp", branch: "main" } as T;
			case "config.get":
				return { config: { nowplaying_enabled: false } } as T;
			case "session.create":
				return { session: SESSION } as T;
			case "skills.search":
				if (this.searchError) throw this.searchError;
				return this.searchResponse(params) as T;
			case "skills.list":
				return { skills: this.candidates.map((row) => ({ name: row.name, description: `Description of ${row.name}`, triggers: row.matchedPhrases, version: "1.0.0", sourceType: row.sourceType, usageCount: 0, enabled: row.enabled })) } as T;
			case "skills.show":
				return { skill: this.showSkill } as T;
			case "skills.preview_route":
				if (this.previewError) throw this.previewError;
				return previewResult({ reasons: [`request=${String(params.request)} [user]`] }) as T;
			case "skills.section":
				if (this.sectionError) throw this.sectionError;
				return this.sectionResponse(params) as T;
			default:
				return {} as T;
		}
	}

	methods(): string[] {
		return this.requests.map((entry) => entry.method);
	}
}

function terminal(columns = 100, rows = 30) {
	let input: (data: string) => void = () => {};
	let onResize: () => void = () => {};
	const term: Terminal & { columns: number; rows: number } = {
		columns, rows, kittyProtocolActive: false,
		start(handler, resize) { input = handler; if (resize) onResize = resize; },
		stop() {}, async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	return {
		term,
		type: (data: string) => input(data),
		resizeTo: (nextColumns: number, nextRows: number) => { term.columns = nextColumns; term.rows = nextRows; onResize(); },
	};
}

interface Booted {
	app: App;
	tui: TuiAltScreen;
	client: ScriptedSkillsClient;
	type: (data: string) => void;
	resizeTo: (columns: number, rows: number) => void;
	screen: () => string;
}

async function boot(columns = 100, rows = 30): Promise<Booted> {
	const { term, type, resizeTo } = terminal(columns, rows);
	const tui = new TuiAltScreen(term);
	const client = new ScriptedSkillsClient();
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return { app, tui, client, type, resizeTo, screen: () => tui.getScreenLines().map(stripTerminalSequences).join("\n") };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const skillsMenuOf = (app: App) => (app as unknown as { skillsMenu: SkillsMenu | undefined }).skillsMenu;
const dialogBounds = (app: App) =>
	(app as unknown as { dialogs: Array<{ handle: { getBounds(): { row: number; height: number } | undefined } }> }).dialogs.at(-1)?.handle.getBounds();

test("SM-08 real /skills opens the Menu dialog; typing drives skills.search; ID survives resize", async (t) => {
	const { app, tui, client, type, resizeTo, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [
		candidate({ skillId: "web-build", name: "web-build" }),
		candidate({ skillId: "web-deploy", name: "web-deploy" }),
		candidate({ skillId: "deploy", name: "deploy", applicability: "conditional" }),
	];
	const mark = client.requests.length;
	type("/skills");
	type("\r");
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /Skills/, "bare /skills opens the Menu dialog");
	const menu = skillsMenuOf(app);
	assert.ok(menu, "the dialog is mounted");
	assert.equal(menu.getView().mode, "results");

	type("web");
	await delay(200);
	tui.renderNow(true);
	assert.ok(
		client.requests.slice(mark).some((entry) => entry.method === "skills.search" && entry.params.query === "web"),
		"typing drives skills.search",
	);
	assert.match(screen(), /web-build/);
	assert.match(screen(), /\[applicable\]/);
	assert.doesNotMatch(screen(), /\[conditional\]/, "filtered-out rows disappear");

	type("\x1b[B");
	assert.equal(menu.getView().selectedSkillId, "web-deploy");
	resizeTo(40, 30);
	await settle();
	tui.renderNow(true);
	assert.equal(menu.getView().selectedSkillId, "web-deploy", "the selected ID survives a terminal resize");
	assert.match(screen(), /web-deploy/);
});

test("SM-08 the dialog is a bottom-anchored bounded box on an 80x24 terminal", async (t) => {
	const { app, tui, client, type } = await boot(80, 24);
	t.after(() => app.exit());
	client.candidates = Array.from({ length: 30 }, (_v, index) => candidate({ skillId: `skill-${index}`, name: `skill-${index}` }));
	type("/skills");
	type("\r");
	await settle();
	tui.renderNow(true);
	const bounds = dialogBounds(app);
	assert.ok(bounds, "the overlay rendered");
	assert.equal(bounds!.height, DEFAULT_MENU_VISIBLE_ROWS + 3, "default cap + title/filter/hint chrome");
	assert.equal(bounds!.row, 24 - (DEFAULT_MENU_VISIBLE_ROWS + 3), "bottom-anchored inside the page");
	const screen = () => tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.match(screen(), /skill-11\b/, "the default twelve rows are visible");
	assert.doesNotMatch(screen(), /skill-12\b/, "the dialog scrolls instead of filling the page");
});

test("SM-08 preview never starts a turn and records zero non-preview RPCs", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ skillId: "web-build", name: "web-build" })];
	const mark = client.requests.length;
	type("/skills");
	type("\r");
	await settle();
	type("make me a website");
	await delay(200);
	tui.renderNow(true);
	assert.ok(!screen().includes("Route preview"), "no preview before the action");

	type("\x12"); // ctrl+r previews the current request text
	await settle();
	tui.renderNow(true);
	const after = client.requests.slice(mark);
	assert.ok(after.some((entry) => entry.method === "skills.preview_route" && entry.params.request === "make me a website"));
	const unexpected = after.filter(
		(entry) => entry.method !== "skills.search" && entry.method !== "skills.list" && entry.method !== "skills.preview_route",
	);
	assert.deepEqual(unexpected, [], "the preview path records zero non-preview RPCs (skills.list is the read-only catalog browse)");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.submit").length, 0, "no turn was started");
	const internals = app as unknown as { inFlight: Set<string>; running: boolean };
	assert.equal(internals.inFlight.size, 0);
	assert.equal(internals.running, false);
	assert.match(screen(), /Route preview: make me a website/);
	assert.match(screen(), /preview only — no turn, no provider call/);
});

test("SM-08 real App: details → s reads skills.section with the shown revision; stale + failure honest", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ skillId: "web-build", name: "web-build" })];
	type("/skills");
	type("\r");
	await settle();
	type("web");
	await delay(200);
	type("\r"); // details
	await settle();
	const mark = client.requests.length;

	type("s");
	await settle();
	tui.renderNow(true);
	const calls = client.requests.slice(mark).filter((entry) => entry.method === "skills.section");
	assert.equal(calls.length, 1, "one versioned section read");
	assert.deepEqual(calls[0]!.params, { skillId: "web-build", contentRevision: "rev0000000000", section: "orientation", budget: 800 });
	const unexpected = client.requests.slice(mark).filter((entry) => entry.method !== "skills.section");
	assert.deepEqual(unexpected, [], "the read path records zero non-section RPCs");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.submit").length, 0);
	let flat = screen().replace(/\s+/g, " ");
	assert.match(flat, /section: Orientation \(11 of 11 chars · declared · content rev00000\)/);
	assert.match(flat, /Start here\./);

	client.sectionRevision = "aaaabbbbccccdddd";
	type("s");
	await settle();
	tui.renderNow(true);
	flat = screen().replace(/\s+/g, " ");
	assert.match(flat, /section stale: orientation was requested at content rev00000, but the skill changed to aaaabbbb/);
	assert.doesNotMatch(flat, /Start here\./, "no passage is served from the edited content");
	type("s");
	await settle();
	tui.renderNow(true);
	const reads = client.requests.filter((entry) => entry.method === "skills.section");
	assert.equal(reads.at(-1)!.params.contentRevision, "aaaabbbbccccdddd");
	assert.match(screen().replace(/\s+/g, " "), /Start here\./);

	client.sectionError = new Error("gateway disconnected");
	type("s");
	await settle();
	tui.renderNow(true);
	assert.match(screen().replace(/\s+/g, " "), /\(section read failed: gateway disconnected\)/);
});

test("SM-08 the delete action routes through the legacy confirmation and RPC", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ skillId: "local-skill", name: "local-skill", sourceType: "learned", trust: "external" })];
	client.showSkill = detailsSkill({ name: "local-skill", sourceType: "learned" });
	type("/skills");
	type("\r");
	await settle();
	type("local");
	await delay(200);
	type("\r"); // details
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /d delete/);
	assert.ok(skillsMenuOf(app), "the dialog is mounted before delete");
	type("d"); // closes the dialog and runs the legacy /skills delete command
	await settle();
	tui.renderNow(true);
	assert.equal(skillsMenuOf(app), undefined, "the dialog closes before the confirmation");
	assert.match(screen(), /Delete skill "local-skill"\?/);
	assert.equal(client.requests.some((entry) => entry.method === "skills.delete"), false, "nothing is deleted before the confirmation");
	type("\x1b[B");
	type("\r");
	await settle();
	const deletes = client.requests.filter((entry) => entry.method === "skills.delete");
	assert.equal(deletes.length, 1);
	assert.deepEqual(deletes[0]!.params, { name: "local-skill" });
});

test("P0-23 delete-close stops the menu's live work (no late skills.search)", async (t) => {
	const { app, client, type } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ skillId: "local-skill", name: "local-skill", sourceType: "learned", trust: "external" })];
	client.showSkill = detailsSkill({ name: "local-skill", sourceType: "learned" });
	type("/skills");
	type("\r");
	await settle();
	type("loc"); // schedules a debounced skills.search
	type("\r"); // details mode; the search timer is still pending
	await settle();
	type("d"); // closes the dialog through onDelete → close, never dismiss()
	await settle();
	assert.equal(skillsMenuOf(app), undefined, "the dialog closed");
	await delay(180); // the scheduled debounce window elapses
	const late = client.requests.filter(
		(entry) => entry.method === "skills.search" && String(entry.params.query ?? "") === "loc",
	);
	assert.equal(late.length, 0, "delete-close leaves no pending search timer behind");
});

test("P0-23 exit stops the menu's live work (no late skills.search)", async () => {
	const { app, client, type } = await boot();
	client.candidates = [candidate({ skillId: "web-build", name: "web-build" })];
	type("/skills");
	type("\r");
	await settle();
	type("rent"); // schedules a debounced skills.search
	await app.exit(); // dialog.cancel() funnels through openDialog close
	await delay(180);
	const late = client.requests.filter(
		(entry) => entry.method === "skills.search" && String(entry.params.query ?? "") === "rent",
	);
	assert.equal(late.length, 0, "exit leaves no pending search timer behind");
});

test("SM-08 a typed /skills list opens the Menu dialog and browses the catalog (no wall of text)", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ name: "web-build" })];
	const before = client.requests.length;
	type("/skills list");
	type("\r");
	await settle();
	tui.renderNow(true);
	const menu = skillsMenuOf(app);
	assert.ok(menu, "the Menu dialog opens for /skills list on an overlay host");
	assert.match(screen(), /Skills/);
	assert.equal(client.requests.slice(before).some((entry) => entry.method === "skills.list"), true, "the empty query browses the catalog through skills.list");
});

test("SM-09 bare /skills lists every skill immediately (catalog browse, no typing)", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [
		candidate({ name: "web-build" }),
		candidate({ name: "rag-pipeline" }),
		candidate({ name: "deploy" }),
	];
	type("/skills");
	type("\r");
	await settle();
	tui.renderNow(true);
	const frame = screen();
	assert.match(frame, /web-build/, "the first catalog row is visible without typing");
	assert.match(frame, /rag-pipeline/, "every catalog skill is listed, not just the first");
	assert.match(frame, /deploy/);
	assert.match(frame, /3 shown/, "the footer reports the full catalog count");
	const emptySearches = client.requests.filter(
		(entry) => entry.method === "skills.search" && String(entry.params.query ?? "") === "",
	);
	assert.equal(emptySearches.length, 0, "empty-query browse goes through skills.list, never an empty skills.search");
});
