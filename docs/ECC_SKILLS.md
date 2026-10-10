# Default automatic ECC skills

The harness ships **42 curated ECC skills**, unchanged, from [ECC](https://github.com/affaan-m/ECC/tree/4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2), commit `4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2`. They are enabled by default and cannot be disabled, deleted, or overwritten through local skill management. No install step, feature flag, upstream account, or explicit skill invocation is required. This is runtime instruction learning and retrieval, not model weight training.

The user authorized removing irrelevant skills after the initial complete import. The retained set targets Python/FastAPI, API contracts, PostgreSQL, testing, security, architecture, research, retrieval/context/cost optimization, Docker/deployment, and MCP work. `CURATED_SKILLS` in `tools/vendor_ecc.py` defines the selection. Unrelated industry, media, social, mobile, blockchain, network operations, other framework, and upstream installer skills are removed. Removed domains can still be installed as ordinary local skills.

## Research and design

[Agent Skills' integration guide](https://agentskills.io/client-implementation/adding-skills-support) describes loading metadata first, instructions when applicable, and resources on demand. [Anthropic's skills design](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) similarly separates those layers. A full catalog still grows with every installed skill; [Anthropic's advanced tool-use guidance](https://www.anthropic.com/engineering/advanced-tool-use) discusses retrieving tool descriptions rather than presenting everything upfront.

The implementation applies those ideas without a provider-specific tool-search API or extra classifier model call:

1. Cache name, description, and trigger metadata locally. Rank each task with BM25-style term weighting, aliases, whole-word local triggers, and technology filters.
2. Compose domain, inspection/planning, testing, security and verification skills into one native workflow. Short follow-ups inherit recent task context; explicit frameworks and local team triggers retain priority. Before the first provider request, automatically load excerpts from at most two anchor workflows, alongside an ordered plan of up to six complementary skills. This shared preparation path covers CLI, gateway, normal and streaming chat, and built-in agents. Restricted custom agents receive the instructions while retaining their explicit tool restrictions.
3. Added skill context is at most **1,200 tokens**, also limited to one fifth of the session budget and the remaining system-prompt allowance. Tiny budgets may leave no space for skills; core instructions and the current user request retain priority.
4. The prompt instructs the model to read remaining workflow steps before using them, and to search for uncovered subtasks. `skill_list(query)` searches metadata, `skill_read(name, offset, limit, query)` reads up to 8,000 characters and can jump directly to a matching passage, and `skill_resource(name, path, offset, limit)` reads supporting files. Excerpts include exact character continuation offsets. Resource listings are paginated too, and paths accept skill-relative references/scripts as well as ECC-root shared files. Resource reads bypass the tool-result cache so integrity checks still run.
5. Every turn routes again; there is no permanently growing catalog in model context. Content and the search index are cached locally. Reading telemetry does not force source reparsing.

A checkout measurement found **21,114 tokens** for the entire enabled metadata catalog and **645,083 tokens** for all instruction bodies. The Python-test sample added about 1,000 tokens. Cached routing was approximately 0.4 ms per query on the development machine; these are local measurements, not guarantees for other tokenizers, machines, or future bundle revisions.

Routing is lexical, so implicit tasks and unfamiliar wording can miss the best initial match. The model's bounded search/read tools provide the second discovery layer. Tests exercise representative tasks and negative matches; they do not prove perfect routing across every possible request. Only selected instruction excerpts are initially loaded, not entire long workflows.

## Resources and permissions

Names are prefixed with `ecc-` to avoid local collisions: for example, `ecc-python-testing`. The curated subset preserves 88 upstream files byte-for-byte: selected skill directories, statically discovered reference/import dependencies, applicable common/Python/TypeScript rules, and notices. A pinned hash manifest lists retained files and records `excluded_references` to intentionally omitted domains/platform material. Those references are not available locally; translate upstream instructions into native workflows or report unavailable prerequisites. Static discovery does not guarantee arbitrary dynamically constructed script paths; review new skills before adding them. The manifest and each resource read are verified. Absolute paths, unlisted paths, symlinks escaping the bundle, and traversal are rejected. Binary assets remain available at their packaged paths to authorized file/image tools.

Skills are reference instructions subordinate to the user, the agent's core role, and the harness permission broker. Skill reads are side-effect-free and do not add approval prompts. Scripts, installation, networking, deployment, and other effects still use existing authorized tools. Upstream Claude/OpenCode slash commands, hooks, external integrations, providers, and agent launchers are not installed or executed merely by loading a skill; the model must translate workflows into available harness tools and state missing external prerequisites when relevant. The request to bundle skills does not authorize purchases, messages, or deployments.

`AGENT_HARNESS_SKILLS_DIR` changes the local skill directory while leaving the packaged ECC defaults available. Wheels and Docker images include the bundle. `ah skills` and the existing skills UI list the defaults; `skills.show` returns complete instructions. Local skill creation and learned skills retain their existing validation and management behavior.

## Reproducing the bundle

Clone ECC and check out the pinned revision, then run:

```sh
.venv/bin/python tools/vendor_ecc.py /path/to/ECC
```

The importer rejects a different revision or modified tracked files, selects the curated set and static dependencies, copies bytes without running upstream scripts, checks existing managed hashes before overwriting/deleting, prunes only previously manifest-owned files, and regenerates `skills/ecc/manifest.json`. Its SHA-256 must match `MANIFEST_SHA256` in `ah/skills/bundle.py`. Updating the pin requires deliberately updating both constants and rerunning bundle, routing, permission, and packaging checks. No update or download occurs during chat.

ECC is MIT licensed: Copyright (c) 2026 Affaan Mustafa. The full notice is preserved in [the bundled license](../skills/ecc/LICENSE) and [third-party notices](../THIRD_PARTY_NOTICES.md).

## Verification

Current debloat verification: **122 focused tests passed**, including real Git fixture importer tests, retained defaults, routing, workflows, resource integrity, cache behavior, and permissions. See [OpenCode continuation](OPENCODE_CONTINUATION.md) for packaging/full-suite results. The larger-bundle results below are historical, not current resource counts.

Historical full-bundle verification passed **132 tests**, read all 302 instruction bodies, and checked 4,293 hashes. This was before the user requested curation; these counts do not describe the current distribution. Backend lint/format checks and UI typecheck/lint passed in that earlier phase; the UI feature suite passed 47 tests.

The full backend run recorded **1,824 passed, 3 failed, 5 skipped**. The failures match the previously reproduced baseline: `TestImportanceScorer.test_score_deterministic` (tiny timestamp-dependent float difference), `test_labeled_identity_sequence_evaluation_cleans_up` (missing research fixture), and `test_static_python_sql_compiles_against_schema` (research benchmark table absent). They are separate from the skill integration. Upstream bundled tests are resource files, not part of harness test discovery.

Cohesive workflow verification additionally exercises actual skill reads, file writes, a Python test-runner subprocess, verification reads and final responses through both normal and streaming agent loops. Those tests use a deterministic provider to verify orchestration and authority, not to claim every model will always follow instructions perfectly. Native search/delegation/memory tools supply common ECC workflow equivalents; account-bound integrations still require their real account credentials.

The earlier cohesive-workflow focused checks passed 122 tests, including 12 workflow tests. After curation, the latest focused checks again passed 122 tests, including tests that reject removed domains and the Claude-hook-specific compaction skill. The current extracted wheel was verified with all 42 defaults, all 88 retained resource hashes, MCP routing, and telemetry persistence. The larger 4,293-file wheel result above is historical.
