// A5 interactive skills directory (spec §12.3, §18 U01–U07).
//
// Part A drives the SkillsPicker component directly (real layout, real row
// mapping, real key handling) over scripted providers whose payloads mirror
// the gateway `skills.search` / `skills.show` / `skills.preview_route`
// handlers field-for-field (machina/gateway/features/skills.py).
//
// Part B boots the real App on a TuiAltScreen with a scripted terminal and
// a recording client: `/skills` opens the overlay through the real command
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
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import {
	SKILLS_SECTION_BUDGET,
	SKILLS_SOURCES,
	SKILLS_TASK_FAMILIES,
	SkillsPicker,
	classificationLabel,
	firstSectionSlug,
	skillDeletable,
	type SkillsCandidate,
	type SkillsDetailsSkill,
	type SkillsPickerProviders,
	type SkillsPreviewResult,
	type SkillsSearchParams,
	type SkillsSearchResult,
	type SkillsSectionParams,
	type SkillsSectionResult,
} from "../src/components/SkillsPicker.ts";
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

/** `skills.section` payload builder (handler `_section_payload` shape). */
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
	providers: SkillsPickerProviders;
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

function lines(pane: SkillsPicker, width = 70): string[] {
	return pane.render(width).map((line) => stripTerminalSequences(line).trimEnd());
}

function plain(pane: SkillsPicker, width = 70): string {
	return lines(pane, width).join("\n");
}

/** Wrapped content lines joined into one normalized stream (details/preview prose). */
function flow(pane: SkillsPicker, width = 70): string {
	return lines(pane, width).join(" ").replace(/\s+/g, " ");
}

function rowOf(pane: SkillsPicker, needle: string, width = 70): number {
	return lines(pane, width).findIndex((line) => line.includes(needle));
}

function mouse(type: "press" | "click", y: number, clickCount = 1, width = 70): TuiMouseEvent {
	return {
		type,
		button: "left",
		x: 3,
		y,
		screenX: 3,
		screenY: y,
		width,
		height: 24,
		shift: false,
		alt: false,
		ctrl: false,
		clickCount,
	};
}

function delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── Part A: component ──────────────────────────────────────────────────────

test("U01 activate searches immediately and shows loading, then results", async () => {
	const rec = recorder(async (params) =>
		searchResult({ query: params.query, candidates: [candidate(), candidate({ skillId: "deploy", name: "deploy", applicability: "conditional", reasons: ["declared stack not confirmed: kubernetes (query does not mention it)"] })] }),
	);
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	assert.equal(rec.searches.length, 1, "activate requests the empty query immediately");
	assert.equal(rec.searches[0]!.query, "");
	assert.equal(pane.getView().status, "loading");
	assert.match(plain(pane), /\(searching…\)/);
	await delay(0);
	assert.equal(pane.getView().status, "ready");
	const screen = plain(pane);
	assert.match(screen, /Skills:/);
	assert.match(screen, /web-build/);
	assert.match(screen, /\[applicable\]/);
	assert.match(screen, /\[conditional\]/);
});

test("U01 empty/loading/unavailable/invalid/conditional states are distinct", async () => {
	const empty = recorder(async (params) => searchResult({ query: params.query, candidates: [] }));
	const pane = new SkillsPicker(empty.providers);
	pane.activate();
	await delay(0);
	assert.match(plain(pane), /\(no query yet — type to search the skill catalog\)/);

	pane.handleInput("z");
	await delay(200);
	assert.match(plain(pane), /\(no lexical evidence for "z"\)/);
	assert.notEqual(classificationLabel("no_lexical_evidence", "z"), classificationLabel("invalid_catalog", "z"));

	pane.handleInput("z");
	await delay(200);
	assert.match(plain(pane), /no lexical evidence/);

	// invalid catalog: distinct from an empty query.
	const invalid = recorder(async () => searchResult({
		candidates: [],
		diagnostics: { classification: "invalid_catalog", catalog_size: 0, retrieved: 0, applicable: 0, conditional: 0, excluded: 0, corrections: [], weights_applied: false },
	}));
	const invalidPane = new SkillsPicker(invalid.providers);
	invalidPane.activate();
	await delay(0);
	assert.match(plain(invalidPane), /\(catalog unavailable — no skills loaded\)/);

	// conditional-only: rows exist but the state is disclosed.
	const conditional = recorder(async () => searchResult({ candidates: [candidate({ applicability: "conditional", reasons: ["unverified precondition: tests run"] })] }));
	const conditionalPane = new SkillsPicker(conditional.providers);
	conditionalPane.activate();
	await delay(0);
	const screen = plain(conditionalPane);
	assert.match(screen, /! conditional matches only — no applicable skill/);
	assert.match(screen, /\[conditional\]/);
});

test("U01 unavailable gateway is an honest error state, never a fake success", async () => {
	const rec = recorder(async () => {
		throw new Error("Method not found: skills.search");
	});
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	assert.equal(pane.getView().status, "error");
	assert.match(plain(pane), /\(search failed: Method not found: skills\.search\)/);
	assert.equal(pane.getView().candidates.length, 0);
});

test("U01 typing debounces provider calls; filters round-trip the closed filter object", async () => {
	const rec = recorder(async (params) => searchResult({ query: params.query, candidates: [candidate()] }));
	const pane = new SkillsPicker(rec.providers, { debounceMs: 15 });
	pane.activate();
	for (const char of ["w", "e", "b"]) pane.handleInput(char);
	assert.equal(rec.searches.length, 1, "no provider call while typing");
	await delay(40);
	assert.deepEqual(rec.searches.map((call) => call.query), ["", "web"]);
	assert.equal(pane.getQuery(), "web");

	// tab → filter row; ↓ cycles the task family; → moves slots.
	pane.handleInput("\t");
	pane.handleInput("\x1b[B");
	assert.equal(pane.getView().filters.task_family, SKILLS_TASK_FAMILIES[0]);
	pane.handleInput("\x1b[C");
	pane.handleInput("layout");
	assert.equal(pane.getView().filters.capability, "layout");
	pane.handleInput("\x1b[C");
	pane.handleInput("\x1b[B");
	assert.deepEqual(pane.getView().filters.source, [SKILLS_SOURCES[0]]);
	pane.handleInput("\x1b[C");
	pane.handleInput("\x1b[B");
	assert.equal(pane.getView().filters.enabled, true);
	assert.equal(pane.getView().filters.include_excluded, false);
	await delay(40);
	const last = rec.searches.at(-1)!;
	assert.equal(last.filters.task_family, SKILLS_TASK_FAMILIES[0]);
	assert.equal(last.filters.capability, "layout");
	assert.deepEqual(last.filters.source, [SKILLS_SOURCES[0]]);
	assert.equal(last.filters.enabled, true);
});

test("U02 selection is preserved by stable skill ID across refresh, filtering and resize", async () => {
	let call = 0;
	const rec = recorder(async () => {
		call += 1;
		return call === 1
			? searchResult({ candidates: [candidate({ skillId: "a", name: "alpha" }), candidate({ skillId: "b", name: "beta" }), candidate({ skillId: "c", name: "gamma" })] })
			: searchResult({ candidates: [candidate({ skillId: "c", name: "gamma" }), candidate({ skillId: "b", name: "beta" }), candidate({ skillId: "a", name: "alpha" })] });
	});
	const pane = new SkillsPicker(rec.providers, { debounceMs: 5 });
	pane.activate();
	await delay(10);
	pane.handleInput("\x1b[B"); // select beta (index 1)
	assert.equal(pane.getView().selectedSkillId, "b");

	pane.handleInput("x"); // narrower query; a fresh provider response reorders the list
	await delay(25);
	assert.deepEqual(pane.getView().candidates.map((row) => row.skillId), ["c", "b", "a"]);
	assert.equal(pane.getView().selectedSkillId, "b", "selection follows the ID, not the row index");

	// resize: a smaller row budget and narrower width keep the same identity
	pane.setVisibleRows(2);
	const screen = plain(pane, 30);
	assert.equal(pane.getView().selectedSkillId, "b");
	assert.ok(screen.includes("beta"), "the selected candidate stays in the viewport");
	for (const line of pane.render(30)) {
		assert.ok(visibleWidth(line) <= 30, `row wider than 30: ${JSON.stringify(stripTerminalSequences(line))}`);
	}
});

test("U03 hostile labels stay on one physical row and mouse maps to the rendered row", async () => {
	const hostile = candidate({
		skillId: "evil",
		name: "evil\n\u001b[31mrow\u001b[0m\ttwo",
	});
	const rec = recorder(async () => searchResult({ candidates: [hostile, candidate({ skillId: "beta", name: "beta-skill" })] }));
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	const rendered = pane.render(70);
	// header + filters + 2 candidates + footer: the newline and controls never
	// create extra physical rows or shift the next candidate.
	assert.equal(rendered.length, 5, "hostile label adds no physical rows");
	for (const line of rendered) assert.ok(visibleWidth(line) <= 70);

	const betaRow = rowOf(pane, "beta-skill");
	assert.ok(betaRow > 0);
	pane.handleMouse(mouse("press", betaRow));
	pane.handleMouse(mouse("click", betaRow));
	assert.equal(pane.getView().selectedSkillId, "beta", "click selects the row under the pointer");

	const evilRow = rowOf(pane, "evil");
	pane.handleMouse(mouse("click", evilRow));
	assert.equal(pane.getView().selectedSkillId, "evil");
	pane.handleMouse(mouse("click", 0)); // the header row never selects a candidate
	assert.equal(pane.getView().selectedSkillId, "evil");

	// A wrapped notice line adds physical rows below the filter row; the hit
	// map must account for the extra span (narrow width forces the wrap).
	pane.handleInput("\x12"); // ctrl+r with an empty query → notice row
	const narrow = lines(pane, 30);
	const noticeHead = narrow.findIndex((line) => line.includes("type a request"));
	const noticeTail = narrow.findIndex((line) => line.includes("box first"));
	assert.ok(noticeHead >= 0 && noticeTail > noticeHead, "the notice wraps onto more than one physical row at width 30");
	const betaNarrowRow = narrow.findIndex((line) => line.includes("beta-skill"));
	assert.ok(betaNarrowRow > 0);
	pane.handleMouse(mouse("click", betaNarrowRow, 1, 30));
	assert.equal(pane.getView().selectedSkillId, "beta", "the wrapped notice never shifts the hit target");
});

test("U05 details show purpose, applicability, provenance, prerequisites, tools, resources and gated actions", async () => {
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
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	pane.handleInput("\r");
	await delay(0);
	assert.deepEqual(rec.details, ["web-build"]);
	const screen = flow(pane);
	assert.match(screen, /web-build — conditional/);
	assert.match(screen, /purpose: Build a website from a plain request/);
	assert.match(screen, /source: bundled \(builtin-ecc\) · version 1\.2\.0 · content rev00000/);
	assert.match(screen, /reasons: tool availability unknown: node, git; unverified precondition: repo has a test runner/);
	assert.match(screen, /prerequisites: repo has a test runner/);
	assert.match(screen, /required tools: node, git/);
	assert.match(screen, /instructions: \d+ chars — read with skills\.section \(content rev00000\)/);
	assert.match(screen, /usage: 4 · enabled: yes/);
	assert.match(screen, /matched: build a website, layout/);
	assert.match(screen, /stages: inspect, implement/);

	// bundled defaults must not acquire an unsupported delete control
	assert.match(screen, /delete unavailable \(bundled default\)/);
	assert.doesNotMatch(screen, /d delete/);
	assert.equal(skillDeletable({ sourceType: "builtin-ecc" }), false);
	assert.equal(skillDeletable({ sourceType: "learned" }), true);

	pane.handleInput("\x1b"); // back to results
	assert.equal(pane.getView().mode, "results");
});

test("U05 deletable skills expose the delete action and its confirmation hookup", async () => {
	const rec = recorder(
		async () => searchResult({
			candidates: [candidate({ skillId: "local-skill", name: "local-skill", sourceType: "learned", trust: "external", applicability: "excluded", enabled: false, reasons: ["disabled: skill 'local-skill' is disabled"] })],
		}),
		async () => detailsSkill({ name: "local-skill", sourceType: "learned", enabled: false }),
	);
	const pane = new SkillsPicker(rec.providers);
	const deletions: string[] = [];
	pane.onDelete = (name) => deletions.push(name);
	pane.activate();
	await delay(0);
	pane.handleInput("\r");
	await delay(0);
	const screen = plain(pane);
	assert.match(screen, /disabled: skill 'local-skill' is disabled/);
	assert.match(screen, /d delete/);
	assert.match(screen, /enabled: no/);
	pane.handleInput("d");
	assert.deepEqual(deletions, ["local-skill"]);
});

test("U04 preview renders descriptor, selection and disclosure estimates from the response", async () => {
	const rec = recorder(async (params) => searchResult({ query: params.query, candidates: [candidate()] }));
	const pane = new SkillsPicker(rec.providers, { debounceMs: 5 });
	pane.activate();
	pane.handleInput("m");
	await delay(25);
	const searchesBefore = rec.searches.length;
	pane.handleInput("\x12"); // ctrl+r → preview this request
	await delay(0);
	assert.deepEqual(rec.previews, ["m"]);
	assert.equal(rec.searches.length, searchesBefore, "the preview action issues no search RPC");
	assert.equal(rec.details.length, 0, "the preview action issues no details RPC");
	assert.equal(pane.getView().mode, "preview");
	const screen = flow(pane);
	assert.match(screen, /Route preview: m/);
	assert.match(screen, /class: coding · scope: create · families: web-build/);
	assert.match(screen, /reasons: task_class=coding \[intent:action-verb\]/);
	assert.match(screen, /selection: coding · skills: web-build/);
	assert.match(screen, /steps: 1\. web build \(web-build, stage implement, ~120 tok\)/);
	assert.match(screen, /disclosure: included 340\/1200 tok \(char\/4\) · chosen: web-build · Implementation/);
	assert.match(screen, /omitted: 1 section\(s\) — budget exhausted/);
	assert.match(screen, /preview only — no turn, no provider call, no installation, no approval change/);
	pane.handleInput("\x1b");
	assert.equal(pane.getView().mode, "results");
});

test("U06 a provider failure keeps the draft/filter and never fakes success", async () => {
	let fail = true;
	const rec = recorder(async (params) => {
		if (fail) throw new Error("gateway disconnected");
		return searchResult({ query: params.query, candidates: [candidate()] });
	});
	const pane = new SkillsPicker(rec.providers, { debounceMs: 5 });
	pane.activate();
	await delay(10);
	pane.handleInput("w");
	await delay(25);
	assert.equal(pane.getView().status, "error");
	assert.equal(pane.getQuery(), "w", "the draft filter text survives the failure");
	assert.match(plain(pane), /search failed: gateway disconnected/);

	fail = false;
	pane.handleInput("e");
	await delay(25);
	assert.equal(pane.getView().status, "ready");
	assert.equal(pane.getQuery(), "we");
	assert.match(plain(pane), /web-build/);
});

test("U01 pagination: nextCursor loads more through the same provider", async () => {
	let call = 0;
	const rec = recorder(async (params) => {
		call += 1;
		if (call === 1) {
			assert.equal(params.cursor, undefined);
			return searchResult({ query: params.query, candidates: [candidate({ skillId: "a", name: "alpha" })], nextCursor: "offset:10" });
		}
		assert.equal(params.cursor, "offset:10");
		return searchResult({ query: params.query, candidates: [candidate({ skillId: "a", name: "alpha" }), candidate({ skillId: "b", name: "beta" })], nextCursor: null });
	});
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	assert.equal(pane.getView().nextCursor, "offset:10");
	assert.match(plain(pane), /ctrl\+n more/);
	pane.handleInput("\x0e"); // ctrl+n
	await delay(0);
	assert.deepEqual(pane.getView().candidates.map((row) => row.skillId), ["a", "b"]);
	assert.equal(pane.getView().nextCursor, null);
});

test("U06 details and preview failures each render an honest error state", async () => {
	const detailsRec = recorder(
		async (params) => searchResult({ query: params.query, candidates: [candidate()] }),
		async () => {
			throw new Error("show unavailable");
		},
	);
	const pane = new SkillsPicker(detailsRec.providers);
	pane.activate();
	await delay(0);
	pane.handleInput("\r");
	await delay(0);
	assert.equal(pane.getView().mode, "details");
	assert.equal(pane.getView().detailsStatus, "error");
	assert.match(plain(pane), /\(details unavailable: show unavailable\)/);
	pane.handleInput("\x1b");
	assert.equal(pane.getView().mode, "results");

	const previewRec = recorder(
		async (params) => searchResult({ query: params.query, candidates: [candidate()] }),
		async () => detailsSkill(),
		async () => {
			throw new Error("preview unavailable");
		},
	);
	const previewPane = new SkillsPicker(previewRec.providers, { debounceMs: 5 });
	previewPane.activate();
	previewPane.handleInput("q");
	await delay(25);
	previewPane.handleInput("\x12");
	await delay(0);
	assert.equal(previewPane.getView().mode, "preview");
	assert.equal(previewPane.getView().previewStatus, "error");
	assert.match(plain(previewPane), /\(preview failed: preview unavailable\)/);
	assert.equal(previewPane.getQuery(), "q", "the request text survives the preview failure");
	previewPane.handleInput("\x1b");
	assert.equal(previewPane.getView().mode, "results");
});

test("tab/esc focus contract: esc leaves the filter row before closing", async () => {
	const rec = recorder();
	const pane = new SkillsPicker(rec.providers);
	let closed = 0;
	pane.onClose = () => { closed += 1; };
	pane.activate();
	await delay(0);
	pane.handleInput("\t");
	assert.equal(pane.getView().focus, "filters");
	pane.handleInput("\x1b");
	assert.equal(pane.getView().focus, "query");
	assert.equal(closed, 0, "esc in the filter row returns to the query, not out");
	pane.handleInput("\x1b");
	assert.equal(closed, 1);
});

// ─── A6: in-overlay versioned section reader (§10.4/§12.2) ─────────────────

test("A6 details read fetches the first declared section with the shown revision and pages its continuation", async () => {
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
			return sectionResult({
				text: "Then build it.",
				start: 26,
				end: 40,
				sectionTotal: 26,
				continuation: null,
			});
		},
	);
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	pane.handleInput("\r"); // details
	await delay(0);
	assert.equal(rec.sections.length, 0, "opening details does not read the section");
	assert.match(flow(pane), /s read section/);

	pane.handleInput("s");
	await delay(0);
	assert.equal(rec.sections.length, 1);
	const first = flow(pane);
	assert.match(first, /section: Orientation \(11 of 26 chars · declared · content rev00000\)/);
	assert.match(first, /Start here\./);
	assert.match(first, /\[section continues at offset 12 — press s\]/);
	assert.match(first, /s continue section/);

	pane.handleInput("s");
	await delay(0);
	assert.equal(rec.sections.length, 2);
	const second = flow(pane);
	assert.match(second, /Then build it\./);
	assert.doesNotMatch(second, /continues at offset/);
	assert.match(second, /s read section/);
});

test("A6 a stale section read is honest and the retry uses the revision the server reported", async () => {
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
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	pane.handleInput("\r");
	await delay(0);
	pane.handleInput("s");
	await delay(0);
	const staleScreen = flow(pane);
	assert.match(staleScreen, /section stale: orientation was requested at content rev00000, but the skill changed to 89abcdef — nothing from the edited text was served/);
	assert.match(staleScreen, /press s to re-read content 89abcdef/);
	assert.doesNotMatch(staleScreen, /Start here\./, "no passage is served from the edited content");
	pane.handleInput("s");
	await delay(0);
	assert.match(flow(pane), /Fresh orientation\./);
});

test("A6 a failing section read is an honest error state, never fabricated text", async () => {
	const rec = recorder(
		async (params) => searchResult({ query: params.query, candidates: [candidate()] }),
		async () => detailsSkill(),
		async () => previewResult(),
		async () => {
			throw new Error("skills.section unavailable");
		},
	);
	const pane = new SkillsPicker(rec.providers);
	pane.activate();
	await delay(0);
	pane.handleInput("\r");
	await delay(0);
	pane.handleInput("s");
	await delay(0);
	assert.equal(pane.getView().sectionStatus, "error");
	assert.match(flow(pane), /\(section read failed: skills\.section unavailable\)/);
	assert.equal(pane.getView().section, null, "no passage is invented on failure");
	pane.handleInput("\x1b");
	assert.equal(pane.getView().mode, "results");
});

// ─── Part B: real App command path ──────────────────────────────────────────

class ScriptedSkillsClient {
	requests: Array<{ method: string; params: Record<string, unknown> }> = [];
	candidates: SkillsCandidate[] = [];
	searchError: Error | undefined;
	previewError: Error | undefined;
	sectionError: Error | undefined;
	showSkill: SkillsDetailsSkill = detailsSkill() as unknown as SkillsDetailsSkill;
	/** Content revision the fake server believes is current (change to simulate edits). */
	sectionRevision = "rev0000000000";

	start(): void {}
	stderr(): string[] {
		return [];
	}
	async stop(): Promise<void> {}

	/** Minimal real-handler behavior: first heading, slug, that section's text. */
	sectionResponse(params: Record<string, unknown>): SkillsSectionResult {
		const content = this.showSkill.content ?? "";
		const requested = params.contentRevision === undefined ? undefined : String(params.contentRevision);
		const wanted = typeof params.section === "string" && params.section ? params.section : firstSectionSlug(content);
		if (requested !== undefined && requested !== this.sectionRevision) {
			return sectionResult({
				skillId: String(params.skillId ?? ""),
				sectionId: wanted,
				heading: "",
				text: "",
				stale: true,
				contentRevision: this.sectionRevision,
				diagnostics: [{
					code: "section-stale-content",
					message: `section '${wanted}' was read against revision ${requested}; the requested revision ${requested} no longer matches the current content`,
					skill_id: String(params.skillId ?? ""),
				}],
			});
		}
		const sections: Array<{ id: string; heading: string; text: string }> = [];
		const lines = content.split("\n");
		let current: { id: string; heading: string; body: string[] } | null = null;
		for (const line of lines) {
			const heading = /^(?:#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
			if (heading) {
				if (current) sections.push({ id: current.id, heading: current.heading, text: current.body.join("\n").trim() });
				current = { id: firstSectionSlug(line), heading: heading[1] ?? "", body: [] };
			} else if (current) {
				current.body.push(line);
			}
		}
		if (current) sections.push({ id: current.id, heading: current.heading, text: current.body.join("\n").trim() });
		const found = sections.find((entry) => entry.id === wanted) ?? sections[0];
		if (!found) {
			return sectionResult({
				skillId: String(params.skillId ?? ""),
				sectionId: wanted,
				heading: "",
				text: "",
				stale: false,
				contentRevision: this.sectionRevision,
				diagnostics: [{ code: "section-unknown", message: `parsed headings contain no section '${wanted}'`, skill_id: String(params.skillId ?? "") }],
			});
		}
		return sectionResult({
			skillId: String(params.skillId ?? ""),
			sectionId: found.id,
			heading: found.heading,
			text: found.text,
			sectionTotal: found.text.length,
			contentRevision: this.sectionRevision,
		});
	}

	searchResponse(params: Record<string, unknown>): SkillsSearchResult {
		const query = String(params.query ?? "");
		const filters = (params.filters ?? {}) as SkillsSearchResult["filters"];
		if (!query) {
			return searchResult({ query, filters, candidates: [], diagnostics: { classification: "no_lexical_evidence", catalog_size: this.candidates.length, retrieved: 0, applicable: 0, conditional: 0, excluded: 0, corrections: [], weights_applied: false } });
		}
		const matched = this.candidates.filter((row) => row.name.toLowerCase().includes(query.toLowerCase()));
		return searchResult({
			query,
			filters,
			candidates: matched,
			diagnostics: {
				classification: matched.some((row) => row.applicability === "applicable") ? "ok" : matched.some((row) => row.applicability === "conditional") ? "conditional_only" : matched.length ? "all_excluded" : "no_lexical_evidence",
				catalog_size: this.candidates.length,
				retrieved: matched.length,
				applicable: matched.filter((row) => row.applicability === "applicable").length,
				conditional: matched.filter((row) => row.applicability === "conditional").length,
				excluded: matched.filter((row) => row.applicability === "excluded").length,
				corrections: [],
				weights_applied: false,
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
				return {
					skills: this.candidates.map((row) => ({
						name: row.name,
						description: `Description of ${row.name}`,
						triggers: row.matchedPhrases,
						version: "1.0.0",
						sourceType: row.sourceType,
						usageCount: 0,
						enabled: row.enabled,
					})),
				} as T;
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

async function boot(): Promise<Booted> {
	const { term, type, resizeTo } = terminal();
	const tui = new TuiAltScreen(term);
	const client = new ScriptedSkillsClient();
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return {
		app,
		tui,
		client,
		type,
		resizeTo,
		screen: () => tui.getScreenLines().map(stripTerminalSequences).join("\n"),
	};
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("U01/U02 real /skills command opens the search overlay and typing filters it", async (t) => {
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
	assert.match(screen(), /Skills:/, "bare /skills opens the interactive picker");
	const pane = (app as unknown as { skillsPicker: SkillsPicker }).skillsPicker;
	assert.ok(pane, "the overlay is mounted");
	assert.equal(pane.getView().mode, "results");

	type("web");
	await delay(200); // provider debounce
	tui.renderNow(true);
	assert.ok(
		client.requests.slice(mark).some((entry) => entry.method === "skills.search" && entry.params.query === "web"),
		"typing drives skills.search",
	);
	assert.match(screen(), /web-build/);
	assert.match(screen(), /\[applicable\]/);
	assert.doesNotMatch(screen(), /\[conditional\]/, "filtered-out rows disappear");

	// U02: selection identity follows the ID across a terminal resize while
	// the same result set is on screen (the provider-reorder case is covered
	// by the component-level test).
	type("\x1b[B"); // select web-deploy (index 1)
	assert.equal(pane.getView().selectedSkillId, "web-deploy");

	resizeTo(40, 30);
	await settle();
	tui.renderNow(true);
	assert.equal(pane.getView().selectedSkillId, "web-deploy", "the selected ID survives a terminal resize");
	assert.match(screen(), /web-deploy/);
});

test("U04 preview never starts a turn and records zero non-preview RPCs", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ skillId: "web-build", name: "web-build" })];
	const mark = client.requests.length;
	type("/skills");
	type("\r");
	await settle();
	type("make me a website");
	await delay(200); // debounce
	tui.renderNow(true);
	assert.ok(screen().includes("Route preview") === false, "no preview before the action");

	type("\x12"); // ctrl+r previews the current request text
	await settle();
	tui.renderNow(true);
	const after = client.requests.slice(mark);
	assert.ok(after.some((entry) => entry.method === "skills.preview_route" && entry.params.request === "make me a website"), "the preview uses the typed request");
	const unexpected = after.filter((entry) => entry.method !== "skills.search" && entry.method !== "skills.preview_route");
	assert.deepEqual(unexpected, [], "the preview path records zero non-preview RPCs (no turn, no provider, no install, no approval change)");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.submit").length, 0, "no turn was started");
	const internals = app as unknown as { inFlight: Set<string>; running: boolean };
	assert.equal(internals.inFlight.size, 0);
	assert.equal(internals.running, false);
	const previewScreen = screen();
	assert.match(previewScreen, /Route preview: make me a website/);
	assert.match(previewScreen, /class: coding/);
	assert.match(previewScreen, /selection: coding · skills: web-build/);
	assert.match(previewScreen, /preview only — no turn, no provider call/);

	type("\x1b"); // back to results
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /Skills:/);
});

test("A6 real App: details → s reads skills.section with the shown revision and records no other RPCs", async (t) => {
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

	type("s"); // read the first declared section
	await settle();
	tui.renderNow(true);
	const calls = client.requests.slice(mark).filter((entry) => entry.method === "skills.section");
	assert.equal(calls.length, 1, "one versioned section read");
	assert.deepEqual(calls[0]!.params, {
		skillId: "web-build",
		contentRevision: "rev0000000000",
		section: "orientation",
		budget: 800,
	});
	const unexpected = client.requests.slice(mark).filter((entry) => entry.method !== "skills.section");
	assert.deepEqual(unexpected, [], "the read path records zero non-section RPCs (no turn, provider, install or approval change)");
	assert.equal(client.requests.filter((entry) => entry.method === "prompt.submit").length, 0, "no turn was started");
	const pane = (app as unknown as { skillsPicker: SkillsPicker }).skillsPicker;
	assert.equal(pane.getView().sectionStatus, "ready");
	let flat = screen().replace(/\s+/g, " ");
	assert.match(flat, /section: Orientation \(11 of 11 chars · declared · content rev00000\)/);
	assert.match(flat, /Start here\./);

	// The skill is edited between reads: the pane serves the honest stale state,
	// then re-reads against the revision the server reported as current.
	client.sectionRevision = "aaaabbbbccccdddd";
	type("s");
	await settle();
	tui.renderNow(true);
	flat = screen().replace(/\s+/g, " ");
	assert.match(flat, /section stale: orientation was requested at content rev00000, but the skill changed to aaaabbbb/);
	assert.match(flat, /press s to re-read content aaaabbbb/);
	assert.doesNotMatch(flat, /Start here\./, "no passage is served from the edited content");
	type("s");
	await settle();
	tui.renderNow(true);
	const reads = client.requests.filter((entry) => entry.method === "skills.section");
	assert.equal(reads.at(-1)!.params.contentRevision, "aaaabbbbccccdddd");
	assert.match(screen().replace(/\s+/g, " "), /Start here\./);

	// A failing read is an honest error state.
	client.sectionError = new Error("gateway disconnected");
	type("s");
	await settle();
	tui.renderNow(true);
	assert.match(screen().replace(/\s+/g, " "), /\(section read failed: gateway disconnected\)/);
});

test("U06 a gateway failure shows an honest state and keeps the filter", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ skillId: "web-build", name: "web-build" })];
	type("/skills");
	type("\r");
	await settle();
	client.searchError = new Error("gateway disconnected");
	type("web");
	await delay(200);
	tui.renderNow(true);
	assert.match(screen(), /search failed: gateway disconnected/);
	const pane = (app as unknown as { skillsPicker: SkillsPicker }).skillsPicker;
	assert.equal(pane.getQuery(), "web", "the draft filter text is retained");
	client.searchError = undefined;
	type("\x7f"); // "we" → request again
	await delay(200);
	tui.renderNow(true);
	assert.match(screen(), /web-build/);
});

test("U05 bundled skills expose no delete control; excluded skills stay honest", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [
		candidate({ skillId: "deploy", name: "deploy", applicability: "excluded", enabled: false, reasons: ["disabled: skill 'deploy' is disabled"], sourceType: "learned", trust: "external" }),
	];
	client.showSkill = detailsSkill({ name: "deploy", sourceType: "learned", enabled: false });
	type("/skills");
	type("\r");
	await settle();
	type("deploy");
	await delay(200);
	tui.renderNow(true);
	assert.match(screen(), /\[excluded\]/);
	assert.match(screen(), /! all retrieved candidates are excluded/);

	// bundled skill: details show the honest gating, no delete control
	const pane = (app as unknown as { skillsPicker: SkillsPicker }).skillsPicker;
	assert.ok(pane);
	pane.dismiss();
	await settle();
	client.candidates = [candidate({ skillId: "web-build", name: "web-build" })];
	client.showSkill = detailsSkill({ name: "web-build", sourceType: "builtin-ecc" });
	type("/skills");
	type("\r");
	await settle();
	type("web");
	await delay(200);
	type("\r"); // details
	await settle();
	tui.renderNow(true);
	assert.match(screen(), /delete unavailable \(bundled default\)/);
	assert.doesNotMatch(screen(), /d delete/);
});

test("U05 the picker delete action routes through the legacy confirmation", async (t) => {
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
	type("d"); // closes the picker and runs the legacy /skills delete command
	await settle();
	tui.renderNow(true);
	assert.equal((app as unknown as { skillsPicker: SkillsPicker | undefined }).skillsPicker, undefined, "the picker closes before the confirmation");
	assert.match(screen(), /Delete skill "local-skill"\?/);
	assert.equal(client.requests.some((entry) => entry.method === "skills.delete"), false, "nothing is deleted before the confirmation");
	type("\x1b[B"); // move to the Delete action
	type("\r");
	await settle();
	const deletes = client.requests.filter((entry) => entry.method === "skills.delete");
	assert.equal(deletes.length, 1);
	assert.deepEqual(deletes[0]!.params, { name: "local-skill" });
});

test("U07 a typed /skills list opens the interactive directory on overlay hosts (legacy table remains the no-surface fallback)", async (t) => {
	const { app, tui, client, type, screen } = await boot();
	t.after(() => app.exit());
	client.candidates = [candidate({ name: "web-build" })];
	const before = client.requests.length;
	type("/skills list");
	type("\r");
	await settle();
	tui.renderNow(true);
	// The interactive directory is the product surface for a typed `list` too
	// (features/skills.ts); the byte-identical printed table is asserted for
	// hosts without that surface in features.test.ts (U07 fallback).
	const pane = (app as unknown as { skillsPicker: SkillsPicker | undefined }).skillsPicker;
	assert.ok(pane, "the interactive picker opens for /skills list on an overlay host");
	assert.match(screen(), /Skills:/);
	assert.equal(
		client.requests.slice(before).some((entry) => entry.method === "skills.list"),
		false,
		"the interactive path issues no legacy list RPC",
	);
});
