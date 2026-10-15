# Adversarial review — first-party skills wave (13 new + 2 reworded)

Date: 2026-10-15
Reviewer: adversarial subagent (read-only; no fixes, no commits)
Method: adversarial-reviewer skill — Saboteur / New Hire / Security Auditor, every persona must surface findings
Target: `5ebec10` (13 new skills) + `5bee364` (scanner-trip reword), evaluated at `main @ 2308751`
Test env used: `export MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`, `.venv/bin/python -m pytest … -q -p no:cacheprovider`

## Scope

Reviewed: `skills/{background-jobs,chaos-engineering,data-visualization,feature-flags,grpc-services,incident-response,kubernetes-ops,observability-otel,prompt-injection-defense,rate-limiting,redis-caching,supply-chain-security,terraform-iac}/SKILL.md`, the two reworded skills (`skills/cost-optimization`, `skills/prompt-engineering`), `docs/verification/proofs/extensions_2026-10-15/**`, packaging config, registry/scanner/routing code, tests, README/docs staleness around this wave, and ECC pins.

Not reviewed as wave scope (verified untouched by this wave only): `skills/ecc/**`, `machina/bundled_agents/**` — `git diff --stat 5ebec10^..HEAD -- skills/ecc/ machina/bundled_agents/` returned empty.

Environment caveats (disclosed, not my edits):
- The worktree was clean at review start (2308751). Mid-review, a concurrent lane modified `machina/memory/__init__.py` (mtime 12:29:56) and `tests/test_config_import_cycle.py` (12:27:27) and added untracked `docs/verification/proofs/holistic_review_2026-10-15/` files. `git diff --stat HEAD -- skills/` is empty, so all skills findings below stand; test runs after ~12:27 executed on a moving non-skills tree (skills suites still 100% green).
- Running the skills suites mutates the gitignored runtime file `skills/telemetry.json` (see W4). This happened during this review's mandated fresh test runs; it is counters-only state, not a repo artifact.

## Verdict: CONCERNS

No CRITICAL findings. Four WARNINGs: no test pins zero-skip catalog loading (W1), duplicate triggers demonstrably misroute one query class (W2), the wave's own evidence doc is stale after `5bee364` (W3), and the telemetry "unchanged" claim does not reproduce (W4). All structural, ground-truth, scanner, packaging, and fresh-test checks otherwise pass. Merge is defensible once W1/W3 are addressed; W2/W4 should be tracked.

### Critical Findings

None. Explicitly: every functional check below (load, scan, package, test, ground truth) passed; the failures are evidence/verification and routing-quality gaps, not breakage.

## Warnings

### W1 — No regression pin that the full first-party catalog loads with zero skips (Saboteur + New Hire; NOTE promoted to WARNING)

The only default-catalog load test asserts three ECC names, not completeness:
- `tests/test_fm6_scanner.py:100-108` — `registry.load_all()` then `assert {"ecc-deep-research","ecc-python-patterns","ecc-tdd-workflow"} <= names`. It would still pass if any of the 34 first-party skills silently dropped.
- The only skill-count pin is ECC-only: `tests/test_ecc_skills.py:13` `assert len(bundled) == 42`. Grep for a first-party pin returns nothing: `grep -rnE "first[-_ ]party|== ?(76|74|34|32|21)\b" tests/ --include="*.py"` → only `tests/test_kimi_provider.py:6` (unrelated) and the ECC pin.
- Direct proof the suite cannot catch a skip: the pre-fix evidence itself — `logs/skill_load_check.log:3-4` shows both skills "Skipping … ValueError" while `logs/skill_suites.log:9` shows "230 passed". The skip co-existed with green suites.

Failure mode: an editor adding a literal attack example to `skills/prompt-injection-defense/SKILL.md` (the natural thing to do) would trip `machina/skills/registry.py:262` (`system\s+prompt`) or the "ignore … previous instructions" family, `load_all` skips it best-effort at `registry.py:686-688` with only a `logger.warning`, and nothing fails. The verified catalog of today is `TOTAL=76 FIRST_PARTY=34 ECC=42 SKIP_WARNINGS=0`; nothing pins that.

Verify: `.venv/bin/python - <<'EOF' … SkillRegistry().load_all() with a capture handler … EOF` → `SKIP_WARNINGS=0`; `grep -rn "load_all" tests/`; read `test_fm6_scanner.py:100-108`.

### W2 — Duplicate triggers: one demonstrated misroute ("retry"), plus cache/rate-limit/streaming collisions (Saboteur + New Hire; NOTE promoted to WARNING)

Dump of all triggers of the 76 loaded skills (ECC bundle skills carry `triggers=[]`, so collisions are first-party):

```
'cache':        [cost-optimization, redis-caching]          # redis-caching introduces the 2nd
'rate limit':   [rate-limiting, security-sandboxing]        # rate-limiting introduces the 2nd
'retry':        [background-jobs, llm-provider]             # background-jobs introduces the 2nd
'embedding':    [embedding-finetuning, rag-pipeline]
'retrieval':    [memory-management, rag-pipeline]
'rls':          [postgresql-pgvector, security-sandboxing]
'streaming':    [langgraph, llm-provider]
'template':     [prompt-engineering, skills-system]
'token budget': [context-runtime, cost-optimization]
```

Containment: `prompt` ⊂ `prompt caching`/`prompt injection`, so `prompt-engineering` fires on prompt-injection queries (observed: "harden our agent against indirect prompt injection attempts" ranks `[prompt-injection-defense, prompt-engineering, ecc-prompt-optimizer, …]`); `cache` ⊂ `cache invalidation`/`cache stampede`; `streaming` ⊂ `streaming rpc` (observed: "grpc streaming rpc deadlines" pulls `langgraph` + `llm-provider` into the workflow).

Demonstrated misroute (the one that matters): query **"add retry logic to the worker"**:

```
rank_skills → ['llm-provider', 'background-jobs', 'ecc-cost-aware-llm-pipeline', …]
plan_workflow → kind=task selected=['llm-provider', 'background-jobs', 'ecc-cost-aware-llm-pipeline']
```

`machina/skills/workflow.py:210-223` marks the first trigger-matching strong skill as the "requested workflow" — so the workflow label attributes a background-jobs question to the LLM-provider skill. Matching semantics that make this stick: flat `+25` for any whole-word trigger (`machina/skills/runtime.py:85-88,115-116`; `workflow.py:60-63`), so both candidates get the boost and tie-breaking decides the lead. Impact is bounded (both skills are still injected, top-2 at `runtime.py:164`, and the header is advisory), which is why this is not escalated further — but the wave added the losing-tie duplicate three times and only the retry case was reproduced end-to-end.

Verify: trigger dump + `rank_skills`/`plan_workflow` script as cited; read `registry.py:704-725`, `runtime.py:85-123`, `workflow.py:50-69,210-223`.

### W3 — The wave's evidence doc is stale after `5bee364` and contradicts HEAD (New Hire + Saboteur; NOTE promoted to WARNING)

`docs/verification/proofs/extensions_2026-10-15/SKILLS_first_party.md` was last modified by `5ebec10`; `5bee364` fixed the two skips but updated no doc. Still-current-at-HEAD claims that are now false:
- Lines 96-104: "`skills/cost-optimization` and `skills/prompt-engineering` are **skipped at load with `ValueError`** … Their files are unmodified by this lane" — both now load (verified: 76/34/42, 0 skips) and both files were modified in `5bee364`.
- Line 105: "No commits were made." — the doc itself shipped in a commit.
- `logs/skill_load_check.log:20` "registry total: 74" and "first-party loaded: 32" — the log is pre-fix; HEAD is 76/34. The 76/0-missing claim lives only in commit messages, not in any regenerated evidence file.

Open action items that should be closed by the fix (still phrased as unresolved):
- `docs/ECC_DEBLOAT_REVIEW.md:50`: "local `cost-optimization` and `prompt-engineering` skills were skipped with `ValueError`. Diagnose these separately; … This review did not establish their failure causes." — the cause is now known (literal "system prompt" matching `registry.py:262`) and fixed.
- `docs/OPENCODE_CONTINUATION.md:134`: "Investigate skipped `cost-optimization` and `prompt-engineering` local skills (`ValueError` during load)." — same.

Verify: `git log --oneline 5ebec10..HEAD -- docs/verification/proofs/extensions_2026-10-15/SKILLS_first_party.md` (empty); grep line numbers above; fresh registry run.

### W4 — Telemetry "left unchanged" does not reproduce; test runs mutate the real file (Saboteur + New Hire; NOTE promoted to WARNING)

`SKILLS_first_party.md:59-60` claims "no telemetry sync was required and `skills/telemetry.json` is left unchanged (`git diff skills/telemetry.json` is empty after the full test run)." Both halves are misleading:
- `skills/telemetry.json` is untracked and gitignored (`git ls-files skills/telemetry.json` → empty; `.gitignore:71-72`), so `git diff` is vacuously empty — it cannot detect any change.
- The fresh suite run **does** mutate it. During this review, `stat` went `1792081505` → `1792081670` (16:25:05Z → 16:27:50Z) across a single `tests/test_ecc_skills.py` run, and the file's counters were bumped: `ecc-python-patterns=3020`, `ecc-python-testing=1139`, `testing-qa=10015` with `last_activity_at=2026-10-15T16:27:50Z`. Mechanism: `tests/test_ecc_skills.py:25-40` builds a default `SkillRegistry()` (no `MACHINA_SKILLS_DIR`) and `automatic_skill_context` calls `record_use` (`runtime.py:231`), which writes merged telemetry (`registry.py:736-742,597-651`).
- The same file also carries stale fixture keys from earlier runs: `['my-learned-skill','skill1','skill2','test','unused','used','x-y-z']` are not on disk, proving fixture-named writes reached the developer's real state.

No tool regenerates or validates enumeration (`grep -rn "telemetry.json" machina/ tools/ tests/` → registry + tmp-fixture tests + `tools/check_release_artifacts.py:84-89`, which only asserts the wheel exclusion — enforced and passing). So "no sync required" is materially true, but the stated verification does not reproduce, and the mutation is real. Counters-only, no secrets — hence WARNING, not higher.

Verify: commands above; read `test_ecc_skills.py:25-40,186-200`.

## Notes

- **N1 — line-count convention (no defect).** The doc/log counts ("41–46 lines", per-file table) are `len(text.split("\n"))`, which counts the trailing newline as an extra entry; `wc -l` gives 40–45. The doc table reproduces exactly under its own convention (verified programmatically against all 13 files). All 13 are within the 40–100-line bar either way. If you care about reviewer reproducibility, state the method in the doc.
- **N2 — body style extension: "## Pitfalls".** All 13 new skills include a `## Pitfalls` section; none of the 21 pre-existing first-party skills do (`skills/prompt-engineering`, `skills/multi-agent`, `skills/skills-system` are Core Skills + Key Resources + Practice Project). New files are 40–45 `wc -l` vs 24–40 for the existing 21. This is a deliberate-looking enrichment, not a defect — but it is a new house style, so decide it explicitly rather than discover it drift-wise. Verify: structural script over all 34 `SKILL.md` frontmatters (`name/description/triggers/version` only) and section scan.
- **N3 — the reworded trigger "system prompt" → "system instructions" neither breaks nor fully restores vocabulary.** Nothing in tests/docs/registry relies on the removed trigger (`grep -rn "system prompt" tests/ ui/` → scanner/general uses only; no routing query). Routing for the old vocabulary still resolves via the generic `prompt` trigger: `rank_skills("write a system prompt for my agent")[0] == prompt-engineering`, `plan_workflow` selects it first. Note the replacement phrase is rarer than the codebase's canonical term (`machina/services.py:286,353` uses "system instructions" for the assembled prompt, so it is at least a real term). No action required.
- **N4 — sdist carries runtime telemetry (pre-existing, documented).** Wheel is clean (0 telemetry entries; all 13 + 76 SKILL.md); the sdist force-includes `skills` wholesale and carried both `skills/telemetry.json` and `skills/telemetry.json.lock` in my build (`pyproject.toml:62-75` documents this; `check_release_artifacts.py` gates only the wheel). Not introduced by this wave; noted for release hygiene if sdists are ever published from used checkouts.
- **N5 — ground truth on the two reworded skills' feature claims (pre-existing lines, untouched by the reword).** Verified to exist: tiktoken token budgeting (`machina/core/assembler.py:19-29,78`), `PromptAssembler` (`assembler.py:87`), prompt caching (`machina/core/provider/transport.py:375`), cache-aware eviction as practice (also in `skills/memory-management:29`). Approximate/residual: "Cost dashboard per session/agent" (`cost-optimization:24`) maps to the `machina usage` command (`machina/cli/__init__.py:670`), which reports accounted tokens/budgets — there is no dollar-cost dashboard anywhere in `machina/`; "Jinja2 template library" (`prompt-engineering:22`, also `skills-system:25`) has no Jinja2 usage anywhere in the repo (deps or code), and `skills-system:21` "Embedding-based trigger matching" contradicts the actual keyword/IDF matcher (`registry.py:704-725`, `runtime.py:81-123`). These read as practice prompts, not feature claims, and predate this wave — flagged for the catalog ground-truth backlog, not as wave defects. Also verified: the 13 new skills reference **zero** machina tools/commands/paths (`grep -rn "machina|skill_read|skill_list|MACHINA|ecc-|delegate|tool(" skills/<13>/SKILL.md` → no matches, exit 1), and all 33 external resource URLs return HTTP 200 (`curl -s -o /dev/null -m 8 -L -w "%{http_code}" <url>` loop).
- **N6 — stale "29 skins" in a historical review record.** `docs/verification/proofs/final_review_2026-10-15/FINAL_REVIEW_2026-10-15.md:119` says "all 29 skins" — current count is 41 (`9098f12`). It is a dated review record, and no user-facing doc (README/docs) enumerates the count, so impact is limited to readers of the proof itself.

## Checklist coverage (items 1–10, all addressed)

| # | Item | Result | Key command / evidence |
|---|---|---|---|
| 1 | Schema + house style of 13 new files | **PASS** (N1, N2 notes) | Structural script: `name==dir`, description str, 4–8 lowercase triggers, `version: 1.0.0`, no `dependencies` key, 40–45 `wc -l`, no permission-granting language (only "Least privilege" advice in `prompt-injection-defense:25`). Compared against `skills/prompt-engineering`, `skills/multi-agent`, `skills/skills-system`. |
| 2 | Ground truth (tools/commands/paths/features) | **PASS for the 13** (N5 for the 2 reworded, pre-existing) | 13 new files have zero machina references; reworded claims spot-checked against `assembler.py`, `transport.py:375`, `cli/__init__.py:670`; no Jinja2/cost-dashboard implementation exists (residual note). All 33 URLs 200. |
| 3 | Injection scanner / all skills load | **PASS** | `SkillRegistry().load_all()` with captured warnings → `TOTAL=76 FIRST_PARTY=34 ECC=42 SKIP_WARNINGS=0`; per-skill "loaded/enabled" print for all 15 reviewed skills; per-file `_scan_injection` run on all 15 files → no trip; near-words found (`ignore` in background-jobs, `forget` in observability-otel, `instructions` in prompt-injection-defense/engineering/cost-optimization) but none form a pattern; `prompt-injection-defense` embeds no raw attack strings and explicitly documents best-effort limits. |
| 4 | Trigger collisions | **FINDINGS W2** (misroute demonstrated); removal-safety **PASS** | Full dup/near-dup dump across 76 loaded skills; `rank_skills`/`plan_workflow` probes; nothing (tests/docs/registry) relies on the removed "system prompt" trigger; old-vocabulary routing still works via `prompt`. |
| 5 | Duplication vs existing skills + ECC | **PASS** | Closest ECC neighbors read via `ECCBundle`: `ecc-agent-security-hardening`/`ecc-security-review` (vs prompt-injection-defense), `ecc-content-hash-cache-pattern`/`ecc-prompt-caching-strategy`/`ecc-cost-aware-llm-pipeline` (vs redis-caching/cost-optimization), `ecc-deployment-patterns`/`ecc-docker-patterns` (vs kubernetes-ops/terraform-iac), `ecc-api-design` mentions rate limiting. Scopes are distinct; no true duplication. |
| 6 | Packaging | **PASS** | `.venv/bin/python -m pip wheel --no-build-isolation --no-deps -w <tmp> .` then zipfile listing → 438 entries, **76 SKILL.md**, all 13 present, 0 telemetry; `tools/check_release_artifacts.py <wheel>` → "Isolated wheel imports, 71 ECC agents, resource hashes: OK". Sdist: 76 SKILL.md **plus** telemetry.json/.lock (N4); config cited: `pyproject.toml:70-75`. |
| 7 | telemetry.json consumers | **W4** (claim not reproducible); materially "no sync" is correct | Consumers: `registry.py`, tmp-fixture tests, `check_release_artifacts.py:84-89` (wheel exclusion, passing). No regenerator exists (`ls tools/`). Fresh runs mutate the ignored file (counters above). |
| 8 | Doc-staleness sweep | **W3, N6**; otherwise PASS | `ECC_DEBLOAT_REVIEW.md:50`, `OPENCODE_CONTINUATION.md:134` stale (W3). Themes: only historical `FINAL_REVIEW_2026-10-15.md:119` "29 skins" (N6); README enumerates no theme count. Copilot default: `machina/core/provider/copilot.py:134 DEFAULT_MODEL="gpt-6.1-sol"`; no stale `gpt-4o` default in docs (remaining `gpt-4o-mini` is the OpenAI API spec default at `machina/core/provider/specs.py:50`, not copilot). Harnesses/interop: `machina/core/self_knowledge.py:27` and `README.md:285` both present `harnesses` as canonical with `interop` as kept alias; no stale CLI guidance found. |
| 9 | Fresh test runs | **PASS; plus W1** | Fresh: 8-suite batch (incl. scanner+triggers, `-p no:cacheprovider`) **277 passed**; exact doc cmd 1 **230 passed**; doc cmd 2 **72 passed**; atomicity/ecc-agents/vendor **15 passed**; fm6 telemetry+workflow-stages **5 passed**; `test_comprehensive.py -k skill` **51 passed, 109 deselected**; `test_code_quality.py` **20 passed**. Missing zero-skip pin → W1. |
| 10 | Evidence integrity | **PASS with caveats (W3, W4, N1)** | Doc table (descriptions, triggers, per-file line counts) reproduces exactly against the files; test totals 230/72/15/51 reproduce; registry claim 76/0-missing reproduces (the doc does not state it; the pre-fix log says 74/32 and the doc's Notes contradict HEAD → W3). |

## Summary

The 13 new skills are structurally sound, real (no invented machina surface), scanner-clean, packaged into the wheel, and covered by green fresh suites; the `5bee364` reword genuinely closed the loading hole (76 loaded, 34 first-party, 42 ECC, 0 skips). The wave's weaknesses are in verification discipline, not the artifacts: nothing pins "the full first-party catalog loads with zero skips" so the exact failure this wave fixed can silently return (W1), the evidence doc and two roadmap docs still describe the fixed skips as open (W3), the telemetry "unchanged" claim cannot be reproduced because the file is gitignored and test runs mutate it (W4), and the new `retry` trigger demonstrably steals the lead from `background-jobs` for retry-on-worker queries (W2). Verdict: **CONCERNS** — merge defensibly, but add the zero-skip regression test and refresh the evidence doc first; track W2/W4.
