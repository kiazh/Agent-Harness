# Final-phase review plan (2026-10-15)

Owner order: after every task is complete, run all code-review skills + logic
review + codebase-architecture review, with a full line-by-line check.

Status gate: launch only after the sequential stage-3 gates are green and the
code is pushed (so agents read a stable, committed tree).

## Method (same two-layer method as Round 4, adapted)

Layer 1 — skill-driven reviewers over the ROUND DIFF (cdc5232..HEAD):
- `adversarial-reviewer` — hostile pass over the whole diff; tries to break the
  fixes; must produce concrete counterexamples or explicit non-findings.
- `code-reviewer` — PR-grade review: SOLID, complexity, smells, test quality.
- `senior-security` (+ `security-pen-testing` slice) — auth/secrets/permission
  boundaries re-attacked: redaction corpus, store fencing, broker/policy,
  MCP authority, audit path.
- `senior-architect` — module boundaries, seam discipline, dead-surface check,
  the ratchet integrity, "test-only" seams.
Layer 2 — full line-by-line partition over the TREE (not just the diff), like
the Round-4 read (A1–A15/U1–U3/T1–T16/I1), but targeted per the review's own
"remaining incomplete areas" list plus every file touched by R4/R5:
- backend slices: core (agent/orchestrator/session/turns/turn_flow), providers,
  security, permissions, memory, connectors, rag/repomap, skills/soulspec,
  research, db, mcp_server, cli, observability, gateway+api.
- UI slices: app/transcript/events/tail/theme/motion + features/components.
- infra slice: install.sh, Dockerfiles, compose, CI, tools/, pyproject.

Rules: read-only; every finding must cite file:line and a falsification attempt;
no fixes inside the review; findings triaged into a ledger, then fixed in
lanes (same discipline as R5), evidence under
`docs/verification/proofs/final_review_2026-10-15/`.

## Deliverables
- `FINAL_REVIEW_2026-10-15.md` (findings ledger + refutations)
- Fix lanes for confirmed findings → commits → rerun affected suites
- Final gate rerun if anything changed → final push.
