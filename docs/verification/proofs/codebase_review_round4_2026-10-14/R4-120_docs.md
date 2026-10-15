# R4-120 docs-truth evidence (2026-10-14)

Lane scope: prose docs only. No code/tests/config touched. No commit.
Pre-edit `git status --short <file>` per candidate:

- `OPERATIONS.md` — clean (tracked, unmodified) → edited.
- `docs/verification/RELEASE_READINESS.md` — clean → edited.
- `docs/verification/CODEBASE_REVIEW_2026-10-14.md` — **untracked (`??`)**,
  no tracked modifications by another lane; annotation explicitly requested by
  the round-6 plan → edited, flagged in report.
- `docs/verification/CURRENT_ISSUES_CHECKPOINT.md` — clean → appended.
- `FEATURE_LEDGER.md` — clean → appended.
- `SECURITY.md` — not touched (D1 lane). No code/tests/config touched.

## Claim 1 — OPERATIONS.md self-migration (was false, corrected)

Old claim (`OPERATIONS.md:54-57`): "The gateway does not self-migrate on
startup."

Verification (all settled at committed HEAD; files checked via
`git show HEAD:<path>`):

- `git show HEAD:machina/gateway/server.py | sed -n '495,515p'` — first DB use
  calls `await db.initialize_runtime_schema()`; failure raises
  `RpcError(DATABASE_UNAVAILABLE, "runtime schema upgrade failed")`
  (fail-closed, never serves traffic on a failed upgrade).
- `git show HEAD:machina/db/connection.py | sed -n '190,212p'` —
  `initialize_runtime_schema()` applies `growth_schema.sql`,
  `delegation_schema.sql`, `image_context_schema.sql`, `soulspec_schema.sql`
  inside one transaction under
  `pg_advisory_xact_lock('machina:runtime_migrations')`.
- `git show HEAD:machina/core/runtime.py | grep -n initialize_runtime_schema`
  → lines 112, 162 (runtime startup path also applies it, fail-closed).
- `git show HEAD:machina/cli/__init__.py | sed -n '228,240p'` — CLI chat path
  applies it too; on failure prints "Runtime schema upgrade failed. Run
  `machina db init` and retry." (line 1237 applies the full schema elsewhere).

Correction written: additive runtime migrations at startup, fail closed; base
schema still requires `machina init` (new base-schema columns without it still
fail at turn time with `UndefinedColumnError`, so the original operational
guidance — migrate before starting new workers — is preserved).

## Claim 2 — RELEASE_READINESS.md DNS (was stale, corrected)

Old claim (`:9`): "complete DNS-rebinding protections remain outside the
release scope."

Verification (clean files, worktree == HEAD):

- Read `machina/connectors/http_transport.py:75-144` (`pinned_http_transport`):
  resolves/validates the hostname once via bounded daemon-thread DNS, dials
  only validated numeric IPs, preserves the origin hostname for HTTP Host, TLS
  SNI, and certificate verification; SSL context pinned with
  `trust_env=False` (`:131`).
- `git show HEAD:machina/connectors/mcp_client.py | grep -n pinned` →
  import `:19` (`run_dns_check`), use `:400,431` (`pinned_http_transport`).
- `git show HEAD:machina/connectors/oauth_network.py | grep -n pinned` →
  import `:19`, use `:309,314`.
- `git status --short` on all three connector files → clean (settled, not
  in-flight; the dirty connector files are models/oauth/oauth_callback/policy).

Correction written: protections "implemented and in use", wired in
`oauth_network.py` and `mcp_client.py`. Deliberately not "complete" — residual
items (plaintext-`http` endpoints, semaphore blast radius) were already tracked
as pre-existing in the checkpoint.

## Claim 3 — round-2 lockfile R16 (was false, annotated fixed)

Claim location found: `docs/verification/CODEBASE_REVIEW_2026-10-14.md:153-159`
(R16: "`uv.lock` lacks embeddings/rerank extras", "needs updating").

Verification (commands run 2026-10-14):

- `grep -n "embeddings-local|embeddings-onnx|rerank|model2vec|fastembed" uv.lock`
  → extras declared at `uv.lock:1289-1296`
  (`embeddings-local=[model2vec]`, `embeddings-onnx=[fastembed]`,
  `rerank=[fastembed]`); `provides-extras` at `:1339` lists all of them;
  locked packages `fastembed-0.9.0` (`:681`) and `model2vec-0.10.0` (`:1678`).
- `uv lock --check --offline` → `Resolved 130 packages in 6ms`, exit 0
  (fresh; contradicts the review's "exits one because the lockfile needs
  updating" — that observation predates the lock regeneration).
- `grep -n optional-dependencies -A 12 pyproject.toml` confirms the manifest
  extras match the lock.

Annotation appended to R16 in place (quoted block, dated, cites the
re-check command so a re-raise requires re-running it).

## Claim 4 — CURRENT_ISSUES_CHECKPOINT.md (append-only annotation)

Clean file; appended "R4-120 docs-truth annotation (2026-10-14)" without
altering the dated 2026-10-12 rows. Contents: DNS rows settled (code
citations above), lockfile check clean (command output above),
self-migration corrected, execution counts explicitly left as dated historical
evidence (not re-verified). No test outcome is claimed — no suite was run.

## Claim 5 — FEATURE_LEDGER.md (settled-behavior rows only)

New "Docs-truth corrections (R4-120)" section, three rows. Settled basis:

- Effort vocabulary: `git show HEAD:machina/core/provider/reasoning.py`
  (`REASONING_EFFORTS = ("low", "medium", "high")`, unset aliases
  `""|none|off|default|auto` → `""`, precedence per-call > instance > config)
  and HEAD `core/config.py:276-285` (`''|low|medium|high|xhigh|max`, xhigh/max
  catalog-validated per model). Proof cites clean test files
  `tests/test_providers_direct.py`, `tests/test_kimi_provider.py`.
- `/provider remove`: `git show HEAD:ui/src/features/settings.ts`
  (`removeProvider`, `:188-262`: user-added only, built-ins protected, re-pin
  to fallback with notice) and HEAD `providers_remove` docstring. Proof cites
  clean `tests/test_features_split.py:78`. (Dirty suites
  `test_gateway_providers.py` / `test_r4_082_gateway_providers.py` deliberately
  not cited.)
- Keys allowlist as-shipped: clean `machina/gateway/features/secrets.py:25-56`
  (`PROVIDER_KEYS` + `MANAGED_ENV_KEYS` + `MACHINA_PROVIDER_<NAME>_API_KEY`
  regex), clean `machina/security/env_file.py:29` (10 catalog keys), clean
  `ui/src/features/keys.ts:24` (UI mirror). Proof cites clean
  `tests/test_fm3_hardening.py:17-27`.

## PENDING (depends on in-flight work — prose deliberately withheld)

1. Keys allowlist final shape: `machina/gateway/features/providers_feature.py`,
   `ui/src/features/settings.ts`, and `ui/src/protocol.ts` are dirty in other
   lanes (`ProviderCleanup.envKey` → `apiKeyEnv` rename,
   `persist_provider_fallback` wiring). Document the final wire shape only
   after those land.
2. `max_iterations` decision: `FEATURE_LEDGER.md:86` ("ignored legacy data")
   left untouched; no settled decision found in the tree
   (`git diff machina/core/config.py | grep max_iteration` → empty).
   Record the decision when the owning lane settles it.
3. `minimal` catalog effort tier + canonical-vocabulary centralization
   (`REASONING_EFFORTS`/`REASONING_CATALOG_EFFORTS` move into `config.py`):
   `machina/core/config.py` (+95) and `machina/core/provider/reasoning.py`
   are dirty. The ledger row covers only the HEAD-settled vocabulary.
4. Provider-remove fallback persistence across restart (dirty
   `persist_provider_fallback` in `config.py`/`providers_feature.py`):
   document after landing.
5. README updates for changed behavior (effort vocabulary, `/provider remove`,
   keys allowlist): out of this lane's candidate-file list; left for the
   owning lane.
