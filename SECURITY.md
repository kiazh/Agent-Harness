# Security model

AgentHarness executes agent-proposed actions on a host controlled by its operator.
The default `ask` policy and approvals restrict agent authority. Host execution
does not isolate Python, plugins, administrators, or a compromised machine.
Do not expose a gateway, database, or Docker socket to untrusted clients.

## Trust boundaries

| Boundary | Authority and enforcement | Regression evidence |
|---|---|---|
| Human → HTTP/gateway | HTTP bearer key; local gateway token. Agent principals cannot resolve human approvals. A gateway without `AH_GATEWAY_TOKEN` is explicitly unauthenticated and belongs only on trusted local stdio. | `test_api.py`, `test_approvals_ownership.py` |
| Agent → tools | Broker evaluates declared effects and exact action; tools execute a stamped mode/backend/workspace snapshot. Durable mode reads fail closed on database failure. | `test_execution_context_boundary.py`, `test_integration_permissions.py` |
| Tool → filesystem | Canonical workspace boundary, private path restrictions, symlink/parent traversal and hard-link defenses, identity checks on file operations. | `test_security.py`, `test_audit_remediation.py` |
| Tool → process | Structured argv and guarded command grammar. Opaque scripts/build tools require approval; shell payloads are distinct approval material. Child environment uses an allowlist. | `test_terminal_safety.py`, `test_scheduler_scripts.py`, `test_integration_permissions.py` |
| Tool → external web | DNS/IP validation at every redirect; connect to the validated address with correct Host/TLS identity. Aggregate deadlines and byte/header bounds apply to extraction. | `test_web_extract_pinned_fetch.py`, `test_deep_fetch_deadline.py` |
| Workers → PostgreSQL | Conditional owner-token claims, database clock, lease renewal, token-matched release/finish. Unknown ownership blocks effects and mutations. | `test_multi_worker_fencing.py`, `test_rest_ownership_loss.py`, `test_claim_failure_boundary.py` |
| Runtime → providers | Provider receives prompts and its own credentials. Owned clients close with bounded observation; failure metrics and sanitized diagnostics remain visible. | `test_provider_close_visibility.py`, `test_usage.py` |
| Runtime → diagnostics | Audit payload filtering, known credential/pattern redaction, exception class/location without raw error payload. Metrics labels contain operation names. | `test_observability_payload_boundary.py`, `test_observability_payload_boundary.py` |
| Runtime → Docker | Sandbox requests require Docker; unavailable Docker refuses execution. No automatic host fallback. Container isolation depends on daemon, image, mount, and network policy. | `test_terminal_safety.py`, `test_terminal_safety.py` |
| Runtime → plugin | In-process Python plugin shares host privileges. Install only trusted code; tool declarations cannot isolate malicious plugin code. | Trust limitation; no plugin isolation claim. |

## Modes and privileged operations

New sessions snapshot the configured default (normally `ask`) into PostgreSQL.
Changing the global default affects future sessions. Session changes atomically
update durable mode and revoke prior session grants. `full` activation issues an
explicit session grant; routing mode alone does not replace a grant. Revocation
returns the session to `ask`. Forks start in `ask`.

`workspace` allows policy-approved operations in the configured workspace.
`sandbox` selects Docker for terminal execution; other tools still have their
own boundaries. `full` permits explicitly granted host capabilities. Credential
access, privilege escalation, security settings, and destructive system actions
remain conflicting capabilities that require explicit review even in full mode.
Agent requests cannot grant themselves human authority or widen delegated
authority inherited from a parent.

## Exact approvals and execution

Action digest version 2 binds session, original turn, agent, tool, operation,
capabilities, canonical targets, argv/shell, cwd, backend, timeout, network
declarations, content digest, and workspace identity. Display previews are
redacted and bounded; full content length and digest preserve review identity.
Changing material action fields requires fresh approval. Legacy proposals that
lack the complete reviewed action cannot authorize a new execution.

Human resolution changes pending to approved/denied/cancelled/expired. Approval
is consumed by a conditional durable execution claim exactly once. HTTP resume
submits the original tool arguments, reconstructs and compares the proposal,
then acquires a fresh fenced turn and executes that tool through the shared tool
pipeline. It does not regenerate the approved action with an LLM. Session grants
are checked for scope, expiry, and revocation; replay and cross-session use fail.
Concurrent proposals read fresh durable mode before constructing their request.

## Script jobs: administrator-trusted dependencies

The script root must be outside the agent workspace. An administrator controls
`.script-policy.json`, which allowlists entry files and declared dependencies:

```json
{"version":1,"scripts":{"report.py":["helpers.py"]}}
```

Entries are bounded regular files with traversal, symlink, hard-link, and
identity checks. The reviewed entry bytes execute from a private snapshot.
Policy and declared dependency hashes are checked again before execution;
changes need fresh review. On POSIX, group/world-writable reviewed files fail.
Protect the root and its ancestors with operator-owned permissions or ACLs.

This is the plan's Option B. Dynamic imports, undeclared resources, interpreter,
installed packages, sourced code, and administrator changes after validation
remain trusted. Declared dependencies are not a complete immutable dependency
snapshot. Do not let agents or untrusted users write the root or those resources.

## Secrets and data

Secrets resolve through environment, mounted files, or an explicit Vault/AWS
backend. Config YAML excludes credential fields. Real environment variables
win over dotenv. Mounted file paths are explicit, bounded, and reject symlinks.
Dotenv writes validate names/content, reject newline injection, and replace the
file atomically; POSIX mode is `0600`, while Windows needs operator-managed ACLs.

Configured credential values are registered for exact outbound redaction,
including rotated values used by retiring providers. Recognized secret formats
are also redacted; streaming redaction holds partial credentials across deltas.
Tool transport payloads, memory/context paths, exports, and diagnostics apply
redaction. Child tools do not inherit provider credentials unless their explicitly
trusted execution environment authorizes a specific value. Database/provider
clients necessarily receive their own credentials. PostgreSQL stores user
content, embeddings, authorization metadata, and sanitized audit metadata;
protect it and its backups as sensitive application data.

Redaction does not identify every arbitrary unknown secret, encoded/obfuscated
credential, or intentionally transformed value. External providers receive
submitted prompts and tool results; their retention policy is outside this
runtime. Administrators can read configuration, process memory, and stored data.

## Cancellation and failure

Approval wait time is excluded from compute time; logical ownership continues
renewing during human waits without a long-held database transaction. Failed
renewal cancels owned execution. Terminal completion follows execution retirement
and confirmed release. Failed release emits `turn.cleanup_pending`, without a
terminal success or compaction, and leaves recovery to DB restoration/claim expiry.

Cancellation-resistant tasks remain strongly tracked; bounded shutdown cannot
guarantee their termination. They retain ownership while alive. HTTP can close
without a terminal event while retirement continues. Expiry and token fencing
stop stale workers from altering successor claims, but cannot undo an already
started external process or provider request. Arbitrary external effects are not
guaranteed exactly once across crashes. Stop a failed process before takeover
when uncontrolled external effects remain possible.

## Reporting and release limits

Report a suspected defect privately to the repository maintainer, with a minimal
reproducer and redacted metadata. Do not attach credentials, raw conversation
content, production dumps, or unrestricted audit logs.

Release evidence and remaining operational gates are recorded in
[the completion ledger](docs/plans/PRODUCTION_COMPLETION_PROGRESS.md).
A passing scanner does not establish isolation or production readiness.
