# Codebase review — 2026-10-13

Reviewed checkout: `0868b75` (`main`). This review changes no implementation code.

## Assessment

Machina has a substantial engineering foundation: explicit permission decisions,
durable ownership and usage accounting, transactional state updates, bounded
cleanup, and a large regression suite. However, the current checkout has
reproducible security and correctness defects and fails its configured lint and
format gates. I would fix the permission, approval, cache, and destructive-export
issues before using it with sensitive workspaces.

Passing tests do not cover several important combinations: recursive commands
and sensitive descendants; identical tool arguments in different workspaces;
declared extension effects and caching; interrupted provider streams; concurrent
document replacement; and failed agent responses returned normally to jobs.

## Verification

| Check | Observed result |
| --- | --- |
| Non-DB backend suite, excluding UI integration and optional workflow trial | 3,125 passed, 1 skipped, 1 expected failure; 309 deselected |
| DB-marked backend suite against local `machina_test` | 300 passed; 3,136 deselected |
| UI `npm run check` | Typecheck and lint passed; 337 tests passed, 6 skipped |
| Ruff 0.16.9 `check machina tools` | Failed: 9 errors, including 2 undefined `ToolDefinition` annotations |
| Ruff 0.16.9 `format --check machina tools` | Failed: 14 files need formatting |
| Focused memory/RAG/skills/trace suite | 458 passed; overlaps broader backend suite |
| Focused CLI/tool suite | 74 passed, 1 expected failure; overlaps broader backend suite |

The initial sandboxed DB-test invocation failed during collection because gateway
import opens a log outside the writable workspace. The approved rerun passed.
Live cloud-provider quality, Docker deployment, and optional workflow-trial
integration were not validated. Counts from overlapping suites are not added.

## Highest-priority findings

### 1. Recursive terminal reads bypass credential approval — high

[permissions/policy.py:168](../../machina/permissions/policy.py#L168)
checks explicit command operands for sensitive paths. It does not account for
sensitive descendants traversed by recursive commands.

In a temporary workspace containing a synthetic `.env`:

```text
cat .env                       -> pending approval
grep -R PRIVATE_TOKEN .         -> allowed
actual grep output             -> ./.env:PRIVATE_TOKEN=review-fixture-secret
```

This is an actual read, not only a policy prediction. Require review for opaque
recursive traversal or perform reads through a backend that enforces descendant
and symlink restrictions.

### 2. Missing or ambiguous approval identifiers approve the first request — high

[ui/features/mode.ts:81](../../ui/src/features/mode.ts#L81)
selects with `startsWith(rest)` and `.find`. Empty strings match every request.
The production command handler dispatches approval of the first request for
both `/approvals allow` and a prefix shared by two pending requests. Require a
nonempty unique identifier and carry the captured session binding into resolve.

### 3. Tool-cache results cross workspace boundaries — high

[tools/base.py:292](../../machina/tools/base.py#L292)
keys its process-global cache only by tool name and argument hash. The active
workspace/session and resolved file identity are absent.

With two temporary workspaces containing different `report.txt` contents:

```text
workspace A: registry read -> workspace a; direct read -> workspace a
workspace B: registry read -> workspace a; direct read -> workspace b
```

Both calls used their own workspace execution snapshot. The second result is
content from an unrelated authorized target. Scope caching to the effective
execution context, and avoid caching mutable filesystem reads without a reliable
freshness contract.

### 4. Approval proposals and displays persist raw credentials — high

[permissions/store.py:355](../../machina/permissions/store.py#L355)
copies raw `argv`, targets and network fields into the proposal;
[display_of:378](../../machina/permissions/store.py#L378) serializes them without
redaction. `create_approval` persists the proposal/display, and authenticated
[REST listing:27](../../machina/api/routes/approvals.py#L27) and gateway listing
return these records.

A synthetic bearer credential in argv remained in both `proposal_of(req)` and
`display_of(req)`. Card/SSE redaction does not protect the stored/public record.
Separate the canonical execution identity from a redacted public view; protect
any raw secret material needed for exact continuation.

## Other confirmed defects

| Priority | Location | Trigger and observed consequence | Recommended correction |
| --- | --- | --- | --- |
| Medium | [memory/redaction.py:651](../../machina/memory/redaction.py#L651) | An unfinished PEM exceeding 32 KiB advances the stream frontier into the key. A synthetic block with 600 normal 64-character body lines was fully redacted as one string but leaked its body through streaming. | Keep an explicit redacted-PEM state while discarding oversized bodies; do not emit raw prefixes. |
| Medium | [soulspec/schema.py:410](../../machina/soulspec/schema.py#L410) | Exporting a package into a nonempty directory deletes unrelated top-level files. A temporary `important.txt` was deleted. CLI export accepts a destination directory. | Refuse unrelated nonempty destinations or remove only files owned by the previous manifest. |
| Medium | [tools/base.py:286](../../machina/tools/base.py#L286) | A custom tool declared with `effects=('exec',)` ran once across two identical calls; the second returned its cached result. Cache classification uses names rather than declared effects. | Base cache eligibility and invalidation on explicit effects/purity. |
| Medium | [core/scheduler.py:925](../../machina/core/scheduler.py#L925) | An agent returns `AgentResponse(outcome='failed')`, without raising. Runner checks pauses only and finishes with `error=None`, recording an idle successful run. | Translate typed failed outcomes into failed job results. |
| Medium | [core/provider.py:2007](../../machina/core/provider.py#L2007), [2094](../../machina/core/provider.py#L2094) | HTTP 200 SSE with one partial text frame and clean EOF, without a finish frame or `[DONE]`, yielded text followed by successful `done`. Verified with `httpx.MockTransport` on DirectProvider. Similar unchecked EOF handling appears in other adapters. | Validate terminal protocol state before completing the response. |
| Medium | [cli/__init__.py:176](../../machina/cli/__init__.py#L176) | Headless `chat --provider ollama --model my-model` passes no overrides to the factory. A production CliRunner repro used the default model while printing the requested provider name. | Forward model/provider overrides to the shared factory. |
| Medium | [rag/pipeline.py:297](../../machina/rag/pipeline.py#L297) | Eight concurrent replacements of the same one-chunk document left seven chunks in PostgreSQL, instead of one. DELETE+INSERT transactions lack source-level serialization. | Serialize replacements by session/source, or use versioned document ownership. |
| Medium | [memory/consolidator.py:572](../../machina/memory/consolidator.py#L572), [251](../../machina/memory/consolidator.py#L251) | Valid JSON of the wrong shape, `{}`, becomes an empty successful extraction and advances the checkpoint. A remembered preference is permanently skipped on subsequent consolidation. | Treat schema-invalid output as retryable extraction failure. |
| Medium | [rag/chunker.py:168](../../machina/rag/chunker.py#L168) | Structured splitting starts at the first definition and discards earlier package/import/constant declarations when multiple definitions exist. Verified with Go source. Extension names such as `py` also differ from splitter language names such as `python`. | Preserve the preamble and normalize language identifiers. |
| Medium | [services.py:376](../../machina/services.py#L376) | Default `AgentDef.tools=[]` means unrestricted tools at execution but means no tool schemas in the compaction estimator. A mocked schema repro undercounted by 2,010 tokens. | Share one effective-allowlist resolution with execution. |
| Medium | [tools/rag.py:31](../../machina/tools/rag.py#L31), [62](../../machina/tools/rag.py#L62) | Rotating one nonempty Cohere key to another preserves the old pipeline/client because the cache tracks only key presence. Repro retained the old client key. | Track credential generation/identity safely and retire replaced clients. |
| Medium | [tools/builtins.py:300](../../machina/tools/builtins.py#L300), [346](../../machina/tools/builtins.py#L346) | `search_files('(a+)+$')` on a 36-character line exceeded an isolated two-second process deadline. Pattern length limits do not prevent catastrophic backtracking; a thread is not a reliable deadline boundary for Python regex holding the GIL. | Use bounded/linear matching or isolated process execution with termination. |
| Medium | [permissions/store.py:535](../../machina/permissions/store.py#L535) | Two-worker transactional fixture: A creates/approves durably, B claims durably, then A's pool is cleared and A claims its stale approved memory mirror again. Ordinary SQL errors already raise; this requires `db.connected=False`. | Never authorize durable-origin approvals from a stale memory fallback. |
| Medium | [api/routes/approvals.py:57](../../machina/api/routes/approvals.py#L57) | Session approval saves an operation such as `file.read` as its capability, while policy requires exact `credential_access`. The same sensitive read still asks after the grant. Gateway has the same mismatch. | Preserve the reviewed sensitive capability identities and scope semantics. |
| Medium | [core/context.py:1247](../../machina/core/context.py#L1247) | The protected-newest-ten eviction subquery includes documents although candidates/counts exclude them. A deterministic concurrent fixture with 20 conversations and 10 newer documents let two callers evict all 20 active conversations. Originals remained archived. Single-call bounds avoid this case. | Exclude documents from the protected survivor set too. |
| Medium | [ui/features/codex.ts:40](../../ui/src/features/codex.ts#L40), [ui/app.ts:569](../../ui/src/app.ts#L569) | `/init` writes `AGENTS.md` with default truncating flags, silently replacing existing instructions. Verified in a temporary directory. | Use exclusive creation or explicit replacement review. |
| Medium | [ui/features/agents.ts:52](../../ui/src/features/agents.ts#L52) | `/agents save helper My helper description` saves the name `helper my helper description`; later delegation to `helper` cannot find it. | Parse the first token as identifier and the remainder as description. |
| Medium | [install.sh:179](../../install.sh#L179) | A relative `MACHINA_ENV_FILE` is resolved for installer writes but not exported as its absolute result. After changing directories, child CLI resolves a different file. | Export the resolved override before initialization/doctor. |
| Medium | [connectors/oauth_network.py:55](../../machina/connectors/oauth_network.py#L55) | `http://[::1]:8080/mcp` normalizes to invalid `http://::1:8080/mcp`; discovery construction also omits brackets. | Preserve brackets when reconstructing IPv6 authorities. |
| Release gate | [tools/base.py:85](../../machina/tools/base.py#L85), [178](../../machina/tools/base.py#L178), plus six linted modules | Pinned Ruff reports two undefined annotations and seven style violations; formatting reports 14 files. Current CI commands fail. | Resolve annotations/imports and run the configured format/lint gates. |

## Lower-impact findings and limitations

- [memory/retriever.py:546](../../machina/memory/retriever.py#L546) accepts repeated
  reranking indices: `[0,0,0]` produces the same memory three times. Current
  production callers do not inject an LLM reranker, reducing exposure.
- [observability/traces.py:465](../../machina/observability/traces.py#L465) catches
  a body exception as setup failure when no turn is active and yields twice.
  A `ValueError` becomes `RuntimeError: generator didn't stop after throw()`.
  The controller wrapper preserves the original exception in its normal path.
- [ui/design/text.ts:60](../../ui/src/design/text.ts#L60) leaves C1 OSC/ST controls
  intact and its control validator accepts them. Terminal impact depends on
  terminal support; this is not a demonstrated universal terminal exploit.
- The bundled ECC eval example exits unsuccessfully because referenced variant
  metadata and reward-hack resources are absent. This affects the shipped
  example, not normal agent execution.
- GNU `find` writing flags are accepted as routine commands without write/security
  classification. Policy acceptance was reproduced; an actual protected write
  was not verified on this macOS host, whose `find` does not support those flags.
- A gateway cancellation-time release branch may emit completion after failed
  release. This remains a candidate, not an independently reproduced finding.
- Runtime startup does not apply every base-schema migration. This is explicitly
  documented in `OPERATIONS.md` as requiring `machina init` before upgrade, so it
  is an operational prerequisite rather than a newly claimed migration defect.

## Architecture observations

The deepest useful modules are the permission broker, durable turn ownership,
usage ledger, and transactional context store. They concentrate meaningful
behavior behind recognizable interfaces.

The largest opportunity is to make the permission module's interface describe
effective reads/writes, rather than infer them mainly from command names and
explicit operands. The recursive-read defect demonstrates missing locality:
policy and execution currently reason about different sets of files.

The tool registry module should own one interface for purity, execution scope,
and cache eligibility. Declared effects already exist, but caching independently
reconstructs mutability from names. Consolidating that implementation would
improve locality and make two-workspace/effect tests exercise the real seam.

The default-agent tool-list convention also needs one interface shared by
execution and compaction. Its two interpretations currently disagree. Large
provider and agent modules, repeated streaming implementations, and extensive
global singleton patching make cross-path mistakes harder to expose despite
the large suite. Favor behavioral integration tests at those seams over more
tests that mirror individual helpers.

## Coverage

Executable production inventory: 205 Python/SQL/JSON files under `machina`
(55,920 lines), 52 TypeScript source files (8,760 lines), installer/deployment
scripts, three release/vendor Python utilities, and bundled ECC JavaScript
scripts/examples. Source was divided across reviewers, with truncated reads
re-read. Test-source coverage is being reconciled separately; execution counts
above do not imply that every test file was read.

Documentation and historical transcripts are supporting material, not a claim
of exhaustive line-by-line documentation review. Dependency implementation,
generated caches, binary assets, and the real local `.env` were not read as
application source. No production data or source was uploaded to an external
audit service.
