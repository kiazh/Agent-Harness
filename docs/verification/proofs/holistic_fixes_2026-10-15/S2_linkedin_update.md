# S2 — LinkedIn update post (research notes + claim audit)

Date: 2026-10-15 · Lane: S2 (LinkedIn update draft, read-only on the repo)
Draft (outside the repo): `/Users/shadow/machina_linkedin_update_2026-10-15.md`

This lane produced **no repo changes** other than this evidence file. Every claim in the
draft's post was checked against code, docs or git history at `/Users/shadow/machina`
and the GitHub API on 2026-10-15. Style rules applied: post #1's voice (first person,
concrete, informal, learning-focused), no hype, no internal/security specifics, no test
counts.

## 1. Commit-count verification ("a couple hundred commits later")

| Check | Value | Source |
| --- | --- | --- |
| Local visible commits | **265** (`git rev-list --count HEAD`) | local clone, HEAD `3ef6d5b` |
| Local clone depth | shallow — `.git/shallow` = `00df258` (2026-10-12 20:38 ET) | `.git/shallow` |
| GitHub `main` total | **389 commits** | commits API `Link: rel="last"`, repo created 2026-10-01 |
| First / last commit | `7471ab8` "Initial commit" 2026-10-01 → `3ef6d5b` 2026-10-15 | GitHub commits API |
| Commits since 2026-10-13 00:00 ET | 264 | commits API `since=2026-10-13T04:00:00Z` |
| Commits since 2026-10-14 00:00 ET | 196 | commits API `since=2026-10-14T04:00:00Z` |
| Commits since 2026-10-15 00:00 ET | 65 | commits API `since=2026-10-15T04:00:00Z` |

Post #1's exact publish time is not recorded in the repo, so the draft says "a couple
hundred commits later" — supported by both the local 265 and the GitHub windows
(196–264 commits since the Oct 13–14 window). Not written: "exactly 200".

## 2. Verified beats used in the post (claim → evidence)

| Post claim | Verified from |
| --- | --- |
| Detects OpenCode / Codex / Hermes; registers itself with them; runs tasks (`machina harnesses run <platform> "<prompt>"`, TUI `/harnesses`) | `machina/interop.py` (`RUN_PLATFORMS` = `opencode run` / `codex exec` / `hermes -z`, `run_task()`), `machina/cli/interop.py`, `machina/gateway/features/interop.py` (`interop.list`/`interop.run`), `ui/src/features/harnesses.ts`; commits `7484236`, `0f70cce` (Oct 15); proofs `harnesses_feature_2026-10-15/H1_harnesses_core.md`, `H2_harnesses_ui.md` (real CLI syntax pinned: opencode v2.0.26 / codex-cli 0.162.1 / Hermes v0.21.6) |
| Machina itself speaks MCP (status / headless chat / session create) | commit `a92c1e8` (Oct 14) "MCP shim: Machina as a stdio MCP server"; `machina/mcp_server/`; README "Machina as an MCP server" (3 tools; approval pause returned as `paused_for_approval`) |
| ChatGPT subscription account + GitHub Copilot seat sign-ins; live model lists instead of hardcoded | commits `0c1a1a4` (Oct 14), `41f0f46` (Oct 14, later revised), `0cf846e` (Oct 15); proofs `chatgpt_auth_2026-10-14/`, `model_catalog_2026-10-14/README.md`, `extensions_2026-10-15/CAT_model_catalogues.md`; README provider table + "Removed subscription flows (2026-10-14)" |
| Skill library at 76 = 34 first-party + 42 curated ECC | dir counts: `skills/*/SKILL.md` = 34; `skills/ecc/skills` = 42; `skills/ecc/manifest.json`; commits `5ebec10` + `5bee364` (Oct 15); proof `extensions_2026-10-15/SKILLS_first_party.md` ("HEAD loads 76 skills … with 0 skips") |
| Learned skills go through a review/rollback queue; bounded; no model training | commit `8fbafa0` (Oct 14) "Auto-applied skill learning (always-on)"; README "Skills, delegation and installing more" (`/skills proposals` queue, `/skills revert`, precision auto-disable; creation-only + caps); docs/PERSONALITY_GROWTH.md boundary ("does not train model weights") |
| SearXNG search + Firecrawl stack; TUI lifecycle (opt-in) | commit `bfee7ed` (Oct 14); proofs `2026-10-13-web-crawl.md`, `2026-10-13-firecrawl-mcp-crawl.md`; `machina/core/firecrawl_lifecycle.py` + `machina/cli/launcher.py` wiring, proof `holistic_fixes_2026-10-15/F_firecrawl_lifecycle.md`; README rows `MACHINA_FIRECRAWL_AUTOSTART` (default false) / `_STOP_ON_EXIT` (default true). Note: `firecrawl_lifecycle.py` is currently untracked (working-tree change per the F proof) |
| 41 themes, contrast-verified | commit `9098f12` ("+12 diverse skins (29 -> 41), all AA-verified"); direct count `ui/src/design/theme.ts` `SKINS` = 41; proof `extensions_2026-10-15/THEMES_skins.md` |
| "Review rounds found real edge cases, reproduced and fixed" (generic) | proofs `codebase_review_round3/4/5_*`, `final_review_2026-10-15/` (F/S/C ledgers, RED-first repros), `holistic_fixes_2026-10-15/` (L1–L5, M1–M5); commits `no-mistakes(review)…`, `FR-*` batches (Oct 14–15). Deliberately no counts/details in the post |

Also mined but left out of the post for length (kept in the draft's REFERENCE list):
one-command installer (`install.sh`, commit `9806bee`), delegated-subagent live view
(`72af07d`/`9d8a3ed`, Oct 14), permissions modes + session-scoped full grant
(`c823097`, Oct 13), HTTP API/SSE surface, image attachments.

## 3. Skills pass (coordinator addendum)

- Harness skill list scanned: no writing/communication skill fits a personal technical
  update post (paper-writing and product-design skills don't apply); no harness skill
  was loaded to avoid irrelevant guidance.
- Loaded and applied the two named repo playbooks: `skills/docs-communication/SKILL.md`
  (clear structure, concrete specifics, README-style framing → post mirrors the
  how-to-describe-a-feature clarity) and `skills/research-writing/SKILL.md`
  (claim discipline: verify every figure/statement against sources → claim audit in
  section 2; nothing unverified entered the post).
- Revision deltas from the pass (draft written once, then revised): removed 4 backtick
  spans and all em dashes from the post body (post #1 uses plain text, no backticks or
  dashes); reordered the file to POST → REFERENCE → variant; trimmed "the addition I'm
  most into" to mirror post #1's "one part I found especially interesting" phrasing;
  added the "when enabled" qualifier to the web-stack bullet (autostart is opt-in);
  confirmed no test counts, no internal/security details, word counts in range
  (post 264 words, variant 108).

## 4. Files

- Draft: `/Users/shadow/machina_linkedin_update_2026-10-15.md` (POST + REFERENCE + <120-word variant)
- This evidence file: `docs/verification/proofs/holistic_fixes_2026-10-15/S2_linkedin_update.md`
