# Deployment and recovery

The v1.0.0 gate requires code checks plus an identified staging deployment,
provider provisioning, monitoring/alert delivery, and a verified restore. Current
evidence is in [the completion ledger](docs/plans/PRODUCTION_COMPLETION_PROGRESS.md).
Use [SECURITY.md](SECURITY.md) for boundaries and limitations.

## Configuration contract

For normal settings: per-call/session override → `AGENT_HARNESS_<KEY>` → documented
legacy variable → YAML configuration → default. Secret settings use the same
overrides/environment precedence, followed by mounted `<ENV_NAME>_FILE`, then the
configured Vault/AWS backend. YAML credential fields are ignored; an explicitly
selected invalid mounted file fails rather than falling through to another key.
Dotenv fills absent environment values and never overrides real environment.
Backend credentials cache for 60 seconds; operator rotation invalidates that
cache, and new provider instances resolve current values. Existing requests may
finish with the credential they originally acquired.

Do not put credentials in YAML, command arguments, image layers, prompts, tool
arguments, or monitoring labels. Use process environment, ACL-protected mounted
files, Vault, or AWS Secrets Manager. `AH_ENV_FILE` selects the operator-managed
dotenv path. Secret updates validate/persist before activating; failed writes
preserve the previous live setting. Configure only trusted backend endpoints.

| Requirement | Setting/check |
|---|---|
| PostgreSQL 16+ | `DATABASE_URL` or `AGENT_HARNESS_DATABASE_URL`; dedicated application role; encrypt remote transport and limit network access. |
| Extensions | `vector`, `pg_trgm`; install with an administrative role before using a restricted runtime role. |
| Test isolation | `AGENT_HARNESS_TEST_DATABASE_URL` must name a separate test database. Never point it at production. |
| Provider | `AGENT_HARNESS_PROVIDER`, `AGENT_HARNESS_MODEL`, matching provider key, or operator-configured Ollama service. |
| HTTP authorization | `AGENT_HARNESS_API_KEY`; terminate TLS at a trusted edge. `/health` and `/ready` are public; `/metrics` requires authentication. |
| Local gateway | `AH_GATEWAY_TOKEN`; TUI launcher provisions its own token. Do not publish unauthenticated stdio transport. |
| Provenance | `AGENT_HARNESS_PROVENANCE_KEY`, distinct from API/provider keys. |
| Workspace | `AGENT_HARNESS_WORKSPACE_ROOT`; provision separate trusted script root with `AGENT_HARNESS_SCRIPTS_DIR`. |
| Docker sandbox | Working daemon and built `Dockerfile.sandbox` image; inspect approved mounts/network policy. Absence refuses sandbox execution. |
| Search | Optional operator-controlled `SEARXNG_URL`; internet fetching still enforces per-hop address/deadline restrictions. |
| HTTP rate limit | `AGENT_HARNESS_HTTP_RATE_LIMIT`, default 120 requests/minute per transport peer per worker; 0 disables. Enforce a deployment-wide limit at the trusted edge. |
| Usage budgets | Session/agent token/request limits under `AGENT_HARNESS_USAGE_*`; defaults 0 (unlimited). Set explicit production budgets. |
| Timeouts | `AGENT_HARNESS_TURN_TIMEOUT` 300 seconds compute; `AGENT_HARNESS_APPROVAL_TIMEOUT` 300 seconds wait. Both validate 1–3600. |
| Ownership | Turn TTL 360 seconds; renew every 30 seconds. Mutation TTL 120 seconds. Scheduler lease 300 seconds, periodically renewed. |

## Install, upgrade, and rollback

Use a pinned release artifact and locked UI dependencies. Python 3.11+ and Node
22.19+ are required for the current TUI. A non-editable Python wheel supplies the
Python runtime and built-in skills; set `AH_UI_DIR` to a matching checkout's `ui/`
and run `npm ci --ignore-scripts --prefix ui` for the TUI. Keep the UI and backend
from the same release; the TUI negotiates protocol v2, while legacy clients that
omit `protocolVersion` retain v1. See [the wire contract](communications/ui-gateway-protocol.md).

Before upgrade: stop accepting work; drain or stop active workers; capture a
consistent backup and record artifact/config versions. Run `ah init` using an
operator migration role. Schema application is an atomic transaction and can be
repeated. Legacy modes migrate to `ask`; execution claims remain separate from
session business status. Version-1 action approvals require fresh review after
the digest/workspace change. Preserve provider/backend configuration separately.

Verify `/health`, `/ready`, authenticated `/metrics`, TUI and HTTP chat, approval
resume, memory/RAG, interval/heartbeat/cron/script jobs, and cancellation before
allowing production traffic. Exercise a competing worker and stale-token finish.
Confirm no sensitive payload appears in exported logs or audit records.

Rollback means stop/drain the new runtime, restore the verified database backup
into a replacement database, reinstall the prior pinned runtime/UI artifacts,
and restore the matching configuration. Do not attempt a destructive in-place
reverse migration. Never run old and new workers against an unverified schema.
Keep the failed database read-only for diagnosis until retention policy permits
disposal. Recheck readiness and representative flows before switching traffic.

## Backup and restore

Use PostgreSQL `pg_dump -Fc` with a client compatible with the server. Set
`PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE` and an ACL-protected password file or
secret-provisioned `PGPASSWORD`; omit credentials from command arguments/logs.
Back up the whole application database, not only transcript tables. Store dumps
encrypted off-host with access auditing and checksums. Back up non-secret config,
trusted scripts/policy, deployment manifests, and references to separately
provisioned secrets. Never copy provider keys into a dump or release artifact.

For each release, restore into an isolated database with both extensions and
matching roles using `pg_restore --exit-on-error --single-transaction`. Compare
session, context, memory, approval, job, and usage counts plus representative
record digests. Re-run initialization and smoke tests on the restored database.
Verify that no stale claimed job or turn resumes automatically without fencing
and review. Record backup checksum, restore duration, tested application version,
and recovery point/time in the deployment record. A dump command succeeding is
insufficient evidence until the restore and application checks pass.

The operator must choose backup frequency, retention, RPO/RTO, encryption keys,
and an off-host destination. This repository does not silently choose a
production retention policy or claim an external backup has been verified.

## Monitoring, audit, and retention

Probe `/health` for liveness and `/ready` for DB/schema availability. Readiness
does not prove that a provider, Docker daemon, or external search service works.
Scrape authenticated `/metrics` with a secret file or protected bearer credential.
The application exposes operation counts/latencies and durable usage aggregates.
Provider, approval, cleanup, claim-loss, RAG, and scheduler diagnostics use bounded
operation names, IDs and counts; never put conversation content in labels.

Alert on unavailable readiness, repeated provider errors/429s, claim loss,
cleanup timeout/late failure, audit persistence failures/dropped entries, budget
exhaustion, scheduler lease loss, and failed jobs. For example, the exported
`ah_operations_total{operation="audit.persistence.failed"}` counter should
produce an alert when its increase over five minutes is nonzero. First verify
the selected metric labels against a staging scrape. Send a controlled test
failure through the configured alert path and confirm delivery and recovery.

Gateway logs rotate at 5 MiB with three backups per gateway log file (about
20 MiB including the current file). Multiple processes writing the same
rotating file need an operator-managed collector or separate process log paths.
Protect `~/.agent-harness/logs`; exception messages are scrubbed, and audit events
exclude raw prompts/completions/tool payloads. The audit writer is best effort:
queue overflow and failed persistence have metrics, not a durable replay queue.
Set PostgreSQL audit retention and Prometheus retention outside the application;
include backups in the same policy. Restrict log readers and verify redaction
with synthetic credentials before enabling external log shipping.

## Incident recovery

On DB failure, stop new effects and investigate connectivity/readiness. Failed
claim renewal cancels work; failed release leaves `turn.cleanup_pending` rather
than a success event. Restore DB access and inspect owner/expiry metadata before
takeover. Stop any still-running failed worker first when it may own uncontrolled
external effects. Do not manually clear another live owner's claim.

For a hung provider or task, use progress stages to distinguish waiting for
provider, running tool, joining optional work, closing clients, and releasing
ownership. Bounded observation retains resistant work; shutdown cannot guarantee
termination. Stop the process/container when necessary, then let leases expire.
Inspect script children/containers before restarting jobs. Resume an approved
HTTP action using its request/session/original-turn binding; never replay a
consumed approval or regenerate a changed action under the old approval.

## Release record

Record artifact hashes, supported platform test results, schema upgrade/rollback
evidence, real gateway/UI checks, provider provisioning, sandbox availability,
staging smoke results, alert delivery, log redaction/retention, and actual backup
restore evidence. Release `v1.0.0` only when every required gate is verified.
