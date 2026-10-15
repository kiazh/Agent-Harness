# Final review — line-by-line pass: rag / repomap / tools / skills / soulspec / research / db

- **Date:** 2026-10-15
- **Mode:** READ-ONLY. No source edits (only this evidence file written). No runtime execution —
  every result is static reasoning plus callsite/counterexample analysis.
- **Tree reviewed:** `HEAD` = `1e1edbf` (post-fix state).
- **Skills applied:** `code-reviewer` (checklist + SOLID/smell lens) and `diagnose`
  (falsification discipline adapted: no runtime loop is permitted, so each finding
  carries an explicit red-team falsification attempt instead of a reproduced failure).
- **Evidence file:** this document only.

## Scope actually read (every line)

- rag: `chunker.py`, `loaders.py`, `pipeline.py`, `search.py`, `embedder.py`
- repomap: `__init__.py`, `cache.py`, `evidence.py`, `scan.py`
- tools: `file.py`, `base.py`, `builtins.py`, `harness.py`, `skills.py`, `terminal.py`, `rag.py`
- skills: `registry.py`, `repo_install.py`, `workflow.py` (+ `bundle.py` for the `skill_resource` call path)
- soulspec: `__init__.py`, `adapters.py`, `lifecycle.py`, `merge.py`, `runtime.py`, `schema.py`
- research: `head_to_head.py`, `benchmark.py`, `workflow_benchmark.py`, `locomo.py`, `identity_eval.py`
- db: `connection.py`, `schema.sql` (+ `lazy.py`, `growth_schema.sql`, `delegation_schema.sql`,
  `image_context_schema.sql`, `soulspec_schema.sql`)

Callsite verification outside the slice (to confirm reachability, not to review): `machina/core/context.py`
(event_seq producer + ordering), `machina/cli/__init__.py` + `machina/gateway/features/skills.py`
(SkillCurator reachability), `tests/test_r5f2_chunk_adjacency.py`,
`tests/test_head_to_head_grader_adversarial.py`, `skills/ecc/manifest.json`.

---

## Method

Two passes over each line: (1) a skill-driven review, (2) a manual read where each candidate
was attacked by trying to construct the counterexample or the guard that disproves it. Only
candidates that survived the falsification attempt are reported as findings. Candidates that
were disproved or are unreachable are recorded under **Questions** or **Explicit non-findings**.

---

## Confirmed findings (severity-ranked)

### P2-1 — `head_to_head.py`: the `success` verdict ignores the harness's own exit status and error
**File:** `machina/research/head_to_head.py:757-763`

```python
result["success"] = bool(
    grade_result["exit_code"] == 0
    and grade_result["passed"] > 0
    and result["tests_failed"] == 0
    and unchanged
    and not result.get("timed_out")
)
```

The verdict is derived only from the graded pytest run plus the fixture-integrity diff. It never
consults `harness_exit_code` (set at `:657` for opencode and `:710` for machina) nor
`result.get("error")` (set at `:729` when the harness raises). `grade()` runs unconditionally at
`:736`, even after the harness has errored.

**Trigger:** a fixture whose graded tests are already green (a "keep the suite green while doing X"
task, or a regression-guard task). The harness crashes before doing anything — `run_machina` raises
at `machina_create_session` (`:365-381`), `run_one` catches at `:728-730`, sets `result["error"]`
and `harness_exit_code != 0` — then grades the **unmodified** run copy:
`exit_code==0, passed>0, failed==0, unchanged=True, timed_out=False` → `success = True`. The
published head-to-head metric credits a harness that never ran.

**Falsification attempt:** checked whether `result["error"]` gates grading anywhere — it does not
(`:736` is unconditional). Checked whether `harness_exit_code` feeds `success` — it does not. Checked
whether the shipped adversarial controls shield it — they do not: every control's stub harness exits
0 (`tests/test_head_to_head_grader_adversarial.py:114-123`), so the crash path is untested. Checked
whether a failing pristine fixture shields it — it does not, but only because such a fixture fails
`tests_failed == 0`; the hole is real for any fixture that is green pre-run. Survives as P2.
**Suggested fix (not applied):** add `result.get("error") is None and result.get("harness_exit_code") == 0`
to the conjunction.

---

### P3-1 — `chunker.py`: recursion still drops a leading separator → single-character coverage hole (no fabrication)
**File:** `machina/rag/chunker.py:342-345, 380-404, 391-401, 427-433`

`_split_recursive` splits on `sep` and filters empty content spans (`:345`). When a `sep`-delimited
part itself *begins* with a character that is a separator of the next level (e.g. splitting on
`"\n\n"` yields a part beginning with a lone `"\n"`), the inner recursion treats that character as a
separator and drops it (the first inner span is empty and filtered at `:345`). The bridge at
`:391-401` only extends the **last** sub-chunk forward across the gap to the following part; it never
stretches the **first** sub-chunk backward over the dropped leading separator.

**Trigger:** `"aaa\n\n\n" + "W"*3000`, default separators (chunk_size 512 → 2048-char cap).
`_separator_spans(text, "\n\n")` → `[(0,3),(5,3006)]`. `i=0` accumulates `(0,3)`; at `i=1` the
candidate `text[0:3006]` is too large, so `text[0:5]` = `"aaa\n\n"` is emitted (ends at index 5),
then `text[5:3006]` = `"\nW…"` recurses on `"\n"` and its first emission starts at index 6 —
**index 5 (the `"\n"`) is emitted by no chunk.**

**Impact:** coverage only. The adjacency guard at `:516` (`prev.end != cur.start`) withholds overlap
across the hole, so no fabricated span is produced — this is the residual of R5-F2, not a regression.
The R5-F2 per-chunk slice invariant (`source[chunk.start:chunk.end] == chunk.text`) still holds; only
the union-of-chunks-covers-source property fails, and no test asserts that property.

**Falsification attempt:** confirmed `sep.join(text.split(sep)) == text` for the outer level, so the
drop is at the inner level; confirmed the bridge is forward-only and cannot cover it; rejected the
argument that a part never starts with a separator — `"a\n\n\nb".split("\n\n") == ["a", "\nb"]`.
Checked `tests/test_r5f2_chunk_adjacency.py`: it asserts substring + span + monotonicity, never full
coverage. Survives as P3.
**Suggested fix (not applied):** when recursing a part, also extend `sub_chunks[0]` backward over
`text[part_start : first_sub.start]` (mirror of the forward bridge).

---

### P3-2 — skill enable/update path bypasses the install manifest (ownership desync)
**Files:** `machina/skills/registry.py:855-894, 896-944, 946-966`; `machina/skills/repo_install.py:932-950, 977-991`

`set_enabled` and `update_skill` guard only `source_type == "builtin-ecc"` (`:913-914`, `:955-956`)
and then rewrite `SKILL.md` in place via `_write_skill_file` (`:882-888`). A repo-installed skill is
copied verbatim from staging (`repo_install.py:1007-1020`); its frontmatter normally carries no
`source_type`, so `SkillParser.parse` sets `source_type="local"` (`registry.py:420`). Such a skill is
therefore writable by this path.

`_build_skill`/`_verify_installed` treat the manifest's recorded `sha256` for `SKILL.md` as the
ownership contract: a reinstall of the same revision re-hashes and, on divergence, raises
`"installed skill '…' diverged from its manifest; remove it first"` (`repo_install.py:977-988`).

**Trigger (reachable):** `SkillCurator.archive_stale` (`:1009-1024`) calls `set_enabled(skill.name, False)`
for any non-`builtin-ecc` skill idle past the cutoff — including a repo-installed skill. It is invoked
from the CLI `/skills archive` path (`machina/cli/__init__.py:998`). The rewrite re-serializes via
`yaml.dump(..., sort_keys=True)` and re-joins `---\n{yaml}---\n{content}\n`, so the bytes differ from
the manifest digest; the next `repo_activate` of that same revision refuses as "diverged".

**Falsification attempt:** checked whether the rewrite is byte-identical when only `enabled` flips — it
is not. Checked whether a no-frontmatter installed skill changes on rewrite — it becomes a fully
rewritten file. Checked for a manifest-aware branch in `update_skill`/`set_enabled` — there is none;
only `delete_skill` consults `remove_installed_skill` (`:984-987`). Survives.
**Note:** `delete_skill` (`:968-994`) *is* manifest-aware, so only the enable/update paths are affected.

---

### P3-3 — `identity_eval.py`: an undefined rejection rate is reported as `0.0`
**File:** `machina/research/identity_eval.py:119-120`

```python
attack_rejection_rate=rejected_attacks / attacks if attacks else 0.0,
benign_acceptance_rate=accepted_benign / benign if benign else 0.0,
```

When a scenario file contains no injected transitions (`attacks == 0`), the attack-rejection rate is
reported as `0.0` — indistinguishable from "every attack was accepted". A clean run and a fully-failed
run print the same number.

**Trigger:** `--test-db` with a scenario array whose `injected` flags are all `false` (valid per
`load_identity_scenarios`, `:42-49`). Output `attack_rejection_rate: 0.0`.

**Falsification attempt:** checked whether `attacks` is guaranteed non-zero by validation —
`load_identity_scenarios` requires non-empty `steps` but never requires an injected step. Checked
whether the metric is documented as `0` when N/A — it is not. Survives (P3; benchmark honesty).
**Suggested fix (not applied):** report `None`/omit and emit a counter when `attacks == 0`, matching
the `SKIPPED` idiom used elsewhere under `research/`.

---

### P3-4 — `repo_install.py`: a plan-less (crashed) staging dir misreports the digest-miss diagnostic
**File:** `machina/skills/repo_install.py:763-794`

`load_staged_plan` iterates staging entries; any directory lacking `plan.json` is appended to `corrupt`
(`:776-779`). If the requested digest is not found and `corrupt` is non-empty, the caller gets
`"staged entries unreadable (…); stage the repo link again"` (`:789-793`) rather than the ordinary
`"no staged plan matches that digest"`.

**Trigger:** `write_staged_plan` copies into `root/<key>.staging-<pid>` (`:713-719`) before `os.replace`
(`:753`). A `SIGKILL` inside the copy leaves that directory behind (the `except OSError` cleanup at
`:720-722` / `:756-758` does not catch `SIGKILL`, and `_prune_staging` runs only from
`write_staged_plan:759`). A later `repo_activate` for a digest that *is* staged still succeeds (the
valid match returns at `:787-788`), but activation of a digest that is not staged surfaces the
misleading "entries unreadable" error and the count of corrupt dirs.

**Falsification attempt:** checked whether `_prune_staging` runs on the read path — it does not.
Checked whether the corrupt dir self-cleans — only by TTL/count on a later `write_staged_plan`.
Checked whether a valid match is rejected — no, it returns first. So the defect is scoped to
digest-miss diagnostics. Survives (P3; honest-error/robustness).

---

### P3-5 — `tools/base.py`: `_infer_schema` collapses non-primitive/union annotations to `"string"`
**File:** `machina/tools/base.py:104-139`

`type_map` maps only bare `str/int/float/bool/list/dict` (`:107-114`). With `from __future__ import
annotations`, `get_type_hints` resolves annotations, but `list[str]`, `dict[str, Any]`, `tuple[...]`,
and `X | None` are not equal to the bare forms, so `type_map.get(annotation, "string")` returns
`"string"` (`:128`). `_validate_tool_args` then enforces `isinstance(value, str)` for that parameter
(`:229-242`).

**Trigger (latent):** any tool registered *without* `parameters=` whose signature uses such an
annotation. Enumerated every `@registry.register(` in `machina/tools/` (repo, rag, mcp, file, agents,
web_crawl, session_recall, repomap, memory, skills, harness, terminal, web_research, builtins): the
only inferred-schema tools are `mcp_describe(name: str)` and `skill_resource(skill_name: str,
path: str, offset: int, limit: int)` — all primitives. Every tool with an object/array parameter
declares `parameters=` explicitly.

**Falsification attempt:** sought a live tool whose inferred schema is wrong — none exists. Reported
at P3 because a future inferred tool with `list[str]` would be silently typed `"string"` and rejected
at call time.

---

### P3-6 — `skills/registry.py`: casefold lookups against non-casefold storage desync the collision guard
**File:** `machina/skills/registry.py:670-675, 679-680, 789, 811`

`load_all` probes `self._skills.get(skill.name.casefold())` (`:670`) but stores under the raw
`skill.name` (`:675`). `create_skill` likewise probes `self._skills.get(name.casefold())` (`:789`).
`get(name)` (`:680`) is an exact-key lookup. `SkillParser._validate_metadata` allows uppercase names
(`:316`), so the storage key is not lowercase in general.

**Trigger (reachable, low impact):** a learned skill whose frontmatter name is `Foo` and a curated
skill named `foo` in a second root. Learned loads first → key `Foo`; curated's casefold probe
`get("foo")` returns `None` (the existing key is `Foo`) → key `foo` is added too. The two coexist,
defeating the documented case-insensitive "later root replaces the earlier non-ECC entry" rule
(`:465-476`) and `create_skill`'s cross-root collision guard. `safe_name` lowercasing and the
`open("x")` exclusive-create guard catch same-directory collisions, which is why this is low impact.

**Falsification attempt:** checked whether the practical builtin shadow-protection breaks — it does
not, because all 42 ECC bundle names are lowercase (`skills/ecc/manifest.json`, verified) so
`casefold(local) == builtin_key` whenever a collision is possible. Checked whether disk guards make
the duplicate unreachable on case-insensitive filesystems — they do on macOS/Windows (`Foo`/`foo`
are one directory), so this is Linux/case-sensitive-FS specific. Survives as P3 (consistency/robustness).

---

## Questions (`Q`) — not confirmed defects

- **Q-1 `rag/embedder.py:317-326`** — `_zero_pad_rows` pads short rows but never truncates long ones.
  `dimensions` returns `max(pad_to, native)` (`:377-382`), so a runtime that emitted more dims than
  `native_dimensions` would exceed `vector(1536)` and the DB insert would fail loudly (not silently).
  Falsification: `NATIVE_DIMENSIONS`/`_native_width_from_model` are trusted constants; a mis-sized
  runtime is a packaging fault, not reachable from config alone.
- **Q-2 `research/benchmark.py:338-339`** — `sum(1 for _ in open(root / rel))` opens files without a
  context manager (bounded to `GOD_FILE_CEILINGS`). Cosmetic leak.
- **Q-3 `rag/search.py:371-396`** — `rrf_fuse` accumulates `rrf_score` and overwrites
  `dense_score`/`sparse_score` if the same `chunk.id` repeats within one branch. Falsification:
  `context_chunks.id` is the PK and each branch is a single `LIMIT`ed query, so duplicates are
  unreachable.
- **Q-4 `rag/pipeline.py:519-533`** — the embedding-space dimension gate hardcodes `1536`; a
  deployment that changes `EMBEDDING_DIMENSIONS`/schema would be rejected or under-checked.
  Pre-existing and config-dependent.
- **Q-5 `tools/skills.py:103-120`** — `skill_read` validates `offset < 0` only *after* the
  `query` branch slices `content[offset:]`. `_validate_tool_args` enforces type but not the schema
  `minimum`, so a direct call with a negative `offset` and a matching `query` can walk from a
  negative slice; the trailing `offset < 0` check still bounds the final read. Not a crash and not
  reachable from the advertised (minimum-0) schema in normal tool-calling; noted for completeness.
- **Q-6 `repomap/cache.py:50`** — `lookup` returns `ent["loc"]`/`ent["imports"]`/`ent["entry"]`
  without a `KeyError` guard; a hand-corrupted cache file (or a schema change) would raise instead of
  missing the cache. `save()` uses temp+`os.replace`, so a self-inflicted partial write is unlikely.
- **Q-7 `research/benchmark.py:297-301` + `_record:510-516`** — a `--locomo-db` production failure is
  nested as `production_archive_search.status="SKIPPED"` and never added to `failures`, so the
  benchmark exits 0 even though the explicitly requested DB comparison did not run. Honest `SKIPPED`
  reporting vs. "optional section" is the intended design; noted as a conscious choice.
- **Q-8 `repomap/scan.py:87`** — `assert isinstance(cache, ContentHashCache)` is stripped under
  `python -O`; it is a debug-only type guard (a wrong type still fails at `cache.lookup`). The only
  `assert` in the slice (see `-O` note below), harmless when stripped.

---

## Explicit non-findings (fixes verified; areas the brief flagged)

- **Chunker R5-F2 core (offsets/adjacency):** `_separator_spans` literal path is exact
  (`sep.join(text.split(sep)) == text`, `:427-433`); the regex path is runs-between-matches with the
  non-capturing assumption documented (`:435-441`, and `is_separator_regex` defaults `False` with no
  production caller). Recursive emissions are literal slices (`:367-377, 406-416`); the `_apply_overlap`
  guard `prev.end != cur.start` (`:516`) can withhold but never fabricate. Hard-bound pass keeps offsets
  exact (`:111-132`); `_hard_split` advances under a positive bound (`:570-588`). `split_markdown`
  (`:147-212`) and `split_code` (`:214-310`) null per-section offsets before returning. Only residual: P3-1.
- **Atomic writes (`tools/file.py` R5-B):** `_open_parent_dir_fd` binds the opened dir's dev/ino to its
  `lstat` identity (`:269-308`); `_write_anchored_inside` probes the leaf with `O_NOFOLLOW` and no
  `O_TRUNC` (`:647-651`), verifies via `_verify_fd_at` (`:656-677`), refuses `nlink>1` (`:662-663`),
  closes before rename, then `_atomic_write_file_at` creates the temp via `openat`
  (`O_CREAT|O_EXCL|O_NOFOLLOW`, `:345-359`), fsyncs, `renameat` (`:383`) and fsyncs the dir (`:391`).
  `os.rename` (not `os.replace`) is safe because the anchored path only runs when `_dir_fd_supported()`
  (`:732`) — i.e. POSIX where `rename` overwrites; Windows falls back to the `os.replace` legacy path
  (`:782`). `_fsync_dir_best_effort` and `os.replace` give the platform fallbacks.
- **Worker join (HELD-44):** `_run_worker_joined` shields the worker, joins on cancellation, and
  re-raises `CancelledError` only after the worker's filesystem effects complete (`:437-466`); a second
  cancel loops rather than orphaning the thread, and the worker's own exception during the join is
  logged while cancellation is preserved (`:461-466`). Cancellation is never downgraded to `ToolError`
  (the outer `except Exception` at `:800` does not catch `CancelledError`).
- **Migration + ordering (HELD-55):** the producer allocates `nextval` for the batch and **sorts** the
  ordinals so record *i* gets ordinal *i* (`core/context.py:268-277`); the consumer tie-break is
  `created_at DESC, event_seq DESC NULLS LAST, id DESC` (`context.py:531,545,663,712`), matching
  `schema.sql:111-112` and `_CONTEXT_EVENT_ORDER_MIGRATION` (`connection.py:26-34`). Legacy NULL
  `event_seq` sorts with `NULLS LAST` and falls through to `id`. The runtime DDL is consistent with
  `schema.sql` and idempotent (`CREATE … IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` / `OWNED BY` /
  `SET DEFAULT`). Both `initialize_schema` and `initialize_runtime_schema` take the same advisory
  xact lock (`machina:runtime_migrations`), so the additive migration cannot race a fresh install.
- **025 removal:** no `max_iterations`/`maxIterations` reference remains in source; `schema.sql:283`
  and `soulspec_schema.sql:7` both `ALTER TABLE IF EXISTS agents DROP COLUMN IF EXISTS max_iterations`.
- **SQL correctness:** `pipeline.index_document` `INSERT … unnest($1..$8)` arg order matches the column
  list exactly (`:343-360`) and is serialized by an advisory xact lock keyed on session+source
  (`:372-375`); `search._dense_search`/`_bm25_search` bind `$1..$4` in order and order by cosine /
  `ts_rank` correctly (`search.py:228-289`); `_enforce_embedding_space` keyset-paginates with
  `$3::uuid IS NULL OR id > $3` and `ORDER BY id` (`pipeline.py:598-647`). The runtime schemas
  (`delegation_schema.sql`, `growth_schema.sql`, `image_context_schema.sql`, `soulspec_schema.sql`)
  are additive/idempotent and their backfills are guarded by `cardinality(...) = 0` predicates.
- **Research `-O` safety:** no correctness reliance on `assert` anywhere under `machina/research/`
  (grep clean); `workflow_benchmark.py:56-63` explicitly replaces asserts with raising checks precisely
  to survive `-O`. The only `assert` in the whole slice is `repomap/scan.py:87` (Q-8).
- **Skill validate-then-publish (R5-O):** `update_skill` validates every proposed field into locals,
  writes atomically, and only then mutates the live object (`registry.py:910-944`); `set_enabled`
  publishes to the object after the successful rewrite (`:952-966`); `_write_skill_file` is
  temp+fsync+`os.replace` with cleanup (`:855-894`). (The manifest desync in P3-2 is orthogonal to
  this ordering guarantee.)
- **Skill install ownership/symlink/fsync guards:** `_confined` rejects absolute/`..`/symlink-escaping
  members relative to the staged tree (`repo_install.py:132-141`); activation recomputes the plan
  digest from only the recorded files and refuses additions/removals/edits (`:865-889`); `_build_skill`
  re-validates every staged `SKILL.md` through `SkillParser` (`:992-1001`), writes into
  `.install-staging/…install-<pid>-<rand>` (matched by `_LEGACY_INSTALL_BUILD_RE` so the registry never
  scans it) and fsyncs file+dir (`:1005-1033`); `activate_plan` renames into place and rolls back
  committed trees on failure (`:896-929`); `remove_installed_skill` proves manifest ownership before
  `rmtree` (`:801-849`). Companion text files are injection-scanned fail-closed (`:355-359`).
- **head_to_head symlink gates:** `escaping_symlinks` walks `followlinks=False` and rejects any link
  whose `realpath` leaves the root, covering dangling/absolute/`..` chains (`:300-323`); it is called on
  the pristine fixture before copy and again on the run copy afterwards, deleting the copy on rejection
  (`:612-629`). `--runs-dir` inside `--task-dir` is refused (`main:816-817`); the graded pytest is
  pinned (`-c .h2h_pytest.ini`, `--noconftest`, `--override-ini=addopts=`, `-p no:cacheprovider`, with
  `PYTEST_ADDOPTS`/`PYTEST_PLUGINS` stripped) (`grade:556-580`); the grader config is written before
  the before-hash so agent edits to it fail integrity (`:284-297, :632-634`); `editable_paths` keeps
  test files, conftests, and pytest configs out of the edit surface (`:246-281`).
- **Source redaction (R5-Nb):** `index_document` redacts the whole doc before chunking (`:260-267`),
  per-chunk (`:276-279`), the stored/audited source via `_redact_source` =
  `redact_secrets → redact_url_credentials` fail-closed to `[REDACTION_FAILED]` (`:42-74`), and metadata
  recursively (`:77-99`); applied consistently at `:256, :304, :417`. `_redact_or_failed` fails closed
  rather than returning raw text.
- **repomap cache/evidence:** `ContentHashCache.lookup` re-hashes and compares `mtime`+`size`+`sha256`
  before serving (`cache.py:33-50`); `build_repomap` re-hashes on a cache hit (`scan.py:106-108`).
  `EvidenceMap` merges adjacent spans and honours end-exclusive bounds (`evidence.py:47-62`).
- **Tool schema contracts:** every `get_tool_definitions` entry carries an explicit
  `additionalProperties` (absent → `False` via `_declared_parameters`, `base.py:164-177`), and
  `_validate_tool_args` fails closed on unknown params and on `bool`-as-int (`:200-247`). No registered
  tool opts into `additionalProperties: true` (grep clean).

---

## Verdict

**No P1.** One **P2** grading hole (`head_to_head.py` success verdict ignores the harness exit/error)
and six **P3** items: the chunker leading-separator coverage residual (P3-1), the skill enable/update
manifest desync (P3-2), the identity-eval undefined rate reported as `0.0` (P3-3), the staging
corrupt-dir misreport (P3-4), the latent inferred-schema type collapse (P3-5), and the casefold-vs-raw
registry key inconsistency (P3-6). The recently-landed fixes called out in the brief — **F2 offsets,
atomic dir-fd writes, HELD-44 worker join, 025 removal, HELD-55 event_seq migration/ordering** —
verify clean against their callsites, with only the P3-1 residual noted. All findings are reachability-
argued; no runtime execution was performed, so each carries a falsification attempt rather than a
reproduced failure.
