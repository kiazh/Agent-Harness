# Final adversarial review — round diff `cdc5232..HEAD` (2026-10-15)

Posture: hostile reviewer (Saboteur / Security Auditor). READ-ONLY. Scope is the
77-commit Round-4 + Round-5 fix campaign (`git diff cdc5232..HEAD`, 740 files).
Every finding cites `file:line`, a concrete counterexample, and whether the
attack was confirmed by reading the code path end-to-end (no runtime execution
of the product; a standalone regex-semantics check is the only executed step and
is labelled as such).

Priorities attacked: RED/GREEN fixes with the weakest-looking verification,
cancellation/ownership paths, lock/generation fences, redaction patterns, MCP
authority ceiling, atomic writes, the event-ordering ordinal, the SSE parser,
rate-limit binding, and the consolidation cursor serialization.

---

## Confirmed findings

### F1 — P2 — Embedded-`@` DB-URL passwords partially leak past the R5-N redactor

- **File:line:** `machina/core/provider/audit.py:58-62` (DB-scheme pattern),
  `:74` (`return redact_url_credentials(sanitized)`), `:99` (`_URL_USERINFO_RE`),
  `:127-133` (`_redact_userinfo_match`, guard at `:129`).
- **Fix under attack:** R5-N / R5-Nb (commit `bf8dd12`, `141675e`) — the new
  `redact_url_credentials` claims (docstring `:96-98`) *"password allows
  embedded `@` (greedy to the last `@` …) so embedded-`@` passwords are fully
  covered"*, wired as the last step of `_sanitize_value`.
- **Counterexample:** an audited/`document_source` string
  `postgres://alice:p@ss@db.example.com:5432/mydb`.
  1. The pre-existing DB-scheme pattern (`:58-62`, `([^@]+)@` — not greedy)
     consumes only up to the **first** `@`, producing
     `postgres://alice:[REDACTED_DB_PASSWORD]@ss@db.example.com:5432/mydb`.
  2. `redact_url_credentials` then matches `postgres://alice` `:` `[REDACTED_DB_PASSWORD]@ss` `@`,
     but `_redact_userinfo_match` (`:129`) sees the captured password
     *starts with* `[REDACTED` and returns the match **verbatim** — so the
     tail `ss` (and, for `postgresql://u:se@cr@et@host/db`, `cr@et`) survives.
- **Verdict:** **confirmed** (read end-to-end + standalone regex-semantics
  check; the DB pattern runs before the userinfo pass in `_sanitize_value`, and
  the `[REDACTED` guard suppresses the second pass).
- **Why the tests miss it:** `tests/test_r5_018_audit_redaction.py:72` and
  `tests/test_redaction_adversarial.py:122` only use single-`@` DB passwords.
  The generic-scheme (`https://`) embedded-`@` case *is* fully redacted (the
  userinfo regex is greedy) — only the DB-scheme flavour (which the earlier
  non-greedy pattern pre-touches) is broken.
- **Impact:** partial credential leakage into `audit_events` rows / RAG
  `document_source` / log lines whenever a DSN with an unencoded `@` in the
  password reaches a sanitized string. Requires an unencoded `@` (invalid but
  seen in hand-written configs and error text), so not P1.

### F2 — P2 — HELD-55 event ordinal can make consolidation **skip** context chunks

- **File:line:** `machina/core/context.py:625`
  (`get_chunks_since` `ORDER BY … event_seq ASC NULLS LAST, id ASC`),
  `machina/memory/consolidator.py:279-284` (cursor path uses `get_chunks_since`
  with `limit=MAX_CHUNKS_PER_CONSOLIDATION`), `:288-302` (cursorless path, same
  reader), `:310-321` (`_checkpoint_for` = **max `(created_at, id)`** over the
  page), `:73` (`MAX_CHUNKS_PER_CONSOLIDATION = 100`).
- **Fix under attack:** HELD-55 (commit `97c6197`) — a durable `event_seq`
  tie-break added to every reader. The evidence doc claims
  (`HELD-55_history_order.md:113-114`): *"adding `event_seq` to the `ORDER BY`
  cannot skip or duplicate rows (the cursor is not the last-by-sort row)"*.
- **Counterexample:** a session with **>100** pending chunks (consolidation
  backlog — consolidation runs best-effort per turn and can be disabled/late).
  Page = the 100 smallest rows by `(created_at, event_seq, id)`. Give one
  `created_at` batch rows with random UUIDs, e.g. page contains
  `(seq=1, id=ffff…ffff)` and `(seq=2, id=0000…0001)`, while `(seq=3, id=1111…1111)`
  is deferred. `_checkpoint_for` picks the **max `(created_at, id)` = ffff…ffff**.
  The next call's `WHERE (created_at, id) > (since_time, since_id)` therefore
  excludes `1111…1111` — which was never processed — so the row is **silently and
  permanently skipped** (the cursor is durable).
- **Verdict:** **confirmed** (read end-to-end). The page is a prefix in the
  `(created_at, event_seq, id)` order but **not** in the `(created_at, id)` order
  the cursor uses; the checkpoint as max-`(created_at,id)`-over-the-page is only
  a correct keyset bound when the two orders agree (true before HELD-55, false
  after). No duplicates are possible; the doc's "cannot skip" claim is the part
  that is wrong.
- **Why the tests miss it:** `tests/test_held_55_history_order.py` asserts
  *ordering* across readers on ≤8-row batches; it never crosses a page boundary
  (`limit=100`) inside a same-`created_at` batch, so the cursor/order mismatch is
  not exercised.
- **Impact:** memory consolidation silently drops some events from long
  backlogs. Chunks remain in `context_chunks` (prompt context is unaffected), so
  the blast radius is extraction completeness, not conversation integrity —
  hence P2.

---

## Lower-confidence / hardening observations

### F3 — P3 — `_write_skill_file` writes its temp via a predictable name that follows symlinks

- **File:line:** `machina/skills/registry.py:883`
  (`temp_file = path.with_name(f"{path.name}.{os.getpid()}.tmp")`), `:885`
  (`temp_file.write_text(...)` — no `O_EXCL`, follows a symlink), `:888`
  (`os.replace`).
- **Counterexample:** a pre-planted symlink at `SKILL.md.<pid>.tmp` pointing
  outside the skills tree makes `write_text` write **through** the link; the
  subsequent `os.replace` renames the link itself over `SKILL.md`, leaving an
  escape. This is the exact class R4-044/R4-090 fixed elsewhere with
  `tempfile.mkstemp(O_EXCL)` (see `env_file.py` `_write_env_lines`,
  `tools/file.py` `_atomic_write_file_at`), but the new R5-O helper copied the
  older predictable-name idiom.
- **Verdict:** **uncertain** — the code path is real and new in the diff, but
  exploitability requires write access to the skills directory plus a correct
  PID guess, and the skills dir is not obviously agent-writable. Reported as
  hardening, not a confirmed vulnerability.

### F4 — P3 — credential query/fragment params named `key`/`*_key` are not redacted

- **File:line:** `machina/core/provider/audit.py:108-124`
  (`_CREDENTIAL_PARAM_SUFFIXES` omits bare `key`; `_is_credential_param`).
- **Counterexample:** `https://host/doc?key=abc123` survives
  `redact_url_credentials` (normalized `key` matches no suffix/neither exact
  name), while `?api_key=`/`?token=` do not. A large class of providers use
  `?key=`.
- **Verdict:** **uncertain** — intentional name-list scoping (adding bare `key`
  risks over-matching benign params); only a partial gap, and most `key=` values
  are also covered by the provider-prefixed/KIA patterns when they contain a
  recognizable token. Not a regression (pre-fix redacted nothing HERE).

---

## Refutations (fixes that survived the attack)

- **MCP authority ceiling — REFUTED (holds).**
  `machina/mcp_server/__init__.py:94-122`, `:524-543`. `_MCP_MAX_MODE` can only
  tighten (`_MODE_RANK[raw] > _MODE_RANK[operator]` → return operator), invalid
  values fail closed to `ask` with a warning, and `mode` is validated into
  `VALID_MODES` before the rank lookup, so no string coercion bypass exists
  (`" full "` → stripped/validated; `""`/non-str refused by
  `_validate_chat_args`). The refusal returns before DSN resolution or spawn.
  Modeless calls are unaffected (child falls back to the operator's own
  configured default), which is the disclosed operator choice.

- **MCP cancelled-child retirement — REFUTED (holds).**
  `machina/mcp_server/__init__.py:571-586`. `cancel_event.set()` is executed
  **before** the shielded join, and `core/child_process.py:319-333` polls
  `cancelled()` every 0.05 s and `kill_tree`s the group, so a second
  cancellation interrupting the join cannot strand the tree (the event is
  already set).

- **HELD-44 write-cancel join — REFUTED (holds).**
  `machina/tools/file.py:450-466`. `asyncio.ensure_future(to_thread(...))` +
  `shield` prevents detach; the `while not worker.done()` re-shield loop joins
  on repeated cancel; a worker error during the join is logged, never silently
  dropped. Gateway ownership retention on a resistant inner task is real:
  `machina/gateway/server.py:1205-1213` re-registers `inner` in `_turns` when
  the bounded grace expires, so a newer turn cannot claim the session before the
  write finishes.

- **R5-B atomic dir-fd anchoring — REFUTED (holds).**
  `machina/tools/file.py:269-394`, `:623-698`, `:732-736`. `_dir_fd_supported()`
  is **True on this platform** (verified: `os.open/rename/stat/unlink ∈
  os.supports_dir_fd`), so the anchored path is live. Temp create is
  `O_CREAT|O_EXCL|O_NOFOLLOW` + random name under the pinned `dir_fd`; the
  publish is `os.rename(..., src_dir_fd=, dst_dir_fd=)`, which replaces the leaf
  *name* and never follows a swapped symlink. The parent dev/ino is verified
  against its `lstat` identity (`:302-307`), closing the validate→use race on
  the directory itself. The disclosed residual (outside-base / non-`dir_fd`
  platforms, legacy `_atomic_write_file`) is accurate.

- **SSE parser — REFUTED (holds).**
  `machina/core/provider/sse.py:20-46`. Strips at most one space after the
  colon (spec-correct), treats a bare field as `""`, returns `None` otherwise;
  `direct.py:254-259`, `openrouter.py:423-426`/`:531-534`, `chatgpt.py:541-548`,
  `copilot.py:285-294`, `anthropic.py:420-430` all compare sentinels on
  `.strip()` and treat bare-`data` as JSON-decodable-fail, not a crash.

- **Rate-limit binding — REFUTED (holds).**
  `machina/core/provider/rate_limit.py:91-116`. Providers set only
  `_rate_limit_key`; `_rate_limiter` is a property that resolves the registry on
  every access, so `reload_rate_limiters` reaches constructed providers. MRO is
  safe (`LLMProvider` defines no `_rate_limiter`; grep-verified), the override
  setter is honored verbatim, and `reload_rate_limiters` validates the budget
  unconditionally.

- **R5-F1 consolidation cursor serialization — REFUTED (holds at the lock-scope
  level).** `machina/memory/consolidator.py:186-210`, `:229-257`,
  `machina/core/agent.py:1488-1496`. `cursor_reader` is evaluated inside
  `_consolidate_session_impl` under `pg_advisory_xact_lock`, and
  `checkpoint_writer` is awaited before the lock context exits; the lock runs on
  a dedicated non-pool connection, so read+extract+commit cannot interleave.
  (The *separate* cursor/order mismatch is F2.) The disclosed residual
  (un-serialized degraded path when no dedicated connection exists) is accurate.

- **R5-LM credential races — REFUTED (holds).**
  `machina/security/store_fence.py:88-134`, `:137-186`, `:223-244` and
  `machina/security/providers.py:363-384`, `:412-433`. Generation bump is
  durable-then-local and `fenced_logout` bumps before unlink; `remove_provider`
  and `add_provider` take the **same** `_store_lock` around store mutation +
  credential cleanup, with identical providers→env-file ordering (no new
  lock-order inversion).

- **R5-E installer path quoting — REFUTED (holds).**
  `install.sh:529-554`. `sh_dquote_escape` escapes `\` **first**, then `"`, `$`,
  `` ` `` (correct order for a double-quoted POSIX word); `fish_quote` escapes
  `\` then `'` before single-quoting; `ps_squote_escape` doubles `'` for a PS
  single-quoted string. Plain paths are byte-identical.

- **R4-021 embedding fail-closed — REFUTED (holds).**
  `machina/rag/embedder.py` validates `len(items) != len(texts)` before any cache
  write or return; `machina/memory/embeddings.py` rejects all-zero vectors with
  `record_failure`.

- **R5-O validate-then-publish — REFUTED (holds, modulo F3).**
  `machina/skills/registry.py:896-966`: every field is validated into locals and
  the live object is published only after `_write_skill_file` succeeds, so a
  late `ValueError`/`OSError` cannot leave memory/disk divergence.

- **HELD-55 ordinal *write* path / reader tie-break — REFUTED (holds).**
  `machina/core/context.py:268-277` sorts reserved `nextval` values ascending so
  the i-th chunk gets the i-th ordinal; readers add `event_seq` between
  `created_at` and `id` consistently (`:531`, `:545`, `:625`, `:663`, `:712`),
  and the transcript CTE carries the column with matching arity (`:644-663`).
  The defect is only the cursor/order interaction (F2), not the ordinal itself.

---

## Summary

Two confirmed defects survived the campaign: **F1** (R5-N/R5-Nb URL-credential
redaction leaves the tail of an embedded-`@` DB password), and **F2** (HELD-55's
`event_seq` ordering of `get_chunks_since` vs. the `(created_at, id)` checkpoint
can silently skip chunks in a >100-chunk consolidation backlog). Both are
incomplete-fix regressions with exact, deterministic counterexamples and clear
test gaps. The MCP authority ceiling, MCP/HELD-44 cancellation and ownership
paths, R5-B dir-fd atomic writes, the SSE parser, the rate-limit binding, the
R5-F1 lock scope, the credential-store generation fences, and the installer
quoting all **held** under attack. Two P3 hardening notes (F3 predictable skill
temp name; F4 bare-`key` params) are recorded as uncertain.

**Confirmed findings:** 2 (F1 P2, F2 P2). **Refutations:** 11. **Uncertain / hardening:** 2.
