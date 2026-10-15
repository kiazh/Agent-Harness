# Skills adversarial review — resolution (2026-10-15)

Follow-up to `SKILLS_adversarial_review.md` (verdict CONCERNS). Every finding
below is closed or explicitly accepted; proof commands/logs in `logs/`.

| # | Finding | Resolution | Proof |
|---|---|---|---|
| W1 | No pin that the full first-party catalog loads with zero skips | Added `tests/test_fm6_scanner.py::test_first_party_catalog_loads_with_zero_skips` — every non-ECC `skills/<name>` must load. RED demo: temporarily re-adding the literal trip phrase to `prompt-engineering` fails the pin. | `logs/w1_red_skip_demo.log` |
| W2 | `retry` duplicate let `llm-provider` steal worker queries and mislabel the requested workflow | Removed the bare `retry` trigger from `skills/llm-provider/SKILL.md` (still reachable via `provider`/`openrouter`/`ollama`/`streaming`/`tool calling`/`token count`, and its description keeps "retry logic" as a scoring term). Added `tests/test_fm6_triggers.py::test_default_catalog_routes_worker_retry_to_background_jobs` covering both the worker lead and the provider-domain lead. | `logs/w2_red_misroute.log` (RED), `logs/w2_green_routing.log` (GREEN + workflow label) |
| W3 | Evidence doc stale after `5bee364`; ECC/OPENCODE skip notes unresolved | Correction block added to `SKILLS_first_party.md`; resolution notes in `docs/ECC_DEBLOAT_REVIEW.md` and `docs/OPENCODE_CONTINUATION.md`. | doc diffs |
| W4 | Telemetry "unchanged" claim does not reproduce | Corrected in the same block: `skills/telemetry.json` is untracked/gitignored runtime state, so `git diff` was vacuous; fresh suite runs mutate counters; no sync is required (the wheel excludes it; `check_release_artifacts` passes). No code change. | `SKILLS_first_party.md` correction |
| N2 | New `## Pitfalls` section in all 13 files | Accepted deliberately as a style extension; recorded in the correction block. | — |
| N5 | Ground-truth lines in three pre-existing skills | Fixed: `skills-system` (keyword/IDF matching, SKILL.md templates), `prompt-engineering` (no Jinja2), `cost-optimization` (`machina usage` wording). | skill diffs |
| N6 | "29 skins" in the dated final-review record | Annotated in place: "(all 29 skins at the time …; 41 skins after `9098f12`)". | doc diff |

Accepted residual (tracked, not fixed): trigger containments `cache` /
`rate limit` / `streaming` and `prompt ⊂ prompt injection` keep bounded top-2
co-injection behavior; no lead-steal beyond the fixed `retry` case was
demonstrated for them.

Verification: skills suite batch **291 passed** (`logs/review_fixes_suites.log`);
ruff check/format clean on the two changed test files; routing probes and the
workflow label in `logs/w2_green_routing.log`.
