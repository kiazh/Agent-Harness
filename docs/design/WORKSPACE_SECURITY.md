# Workspace Threat Model (STRIDE) — Integrated Editor + LSP

Scope: the workspace feature surface — gateway workspace methods, file I/O adapter,
document mirrors, LSP subprocesses, explorer/Quick Open, agent code-intelligence access.
Written at T0/W1; T11 verifies the listed tests actually exist and pass.

## Assets

1. User source code on disk (integrity + confidentiality).
2. Unsaved editor buffers (UI authority; must never be silently replaced).
3. Gateway auth token and other secrets in the gateway process environment.
4. The user's authorization decisions (approvals, workspace trust).
5. Terminal integrity (no untrusted bytes as live control sequences).

## Trust boundaries

- UI process ↔ gateway: token-authenticated stdio JSON-RPC (existing).
- Gateway ↔ language server: Content-Length JSON-RPC over stdio; server runs with the
  user's OS privileges — **disclosed boundary**; protocol-level filtering does not
  sandbox a native process (plan §14).
- Gateway ↔ filesystem: authorized workspace root only; symlink/TOCTOU hazards.
- Model/agent ↔ workspace state: agent access requires explicit capability; view
  visibility grants nothing (plan §20).

## STRIDE findings and required controls

| # | Threat | Vector | Control (owner task) |
|---|---|---|---|
| S1 | Spoofed actor | Model supplies `params.actor="user"` | Actor derives from trusted invocation context only; UI authority = gateway token (T5, tested) |
| S2 | Spoofed workspace | Stale/foreign workspaceId reused | Server-side binding workspaceId↔client↔epoch; unknown ids → structured NOT_FOUND (T5) |
| T1 | Path traversal write/read | `..`, absolute, encoded separators in rel paths | realpath containment + fd-verified opens + O_NOFOLLOW walk (T5) |
| T2 | Symlink escape | File or parent dir symlinked outside root | Ancestor-symlink rejection; entries flagged; no following out of root (T5) |
| T3 | TOCTOU on save | Disk changed between read and replace | Expected-revision recheck immediately before commit; per-path coordinator; conflict result, never overwrite (T5) |
| T4 | Hardlink pivot | Target hardlinked to outside file | Refuse `st_nlink > 1` targets (T5) |
| T5 | Silent dirty-buffer overwrite | Agent write / reload while unsaved edits exist | Structured conflict or versioned staged proposal; never silent replace (T5 backend, T10 agent path, T9 staged edits) |
| T6 | Malformed LSP framing | Split frames, oversized headers, negative lengths | Byte-exact framing parser with bounds (16 KiB header / 8 MiB body), typed errors per-connection only (T7) |
| R1 | Unattributed edits | Multi-file rename without origin trace | Workspace edits staged with origin URI/version + preview before apply (T9) |
| I1 | Secret exfiltration via LSP env | Server inherits gateway token/cloud creds | `sanitized_env` allowlist; explicit test that MACHINA_GATEWAY_TOKEN is absent (T7) |
| I2 | Source disclosure to model | Opening a tab auto-sends content | No implicit sharing; explicit selection attachment with visible scope; capability-gated agent reads (T10) |
| I3 | Secret in logs/diagnostics | Server stderr, diagnostics, hover content | Bounded sanitized rings; display via `sanitizeDisplay`; redaction boundaries on agent results (T7, T10) |
| I4 | Terminal escape injection | Hostile filenames/diagnostics/hover markdown | All untrusted text through sanitizeDisplay / hasNoTerminalControls before render (T4/T6/T9, tests) |
| D1 | LSP stdout flood starves gateway | Noisy server | Separate pipes; bounded queues; gateway dispatch fairness (T7) |
| D2 | Restart storm | Crash loop | Bounded backoff 3/5min then manual restart (T7) |
| D3 | Huge file/listing DoS | Giant file, deep tree, wide dir | 1 MiB readable default, 200-entry pages, bounded recursion, no blocking key path (T5/T6) |
| E1 | Unauthorized code execution | LSP `executeCommand`, auto-install suggestions, auto-run project scripts | executeCommand disabled by default; no auto-install; no shell; server-initiated requests answered or refused explicitly (T7) |
| E2 | Unsolicited workspace edits | Server `workspace/applyEdit` | Preview + version validation + explicit consent; applied=true means model-applied, not disk-saved (T9) |
| E3 | Trust laundering | Repo-supplied binary/config auto-trusted | Explicit user trust decision; langservers resolved from validated absolute paths or explicit config; never auto-trust `node_modules` binaries (T7) |

## Residual risks (documented, not hidden)

- A language server runs as the user with ambient filesystem privileges unless an OS
  sandbox is available; protocol filtering is not a sandbox. Disclosed in setup docs.
- External writers that ignore the revision protocol can still race a save; detected
  revision changes reduce, not eliminate, the window.
- Redaction at result boundaries is heuristic; it is not proof that every secret class
  is detected.
- Multi-file disk savings are not globally atomic; per-file outcomes are reported.
