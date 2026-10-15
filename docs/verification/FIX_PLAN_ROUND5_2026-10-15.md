# Fix plan — Round 5 (from `CODEBASE_REVIEW_CURRENT_2026-10-14.md`, 2026-10-15)

Source review covered the tree at `b1794be` + uncommitted changes. Limits inherited
from that review: tests were excluded, concurrency interleavings were NOT reproduced,
"confirmed" = code path exists. **Therefore every P1/P2 lane below must reproduce
first**: build the adversarial/E2E proof, watch it fail for the named reason (RED),
then fix (GREEN). If the RED does not reproduce, the item is refuted — record it in
the lane evidence doc and stop, do not "fix" unproven code.

Global rules (same as Round 4): sole-writer file lanes; no commits from lanes
(coordinator commits); E2E/adversarial primary, mocks only at network/provider
boundaries; deliberate errors (never swallow); smallest safe change; behavior/API
changes called out; `set -o pipefail`; DB tests use self-managed `isolated_schema`
private schemas (G3 pattern) against `machina_test`; retry-once-in-isolation on
shared-DB contention flakes; evidence under
`docs/verification/proofs/codebase_review_round5_2026-10-15/`.

## Lane map (collision-checked 2026-10-15)

| Lane | Findings | Files owned | State |
|---|---|---|---|
| R5-A | 1 MCP mode escalation, 15 MCP child cancel, 2-policy-side (grant path) | mcp_server/__init__.py, permissions/policy.py (grant path only) | LAUNCHED |
| R5-D | 8 session-mode indicator, 2-disclosure-side (mode copy + README + SECURITY) | ui/src/features/mode.ts, gateway/features/mode.py, ui/src/app.ts (mode paths), README.md, SECURITY.md | LAUNCHED |
| R5-B | 3 atomic-write dir escape (conditional) | machina/tools/file.py (atomic-write path) | LAUNCHED |
| R5-C | 7 scheduler job identity | machina/core/scheduler.py, machina/core/agent_factory.py | LAUNCHED |
| R5-E | 9 installer path quoting | install.sh | LAUNCHED |
| R5-F | 10 menu hit-testing | ui/src/components/Menu.ts, ui/src/features/sessions.ts | LAUNCHED |
| R5-GH | 11 ollama cache identity, 12 SSE space-tolerance (+centralize), 13 live rate-limit reload | core/provider/ollama.py, direct.py, anthropic.py, chatgpt.py, copilot.py, openrouter.py, rate_limit.py (+1 new shared SSE helper) | LAUNCHED |
| R5-J | 14 Host authority (ports/IPv6) | machina/security/fetch.py | LAUNCHED |
| R5-LM | 16 gh-import fence, 17 provider-removal credential race | machina/security/copilot_auth.py, machina/security/providers.py | LAUNCHED |
| R5-N | 18 RAG audit credential redaction | machina/rag/pipeline.py, machina/core/provider/audit.py, machina/observability/audit.py | LAUNCHED |
| R5-O | 19 skill-update atomicity | machina/skills/registry.py | LAUNCHED |
| R5-P | 20 benchmark symlink escape (conditional) | machina/research/head_to_head.py | LAUNCHED |
| R5-QR | 21 optional secret input, 22 memory ID lookup scope | ui/src/features/settings.ts, ui/src/components/Form.ts, ui/src/features/memory.ts | LAUNCHED |
| R5-S3 | follow-up 3 backfill starvation (latent) | machina/memory/store.py (backfill path only) | LAUNCHED |
| HELD-44 | 4 cancelled-write overwrite (needs agent.py turn-cancel side) | tools/file.py handle + core/agent.py | after R4-025 |
| HELD-55 | 5 history ordering (needs context.py + agent.py + schema migration) | core/context.py, core/agent.py, db/schema.sql + migration | after R4-025 + R4-096 |
| HELD-F1 | follow-up 1 consolidation cursor (needs agent.py) | core/agent.py, memory/consolidator.py | after R4-025 |
| HELD-F2 | follow-up 2 chunk adjacency (needs chunker.py) | machina/rag/chunker.py | after R4-096 |

Already covered elsewhere: finding 6 == PB-1 (R4-025 lane owns the fix + xfail flips).

## Subtasks

### R5-A · MCP authority + child lifecycle + grant path (P1s)
- [ ] A1 RED: MCP `machina_chat` with `mode="full"` from a non-entrusted caller → assert
  full grant is minted (proves finding 1). Fix: server-configured mode ceiling
  (`MCP_MAX_MODE` or config; default = operator mode, never above); tool-supplied mode
  clamped + audit-logged; `grant_full_default` unreachable from tool args.
  Proof: E2E over real stdio server — `full` request yields ≤ceiling; ceiling breach
  emits a typed refusal, never a grant.
- [ ] A2 RED: cancel an in-flight `machina_chat` → assert child process tree is retired
  and budget stops (proves 15). Fix: cancellation stop-handle (process group kill +
  join with bounded wait) before the handler reports stopped.
- [ ] A3 (finding 2, policy side): evaluate `_sensitive_capabilities` BEFORE the
  full-grant fast path, or keep the fast path and hand the exact disclosure text to
  R5-D. Do not change the disclosure copy in this lane (R5-D owns it).
- Watch-outs: existing MCP E2E (DSN passthrough from R4-081) must stay green;
  no behavior change for callers already at/under ceiling.

### R5-D · Mode indicator truth + consent disclosure (P2 + P1-disclosure)
- [ ] D1 RED: `/mode full` then bare `/mode` → reports `ask` (proves 8). Fix: pass the
  active session ID on status requests; refresh effective authority on session switch;
  footer renders governing authority, never the global default.
- [ ] D2 (finding 2, disclosure side): rewrite `mode.ts:56` copy + README:250 +
  SECURITY.md:45 to describe EXACTLY what a full grant authorizes (per R5-A's final
  grant-path semantics — read R5-A's evidence first; if R5-A gated sensitive caps,
  say so; if the fast path stands, disclose it). UI E2E proof for D1; prose verified
  against code for D2.

### R5-B · Atomic-write directory identity (P1, conditional)
- [ ] RED first with a real concurrent renamer: validate dir → rename subdir to symlink
  mid-write → assert content lands outside (proves 3) or REFUTE (record + stop).
  Fix: anchor traversal + tmp creation + replace to validated directory descriptors
  (`openat`/`O_DIRECTORY`, `linkat`/`renameat`); keep the `O_NOFOLLOW` leaf probe.
  Proof: adversarial FS (H-FS style) — racer active during N≥20 writes, 0 escapes.

### R5-C · Scheduler agent identity (P2)
- [ ] RED: job with restricted specialist on harness-owned session → assert harness
  definition executes (proves 7). Fix: honor `job.agent_name` within the session's
  authority restrictions, or reject incompatible job/session pairs at creation with a
  typed error (decide + document; prefer honor-with-restrictions). E2E proof with a
  real scheduler tick + session.

### R5-E · Installer quoting (P2)
- [ ] RED: install into a path containing `$`, backticks, quotes, backslash (and a
  PowerShell `O'Connor` profile dir) → assert shim executes wrong code (proves 9) or
  REFUTE. Fix: proper shell/PowerShell literal quoting (or data/code separation);
  test in containers if available, else fixture-level quoting round-trip + shellcheck.
  No behavior change for normal paths.

### R5-F · Menu physical-row mapping (P2)
- [ ] RED (fake-terminal H-UI): two-line session title → click second physical row →
  assert wrong session checked (proves 10). Fix: single physical-row→item map shared
  by layout, hit-testing, and viewport budgeting (or normalize labels to one line —
  decide, disclose). UI E2E proof incl. deletion-picker path.

### R5-GH · Provider transport correctness (P2s, one lane owns all provider files)
- [ ] G1 RED: same call, two ollama `base_url`s within TTL → assert second returns
  first server's response (proves 11). Fix: normalized endpoint in cache identity
  (mirror direct provider).
- [ ] G2 RED: SSE stream with `data:{...}`/`data:[DONE]` (no space) → assert events
  dropped (proves 12). Fix: ONE shared SSE field parser (tolerate missing space,
  assemble consistently), all six providers call it; delete the duplicated prefixes.
- [ ] G3 RED: change rate-limit config → assert live provider keeps old bucket
  (proves 13). Fix: stable shared bucket identities updated in place, or resolve
  the active bucket per request; wire runtime config changes to the refresh path
  (or delete `reload_rate_limiters` + document static config — decide).
- Proofs: boundary-mocked transports (providers are the allowed mock edge), real
  parser/bucket seams.

### R5-J · Host authority formatting (P2)
- [ ] RED: request to custom-port / IPv6-literal URL → assert `Host` is wrong
  (proves 14). Fix: Host from effective host/port with IPv6 brackets + default-port
  elision. Proof: unit + live-shape wire test at the fetch seam.

### R5-LM · Credential publication/removal races (P2s)
- [ ] L1 RED: `gh` import racing device login/logout → assert stale import wins or
  resurrects (proves 16). Fix: capture + revalidate generation under the publication
  lock (same fencing the device path uses); preserve intentional re-import.
- [ ] L2 RED: provider re-created between unlock and `.env` deletion → assert new
  credential deleted (proves 17). Fix: hold one serialization boundary across store
  + credential cleanup, or bind cleanup to the removed generation.
- Proofs: H-RACE style, real files + real flock, N≥5.

### R5-N · Audit-path credential redaction (P2)
- [ ] RED: index `https://alice:secret@example.invalid/doc` (and `?api_key=`) →
  assert userinfo/query credential persists in `audit_events` (proves 18). Fix: run
  the shared complete redactor over source metadata before emission AND persistence;
  explicit userinfo + credential-query handling. Proof: E2E index + audit-row read.

### R5-O · Skill-update atomicity (P2)
- [ ] RED: valid description + invalid content update → assert live skill mutated
  and/or failed write leaves proposed fields live (proves 19). Fix: validate owned
  snapshot → atomic write → publish; same for `set_enabled`. Proof: adversarial
  (invalid-field + injected write failure), live registry diffed before/after.

### R5-P · Benchmark symlink containment (P2, conditional)
- [ ] RED: fixture containing a symlink escaping to the original → assert harness
  edit lands outside the copy and the hash misses it (proves 20) or REFUTE.
  Fix: reject escaping links or materialize verified content. Proof: H-GRADE style
  fixture attack + genuine-fix still scores.

### R5-QR · UI affordances (P3s)
- [ ] Q1: SecretForm optional-key support (explicit keyless path; Escape stays cancel).
  UI test: blank submit proceeds on keyless-configured providers, rejected elsewhere.
- [ ] R1: full-UUID memory forget/share bypasses the newest-500 window (direct
  ownership-checked call); bounded prefix lookup only for abbreviations + disclosed
  scope. Same for learning approval/revert latest-100. UI + route E2E proof.

### R5-S3 · Backfill progress (latent, P3)
- [ ] No production caller exists — confirm first; if truly dead, DELETE the method
  + note it (do not "fix" dead code). If reachable, add progress cursor/failure
  marker. State the verdict with the caller grep in evidence.

### HELD lanes (launch when blockers clear)
- HELD-44: file-side joinable worker handle first (return/await the worker future on
  cancel paths in `tools/file.py`); agent.py turn-cancel wiring after R4-025.
- HELD-55: durable event ordinal (schema migration + writer assignment in event
  order + retrieval ordering), after R4-025 + R4-096.
- HELD-F1: cursor read/extract/checkpoint into one serialized op (or monotonic
  checkpoint compare), after R4-025. Reproduce the interleave adversarially first.
- HELD-F2: carry source offsets/separators through recursive emissions, after R4-096.

## Gates (after all lanes commit)
R4-stage-3 equivalent (backend serial, then UI alone) → R4-131-style ledger for THIS
review (per-finding: fixed / refuted / deferred) → review phase (adversarial-reviewer,
code-reviewer, senior-security, senior-architect, security-pen-testing + line-by-line
diff pass) → push.
