# Final review — line-by-line pass: security + permissions slice

Date: 2026-10-15 · Reviewer role: senior-security + code-reviewer · Mode: READ-ONLY
(no source edits; this file is the only write). No runtime execution used; all
findings are static traces verified by reading the code and its callsites.

> Note on a moving tree: `machina/security/fetch.py` was modified by a sibling
> fix lane at 00:35 during this pass (the invalid-port guard, below). All other
> slice files were unchanged while read. Line numbers below are the current
> on-disk state.

## Scope actually reviewed (every line of each)

Security: `machina/security/store_fence.py` (244), `chatgpt_auth.py` (827),
`copilot_auth.py` (552), `providers.py` (560), `env_file.py` (399),
`oauth_keys.py` (218), `secrets.py` (184), `fetch.py` (289).

Permissions: `machina/permissions/broker.py` (572), `policy.py` (1137),
`store.py` (903), `commands.py` (186), `shell.py` (105), `tools.py` (106),
`__init__.py` (34).

Connector network boundary (present per task; **correction of the earlier
draft of this file** which looked only under `machina/security/` and wrongly
declared them absent): `machina/connectors/oauth_network.py` (973),
`machina/connectors/http_transport.py` (144).

Callsites consulted: `core/agent.py` (guard + `clear_execution_context`),
`core/scheduler.py` (`guard(script_proposal)`), `core/approval_resume.py`
(`matches_approval`, `resumed_request`), `connectors/execution.py`
(`call_spec`, `_connect_request`, `connect_authorized`),
`connectors/oauth.py` (`OAuthNetworkClient` lifecycle, `aclose`), `api/routes/
approvals.py:47`, `gateway/features/mode.py:192`, `core/runtime.py:424-435`
(loopback key resolution), `tests/test_permission_risk_policy.py`
(R01/R4-060/R4-062 pins).

---

## Confirmed findings (severity-ranked)

### F1 · P2 (conditional) — REV:path gate misses git's index-stage revspec (`:N:path`)

- **Files/lines:** `machina/permissions/policy.py:199-204` (revspec split) feeding
  `machina/permissions/policy.py:186-228` (`sensitive_path`); `git show` admitted
  as routine by `machina/permissions/commands.py:44` (`_GIT_COMMANDS` has `show`)
  and `machina/permissions/commands.py:165-170`.
- **Trace (ask/workspace/sandbox):** `terminal git show :0:credentials` →
  `parse_terminal_command` (no shell metachars; `:` is not in the opaque set
  `shell.py:61`) → `argv=["git","show",":0:credentials"]` →
  `_sensitive_capabilities` (`policy.py:681-688`) resolves the operand to
  `<root>/:0:credentials` → `sensitive_path` strips the root to `":0:credentials"`,
  the revspec branch fires (`text[1] != ":"`) and appends
  `text.split(":",1)[1].split("/")` = `["0:credentials"]`. Neither token equals
  a `private_files` member (`policy.py:207-216`), and none matches the dotenv
  rules (`:221-225`) → **no `credential_access`**. `routine_project_command`
  returns True → `decide` (`policy.py:947-956`) → `allowed`, "routine trusted
  project command".
- **Why the R4-060 fix misses it:** it splits on the **first** colon
  (`split(":",1)[1]`), correct for `REV:path` but wrong for git's
  `:[<n>:]<path>` index form where the first colon is empty. The R4-060 tests
  only cover `HEAD:path` (`tests/test_permission_risk_policy.py:630-643`).
- **Precondition (honesty):** the leaked object must be **tracked or staged**
  (`git show :0:path` reads the index; `git show HEAD:path` reads a tree). A
  gitignored credential file yields a git error, not content. The exploit class
  is a mistakenly-committed `credentials`/`.netrc`/`id_rsa`/`.envrc` — exactly
  what the gate exists to catch.
- **Scope of gap:** only single-component names at repo root evade; a
  multi-component tail (`:0:sub/credentials`) leaves the sensitive component as
  its own part, and any `*.env` is caught by the suffix rule.

### F2 · P2 — `grep`'s clustered `-d recurse` escapes recursive-review classification

- **Files/lines:** `machina/permissions/policy.py:446-465` (`_recursive_traversal`
  grep branch), specifically the three `-d`/`--directories` detectors at
  `:452-458`; `grep` admitted routine by `machina/permissions/commands.py:15`
  (`_PROJECT_COMMANDS`).
- **Trace:** `terminal grep -id recurse REVIEW_TOKEN .` → `argv=["grep","-id",
  "recurse","REVIEW_TOKEN","."]`. The grep branch tests (a) exact `{"-d",
  "--directories"}`, (b) `--directories=…`, (c) `arg.startswith("-d")`, and
  (d) `any(ch in arg[1:] for ch in ("r","R"))`. For `-id`: (a) false, (b) false,
  (c) false (`"-id"` does not start with `"-d"`), (d) false (`"id"` has no
  `r`/`R`). So recursion is not detected. In `routine_project_command`
  (`commands.py:132-179`) the token `-id` matches `-[A-Za-z0-9]+`
  (`commands.py:149-153`), so the "unknown compact option" fail-closed guard
  does **not** fire, and `grep` is in `_PROJECT_COMMANDS` → routine True. Net:
  `decide` returns `allowed`. But `grep -d recurse` recurses **including hidden
  files**, so `grep -id recurse TOKEN .` prints matching `.env` lines with no
  approval and no `credential_access`.
- **Why the R01 fix misses it:** R01 added the exact, attached (`-dX`) and
  `=`-forms but not the **cluster** position where `d` is not first
  (`-id`, `-sd`, `-Ed`, `-Fd`, `-Hd`, …). The existing pin
  `tests/test_permission_risk_policy.py:281-283` covers only `-d recurse`,
  `-drecurse`, `--directories=recurse`.
- **Falsification attempt:** (a) Tried the compact-option guard — refuted, `-id`
  matches its regex. (b) Tried `command_path_values` — it appends `recurse`,
  `REVIEW_TOKEN`, `.`, all resolving inside root, so it does not force review.
  (c) Tried `_sensitive_capabilities` paths — no operand names a credential file
  directly, so `credential_access` is not added. (d) Confirmed single-token
  forms `-r`/`-R`/`-d recurse` **are** caught, so this is a form-specific hole.
  Not refuted → CONFIRMED.

### F3 · P3 — `device_flow_poll` takes a server-supplied `interval` as an unbounded sleep

- **Files/lines:** `machina/security/copilot_auth.py:431-432` (loop + `sleep(wait)`)
  and `:463-468` (`slow_down` branch sets `wait = float(server_interval)`).
- **Trace:** the budget is checked only at the loop head
  (`while time.monotonic() < deadline`). On `error == "slow_down"` with a
  positive numeric `interval`, `wait` is replaced by the server value with **no
  clip** to the remaining deadline and **no cap**, so `sleep(wait)` can block far
  past the `DEFAULT_DEVICE_TIMEOUT` (300 s) contract. The initial and `else`
  paths keep `+DEVICE_POLL_SAFETY_MARGIN`/`+= 5.0`, so the omission is specific
  to the server-supplied value.
- **Falsification attempt:** endpoint is `https://github.com/login/oauth/
  access_token` over TLS, so triggering it needs endpoint compromise/MITM (low
  exploitability); but a legitimate large `interval` (or buggy proxy) violates
  the documented "until it expires" contract without an attacker. Not refuted →
  CONFIRMED (availability/robustness), P3.
- **Fix direction:** `wait = min(max(1.0, float(server_interval)) +
  DEVICE_POLL_SAFETY_MARGIN, max(0.0, deadline - time.monotonic()))`.

### F4 · P3 — `find_pending` / `find_approved_unclaimed` bypass the redacted view boundary

- **Files/lines:** `machina/permissions/store.py:812-838` (`find_pending`) and
  `:841-881` (`find_approved_unclaimed`) return `_with_completeness(dict(row))`
  **without** `public_view`, unlike `get_approval` (`:784-809`) and `list_pending`
  (`:884-903`). Surfaced to transports by `broker.py:199-201`, `:214`,
  `:253-256` (`NeedsApproval(existing/current/approved)`).
- **Trace:** the top-level `target` column is persisted **raw**
  (`store.py:490`, `:521`). R4-105b's stated rule (`store.py:451-455`) is that
  this raw column must be redacted at every view boundary (`public_view` does).
  `find_pending`/`find_approved_unclaimed` do not, so the same approval can be
  surfaced raw or redacted depending on which helper the broker happens to use.
- **Falsification attempt:** (a) Is `target` ever secret-bearing? For the mapped
  tools it is a filesystem path or agent name (`tools.py:36-83`);
  `network`/`argv` carry the risky values and are redacted in the stored
  proposal (`store.proposal_of`), so real exposure is limited. (b) Does the
  transport re-redact? Not at this boundary — `NeedsApproval` carries the dict
  verbatim. Not fully refuted → CONFIRMED as a defense-in-depth/consistency
  defect (P3, low real impact).
- **Fix direction:** route both helpers through `public_view` (it preserves
  `digest`/`session_id`/`status`).

---

## Q (quality/design) observations — not confirmed security defects

- **Q1 (fence duplication / logout lock asymmetry).** `chatgpt_auth.py:99-170`
  re-implements `store_fence`'s generation mechanics in a private namespace
  instead of calling the shared ones (which `copilot_auth.py:82-89` does).
  Within the module the read→write fence is consistent (both use the chatgpt
  local dict **plus** the shared durable file), and no cross-module caller uses
  `store_fence.*` on the chatgpt store (grep of all
  `credential_generation`/`invalidate_credentials`/`fenced_logout` callsites), so
  no hole today. Related: `chatgpt_auth.logout` (`:411-412`) **does** take
  `_store_lock`, whereas `store_fence.fenced_logout` deliberately does not
  (`store_fence.py:19-23`, `copilot_auth.py:315-316`). Consequence: a chatgpt
  logout can block behind an in-flight refresh POST (bounded: 20 s default
  timeout under the 45 s lock bound); the post-POST generation gate at
  `chatgpt_auth.py:623` is then unreachable for same-store logouts (dead branch,
  no defect).
- **Q2 (fd-close completeness).** `store_fence.chmod_temp_file` correctly closes
  the mkstemp fd when `os.chmod` fails (`store_fence.py:205-220`; used at
  `chatgpt_auth.py:153,319`, `copilot_auth.py:167`, `providers.py:297`). Residual:
  if `os.fdopen(fd, …)` itself raised after a successful chmod, none of those
  four writers would close `fd` (`env_file.py:340-357` handles it with a `fd=-1`
  sentinel). Extremely unlikely; not reproduced.
- **Q3 (`secrets` Windows read).** `secrets._read_file` fallback
  (`secrets.py:59`) does `source.read_bytes()[:8193]` — the whole file is read
  before slicing, so the 8 KiB bound is a post-hoc slice, not a read cap. The
  POSIX branch (`:40-50`) is genuinely bounded. `O_NOFOLLOW`-less platforms only.
- **Q4 (keyring allowlist breadth / Windows lock).** `oauth_keys
  ._keyring_backend_usable:76-80` admits any backend whose qualified name
  contains `"keyring"` on posix/nt (`and` binds tighter than `or`, and
  `os.name` is always posix/nt here, so the parenthesised clause is inert). That
  is the documented "keyring-package backends" behaviour and `plain`/`plaintext`
  still needs the escape-hatch flag (`:75`); noted only because a chainer or
  third-party plugin whose name contains "keyring" is trusted without the
  `_ALLOWED_BACKENDS` check. Also, `_acquire_creation_lock` uses blocking
  `msvcrt.LK_LOCK` on Windows (`:168`) instead of the polling deadline — but the
  whole creation runs inside `_keyring_bounded`, whose daemon-thread future
  enforces the caller timeout, so it cannot hang the event loop.
- **Q5 (`rg --files` pattern-position assumption — no content impact).**
  `_rg_directory_walk` (`policy.py:300-351`) assumes the first positional is the
  pattern unless `-e/-f` was given, so the pattern-less `rg --files <dir> <file>`
  drops `<dir>` and classifies on `<file>` (`policy.py:339`). However `--files`
  prints file **names**, not contents, and recursive name listing is already
  routine via `ls -R` (allowed by `commands.py:12-38,179`), so this is not an
  escalation. Consistent with `_RG_VALUE_LONG_OPTIONS` (`:257-295`) omitting the
  boolean `--files`; flagged only as grammar-model completeness.
- **Q6 (recursive content readers not modelled: `diff -r`).** `_recursive_traversal`
  covers `grep`/`rg`/`find`/`git grep` only. `diff` is in `_PROJECT_COMMANDS`
  (`commands.py:24`) and `diff -r dirA dirB` (both operands inside root) is
  routine, yet `diff` prints differing **content**. Exploitation requires a
  same-named counterpart file in `dirB`; for actually-sensitive names the write
  of the counterpart is itself `credential_access`-gated (`sensitive_path`
  matches `credentials`, `*.env`, …), so no practical bypass was constructed —
  recorded for coverage.
- **Q7 (error typing in the OAuth secret path).** `oauth_network.refresh_token`/
  `revoke_token` call `get_secret(record.client_secret_ref)`; a `basic` record
  with an empty ref makes `get_secret("")` raise `ValueError` (not
  `OAuthNetworkError`) at `oauth_network.py:891`. Correctness/typing only.
- **Q8 (execution-context clear symmetry).** `core/scheduler.py:933` calls
  `guard(script_proposal)` and never clears `_execution_context`
  (`broker.py:51-68` contract). Contained in practice (each job is its own
  `asyncio.create_task` and nothing in it reads `get_execution_context()`), so no
  escalation observed — flagged for contract symmetry.

---

## Recent fixes — verified to hold under callsite inspection

- **fd-close helper (R4-049b):** `store_fence.chmod_temp_file` (`:205-220`)
  closes `fd` on chmod failure and reraises; all four documented callsites use it.
  Holds.
- **Removal race serialization (R6-2 / R5-LM L2):** `providers.remove_provider`
  holds one `_store_lock` across store-entry deletion **and** `.env` line
  removal, `os.environ` pop, and `invalidate_secret_cache` (`providers.py:412-433`);
  `add_provider` nests the same lock around `set_env_values` (`:363-384`). No
  torn registered-but-keyless state is reachable. Holds.
- **`gh`-import fence (R5-LM L1):** `import_from_gh_cli` captures
  generation+fingerprint before the slow `gh` probe (`copilot_auth.py:268-270`),
  gates under `store_lock` (`:280-295`), and post-save re-checks the generation
  and undoes its own write (`:297-303`). `publish_device_token` mirrors it
  (`:509-522`); `_probe_gh_token` strips `GITHUB_TOKEN`/`GH_TOKEN` and is
  timeout-bounded (`:194-214`). Holds.
- **Host authority (R5-J):** `_host_authority` re-brackets IPv6 and appends
  non-default ports, eliding scheme defaults (`fetch.py:188-200`), called with
  `parsed.port` per redirect hop (`fetch.py:232`). Holds.
- **Invalid-port guard (final-review fix, `fetch.py:225-230`):** `parsed.port`
  is now read inside a `try/except ValueError` that raises `FetchError`, so
  `http://h:99999/` no longer escapes the `FetchError` contract as a bare
  `ValueError`. Verified in the current file; holds.
- **R5-Nb source redaction:** not in this slice's files
  (`rag/pipeline.py`, `core/provider/audit.py`); the slice's only redaction
  consumers (`broker._redacted`, `store._redact_public_text`,
  `policy.build_request` preview) route through
  `machina.memory.redaction.redact_secrets` and fail closed
  (`broker.py:401-423`, `store.py:341-358`, `policy.py:1096-1107`). No
  interaction defect found.

---

## Refuted candidates (recorded for coverage)

- **IPv4-mapped IPv6 SSRF bypass in `fetch._check_ip_public`** — REFUTED.
  `ipaddress`'s IPv6 `_private_networks` includes `::ffff:0:0/96`, so
  `IPv6Address('::ffff:127.0.0.1').is_private` is True and
  `fetch.py:55-68` rejects every IPv4-mapped address. No second-resolution
  rebind either: `pinned_fetch` connects to the validated `target_ip`
  (`fetch.py:236-246`).
- **Plaintext/loopback guard bypass (`providers._require_plaintext_allowed`)** —
  REFUTED. Uses `urlparse(...).hostname` (userinfo excluded) and exact-string
  loopback matching (`providers.py:258-265`, noting it deliberately rejects
  `127.0.0.2`-style spellings fail-closed); `_validate_base_url` additionally
  rejects userinfo/query/fragment at store time (`:245-255`); `fetch_models`
  re-checks before the bearer leaves (`:514`). No hand-edited-store leak.
- **Broker deadline / claim-once** — REFUTED as a defect. `_approval_deadline`
  re-reads durable truth before reporting (`broker.py:315-344`); `claim_execution`
  is a conditional `UPDATE … WHERE status='approved' AND consumed=FALSE` and the
  memory path re-verifies `consumed` (`store.py:639-700`); `complete_execution`
  is durable-first (`:703-756`). No double-execution path.
- **Audit completeness** — REFUTED. `guard` emits `pending`/`allowed`/`denied`
  on every branch (`broker.py:120,138,146,154,167,179,182,188,192`) and the
  headless/interactive helpers audit their own terminal verdicts
  (`:211,247,251,275,285,292,334,343`); `audit_permission_verdict` never raises
  (`:440-471`).
- **Plaintext keyring default** — REFUTED. `_keyring_backend_usable` gates
  `plain`/`plaintext` behind `MACHINA_OAUTH_ALLOW_PLAINTEXT_KEYRING == "1"`
  (`oauth_keys.py:70-80`); default refuses.
- **SSRF in `oauth_network.classify_url_host`** — REFUTED. Metadata hosts,
  loopback-without-local-access, mixed public/private classes, and redirects are
  all refused (`:80-151,345-355`); every outbound URL passes `_validate_url`
  before dispatch and the pinned transport re-resolves+re-validates at connect
  (`http_transport.py:89-120`), so there is no TOCTOU rebind window.

---

## Verdict

Four confirmed defects, none with a demonstrated remote path: one concrete but
narrow **policy bypass of the credential-review gate** (F1 `:N:path` revspec) and
one **recursive-read classification bypass** that does disclose file contents
(F2 clustered `grep -d recurse`), plus two P3 boundary/robustness issues (F3
device-poll sleep, F4 unredacted `find_*` view). The named recent fixes (fd-close,
removal-race serialization, gh-import fence, host authority, invalid-port guard,
R5-Nb redaction) hold end-to-end. Highest-value new issue is **F2** — it is a
live content-disclosure path in `ask`/`workspace`/`sandbox`, and its regression
pin (R01) covers only the non-clustered spellings.
