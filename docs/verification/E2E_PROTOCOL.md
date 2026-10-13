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

```bash
# all journeys (j4 self-skips when the Firecrawl stack is down)
python tools/e2e_gate.py

# cheap real subset
python tools/e2e_gate.py --journeys j1,j5

# extra JSON report at a custom path
python tools/e2e_gate.py --journeys j1,j2,j3,j5 --json /tmp/e2e.json
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

Exit codes: `0` no failures (skips allowed), `1` at least one FAIL, `2` usage
or missing venv. A SKIP is missing evidence, not a pass — name it in the
change record.

Notes that keep the gate honest:

- Fixtures must be collision-proof: memory consolidation deduplicates content
  against stored rows, so j1 uses a unique pet name per run.
- Never widen an assertion to make it pass; fix the feature or record a
  failure with the diagnostic the gate printed.
- The gate exercises the current working tree. Record the git head and dirty
  status sha (the proof does this automatically).

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
