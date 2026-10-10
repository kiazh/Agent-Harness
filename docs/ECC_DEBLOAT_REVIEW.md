# ECC usefulness and debloat review

Measured on 2026-10-10 against pinned ECC revision `4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2`. The initial assessment below proposed retaining all skills. **The user subsequently authorized removing irrelevant skills too. The implementation now retains 42 curated skills and 88 files (598,955 source bytes).** See [OpenCode continuation](OPENCODE_CONTINUATION.md) for verification and remaining work. The original proposal below is assessment history, not current scope.

## What is useful

ECC is a broad agent-workflow collection, not a runtime dependency required to operate this harness. Its upstream scope spans skills, hooks, agents, commands, integrations, and multiple agent platforms: [upstream repository](https://github.com/affaan-m/ECC). The harness consumes skill metadata, selected instructions, and requested resources through its own adapters. Copying the other platform implementations does not make their functionality active.

The most directly useful instructions for this project concern Python, testing, PostgreSQL, security review, API design, research-first development, verification, and MCP server patterns. Other language/domain skills help when users work on other projects, so absence of relevance to the harness's own source is not sufficient reason to delete them. The initial recommendation preserved all 302. The later user instruction explicitly supersedes that requirement: the 42 retained skills remain automatic defaults, and unrelated domains are no longer shipped.

Instructions can guide better work, but no controlled model evaluation has established a percentage improvement in code quality. Local orchestration tests prove routing, tool integration, resource integrity, and bounded context—not improved task success on every model. Actual lint, type checks, tests, review, and exact permission enforcement remain the evidence for code quality.

## Initial measured weight before curation

Sizes below are sums of tracked source bytes, not filesystem allocation. Compression uses Python ZIP deflate in memory, excluding the manifest and unrelated harness files; it is an estimate of vendor payload, not a rebuilt wheel measurement.

| Payload | Files | Source bytes | ZIP bytes |
| --- | ---: | ---: | ---: |
| Current complete upstream snapshot | 4,293 | 58,794,601 | 37,338,262 |
| Initial conservative proposal, superseded by 42-skill curation | 1,242 | 9,138,922 | 3,430,961 |

The initial proposal would have removed approximately **84.5% of source bytes** and **90.8% of compressed vendor bytes**. The implemented curation is substantially smaller, as shown at the top of this document and in [the continuation handoff](OPENCODE_CONTINUATION.md). Neither removes prior Git history; rewriting history is outside scope.

Largest current categories:

| Category | MiB | Current value to this harness |
| --- | ---: | --- |
| `assets/` | 22.48 | Mostly upstream illustration/promotional material; not automatically consumed |
| `docs/` | 12.07 | Some referenced guidance useful; the complete documentation/release archive is excessive |
| `tests/` | 5.73 | Tests for upstream implementation; not collected as harness tests |
| `skills/` | 4.59 | Core instruction bodies and skill-local resources; retain all |
| `scripts/` | 2.96 | Potential resources/dependencies for skill workflows; retain initially, never auto-run |
| Other platform/runtime implementations | Several MiB | Duplicate inactive implementations and configuration; generally removable after reference audit |

## Measured runtime behavior

One local process measured default registry construction/load at **24.29 ms**. Thirty repeated cached ranking calls per query gave medians around **1.37–1.43 ms**. These are machine-specific samples, not benchmarks of total turn latency or remote model performance.

| Task | Initial skill-context tokens | Observation |
| --- | ---: | --- |
| Python pytest fixtures | 879 | Local testing and ECC Python skills rank highly |
| PostgreSQL optimization | 1,132 | PostgreSQL guidance ranks appropriately |
| TypeScript TUI keyboard work | 885 | Local TUI skill ranks first; terminal-launch/ops skills may be less relevant |
| Authentication/permission security | 894 | Security skills rank highly, but BGP diagnostics also appears in top three |
| Python MCP support | 1,131 | MCP server patterns and Python guidance are discoverable |
| Greeting | 116 | No ranked skills; residual context includes the discovery guidance |

Reducing inactive images and upstream tests should reduce download/build/install size and manifest overhead. It should **not** be described as an equivalent reduction in model latency or prompt tokens: those files are already lazy. The existing 1,200-token initial budget remains the important prompt constraint. More relevant ranking and fewer unnecessary instruction reads are the next model-cost opportunities.

Registry loading also reported that local `cost-optimization` and `prompt-engineering` skills were skipped with `ValueError`. Diagnose these separately; do not weaken skill validation merely to load them. This review did not establish their failure causes.

## Concrete recommended cleanup

Preserve all files beneath:

```text
skills/
agents/
rules/
commands/
scripts/
schemas/
contexts/
mcp-configs/
```

Also preserve the root license and known directly linked shared references:

```text
LICENSE
docs/capability-surface-selection.md
docs/design/plan-canvas.md
hooks/README.md
```

The measured 1,242-file subset is the starting candidate, not a final dependency closure. Before removal, scan **all retained text resources**, not just canonical SKILL.md Markdown links, for relative references, script imports, supporting assets, and license requirements. Include any genuine transitive dependency. Some references point to external or absent projects such as `ECC-Tools`; retaining every upstream file cannot make those available. Record missing prerequisites honestly.

Remove unreferenced marketing assets, upstream test trees, release archives/translations, unused editor/plugin installations, duplicate runtimes, dashboards, package lockfiles, and unrelated upstream development configuration. Preserve retained files byte-for-byte and their notices. Do not trim individual skill instruction bodies in this pass.

Update `tools/vendor_ecc.py` to reproduce the selected dependency-closed subset from a clean pinned clone. Regenerate the manifest and its hardcoded integrity hash together. Test reproducibility, all 302 bodies, path confinement/tampering, shared resource access, and no silent broken resource references. Update `THIRD_PARTY_NOTICES.md`, `docs/ECC_SKILLS.md`, and `docs/HANDOFF.md` so none still claims the complete 4,293-file snapshot is shipped.

Build the wheel and inspect/extract it outside the checkout with a custom local skills directory. Confirm default discovery, task routing, all retained hashes, and offline reads. Compare actual wheel size before/after. Run the focused workflow, ECC, cache, and permission suites plus lint and whitespace checks. No new packages or platform-wide installations should be necessary.

## Follow-up performance and code-quality work

After package cleanup, evaluate relevance separately. Add a small labeled task set including security, TUI, MCP, framework switches, greetings, and general writing. Measure relevant top matches, context tokens, unnecessary skill reads, and actual task outcomes where feasible. Fix demonstrated false positives rather than deleting broadly useful skills or claiming lexical matching is semantic understanding.

Use meaningful provider/task evaluations before claiming code-quality gains. Track regressions, task completion, validation failures, latency, and model tokens. Keep deterministic harness tests for orchestration and authority. Do not add an extra classifier LLM request just to choose skills.

Broad removal of native harness subsystems, dependencies, research functionality, or public commands requires an actual usage/import analysis. This ECC assessment provides no evidence that those features are dead. Keep the UI/MCP plan intact and preserve the existing uncommitted workflow changes.
