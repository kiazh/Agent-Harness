# End-to-end verification protocol

Status: active policy (2026-10-13). Applies to every feature, fix and "done"
claim made about this repository. Tooling: `tools/e2e_gate.py`. Recorded
evidence lives in `docs/verification/proofs/`.

## 1. Rule

A change is accepted only when an end-to-end journey proves its **observable
end state** on real components. "Observable end state" means rows in
PostgreSQL, files on disk, HTTP/API responses, indexed RAG chunks, tool
outcomes, or process exit codes — inspected after the real action ran.

- Unit tests are **regression pins for specific bugs**. They can pass on a
  mocked path, be rewritten around a bug, or be deleted with the feature. They
  are never acceptance evidence.
- A journey is: **setup → real action → assert observable end state →
  cleanup/revoke → record**. No journey, no acceptance.
- If the observable end state cannot be asserted (no stable surface yet), the
  change is not ready to accept; say so instead of substituting a proxy.

## 2. Proof rule (no proof, no "done")

Every claim that something "passes", "is fixed" or "works" must carry recorded
proof:

1. the **exact command** that was run;
2. the **observed output or queried values** (ids, row counts, substrings,
   status codes — never an interpretation of what happened); and
3. an **artifact path** where that evidence is stored.

The gate enforces this by writing
`docs/verification/proofs/<UTC date>-e2e-gate.json` and `.md` on every run,
with per-journey commands, timestamps, checks (expected vs observed) and
bounded observed values. Secrets never enter proofs: values are ids, counts,
substrings and short excerpts. Standalone proofs follow
`<UTC date>-<slug>.md` and must list their source revision and provenance.

## 3. Running the gate

The gate writes real sessions, chunks and memories, so it refuses a non-test
database unless `--db-url` names it explicitly — the flag is the consent. The
documented default `.env` database is `machina` (no "test"), so pass it (or
point `MACHINA_DATABASE_URL`/`DATABASE_URL` at a test-named DB and run flagless):

```bash
# all journeys (j4 self-skips when Firecrawl is down; j9 without opencode + registration)
python tools/e2e_gate.py --db-url postgresql://postgres:postgres@localhost:5432/machina

# cheap real subset
python tools/e2e_gate.py --db-url postgresql://postgres:postgres@localhost:5432/machina --journeys j1,j5

# extra JSON report at a custom path
python tools/e2e_gate.py --db-url postgresql://postgres:postgres@localhost:5432/machina --journeys j1,j2,j3,j5 --json /tmp/e2e.json
```

Journeys (real DeepSeek, real network, repo `.env` PostgreSQL, self-hosted
Firecrawl on `localhost:3002`):

| id | proves |
| --- | --- |
| j1 | chat turn → consolidation → `memories` row with a collision-proof fixture fact + advanced session cursor; full-mode grant revoked |
| j2 | live `web_research` indexes `context_chunks` rows for the docs.python.org json page |
| j3 | live `web_crawl` indexes chunks for the fetched page |
| j4 | Firecrawl MCP connector connect + broker-approved `execute_call` scrape returns `ok:true` and non-empty markdown (SKIP if unreachable) |
| j5 | `machina chat "Reply with exactly: OK" -q` exits 0 with no `InterfaceError` / `Memory extraction failed` on stderr |
| j6 | compaction triggers: real SSE turn + real stdio-gateway turn + explicit `machina compress` produce `context_archive` rows, a `compression_summary` chunk and `sessions.state.last_auto_compact_tokens`; document chunks survive compaction; an idle second turn compacts nothing (no-progress guard) |
| j7 | cross-transport turn parity: real HTTP SSE turn → real stdio-gateway turn → SSE turn on one session; `[DONE]` / `message.complete` terminal events, durable `user_message`/`assistant_message` rows, and claim release in both directions |
| j8 | provider facade: config-resolved `get_provider` + real `usage_store.complete_call` returns the fixture text and a durable `llm_usage` row (`status=complete`, known tokens); `machina status` exits 0 with a `Chat provider` line |
| j9 | Machina-as-MCP-server: `opencode run` drives the `machina_chat` shim tool, the reply token round-trips in the OpenCode transcript, and a fresh Machina session's `context_chunks` rows contain the token (SKIP unless `opencode` is installed and the project has a registered `machina` MCP server) |

Exit codes: `0` no failures (skips allowed), `1` at least one FAIL, `2` usage,
the database guard, or missing venv. A SKIP is missing evidence, not a pass —
name it in the change record.

Notes that keep the gate honest:

- Database guard: without `--db-url` the gate reads only the environment
  (`MACHINA_DATABASE_URL` then `DATABASE_URL`), never a config file, and exits
  `2` before any journey when that is unset, is not postgres, or names a
  non-test database. "Test-like" is a substring heuristic (`is_test_database`:
  the name need only contain "test"), so `latest`/`contest`/`attest` pass
  without `--db-url` — treat the flag as the real consent, not the name check.
  A blank `--db-url` value is treated as absent.
- Fixtures must be collision-proof: memory consolidation deduplicates content
  against stored rows, so j1 uses a unique pet name per run.
- Never widen an assertion to make it pass; fix the feature or record a
  failure with the diagnostic the gate printed.
- The gate exercises the current working tree. Record the git head and dirty
  status sha (the proof does this automatically).
- j6/j7 boot the real HTTP API (`python -m machina.api`) on an ephemeral
  loopback port with an ephemeral `MACHINA_API_KEY` generated per run, and
  drive a real `python -m machina.gateway` stdio child with an ephemeral
  `MACHINA_GATEWAY_TOKEN`. Ephemeral secrets never enter proofs: recorded
  commands show `<redacted>` placeholders only.
- j6 forces the post-turn compaction trigger with
  `MACHINA_COMPRESSION_THRESHOLD=0.001` on its own child processes so small
  fixture sessions exercise the trigger, archive and no-progress-guard paths.
  The threshold is provisioning, not a weakened assertion.
- j6/j7 SKIP with "HTTP stack unavailable (uvicorn missing in the venv)" when
  the venv cannot run the API server; that is missing evidence, not a pass.
  j8 SKIPs when the selected provider is `ollama` and the daemon is
  unreachable.
- j6/j7 own exactly one server/gateway child at a time and always kill the
  process group in `finally` (ephemeral ports only, readiness via
  `GET /ready`), so a FAIL cannot leak a process into the next journey.
- j9 requires both the `opencode` CLI and a project-registered `machina` MCP
  server (`opencode mcp add machina -- <venv-python> -m machina.mcp_server`;
  this writes the gitignored project `opencode.json`). Without both it SKIPs —
  missing evidence, not a pass; the shim's recorded round trip lives in
  `docs/verification/proofs/interop_2026-10-14/`.

## 4. Adversarial verification before merge

Before any merge, three hostile personas examine the change and its evidence
against each other. Each persona must produce at least one concrete challenge
or an explicit "no findings on this axis".

1. **False-Pass Auditor** — "This claim is green; where is the raw output?"
   For every "fixed/passes" statement, demand the command, the observed values
   and the artifact. Reject claims supported only by unit tests, mocks,
   previously cached results, or an assertion that would pass with the feature
   disabled.
2. **Environment Skeptic** — "Did this actually hit a real service?" Check
   that the journey used the real database/network/provider and that the
   asserted end state was produced by *this* revision, not a leftover row, a
   fallback path, or a manual step outside the recorded command. Check that
   failures and skips were reported as such.
3. **Regression Saboteur** — "If I broke the feature, would anything go red?"
   Identify the specific journey or check that would fail, and confirm it
   currently observes the real end state (not a tautology). If no journey
   covers the claimed behavior, that is the finding.

The review records, per persona, the challenges raised and how each was
resolved (fixed, documented, or accepted as a known gap). Unresolved
challenges block the "done" claim.

## 5. Merge checklist

- [ ] An E2E journey (new or existing) proves the observable end state.
- [ ] Proof recorded: command(s), observed values, artifact path.
- [ ] Unit tests, where added, are regression pins with a named bug or
      invariant — not acceptance evidence.
- [ ] Adversarial personas answered; unresolved challenges listed.
- [ ] Skips and known gaps named explicitly in the change record.

## Related tooling

`no-mistakes` (v1.84.0) was evaluated 2026-10-13 as an optional pre-push
review→test gate. Its OpenCode adapter requires an OpenCode 1.x binary; the
locally installed OpenCode 2.0.26 HTTP API is incompatible (`POST /session`
returns 405). With a 1.x binary it completed review→test→push headlessly via
`no-mistakes axi run` and produced step logs and evidence artifacts under
`~/.no-mistakes/`. See `docs/verification/proofs/2026-10-13-head-to-head-summary.md`
for the sibling harness comparison.
