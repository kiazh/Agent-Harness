# S1 — self-knowledge completeness (always-on capability brief)

- Repo: `/Users/shadow/machina`. This audit started at HEAD `3ef6d5b` with the
  2026-10-15 campaign **uncommitted in the tree** (files were the source of
  truth; `git log` alone was stale). The coordinating lanes committed the
  campaign while this lane ran (HEAD moved to `923b946`); all HEAD-verified
  facts were re-checked against the working tree afterwards, and the lane's
  own files remain uncommitted per instruction.
- Lane S1 sole-writer scope: `machina/core/self_knowledge.py`,
  `tests/test_self_knowledge.py`, new `tests/test_self_knowledge_completeness.py`,
  this evidence file. Nothing else was modified. No commits.
- Environment for every pytest run:
  `MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test'`
  and `-p no:cacheprovider`.

## Mission

The agent should be *one with the harness*: know every user-facing feature, what
it does, how to inspect it, and how to **change** it (config keys, CLI, TUI,
tools); know it may edit/configure the harness itself when the user asks
(within the permission model); and ask the user for choices that belong to
them. The always-on brief is `FEATURES` + `configuration()` in
`machina/core/self_knowledge.py`, injected by
`capability_context()` → `core/prompt.py::build_capability_section` →
`ReActAgent._prepare_context` on every model call (all transports that use the
agent loop), and also embedded in the `harness_status` tool payload
(`machina/tools/harness.py`).

## Surface inventory (verified by reading the code, not docs)

| Surface | Inventory at HEAD | Source verified |
| --- | --- | --- |
| TUI slash commands | **42** top-level commands extracted from `ui/src/features/*.ts` (`name:` + `description:` pairs) | regex extraction + `/help` text |
| Themes | **41** skins (`SKINS` keys; quoted hyphenated names included) | `ui/src/design/theme.ts` |
| Skills | **42** curated ECC (pinned by `tests/test_ecc_skills.py`) + **34** first-party = 76 enabled | `ECCBundle().skills()`, `SkillRegistry(skills/)` |
| Subagents | **71** bundled ECC roles + 3 built-ins (`harness`, `researcher`, `coder`) | `machina/bundled_agents/ecc/manifest.json`, `BUILTIN_AGENTS` |
| Tools | **31** registered names (`registry.list_tools()`) | `machina/tools/*.py` |
| CLI | **36** commands + **5** groups (`auth`, `growth`, `harnesses`, `interop`, `providers`) | `machina.cli.app` introspection |
| Config keys | `DEFAULTS` (non-secret) + `SECRET_KEYS`; recent additions `firecrawl_autostart/firecrawl_stop_on_exit/firecrawl_home` | `machina/core/config.py` |
| Modes | `ask|workspace|sandbox|full` (`MODES`) | `machina/core/session_mode.py` |

Campaign cross-checks: harnesses = list/register/unregister/run with literal
prompt delivery and `interop` alias (`harnesses_feature_2026-10-15/H1`); skills
catalog incl. first-party + ECC + learn/curator/hub (`extensions_2026-10-15/`);
Firecrawl/SearXNG lifecycle `~/.machina/logs/firecrawl-lifecycle.log`
(`holistic_fixes_2026-10-15/F`); model catalogue surfaces (`/models`,
`/default-model`, `/provider`, `/effort`, `machina providers`); memory
retrieval modes (`remember/recall`, `session_recall`, `search_documents`);
usage budgets (`usage_*`); permissions (`/mode`, `/approvals`).

## Audit table — feature → brief status before → accuracy → change surface → entry after

"Pres." = named in FEATURES before this lane. "Accurate?" = claims matched HEAD
after reading the code. Every "change surface" below was grepped in the code.

| Feature | Pres. | Old brief accuracy | How to inspect / change at HEAD | Entry (after) |
| --- | --- | --- | --- | --- |
| Persona / SoulSpec | yes | accurate | `/persona`, `persona_description`, `persona_memory_enabled`, `persona_default_emotion`; `machina growth export-soul/import-soul` | `persona` |
| Growth (notes/lessons) | yes | accurate | `/growth`, `learn_personality`; `machina growth show/teach/feedback/forget/rollback` | `growth` |
| RSI | yes | accurate (kept honesty caveats) | `/growth`(revisions/evidence); `rsi_enabled` | `rsi` |
| Memory + approvals + profiles | partial | RAG sentence generalized; pending queue and profiles missing | `/memory list\|search\|add\|forget\|share\|pending\|approve\|reject\|stats`; `machina memory-*`; `/profile`, `/profiles`, `machina user-profile*`; `memory_approval_enabled` | `memory` |
| Sessions | **no** | controls named `/sessions` without lifecycle or CLI mirror | `/sessions /resume /new /search /recall /rename /goal /fork /delete /export`; `machina sessions\|sessions-search\|export\|fork\|delete` | `sessions` (new) |
| Context / compaction | **no** | not covered | `/context`, `/compress`, `machina compress\|context`; `compression_enabled`, `auto_compaction_enabled`, `compression_threshold`, legacy `context_budget` | `context` (new) |
| Web research + Firecrawl stack | partial | only `web_research`; `web_search`, `web_crawl` and the new lifecycle keys missing | tools `web_search/web_research/web_crawl`; `firecrawl_autostart/firecrawl_stop_on_exit/firecrawl_home`; log path | `web` |
| Skills management | partial | "activate automatically" overstated; no management surfaces | `/skills list\|show\|learn\|proposals\|approve\|reject\|revert\|delete\|curator`; `machina learn\|hub\|curator`; learned skills publish for review | `skills` |
| Subagents | **no** | execution entry only implied delegation | `/agents list\|show\|save\|history\|delete`, `/delegate`; 71 ECC + 3 built-ins | `agents` (new) |
| Jobs | partial | only named as command | `/jobs list\|add\|script\|heartbeat\|cron\|on\|off\|delete`; consent pause semantics | `jobs` (new) |
| Usage budgets | partial | mentioned as "supported" | `machina usage`, `/usage`; `usage_session_token_limit`, `usage_session_request_limit`, `usage_agent_token_limit`, `usage_agent_request_limit` | `usage` (new) |
| Models/providers/keys | partial | only `/models`, `/provider` names | `/models`, `/default-model`, `/provider`, `/effort`; `machina providers list\|add\|remove\|models`; `/keys` (.env 0600) | `models` (new) |
| Subscription auth | **no** | not covered | `/auth`; `machina auth chatgpt\|copilot\|status\|logout` (PKCE, device flow) | `auth` (new) |
| Themes | **no** | not covered | `/theme` (41 skins), `theme` config key | `theme` (new) |
| Modes / approvals | partial | `/mode` only | `/mode`, `/approvals list\|allow\|deny\|revoke`, `machina mode`; session-scoped `full` | `modes` (new) |
| TUI meta controls | stale list | `/theme /harnesses /skills /keys /auth /agents /delegate /context /compress /memory /approvals /output /init /review` missing; `/profile`, `/sessions` mismatched family | `/help`, `/config` (`--save`), `/status`, `/init`, `/review` | `controls` (rewritten; `clear`, `exit`, `output` allowlisted) |
| Self-modification stance | **no** | no statement existed | `machina config-set`; TUI config commands; permission-gated file edits | `self_change` (new) |
| Install-from-repo | yes | accurate (pinned by tests) | `repo_stage`/`repo_activate`; no test/installer/hook execution; MCP drafts `/mcp` | `install` (kept) |
| Permissions/execution | partial | "Offered tools define access" kept; approval flow added | `/mode`, `/approvals`, `share_memory` | `execution` (extended) |
| MCP | yes | accurate (OAuth/PKCE, exact approval) | `/mcp`, `mcp_connect/disconnect/list/describe/call`, trusted-read | `mcp` (kept) |
| Harnesses (+alias) | yes (H1) | accurate; `/interop` alias not named | `machina harnesses status\|enable\|disable\|setup\|run`, `/harnesses`, `/interop`, `~/.machina/interop.json` | `harnesses` (kept, +`/interop`) |
| Images | yes | accurate | clipboard/drop markers `[image 1]`; vision-capable model required | `images` (kept) |

TUI coverage result (enforced by the new test): all **42** commands are either
named in FEATURES (39) or allowlisted with a reason (3: `/clear`, `/exit` —
terminal housekeeping; `/output` — read-only viewer of already-retained tool
output). Config keys named in the brief all exist in `DEFAULTS`/`SECRET_KEYS`;
every snake_case token is a config key or a registered tool (verified by test).
CLI references are word-verified against the Typer app (not hardcoded).

## What changed

- **12 → 22 entries.** New: `sessions`, `context`, `agents`, `jobs`, `usage`,
  `models`, `auth`, `theme`, `modes`, `self_change`. Rewritten/extended:
  `memory`, `web`, `skills`, `controls`, `execution`, `rsi` (wording),
  `harnesses` (added `/interop` alias), plus identity/typo trims elsewhere.
  Kept verbatim (pinned or already accurate): `persona`, `growth`, `install`,
  `mcp`, `images`.
- **Stale claims fixed:** `controls` no longer omits the campaign surfaces;
  `skills` no longer says workflows "activate automatically" as the whole
  story; `execution` no longer lists "jobs, sessions, compaction and usage
  budgets are supported" without the surfaces that manage them.
- **Self-modification stance** (`self_change`): the agent *may* change this
  installation when the user asks — through `machina config-set` (non-secret
  settings only, secrets stay in `/keys`/`.env`), the TUI config commands, or
  permission-gated file edits — while autonomous RSI still never rewrites
  model weights, code or permissions; user-owned choices (model, provider,
  approvals) are **asked, not guessed**. This mirrors the existing honesty
  constraints instead of granting new autonomy.
- **Budget:** brief measured **1493–1496 tokens** (was 791; test bound raised
  850 → 1600 with the S1 justification in `test_self_knowledge.py`). The
  status payload constraint forced a measured compaction pass: `FEATURES`
  JSON is 6364 bytes; with the full `harness_status` skeleton the normal
  payload is **7895 B** (notes retained) and the adversarial 400-skill /
  25-source ladder case is **8333 B** (cap 8500) — the truncation ladder still
  has headroom, and the brief is documented as "trim before adding".

## New completeness test — `tests/test_self_knowledge_completeness.py`

Structural invariants only (no prose snapshots); reasons:

1. **Every snake_case token in FEATURES is a real config key or registered
   tool.** Extracted with `\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b` and checked
   against `DEFAULTS ∪ SECRET_KEYS ∪ registry.list_tools()`. Catches invented
   identifiers and renamed keys; does not care about wording.
2. **Every backticked `machina <command>` reference exists** in
   `machina.cli.app` (commands + groups introspected at test time). This is
   the CLI analogue and cannot rot with renames.
3. **Every TUI feature command is named or allowlisted with a reason, and the
   reverse.** The test extracts `name:` + `description:` pairs from
   `ui/src/features/*.ts`, asserts a canary set plus the full expected
   registry, then requires each extracted command to appear as `/<name>` in
   FEATURES or in `TUI_ALLOWLIST` (three entries, each with a reason); it also
   fails on any `/command` mentioned in the brief that the UI does not
   register. A UI refactor that changes the regex shape fails loudly on the
   canary assert.
4. **The self-modification stance is pinned** (may change / permissions /
   asked-not-guessed) so a future edit cannot silently drop it.
5. **Entries are single-line strings** (the brief joins facts with `\n`; a
   multi-line value would corrupt the block).

The invariants are deliberately about *taxonomy* (names exist, commands
exist, coverage), not about exact sentences, so future feature work updates
text freely as long as it stays real.

## Skills-informed pass (coordinator addendum)

Loaded the `senior-prompt-engineer` skill and read the repo playbooks
`skills/skills-system/SKILL.md`, `skills/cli-tui/SKILL.md`,
`skills/context-runtime/SKILL.md`, `skills/prompt-engineering/SKILL.md`,
`skills/docs-communication/SKILL.md`. Delta caused by the pass:

- **Prompt-optimizer lens → measured compaction.** The first complete draft
  was 1687 tokens / 7234 B of FEATURES JSON and made `harness_status` drop
  learning notes (and the 400-skill adversarial case refused). Treating the
  brief as a prompt under a hard consumer budget, I compacted to 6080 B
  values /6364 B JSON /1496 tokens while keeping every fact — instead of
  just raising bounds.
- **Progressive disclosure → surface-level entries.** Per the skills-system
  playbook (metadata always, content on trigger), entries name the feature,
  its inspection command and its change command; depth stays in
  `harness_status`, `/growth`, `/skills proposals`, etc. No full subcommand
  manuals inline.
- **cli-tui playbook → registry introspection tests.** Command validity is
  checked against the live Typer app and the tool registry rather than a
  hardcoded list (zero invented names; no brittleness on renames).
- **No false additions from playbooks.** Generic playbook examples that do
  not exist in Machina (e.g. a "Ctrl+P palette") were verified absent and
  deliberately not added — every named command/key is greppable in the code.

## Verification

| Command (abridged) | Result |
| --- | --- |
| `pytest tests/test_self_knowledge_completeness.py tests/test_self_knowledge.py tests/test_harnesses_core.py tests/test_harnesses_feature.py tests/test_interop.py tests/test_fm7_cli.py tests/test_godfile_extraction.py tests/test_repo_install.py tests/test_turn_intent.py tests/test_installer_env.py tests/test_v2_triage_hints.py tests/test_r4_090_tools.py` (final state) | **308 passed** |
| first focused pass (10 files, before the reverse-invariant test) | **270 passed** |
| `pytest tests/test_v2_triage_hints.py tests/test_r4_090_tools.py tests/test_self_knowledge_completeness.py tests/test_self_knowledge.py tests/test_godfile_extraction.py tests/test_harnesses_core.py tests/test_repo_install.py` | **125 passed** |
| Token bound | measured 1493–1496 ≤ 1600 (was 791 ≤ 850) |
| `harness_status` payloads | normal 7895 B, adversarial 8333 B (cap 8500; notes retained / ladder completes) |
| `ruff check` + `ruff format --check` on the three files | clean |
| Repo invariants re-checked after edits | 41 skins, 42 ECC skills, 71 ECC agents + 3 built-ins, 31 tools, 36 CLI commands + 5 groups, 42 TUI commands |

## Notes / non-goals

- `configuration()` was not extended (no model/provider/theme keys added to
  the always-on JSON): its docstring promises an allowlisted, bounded knob
  set, and arbitrary config strings must not enter the always-on prompt
  (existing leak tests). The brief instead names the inspection surfaces
  (`/status`, `/config`).
- All subcommand enumerations in the brief were verified against
  `ui/src/features/*.ts` and the CLI help; report/`shell`-level names were not
  added.
- No commits; only lane-owned files were touched.
