# Sign-in surface — CLI + gateway + TUI live proof (2026-10-14)

**Goal.** Give the ChatGPT-account provider a user-facing sign-in surface —
`machina auth login|status|logout`, the `auth.chatgpt.*` gateway control
plane, and the TUI `/auth` command — so users pick "sign in with ChatGPT"
instead of pasting an API key.

**Verdict.** PASS — the CLI surface was exercised live: first read-only
against this machine's real home, then fully sandboxed. `auth status` reports
both states (`source: codex` for a Codex-only sandbox, `source: machina` after
import), `login --import-codex` copies the Codex tokens into the sandboxed
Machina auth path (0600) while the Codex file stays byte-identical, the login
command prints the real authorization URL and times out cleanly (no exchange),
and `logout` removes ONLY the Machina store (Codex file untouched; status
falls back to `source: codex`). Runner exit 0; all 11 checks pass.

- Repo: `/Users/shadow/machina` (lane worktree, uncommitted)
- Runner:
  `.venv/bin/python docs/verification/proofs/chatgpt_auth_2026-10-14/run_signin_surface_proof.py`
- Artifacts: [`auth_status.txt`](auth_status.txt) ·
  [`auth_login_url.txt`](auth_login_url.txt) ·
  [`auth_import_logout.txt`](auth_import_logout.txt)

## What the runner proves (from the artifacts)

| step | command | observed |
| --- | --- | --- |
| help surface | `machina auth --help` | exit 0; `login`, `status`, `logout` listed |
| real home, read-only | `machina auth status [--json]` | exit 0; `signed_in: true, source: machina` (the provider lane's store; nothing written by the peek) |
| codex-only sandbox | `machina auth status --json` | exit 0; `signed_in: true, source: codex`, account `fa769bce-…` — the **signed-in-via-codex** state |
| import | `machina auth login --import-codex` | exit 0; sandboxed store written 0600; Codex file sha256 identical before/after |
| after import | `machina auth status [--json]` | exit 0; `source: machina`, same account, expiry `2026-10-23 10:23 UTC` |
| URL generation | `machina auth login --no-browser --timeout 0.3` | exit 1 (clean timeout); the real `auth.openai.com/oauth/authorize` URL printed, no exchange |
| logout | `machina auth logout` | exit 0; sandboxed store removed; Codex sha256 identical; `source: codex` again |

The sandbox used a disposable `HOME`, a `CODEX_HOME` copy of the real
`~/.codex/auth.json`, and `MACHINA_CHATGPT_AUTH_FILE` pointed at the sandbox;
the temp tree was removed after the run. The real `~/.machina` and
`~/.codex` files were only read.

## Manual step (not attempted in automation)

A real sign-in requires a human browser: `machina auth login` (or `/auth
sign-in` in the TUI, or the `auth.chatgpt.login` RPC) opens the authorization
page while the localhost listener completes the PKCE exchange. Automation
stops at URL generation by design — no token exchange was attempted, no
browser was opened, and the printed URL's one-time `state`/`code_challenge`
values are already dead (the flow timed out; the `code_verifier` was never
printed).

## Redaction

No token material is present in any artifact. The runner reads the live
access/refresh/id token values only to scan every artifact for them
(`redaction scan: 0 token values found` in `auth_import_logout.txt`). Account
ids, expiries, paths, and the one-time PKCE URL values are evidence, not
credentials.

## Registration note (deferred line)

`machina/cli/__init__.py` is held by another lane; `machina/cli/auth.py` is
complete and the runner mounts it through the exact deferred lines:

```python
from machina.cli.auth import app as auth_app
...
app.add_typer(auth_app, name="auth")
```

The same runner output (`machina auth --help`) is the acceptance check for
that one-line registration once the orchestrator lands it.

## Tests backing this surface (hermetic)

- `tests/test_chatgpt_cli_auth.py` (13) — status `--json` shape/exit codes,
  logout scope (never touches the Codex file; honors the auth-file override),
  import + no-clobber, the login flow against a real loopback callback
  (monkeypatched exchange), and state-mismatch/timeout/port-conflict paths.
- `tests/test_gateway_auth_chatgpt.py` (9) — status payload, login returns the
  URL immediately (listener already bound) and emits `auth.chatgpt.completed`
  via the fake exchange, pending idempotency, timeout/state-mismatch/bind
  failures, logout scope, import status.
- `ui/test/auth.test.ts` (16) — the `/auth` status card, sign-in URL + event
  settle, import/sign-out messaging, aliases, completions, and the one-line
  `/provider` + `/keys` pointers.
