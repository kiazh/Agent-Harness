# R4-GEMINI-REMOVE / R4-CLAUDE-REMOVE / R4-KIMI-ADD — vendor-compliance wave

Date: 2026-10-14. Wave: `R4-COMPLY` (single writer). No commit (per wave rule).
Task source: `docs/verification/FIX_PLAN_ROUND4_TASKS.md`, "Wave B additions"
(R4-GEMINI-REMOVE, R4-CLAUDE-REMOVE, R4-KIMI-ADD).

## 1. Decisions and rationale (primary sources)

- **Remove Gemini Code Assist OAuth (subscription license flow).** Google's
  Gemini CLI FAQ declares third-party OAuth reuse a direct ToS violation
  subject to account suspension; abuse detection is live and account/project
  suspensions for this pattern are documented upstream (discussion #22970,
  2026-10-11; #18999, #25648). The legitimate API-key path
  (`provider=google`, `GOOGLE_API_KEY`, model `gemini-2.0-flash`) is kept and
  untouched.
- **Remove Claude Pro/Max OAuth (Claude Code flow).** Anthropic's
  legal-and-compliance docs (2026-02-19) declare Free/Pro/Max OAuth tokens
  "not permitted" in any product other than Claude Code / native Anthropic
  apps, and forbid collecting/storing/intermediating Claude.ai tokens. The
  legitimate API-key path (`provider=anthropic`, `ANTHROPIC_API_KEY`) is kept
  and untouched.
- **Add Kimi Code (`provider=kimi`, API key).** Kimi Code docs (checked
  2026-10-14) state subscribers "can also obtain an API Key to integrate Kimi
  Code's model capabilities into third-party development tools and
  platforms", with first-party guides for Claude Code, OpenCode and Codex —
  officially sanctioned, key-based (no OAuth piggyback), so no new auth
  machinery was built: the existing OpenAI-compatible `DirectProvider`
  transport serves it.

Removed providers fall through to the existing generic typed error
(`ValidationError: Unknown provider: …`) — no removal-specific machinery.

## 2. RED-first evidence (unfixed tree)

New tests first, run against the pre-change tree (`git stash`-free; tests added
before any production edit):

```
$ MACHINA_TEST_DATABASE_URL=... .venv/bin/python -m pytest \
    tests/test_subscription_removals.py tests/test_kimi_provider.py -q -p no:cacheprovider
21 failed in 1.80s
```

Full log: `R4-GEMINI_CLAUDE_KIMI_logs/red_before_fix.log`. Failure mechanisms,
each exactly the named reason:

- `DID NOT RAISE ValidationError` for `get_provider("claude")` /
  `get_provider("gemini")` (both still routed).
- CLI: `"No such command 'claude'"` absent — `Usage: auth claude …` /
  `Usage: auth gemini …` rendered instead.
- Import probes: `machina.security.claude_auth is still importable`,
  `machina.security.gemini_auth is still importable`,
  `machina.core.provider.claude is still importable`,
  `machina.core.provider.gemini is still importable`.
- Facade still exports `ClaudeProvider` / `GeminiProvider`.
- Source census found the live references; Kimi tests failed with
  `KeyError: 'kimi'` / `KeyError: 'kimi_api_key'` / `Unknown provider: kimi`.

## 3. GREEN evidence (fixed tree)

```
$ MACHINA_TEST_DATABASE_URL=... .venv/bin/python -m pytest \
    tests/test_subscription_removals.py tests/test_kimi_provider.py -q -p no:cacheprovider
23 passed in 1.10s
```

Log with per-test PASSED lines: `R4-GEMINI_CLAUDE_KIMI_logs/green_after_fix.log`.

Focused affected suites (removals, Kimi, CLI auth, provider table/store/custom,
context windows, Anthropic tool results, the three import-surface guards,
gateway provider surfaces):

```
$ MACHINA_TEST_DATABASE_URL=... .venv/bin/python -m pytest \
    tests/test_subscription_removals.py tests/test_kimi_provider.py \
    tests/test_oauth_cli_auth.py tests/test_providers_direct.py \
    tests/test_provider_store.py tests/test_provider_custom.py \
    tests/test_context_windows.py tests/test_anthropic_tool_results.py \
    tests/test_architecture_layers.py tests/test_config_import_cycle.py \
    tests/test_leaf_independence.py tests/test_gateway_providers.py \
    tests/test_gateway_features.py -q -p no:cacheprovider
234 passed in 5.59s
```

Log: `R4-GEMINI_CLAUDE_KIMI_logs/focused_suites_green.log`.

Full backend suite (serial, `machina_test`): `backend_full_after.log` — see
section 7.

UI: `cd ui && npm run check` → exit 0, 436 tests (430 pass / 6 skip / 0 fail),
typecheck + eslint clean. Log: `R4-GEMINI_CLAUDE_KIMI_logs/ui_check_after.log`.

Ruff: `ruff format` (3 files reformatted, 15 unchanged) + `ruff check` → "All
checks passed!" on every touched Python file.

## 4. Removed surfaces (complete list)

Deleted:

- `machina/security/claude_auth.py`, `machina/security/gemini_auth.py`
  (token stores, refresh, PKCE/import flows).
- `machina/core/provider/claude.py`, `machina/core/provider/gemini.py`
  (subscription transports — the license/auth-file mode was their only mode;
  the API-key paths are `anthropic.py` / the `google` spec, untouched).
- Tests: `tests/test_claude_auth.py`, `tests/test_gemini_auth.py`,
  `tests/test_claude_provider.py`, `tests/test_gemini_provider.py`, and
  `tests/test_auth_store_adversarial.py` (see surprise S1).

Edited (removal):

- `core/provider/factory.py` — imports + `claude`/`gemini` branches.
- `core/provider/__init__.py` — facade exports.
- `core/provider/specs.py` — `PROVIDERS` entries + registry comment.
- `core/runtime.py` — readiness branches (chatgpt/copilot kept; the removed
  names now report `unknown provider '…'`).
- `permissions/policy.py` — credential-store module tuple.
- `security/store_fence.py` — docstring mentions (both sites).
- `security/providers.py` — `RESERVED_NAMES` (now + `kimi`).
- `cli/auth.py` — sub-apps, imports, help, module docstring; the
  claude/gemini-only helpers `_free_port`/`_wait_for_callback` were dead code
  after the removal and were deleted with them.
- `ui/src/features/settings.ts` — `/provider` argumentHint + completion rows.
- Tests pruned: `test_oauth_cli_auth.py` (claude/gemini sections and their
  socket/urllib helpers; copilot + runtime readiness kept and rewritten for
  copilot), `test_anthropic_tool_results.py` (the ClaudeProvider inheritance
  test; the AnthropicProvider coalescing proof stays), `test_providers_direct.py`
  (registry/spec lists), `test_provider_store.py` (reserved-name pin),
  `test_context_windows.py` (kimi window pins).
- `tests/test_architecture_layers.py` — removed the deleted files'
  `GOD_FILE_CEILINGS` rows; deferred-import cap 692 → 689 and per-file/total
  ceilings updated with a documented delta note (the guard explicitly requires
  a delta table for every move).

Proof calls: no `machina/` source references `claude_auth`, `gemini_auth`,
`ClaudeProvider`, `GeminiProvider`, `auth claude` or `auth gemini`
(`test_no_machina_source_mentions_a_removed_flow` + `grep`), and no code reads
`~/.machina/auth/{claude,gemini}.json`, `~/.claude/.credentials.json` or
`~/.gemini/oauth_creds.json` (`grep` clean; `gemini.json`/`oauth_creds` absent
from the package).

## 5. Kimi addition

- `core/provider/specs.py`:
  `ProviderSpec(name="kimi", base_url="https://api.kimi.ai/coding/v1",
  api_key_env="KIMI_API_KEY", default_model="kimi-for-coding",
  reasoning="openai", description=…)`.
  The Kimi Code model table documents a **top-level `reasoning_effort`**
  (`low`/`high`/`max`) for `k3`, `k3-256k` and `kimi-for-coding`, so the
  OpenAI-style knob is wired as-is; no value mapping was invented. The harness
  vocabulary is low/medium/high, so only `low`/`high` (or unset = model
  default) match the wire — documented in the README.
- `core/config.py`: `kimi_api_key` in `DEFAULTS`, `LEGACY_ENV_VARS`
  (`KIMI_API_KEY`), `SECRET_KEYS`, and the `Config` dataclass (without this
  registration the spec could never resolve its key; see surprise S2).
- `security/providers.py`: `RESERVED_NAMES` is hardcoded (not derived from
  specs) — it was pinned against `PROVIDER_SPECS` by
  `tests/test_provider_store.py`, which is now updated; `kimi` added.
- `core/context_windows.py`: `MODEL_CONTEXT_WINDOWS` entries — `k3-256k`
  262 144, `k3` 1 048 576, `kimi-for-coding-highspeed` 262 144,
  `kimi-for-coding` 1 048 576 (longest-key matching keeps each id on its own
  row; pinned in `tests/test_context_windows.py`).
- `.env.example`: commented `KIMI_API_KEY=` block (Kimi Code Console;
  membership supplies coding quota).
- `README.md`: provider table row + a "Kimi Code" setup/endpoint/model/
  reasoning-effort note, with the docs link.
- `ui/src/features/{settings,models}.ts`: `/provider` hint + completion row and
  one curated `/models` fallback row (`kimi-for-coding`).
- Wire-shape E2E (`tests/test_kimi_provider.py`): real
  `DirectProvider` from the factory, `httpx.MockTransport` at the network
  boundary, asserting `POST https://api.kimi.ai/coding/v1/chat/completions`,
  `model=kimi-for-coding`, `Authorization: Bearer …`, and that unset effort
  sends no `reasoning_effort`; plus `"kimi" in PROVIDERS`, spec pin, config
  plumbing, runtime readiness, context windows, reserved name.

## 6. Documentation

- `README.md`: subscription paragraph rewritten (copilot kept; removal reason
  + date), provider table updated, Kimi section added.
- `FEATURE_LEDGER.md`: new "Vendor-compliance wave (2026-10-14)" section with
  per-item change/proof rows.
- `docs/verification/OAUTH_RISK_REGISTER.md`: new
  "Subscription-provider compliance update (2026-10-14, owner-directed)"
  section (vendor sources, dispositions, kept provider).
- Historical proofs were left as-is per instruction (R4-013/R4-014 logs remain
  as the record of work that this wave supersedes).

## 7. Full backend suite

```
$ MACHINA_TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/ -q --tb=short -p no:cacheprovider
38 failed, 4406 passed, 1 skipped in 229.06s (EXIT=1)
```

Log: `R4-GEMINI_CLAUDE_KIMI_logs/backend_full_after.log`.

**All 38 failures belong to the concurrent R4-016 redaction lane, not this
wave** (disjoint files; `R4_STAGE2_LEDGER.md:41-44` documents R4-016 running
"as its own agent … in parallel with wave C (disjoint files)"):

- 37 × `tests/test_redaction_adversarial.py` — `XPASS(strict)` on the R4-016
  H-CORPUS entries: the redaction fix has landed in the shared tree while the
  strict xfail markers are still waiting for their flip by that lane. Failure
  text names the R4-016 regex quantifiers, e.g. "generic_secret … short run
  before '.' fails its {16,} quantifier; the value leaks".
- 1 × `tests/test_handoff_regressions.py::test_streaming_equals_whole_recognition_all_formats_all_splits`
  — a streaming-redaction split artifact (`password=[REDACTED_PASSWORD]` vs
  `…]}`) in the same lane's `machina/memory/redaction.py` work.

Evidence of disjointness: `tests/test_redaction_adversarial.py` imports only
`machina.memory.redaction`; `tests/test_handoff_regressions.py` is untouched by
this wave and its failing assertion is a redaction-regex expectation. None of
the 38 failures names a provider/auth surface, and the two redaction files are
modified in-tree by the other lane (`git status`: `machina/memory/redaction.py`
M, harness untracked). Every suite this wave touched is green (sections 3).

## 8. Surprises / deviations (reported, not hidden)

- **S1 — `tests/test_auth_store_adversarial.py` had no chatgpt tests.**
  Every test in it was a claude or gemini fence/sanity test (the R4-013/R4-014
  gates). With the code removed there was nothing left to keep, so the file
  was deleted rather than "pruned to empty". The chatgpt adversarial harness
  lives in `tests/test_chatgpt_auth_adversarial.py` and is untouched.
- **S2 — config registration was an implicit dependency.** The task listed
  the spec/`PROVIDER_SPECS` change, but `kimi_api_key` also had to be added to
  `SECRET_KEYS`, `DEFAULTS`, `LEGACY_ENV_VARS` and the `Config` dataclass in
  `core/config.py`; without it `KIMI_API_KEY` could never satisfy
  `requires_key=True`. `tests/test_providers_direct.py` additionally required
  the new spec in its pinned `expected` table.
- **S3 — architecture ratchets moved on purpose.** `GOD_FILE_CEILINGS` held
  rows for the deleted files (the parametrized test would error on the missing
  path); the provider package total fell 5501 → 4780 by exactly
  −720 −8 −2 +9, and the deferred-import census fell to 689 with the two
  runtime readiness probes. All recorded in the in-file delta table.
- **S4 — dead helpers.** `_free_port`/`_wait_for_callback` in `cli/auth.py`
  became unreachable with claude/gemini gone; deleted (no tombstone).
- **S5 — CWD note.** `ruff format` also touched three files' surrounding
  formatting only (auth.py, the two new tests); no behavior change.
- **S6 — concurrent R4-016 redaction lane.** The full-suite run shows 38
  failures, all in that lane's in-flight corpus (see section 7). They were not
  touched, masked, or re-run green; the failing files are documented as
  disjoint by `R4_STAGE2_LEDGER.md`.
- **S7 — stale comment left in `provider/anthropic.py:54`** ("the Claude
  subscription subclass shares every code path"). The deleted `ClaudeProvider`
  is the subclass it names; the file belongs to the R4-012 lane (not in this
  wave's edit list), so the comment was deliberately left and reported instead
  of edited cross-lane.

## 9. Residual risks

- The removed stores may still exist on disk from earlier installs
  (`~/.machina/auth/{claude,gemini}.json`). Nothing reads them; they are inert
  local files. No cleanup command was added (out of scope; deleting user files
  automatically would be worse).
- Kimi `reasoning_effort` vocabulary mismatch is documented, not masked: the
  harness offers low/medium/high, the Kimi wire accepts low/high/max, so
  `medium` would 400 from Kimi if a user sets it. R4-082 (effort-vocabulary
  unification) is the natural follow-up.
- Kimi models/context windows/tier availability are vendor-side facts that can
  drift; pinned from the 2026-10-14 docs and easy to update in
  `specs.py`/`context_windows.py`.
- The historical R4-013/R4-014 proof logs now document removed code; they were
  intentionally not rewritten (evidence preservation).
