# OpenCode continuation: curated ECC debloat

**Start here. Updated 2026-10-10.** The user asked to stop with remaining usage and leave the work for another agent. This file supersedes the ECC counts in the older [handoff](HANDOFF.md). The handoff's detailed UI interaction and MCP plans still apply.

## User's latest direction

The original requirement was all 302 ECC skills as automatic defaults. The user **explicitly changed that requirement**: remove skills that do not matter to this harness, not just upstream images and tests. Do not restore all 302 merely because an older document says they are mandatory.

The target is a smaller, coherent Python/TypeScript agent harness with automatic relevant skills, risk-based permissions, durable persona/growth, and future interactive TUI/MCP support. The next TUI work is planned only: `/persona` should open Add/Reset controls, Add should accept text and persist the default config, and Reset should ask yes/no. Do not mistake the documentation for an implemented UI.

## What was completed

| Measure | Before | Current |
| --- | ---: | ---: |
| Canonical ECC skills shipped | 302 | **42** |
| Manifest-owned upstream files | 4,293 | **88** |
| Vendor source bytes | 58,794,601 | **598,955** |
| ECC text lines | 901,551 | **18,040** |
| Filesystem allocation | About 67 MB | **616 KiB**, after generated cache cleanup |
| Built wheel | Historical prior build about 38.31 MB | **696,494 bytes**, about 680 KiB |

Source bytes dropped about **99.0%**, and ECC text lines dropped about **98.0%**. These are vendor measurements, not total source/dependency/Git-history size. Native Python harness text was about 36,783 lines and TUI source about 4,193 lines before this cleanup. The apparent million-line repository was overwhelmingly vendored ECC material.

The old wheel figure is historical; current wheel and source archive were freshly rebuilt and inspected. Git history remains unchanged: deleting files from the working tree does not shrink prior commits. Do not rewrite history as part of this task.

### Selection and provenance

Upstream pin remains `4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2`. Retained upstream bytes were not edited. `tools/vendor_ecc.py` now has the explicit `CURATED_SKILLS` selection, and computes bounded static reference/import dependencies. It preserves notices and applicable common/Python/TypeScript rules instead of all other language rules.

The current manifest hash, also set in `ah/skills/bundle.py`, is:

```text
01b3ca9e35a5e9d4b2cc9a80a0c27eae4d6d07d241312aa9015a26ff0e28ed36
```

The 42 defaults cover:

- Agent architecture, harness construction, introspection/debugging, and agent security hardening.
- API connectors/design, contracts, architecture decisions, hexagonal architecture, existing-style preservation, and scoped intent.
- Python patterns/tests, FastAPI, coding standards, error handling, TDD, verification, onboarding, and repository inspection.
- PostgreSQL and migrations, Docker/deployment, production audit, and Git workflow.
- Research-first/documentation/deep research, retrieval, compaction, prompt caching/optimization, cost-aware pipelines, content hashing, latency optimization, and growth lessons.
- MCP server patterns, MCP dependency review, and agent evaluation methodology.

The exact names are in `CURATED_SKILLS`, not this narrative list. Removed skills include unrelated languages/frameworks, mobile, media, social publishing, logistics, healthcare, financial/trading specialties, network equipment, and ECC platform installers. Platform-specific security-scan/skill-stocktake/compaction skills were also excluded; they were tightly tied to Claude configuration/hooks rather than native harness operation.

All retained defaults still activate automatically, stay bounded by the existing 1,200-token initial budget, and remain protected from local overwrite/disable/delete. Ordinary local skills are still supported. Removed domains can be added locally if genuinely needed later.

### Importer behavior and safety

The importer now:

1. Requires the pinned clean tracked Git snapshot.
2. Rejects symlinks/escaping paths before following references.
3. Starts from selected skill directories and notices.
4. Follows static Markdown/string/path references, relative Python imports, Node extension/index resolution, and some `path.join(__dirname, …)` references.
5. Excludes unselected skill domains and unsupported platform paths instead of pulling the entire upstream repo back in.
6. Records intentionally excluded known paths in manifest `excluded_references`.
7. Verifies all previous manifest-owned file hashes before overwriting/deleting, refusing locally modified content.
8. Prunes only previously manifest-owned files; unrelated untracked files are preserved.

Real Git fixture tests cover selected/removed skills, transitive Markdown/Python/Node references, deterministic repeated import, refusal to delete local modifications, and preservation of unrelated local files. These tests failed against the old importer before implementation and now pass.

The temporary upstream clone currently exists at `/private/tmp/ah-ecc-source`. Do not depend on its continued existence; clone upstream and check out the pin if needed. Reimport was run twice and produced byte-identical manifests:

```bash
.venv/bin/python tools/vendor_ecc.py /path/to/pinned/ECC
```

Updating selection requires regenerating the manifest and updating its hardcoded hash together. Never auto-download/update during chat.

### Small first-run bug fixed

Outside-checkout wheel verification exposed a real existing bug: built-in skill telemetry failed when the configured local skills directory did not exist. `ah/skills/registry.py` now creates the telemetry parent directory before saving. A red/green regression in `tests/test_skill_registry_cache.py` verifies usage survives a fresh registry with a newly created local directory.

Generated orphan `.pyc` files from removed upstream directories were cleaned. `.gitignore` now re-excludes ECC bytecode caches after its vendor allowlist. The wheel inspection found no bytecode caches included. A final narrow pass also removed Claude-hook-specific `strategic-compact` and its suggested-compaction hook, plus three orphan license files belonging to removed platform content.

## Exact verification at stopping point

| Check | Fresh result |
| --- | --- |
| Focused importer/workflow/ECC/runtime/cache/permission suite | **122 passed** (before the final removal of one Claude-specific skill; rerun after this last curation edit is pending) |
| Full backend suite after telemetry fix | **1,825 passed, 3 failed, 5 skipped**, 88.76 seconds, before the final removal of one Claude-specific skill |
| Ruff check | Passed for importer, new importer tests, bundle, registry, ECC/workflow/cache tests |
| Ruff format check | All seven checked files formatted |
| Wheel build and extracted-artifact smoke test | Passed after final 42-skill trim |
| Extracted-wheel execution outside checkout | Passed: imports from extracted artifact, 42 defaults, 88 hashes, all bodies, removed-domain absence, bounded coding workflow, fresh-directory telemetry |
| Source archive build/inspection | Passed: 42 skills, no `.env`, no upstream asset/test trees; archive 2,633,366 bytes |
| Reimport reproducibility | Byte-identical manifest |
| UI | No UI code changed; UI suites were not rerun in the debloat phase |
| Docker | Not built in this phase |

The full backend suite still has the same three documented baseline failures:

- `tests/test_memory.py::TestImportanceScorer::test_score_deterministic`: tiny timestamp-dependent float difference.
- `tests/test_research_phase2.py::test_labeled_identity_sequence_evaluation_cleans_up`: missing gitignored `research/identity_scenarios_example.json` fixture.
- `tests/test_schema_conformance.py::test_static_python_sql_compiles_against_schema`: `workflow_benchmark.py:69: UndefinedTableError` for a missing research table.

Do not claim the full suite is green. An earlier sandboxed focused run also failed to open `~/.agent-harness/logs/gateway.log`; rerunning with the already authorized pytest escalation passed. This is the development sandbox, not a harness approval-policy regression.

Useful repeat commands:

```bash
.venv/bin/python -m pytest tests/test_vendor_ecc.py tests/test_skill_workflows.py tests/test_ecc_skills.py tests/test_skill_runtime.py tests/test_skill_registry_cache.py tests/test_permission_risk_policy.py -q --tb=short
.venv/bin/python -m pytest -q --tb=short
.venv/bin/ruff check tools/vendor_ecc.py tests/test_vendor_ecc.py ah/skills/bundle.py ah/skills/registry.py tests/test_ecc_skills.py tests/test_skill_workflows.py tests/test_skill_registry_cache.py
.venv/bin/ruff format --check tools/vendor_ecc.py tests/test_vendor_ecc.py ah/skills/bundle.py ah/skills/registry.py tests/test_ecc_skills.py tests/test_skill_workflows.py tests/test_skill_registry_cache.py
.venv/bin/python -m hatchling build -t wheel
.venv/bin/python -m hatchling build -t sdist
git diff --check
```

Do not run schema-mutating test suites concurrently. Keep source unchanged during full-suite execution. Use `.venv/bin/python -m pip` if installing development tooling; the direct pip script previously had a stale checkout shebang. Never copy real `.env` content into diagnostics.

## Performance evidence and its limits

Cached routing with the default local playbooks was approximately **0.78–0.83 ms/query** after trimming versus about **1.4 ms/query** in the earlier full-bundle measurement. These are small machine-specific samples, not a rigorous benchmark or a promise of end-to-end model latency improvement. Default cold registry loading was 22.11 ms after trimming versus 24.29 ms before; local parsing still dominates part of that path.

With an empty custom local directory, curated-only loading was 3.17 ms and cached ranking about 0.28–0.36 ms. This is a different workload; do not use that cold figure as an apples-to-apples speedup claim.

Security ranking no longer produces unrelated BGP diagnostics. MCP patterns/dependency review and Python testing remain relevant high matches. However, a TUI query with no local playbooks still produced error-handling/prompt-optimizer/hexagonal-architecture as top matches. With default local skills, `cli-tui` ranks first. That local playbook itself mentions Textual even though the current UI uses `pi-tui`; it deserves a separate correction during actual UI work.

Initial injected context for sampled curated-only coding/security/MCP/TUI requests was around 1,131–1,133 tokens. Trimming files does not automatically reduce per-turn prompt size because the maximum budget is unchanged and remaining skill bodies are still long. No controlled evaluation has established a numerical code-quality gain. Deterministic tests establish orchestration, not model obedience or external-service availability.

## Remaining work, in priority order

1. **Review the large deletion diff carefully.** Expected outcome: roughly 882,000 removed lines, almost all vendor content. Check actual current Git stats; do not revert the user's intentional skill removals. No changes were committed or pushed.
2. **Audit retained-resource assumptions.** Static dependency discovery is conservative but not complete for computed paths. The manifest records 112 intentionally excluded known references, mostly other language rules and platform/project examples. This is not a guarantee that every retained upstream script runs unchanged. Skills remain instructions adapted to native tools; external accounts, hooks, binaries, and platform services are not installed. Decide whether to remove any retained platform-heavy skill or add narrowly needed resources based on an actual task, not restore an entire subtree.
3. **Improve relevance and prompt overhead using evidence.** Add a small labeled routing set for TypeScript/TUI, security, MCP, Python, short follow-ups, greetings, and general requests. Avoid selecting generic unrelated specialists just because their descriptions overlap. A greeting currently may still receive about 116 tokens of discovery boilerplate despite no skills matching; this was observed earlier and not changed. Preserve discovery for useful tasks while removing demonstrably unnecessary context. No extra classifier-model call.
4. **Correct local playbooks that do not match the runtime.** Investigate skipped `cost-optimization` and `prompt-engineering` local skills (`ValueError` during load). Diagnose the exact validation cause, not by weakening injection checks. Update the local TUI skill to the actual terminal library when implementing UI work.
5. **Triage baseline failures separately** if a clean release is required. Do not label them caused by this trim or erase assertions to hide them.
6. **Proceed with the interactive TUI design** in [HANDOFF.md, section 11](HANDOFF.md#11-next-plan-improve-the-existing-terminal-ui). Apply Impeccable during implementation. `/persona` is the first complete flow; reuse picker/editor/confirmation primitives throughout management commands. Keep it distinct from model chat and tool approval.
7. **Implement MCP natively** using [HANDOFF.md, section 12](HANDOFF.md#12-next-plan-native-mcp-support). Research current official SDK/spec, use shared `RuntimeServices`, enforce the existing broker, discover tools lazily, keep credentials redacted, and verify real server process/wire behavior.

Broad deletion of native research, permission, growth, scheduling, provider, or gateway subsystems was not done. This vendor trim supplied the largest justified reduction. Native feature/dependency removal needs a separate usage/import analysis and preservation of existing public behavior.

## Working-tree context: preserve previous work

Before debloat there were already changes to `ah/core/agent.py`, `ah/skills/runtime.py`, `ah/tools/base.py`, `ah/tools/skills.py`, `docs/ECC_SKILLS.md`, plus new `ah/skills/workflow.py`, `tests/test_skill_workflows.py`, and the handoff/review documents. They implement coherent automatic workflows and skill paging. Those were preserved. Some workflow tests were adapted from removed Rust/domain examples to retained Python/FastAPI/Docker examples while continuing to test stack switching/inheritance.

This phase additionally changed the importer, bundle manifest/hash, registry telemetry, README, third-party notices, ECC docs, Git cache exclusions, and tests; it removed unselected vendor files. Do not assume all uncommitted changes originated in debloat. Inspect `git status` and `git diff`; do not reset or clean indiscriminately.

The user wants action with little approval friction. Routine authorized reversible implementation should proceed. Exact dangerous effects still require the native policy. Skill contents, learned persona, remote resources, and MCP tool descriptions cannot override it.
