# Proof — Firecrawl MCP crawl through `mcp_call` (self-hosted stack)

- **Date observed:** 2026-10-13, live run `Firecrawl crawl v3`
- **Status:** PASS (observed on the live dev checkout)
- **Provenance:** `docs/verification/COMPLETION_BUNDLE.md`, section
  "10. Post-program fixes (second pass, 2026-10-13)", bullets
  "MCP connector compatibility fixes (`4680eb7`, `0f1277a`)" and
  "Self-hosted Firecrawl (operator setup, not committed)".

## Commands / actions (as recorded)

- Machina agent ran `mcp_list → mcp_describe → mcp_call(firecrawl__firecrawl_crawl, limit=2)`.
- Toolchain: self-hosted Firecrawl on `:3002` (api + workers + playwright +
  redis + rabbitmq + nuq-postgres, keyless, `USE_DB_AUTHENTICATION=false`);
  Machina connector `firecrawl-mcp` over stdio with `FIRECRAWL_API_URL` via
  `env_allowlist` and a `trusted_read` allowlist for scrape/map/crawl/check.

## Observed end state (as recorded)

- `mcp_call` outcome: `ok: true, status: completed, completed: 2/2`
- 4 iterations, 4 tool calls in the run.
- Operator setup probe: live scrape of `example.com` returned markdown in 1.7 s.

## Session (operator run record, re-verified in dev DB 2026-10-13)

- Session id: `200c89bb-2815-4e0e-bae5-462b38a944a5`
  (title "Firecrawl crawl v3", agent `harness`, execution mode `ask`,
  created `2026-10-13 22:47:41 UTC`).
- The session's RAG holds 8 context chunks from the run; the external MCP
  result itself is not indexed as a `document_source` (unlike `web_crawl`).

## Where this is enforced today

`tools/e2e_gate.py --journeys j4` connects connector `firecrawl` in-process,
drives the broker-approved `execute_call` scrape of `https://example.com`
against the live stack, and asserts `ok:true` plus non-empty markdown. It
SKIPs (named) when `localhost:3002` is unreachable.
