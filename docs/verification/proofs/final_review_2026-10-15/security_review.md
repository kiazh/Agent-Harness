# Final security review — round diff `cdc5232..HEAD`

- **Reviewer role:** final pass, `senior-security` + `security-pen-testing` posture (STRIDE / OWASP A01–A10).
- **Review baseline (authoritative target):** `cdc5232` (base) → `56c0222` (`HEAD`, committed 2026-10-15).
  Every finding below is stated against **HEAD**.
- **Mode:** READ-ONLY. No product file was modified by this review; only this evidence file
  is written.
- **Method:** for each named surface, read the post-fix code end-to-end, then attempt a
  bypass the fix could have missed (encoding tricks, ordering, error paths that skip
  redaction, authority confusion, TOCTOU, resource exhaustion, injection via new
  parameters, fail-open defaults). Reproductions use the project interpreter
  (`.venv/bin/python`). The test corpus contains synthetic secrets by design; **fixtures are
  not reported** — every finding is reproduced against product code.

## Baseline & working-tree caveat (read this first)

While this review ran, the working tree moved: `git status` shows **~36 uncommitted,
unstaged** files (≈1072 insertions / 351 deletions) carrying an in-progress remediation
wave that labels its own findings (`final-review F1`, `security review S2`, `final-review
S6/F3`, …). Several of those uncommitted edits **already patch findings below** — in
particular `machina/core/provider/audit.py`, `machina/mcp_server/__init__.py`,
`machina/security/copilot_auth.py`, `machina/permissions/policy.py`,
`machina/permissions/store.py`, `machina/tools/file.py`.

Consequences:

1. This report is graded against **HEAD**, not the dirty tree. Where the working-tree edit
   changes a verdict, the finding says so under **Working tree**.
2. File/line references are **HEAD** line numbers unless marked otherwise. (The `read` tool
   reads disk, so early drafts of this file quoted working-tree line numbers; they have
   been re-derived against HEAD.)
3. `security_review.md` is the only file this review created; the product-file changes in
   `git status` are not mine.

## Summary

| ID | Surface | Finding | Sev | Working tree |
|----|---------|---------|-----|--------------|
| SEC-1 | MCP authority ceiling | `machina_chat` skips the mode ceiling when `mode` is omitted; the child inherits the operator's persisted `execution_mode` | **P2** | fixed (clamp) |
| SEC-2 | Audit redaction (R5-N) | Presigned-URL credential params (`sig`, `X-Amz-Signature`) are not redacted — a live credential persists to `audit_events` | **P2** | open |
| SEC-3 | Audit redaction (R5-N) | Password-only userinfo (`redis://:pw@h`, `postgresql://:pw@h`) is not redacted by any rule | P3 | **partially open** (DB-regex widened, username group still `[^:]+`) |
| SEC-4 | Audit persistence boundary | `_sanitize_value` passes non-primitive objects through; `json.dumps(..., default=str)` persists `str(obj)` verbatim | P3 | open |
| SEC-5 | RAG source redaction (R5-Nb) | `_redact_metadata` uses only the memory redactor; URL-credential redaction is applied only to `source` | P3 | open |
| SEC-6 | Credential store logout | `chatgpt_auth.logout` holds the store lock before bump+unlink (inconsistent with `store_fence.fenced_logout`) | P3 | open |
| SEC-7 | Credential store locks | Copilot `store_lock` timeouts are not translated to `CopilotAuthError`; an untyped `TimeoutError` leaks | P3 | open |
| SEC-8 | Audit record identity | caller kwargs `event` / `timestamp` override the audit record's identity | P3 | fixed (reserved filter) |
| ADJ-1 | Copilot device flow | untrusted server `slow_down.interval` sets an unbounded `sleep` past the timeout contract | P3 | fixed (cap) |

Strong refutations (bypass attempts that failed) are in [Refuted attempts](#refuted-attempts);
they are as valuable as the findings.

---

## SEC-1 — MCP mode ceiling is not enforced when `mode` is omitted (P2)

**Location (HEAD):** `machina/mcp_server/__init__.py:504-544` (guard at `527-544`), ceiling at
`94-122`, child command at `424-437`.

```python
# HEAD machina_chat(...)
requested = mode.strip().lower() if isinstance(mode, str) else ""
if requested:                       # <-- only a *requested* mode is capped
    ceiling = _mcp_mode_ceiling()
    if _MODE_RANK[requested] > _MODE_RANK[ceiling]:
        return _json_text({... "code": "mode_ceiling_exceeded" ...})
cmd = _chat_command(prompt, session_id, mode)   # mode=None => no --mode flag
```

`_chat_command` appends `--mode <m>` only when `m` is truthy. With `mode=None` the child is
`python -m machina chat -q -- <prompt>`; the CLI leaves `config["execution_mode"]` untouched
(`machina/cli/__init__.py:225-243`), so the child runs with the operator's **persisted global
mode**, not with the MCP ceiling.

**Attack sketch (authority confusion / fail-open default):**

1. Operator runs the CLI at `execution_mode: full` and launches the MCP shim with
   `MCP_MAX_MODE=ask` (the documented way to cap the MCP surface below their own mode).
2. A live **full-mode session grant** already exists for some session (the operator
   activated it, or a prior turn created it).
3. The MCP caller invokes `machina_chat(prompt, session_id=<granted>, mode=None)`.
4. The ceiling branch is skipped (`requested == ""`); the child boots in `full`, sees the
   grant via `list_grants(session_id)`, and `policy.decide` takes the
   `mode == "full" and _has_full_grant(...)` fast path — **full host authority with no
   approval card**, despite the `ask` MCP cap.

**Why the existing guards miss it:** the R5-A finding-1 work (mirrored in the
`R5-A_mcp_authority.md` proof) reasons only about *tool-supplied* modes; every guard is
inside `if requested:`. The parent already holds the ceiling at that point and could pin
`--mode <ceiling>` for the omitted case.

**Falsification / mitigations:**

- With the common configuration (`MCP_MAX_MODE` unset), the ceiling equals the operator
  mode, so there is no escalation; the bug needs `MCP_MAX_MODE < execution_mode`.
- Absent a live full-mode session grant, a `full` child **pauses** (`policy.py:874-881` →
  `pending`), so the realized impact is bounded to the grant-session scenario. Hence P2,
  not P1.

**Working tree:** already fixed — `ceiling = _mcp_mode_ceiling()` is hoisted and, when
`mode` is omitted and `_MODE_RANK[ceiling] < _MODE_RANK[_operator_mode()]`, the child is
spawned with `effective_mode = ceiling` (fail-closed clamp; unchanged when ceiling ==
operator mode).

---

## SEC-2 — Audit redaction misses presigned-URL credential params (P2)

**Location (HEAD):** `machina/core/provider/audit.py:99,103-124,145-155`; enforcement at
`machina/observability/audit.py:94-110`.

**Reproduction (HEAD):**

```
>>> from machina.core.provider.audit import _sanitize_value as s
>>> s("https://acct.blob.core.windows.net/c/b?sv=2021&sig=SECRETsig123")
'https://acct.blob.core.windows.net/c/b?sv=2021&sig=SECRETsig123'
>>> s("https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc123def456&X-Amz-Credential=AKIA1")
'https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc123def456&X-Amz-Credential=AKIA1'
```

The R5-N fix redacts credential query/fragment values by **parameter name**
(`_CREDENTIAL_PARAM_SUFFIXES`, `_CREDENTIAL_PARAM_EXACT`). The allowlist omits `signature`
/ `sig` — *the* credential of Azure Blob SAS URLs (`?sv=…&sig=<secret>`) and AWS SigV4
presigned URLs (`?X-Amz-Signature=<secret>`). `X-Amz-Credential` is caught only
incidentally (normalizes to `…credential`).

**Attack sketch (A09 — information disclosure through the audit store):** a URL carrying
`?sig=`/`X-Amz-Signature=` reaches an audit field — e.g. `audit_log("rag_tool_index_document",
path=path, …)` (`machina/tools/rag.py:254`), an HTTP-error payload (`error=str(e)` echoing
the URL), or any dict value passed to `audit_log`. `_sanitize_value` runs all patterns and
then `redact_url_credentials`; neither knows the name, so the value survives. The
persistence boundary (`observability/audit.py:106-110`) re-sanitizes with the *same*
sanitizer and INSERTs the row; the durable `audit_events` record and any listings then hold
a live, replayable credential.

**Why the fix misses it:** the docstring claims to close "credential-bearing query / fragment
values **of any length or format**", but the implementation is a fixed name allowlist, and
`tests/test_r5_018_audit_redaction.py` only asserts `api_key` / `access_token` / `token`
spellings. `sig` is precisely the "any format" case the comment promises.

**Falsification:** the leak requires such a URL to actually enter an audit value; the
`path` argument of `rag_tool_index_document` is normally a filesystem path. If no call site
ever receives a presigned URL this is a completeness gap. It stays P2 because presigned
URLs are a natural payload of the web/fetch tooling and the sink is durable.

**Working tree:** open (the uncommitted change only widens the DB-scheme regex).

**Suggested fix:** add `"signature"` as a suffix and `"sig"` as an **exact** name (to avoid
over-redacting words ending in `sig`); add Azure SAS + AWS presigned fixtures.

---

## SEC-3 — Password-only userinfo is not redacted (P3)

**Location (HEAD):** `machina/core/provider/audit.py:59` (DB scheme) and `:99` (userinfo).

**Reproduction (HEAD):**

```
>>> s("redis://:secretpw@host:6379")   # unchanged
>>> s("postgresql://:pw@h/db")         # unchanged
>>> s("mysql://:pw@host/db")           # unchanged
```

Both rules require a non-empty username: the DB-scheme pattern is `…://([^:]+):([^@]+)@`
and the new userinfo regex is `…://[^/\s?#@]+):([^/\s?#]*)@`. Redis `AUTH` and several
drivers legitimately omit the username (`scheme://:password@host`).

**Attack sketch:** a Redis/Postgres DSN with password-only userinfo reaches an audit value
(DB URIs are the canonical example the DB-scheme rule was written for); the password
persists into `audit_events` and tool surfaces.

**Why the fix misses it:** the "remainder" rule was engineered around `user:password@` with
the assumption that a bare `:password@` is malformed. It is not, for Redis and some
drivers.

**Falsification:** configuration written by this project uses `postgresql://user:pass@…`;
a password-only DSN requires the operator to author it (or a Redis URL to be logged). P3.

**Working tree:** the DB-scheme regex was widened to `([^/\s?#]*)@` for the *password* half,
but the **username group is still `([^:]+)`**, so `redis://:pw@h` / `postgresql://:pw@h`
still leak. Partially open.

**Suggested fix:** accept an empty username in both rules (`([^:]*)` and
`([^/\s?#@]*)`), keeping the `[REDACTED_…]` short-circuit.

---

## SEC-4 — Non-primitive audit values bypass sanitization at persistence (P3)

**Location (HEAD):** `machina/core/provider/audit.py:86-87` (`else: return value`);
`machina/observability/audit.py:106-110` (`json.dumps(entry, default=str)`).

**Reproduction:**

```
>>> import json
>>> from machina.core.provider.audit import _sanitize_value as s
>>> class O:
...   def __str__(self): return "redis://:pw@h"
>>> json.dumps(s({"cfg": O()}), default=str)
'{"cfg": "redis://:pw@h"}'
```

`_sanitize_value` handles bytes/str/dict/list/tuple/set and returns every other object
unchanged; the persistence boundary serializes it with `default=str`, so `str(obj)` is
persisted verbatim without ever passing a pattern.

**Why the fix misses it:** R5-N added a second pass at the INSERT boundary but kept the
"known primitive" dispatch; `default=str` is the escape hatch that reintroduces raw text
after sanitization.

**Falsification:** no current `audit_log(...)` call site passes an arbitrary object — the
observed callers pass `str`/`int`/`dict`/`list`. Latent (a future caller passing a wrapper
object or a `Path` whose `str()` carries credentials would leak). P3.

**Working tree:** open.

**Suggested fix:** coerce unknown scalars with `str(...)` inside `_sanitize_value`, or make
the persistence serializer reject non-JSON types rather than `default=str`.

---

## SEC-5 — RAG `_redact_metadata` skips URL-credential redaction (P3)

**Location (HEAD):** `machina/rag/pipeline.py:56-99` (`_redact_source` at `:56`;
`_redact_metadata` at `:77`), call sites `:304-312` / `:417`.

`_redact_source` chains `redact_secrets` + `redact_url_credentials` (R5-Nb).
`_redact_metadata` uses only `_default_redactor._redact_value` (the memory redactor), which
was verified **not** to redact generic URI userinfo and not to redact credential query
params:

```
>>> from machina.memory.redaction import redact_secrets as r
>>> r("https://user:pass@h/p").text        # unchanged
>>> r("https://h/p?api_key=short1").text   # unchanged
```

A credential-bearing URL placed in document/chunk **metadata** (rather than `source`) is
therefore persisted to `context_chunks` and emitted unredacted, contradicting the R5-Nb
docstring ("source metadata … carries zero credentials").

**Falsification:** the built-in `FileLoader` metadata is
`{filename, extension, size_bytes}` (`machina/rag/loaders.py:198-213,236-241`) — no URLs —
so the built-in path is safe today; a custom/third-party loader, or a future web loader
that sets `metadata["url"]`, would trigger it. P3.

**Working tree:** open (`rag/pipeline.py` is not in the dirty set).

**Suggested fix:** route `_redact_metadata` string values through `_redact_source`.

---

## SEC-6 — `chatgpt_auth.logout` holds the store lock before invalidating (P3)

**Location (HEAD):** `machina/security/chatgpt_auth.py:401-417`; contrast
`machina/security/store_fence.py:19-23,223-244` (`fenced_logout`, used by Copilot).

```python
def logout(*, path=None) -> bool:
    target = Path(path) if path is not None else auth_path()
    with _store_lock(target):           # bounded 45 s; blocks behind an in-flight refresh
        invalidate_credentials(target)  # bump
        target.unlink()                 # remove
```

`store_fence.fenced_logout` documents the deliberate opposite design: *"Deliberately does
NOT take `store_lock`: refreshes hold that lock across their token-endpoint round trip, so
blocking here would wedge a logout behind a slow (or hung) provider round trip."* The
ChatGPT module was not migrated to it.

**Attack sketch (fail-open sign-out):** another process holds `chatgpt.json.lock` across its
token round trip. The user's `machina auth logout` blocks, then `_store_lock` raises
`chatgpt_auth_lock_timeout` **before** `invalidate_credentials`, so the store survives and
the generation is not bumped — the credentials stay valid until that refresh completes. The
user sees an error (not a silent success), which caps severity.

**Why the guard misses it:** the R5-LM credential-race work centralised the mechanics in
`store_fence` and adopted `fenced_logout` for Copilot, but `chatgpt_auth.logout` keeps the
old lock-then-unlink ordering.

**Falsification:** `flock` is released automatically on holder death and
`STORE_LOCK_TIMEOUT_SECONDS` (45 s) exceeds the module's HTTP budget (≤30 s), so a *live*
wedged holder is required; logout normally acquires the lock. P3.

**Working tree:** open.

**Suggested fix:** have `chatgpt_auth.logout` call `store_fence.fenced_logout(target)`
(bump-then-unlink, no lock) for parity with Copilot.

---

## SEC-7 — Copilot store-lock timeout is untyped (P3)

**Location (HEAD):** `machina/security/copilot_auth.py:280` (`import_from_gh_cli`),
`:509` (`publish_device_token`), `:525-552` (`resolve_tokens`); `machina/security/store_fence.py:56,157-172`.

`chatgpt_auth._store_lock` translates `store_fence.StoreLockTimeout` into the module's typed
`ChatGPTAuthError(code="chatgpt_auth_lock_timeout")`. Copilot calls
`store_fence.store_lock(...)` directly, so a timeout surfaces as a bare
`StoreLockTimeout(TimeoutError)` out of `import_from_gh_cli` / `publish_device_token` and
propagates through `resolve_tokens` (`copilot_auth.py:539`), violating its documented
"Raises `CopilotAuthError`" contract.

**Why it matters:** callers that branch on `CopilotAuthError`/stable `code` fall into the
generic exception path. Not a confidentiality/integrity break → P3; flagged because it is an
error-path inconsistency introduced by the same refactor that typed the ChatGPT timeout.

**Working tree:** open (the dirty `copilot_auth.py` change is about the device-poll interval,
not this).

**Suggested fix:** wrap both Copilot `store_lock` uses in a helper mapping `StoreLockTimeout`
→ `CopilotAuthError(code="copilot_auth_lock_timeout")`.

---

## SEC-8 — Caller kwargs can rewrite the audit record identity (P3)

**Location (HEAD):** `machina/core/provider/audit.py:210-215`.

```python
sanitized_kwargs = _sanitize_value(metadata(kwargs))
entry = {
    "timestamp": time.time(),
    "event": event_type,
    **sanitized_kwargs,          # a caller kwarg named "event"/"timestamp" wins
}
```

`metadata()` filters `protected` names and `_content`/`_preview`/`_payload` suffixes, but
not `event` / `timestamp`. `audit_log("real_event", event="spoofed", timestamp=0)` produces
`{"event": "spoofed", "timestamp": 0, …}` — the durable row's event name and time are
attacker-influenced (audit spoofing / repudiation, A09).

**Falsification:** no current call site forwards an arbitrary dict via `**kwargs` into
`audit_log`; all observed callers pass literal keyword names. Latent. P3.

**Working tree:** fixed — reserved keys are filtered before the authoritative values are set
(`reserved = {k: v … if k not in ("event", "timestamp")}`).

---

## ADJ-1 (adjacent, pre-existing) — Untrusted `slow_down.interval` sets an unbounded sleep

**Location (HEAD):** `machina/security/copilot_auth.py` `device_flow_poll` — on
`error == "slow_down"`, `wait = float(server_interval)` from the token-endpoint response,
and the loop then `sleep(wait)`. `slow_down` is otherwise an *unchanged* pre-round function,
so this is **not** introduced by this round; it is flagged because it sits inside the
credential store reviewed here.

**Attack sketch:** a hostile/compromised token endpoint (or a trusted MITM) returns
`{"error": "slow_down", "interval": 1e9}`; the poll sleeps ~31 years, far past the
`DEFAULT_DEVICE_TIMEOUT` (300 s) contract.

**Falsification:** the endpoint is `https://github.com` over TLS with certificate verification
(`_client_or_new`), so this needs GitHub itself or a TLS-trust compromise; also
`DEVICE_POLL_MAX_WAIT` is unnecessary if one trusts GitHub. P3.

**Working tree:** fixed — `DEVICE_POLL_MAX_WAIT = 60.0` plus
`wait = min(wait, DEVICE_POLL_MAX_WAIT, max(0.0, deadline - now))`.

---

## Refuted attempts

Each entry: the bypass attempted and why it fails. (Where the cited file is in the dirty set,
the refutation was performed against the *newer* working-tree code, which is a superset of
the HEAD guards; the HEAD guards named are the ones from `git diff cdc5232..HEAD`.)

- **R1 — CRLF/header injection via `pinned_fetch`.** Request line from `parsed.path`/`query`,
  `Host` from `parsed.hostname` (`security/fetch.py:230-255`). Tried
  `http://host/path?a=1\r\nX-Injected: 1` and `http://hos\nt/p`. **Refuted:** CPython's
  `urlsplit` strips ASCII newline/tab (query became `a=1X-Injected: 1`); `.encode("ascii")`
  rejects residual non-ASCII. No request splitting.
- **R2 — SSRF / DNS rebinding / host-authority confusion in `pinned_fetch`.** **Refuted:**
  `_bounded_resolve` runs once; **every** returned address is checked against
  private/loopback/reserved/link-local/multicast/unspecified (`fetch.py:232-234`) before
  connecting to `ips[0]`; TLS verifies `server_hostname=hostname`; redirects re-validate
  scheme/host/IP; userinfo never reaches the `Host` header.
- **R3 — `security/providers.py` plaintext-http guard bypass with leading whitespace.**
  Tried `" http://evil"` so `urlparse` sees no scheme while `fetch_models` strips and sends
  the bearer in cleartext. **Refuted:** `urlparse` strips leading/embedded whitespace
  (`scheme='http'`), and `_validate_base_url` `.strip()`s first. (`is_loopback_base_url`
  treating `0.0.0.0` as loopback is defensible — connecting to `0.0.0.0` reaches the local
  host on supported platforms.)
- **R4 — credential-store resurrection via `store_fence` TOCTOU.** Tried: logout bumps the
  generation while a refresh in flight saves *after* the unlink. **Refuted:**
  `fenced_logout` bumps **before** unlink; `publish_device_token` / `import_from_gh_cli`
  re-check the generation **after** `save_tokens` and unlink their own write if it changed;
  a store removed without a bump is detected via `store_fingerprint`
  (`was_present and not now_present`); `refresh` with caller-supplied tokens refuses when the
  store is gone (`chatgpt_auth_signed_out`). No ordering resurrects revoked credentials.
- **R5 — `tools/file.py` atomic/anchored write symlink + hardlink + TOCTOU escape.** Tried:
  swap a workspace subdirectory to a symlink between validation and write; write through an
  inside name that hard-links `/etc/…`. **Refuted:** the inside-base path uses
  `_open_parent_dir_fd` (binds the validated parent dir dev/ino), `O_NOFOLLOW`,
  `_verify_fd_at` (re-stat under the dir fd), an explicit `st_nlink > 1` refusal, and
  `os.rename(src_dir_fd=…, dst_dir_fd=…)`; `rename` replaces a symlink destination rather
  than following it, so no write escapes.
- **R6 — `head_to_head` fixture symlink escape / grader integrity.** Tried: a fixture link
  resolving to the pristine original (or outside) so an edit lands elsewhere while the link
  path is hashed. **Refuted:** `assert_no_escaping_symlinks` runs on the fixture and again on
  the copied run dir (discarding the copy on failure); `escaping_symlinks` uses `realpath`
  (absolute, `..`, nested, dangling links); the graded run is pinned with
  `-c`/`--noconftest`/`--override-ini=addopts=`/`-p no:cacheprovider`; agent-created files
  are outside the editable allowlist (fail closed).
- **R7 — MCP child-cancel orphan.** Tried: cancel the `machina_chat` coroutine so the child
  keeps mutating files after the request returns. **Refuted:** the coroutine runs the child
  on a worker bound to a `cancel_event`; cancellation sets the handle and `run_bounded`'s
  supervisor kills the process group; the handler joins the worker with a bounded, shielded
  wait before `CancelledError` propagates (`child_process.py:317-341`,
  `mcp_server/__init__.py:573-586`).
- **R8 — installer command injection through the install path.** Tried a path with `$`,
  backtick, `"`, `'`, `\` landing in the shim / shell-rc line / PowerShell command.
  **Refuted:** `sh_dquote_escape` (escapes `\ " $ \``), `fish_quote` (single-quotes and
  escapes `\`/`'`; plain paths stay bare so rc idempotency holds), and `ps_squote_escape`
  (doubles `'`) apply the escaping for the exact quoting layer each value lands in
  (`install.sh:441-560`).
- **R9 — agent self-approval.** Tried an agent principal resolving its own approval.
  **Refuted:** the broker denies when `principal == req.agent_id` or `principal` starts with
  `agent:` (`permissions/broker.py:187-191`); the store independently refuses
  `_agent_principal(principal)` and `principal == row["agent_id"]`
  (`permissions/store.py:166-167,561-562,580-581,602-603`).
- **R10 — permissions command-calibration escape.** Tried the calibrated flags / attached
  option forms: `cp -t/etc payload`, `cp -t /etc payload`, `cp -r /etc .`, `ls -C /etc`,
  `cat /etc/passwd`, `mkdir /tmp/x`, `grep -f /etc/passwd x`. **Refuted:** all return
  `False` from `routine_project_command`. `find . -name x`, `rg TOKEN .`, `grep -R x .` are
  routine at the command layer but gated by `opaque_execution_review` via
  `_recursive_traversal` (verified end-to-end through `_sensitive_capabilities`).
- **R11 — redaction marker rewriting in the R4-016 re-scan.** The first `_apply_patterns`
  pass is not marker-guarded, so re-redacting an already-redacted string could in theory
  corrupt a marker. **Mostly refuted:** markers are `[REDACTED_…]`; every value class
  requires alphanumerics/URL-safe bytes and the `[`/`]` delimiters terminate the match, so
  markers are stable (verified `_redact_public_text` idempotence on produced markers).
  Residual: a future pattern whose value class includes `[`/`]` breaks this.
- **R12 — SQL injection in new/changed store queries.** **Refuted:** grant/approval reads and
  writes bind parameters (`$1`…`$10`); the lone f-string is a fixed column list
  (annotated `nosec B608`); UUIDs/digests are bound.

## Residual notes (not findings)

- `_is_sensitive_audit_key` does not treat `DATABASE_URL` (or any `*_URL`) as sensitive by
  **name**; safety leans entirely on value redaction, which is where SEC-2/SEC-3 live.
  Consider a `url`/`dsn` key heuristic.
- `machina_session_create` (`mcp_server/__init__.py:645-658`) validates `title` by type only
  (no length bound) — resource exhaustion, P3, outside the round's declared surfaces.
- `safe_logging` fails the whole record closed on a redactor exception and masks containers
  past depth 8 (`observability/safe_logging.py:6-80`) — reviewed, no bypass found.

## Reproduce

```sh
cd /Users/shadow/machina
.venv/bin/python - <<'PY'
from machina.core.provider.audit import _sanitize_value as s
print(s("https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc123def456"))  # SEC-2: unredacted
print(s("redis://:secretpw@host:6379"))                                    # SEC-3: unredacted
import json; print(json.dumps(s({"x": object()}), default=str))            # SEC-4: object str passes
PY
```
