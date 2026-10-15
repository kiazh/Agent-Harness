# First-party skills extension — verification

Date: 2026-10-15
Lane: LANE SKILLS (sole writer of `skills/<name>/` new dirs)
Scope: added **13 new first-party skills** to Machina's own catalog
(`skills/<name>/SKILL.md` only). No `skills/ecc/**`, no `machina/bundled_agents/**`,
no `machina/core`, `ui/`, or gateway files were touched.

## Added skills

All names are the directory name and the frontmatter `name`; every file has a
one-line `description` (string), a lowercase `triggers` list of 4–8 entries,
`version: 1.0.0`, and a terse body ("Core Skills" table + sections). 41–46 lines
each.

| Skill | Description | Triggers | Lines |
|---|---|---|---|
| `observability-otel` | Observability with OpenTelemetry — traces, spans, metrics, logs correlation, context propagation, sampling, SLOs, dashboards | observability, opentelemetry, otel, tracing, spans, metrics, dashboards, slo | 44 |
| `feature-flags` | Feature flags & progressive delivery — kill switches, staged rollout, targeting rules, flag lifecycle, experimentation, safe defaults | feature flag, feature flags, progressive rollout, staged rollout, kill switch, unleash, targeting rule | 42 |
| `chaos-engineering` | Chaos engineering — fault injection, resilience testing, game days, blast radius control, steady-state hypotheses, rollback | chaos, chaos engineering, fault injection, failure injection, game day, blast radius, resilience | 42 |
| `supply-chain-security` | Software supply chain security — SBOMs, dependency provenance, artifact signing, SLSA levels, pinned versions, reproducible builds | supply chain, sbom, slsa, artifact signing, dependency provenance, sigstore, reproducible build | 43 |
| `prompt-injection-defense` | Prompt injection defense — trust boundaries, instruction hierarchy, indirect injection via tool output, input normalization, output handling, defense in depth | prompt injection, injection defense, untrusted content, indirect injection, input hardening, untrusted instructions | 46 |
| `grpc-services` | gRPC services — protobuf schemas, service definitions, streaming RPCs, deadlines, interceptors, error codes, grpc-web | grpc, protobuf, protocol buffers, protoc, streaming rpc, grpc-web | 41 |
| `kubernetes-ops` | Kubernetes operations — deployments, probes, resource requests and limits, autoscaling, namespaces, rollouts, operators, troubleshooting | kubernetes, k8s, kubectl, helm, pod, namespace, hpa, operator | 44 |
| `terraform-iac` | Terraform & infrastructure as code — modules, state management, plan review, drift detection, workspaces, remote backends, providers | terraform, infrastructure as code, iac, hcl, terragrunt, pulumi, state file | 42 |
| `redis-caching` | Redis & caching — data structures, cache invalidation, TTLs, eviction policy, stampede protection, distributed locks, pub/sub | redis, cache, caching, cache invalidation, ttl, cache stampede, memoization | 43 |
| `data-visualization` | Data visualization — chart selection, encoding, color and accessibility, matplotlib, dashboards, annotation, honest axes | visualization, chart, plot, dataviz, matplotlib, dashboard, d3 | 43 |
| `background-jobs` | Background jobs & task queues — workers, scheduling, retries with backoff, idempotency, dead-letter queues, at-least-once delivery | background jobs, task queue, job queue, celery, cron, worker, retry | 43 |
| `rate-limiting` | Rate limiting & throttling — token bucket, leaky bucket, sliding window, quotas, backpressure, retry-after, fair sharing | rate limit, rate limiting, throttling, token bucket, quota, backpressure, leaky bucket | 43 |
| `incident-response` | Incident response — severity levels, on-call triage, mitigation before diagnosis, communication cadence, blameless postmortems, runbooks | incident, on-call, postmortem, runbook, sev, outage, escalation | 43 |

Topic selection deliberately avoids the 18 (now 21) existing first-party skills
and the pinned ECC specialties (`api-design`, `docker-patterns`,
`database-migrations`, `eval-harness`, `mcp-*`, `security-review`, …).

## Count pins

- The only pinned skill count is the **42 curated ECC skills**
  (`tests/test_ecc_skills.py::test_curated_ecc_skills_are_mandatory_even_with_local_override`
  → `assert len(bundled) == 42`). It is preserved (verified below).
- **No first-party count pin exists.** `grep` over `tests/` and `docs/` for
  `first-party`, `len(bundled)`, `list_by_source`, `== 42`, `42/88`, and skill
  enumerations found no assertion on the first-party total, so no pin needed
  updating. (The `42/88` references in `docs/HANDOFF.md` describe ECC only.)

## telemetry.json

`skills/telemetry.json` is runtime state, not a schema. No validator or tool
requires it to enumerate every skill:

- `machina/skills/registry.py` reads/writes it best-effort; a missing skill key
  is simply absent telemetry (`_load_telemetry`).
- Consumers are tests that build their own telemetry fixtures in tmp dirs
  (`tests/test_fm6_telemetry.py`, `tests/test_code_quality.py`,
  `tests/test_skill_registry_cache.py`); `tools/check_release_artifacts.py` only
  asserts it is excluded from wheels; `tests/test_final_pass_fixes.py` only
  asserts it is dockerignored.

Therefore **no telemetry sync was required** and `skills/telemetry.json` is left
unchanged (`git diff skills/telemetry.json` is empty after the full test run).

## Verification

Evidence logs: `logs/skill_load_check.log`, `logs/skill_suites.log`, `logs/ruff.log`.

```text
# 1. Registry load: all 13 new skills parse, load enabled, ecc still 42
.venv/bin/python  (see logs/skill_load_check.log)
  new skills: 13   (each name==dir, version 1.0.0, 4-8 lowercase triggers)
  ecc loaded: 42 (pinned 42)
  OK: all 13 new skills parse, load enabled, ecc still 42

# 2. Primary skill suites
.venv/bin/python -m pytest tests/test_skill_runtime.py tests/test_skill_registry_cache.py \
    tests/test_ecc_skills.py tests/test_fm6_skill_read.py \
    tests/test_fm6_registry_hardening.py tests/test_skill_workflows.py -q
  => 230 passed

# 3. Supporting skill suites
.venv/bin/python -m pytest tests/test_fm6_scanner.py tests/test_fm6_triggers.py \
    tests/test_fm6_workflow_stages.py tests/test_code_quality.py tests/test_fm6_telemetry.py -q
  => 72 passed

# 4. Atomicity / ECC agents / vendor / comprehensive skill subset
.venv/bin/python -m pytest tests/test_r5_019_skill_atomicity.py tests/test_ecc_agents.py \
    tests/test_vendor_ecc.py -q
  => 15 passed
.venv/bin/python -m pytest tests/test_comprehensive.py -k skill -q
  => 51 passed, 109 deselected

# 5. Ruff (no Python was modified; run for completeness)
.venv/bin/ruff check machina/skills tests/test_skill_runtime.py ... -> All checks passed!
.venv/bin/ruff format --check machina/skills                        -> 8 files already formatted
```

## Notes / pre-existing conditions

- `skills/cost-optimization/SKILL.md` and `skills/prompt-engineering/SKILL.md`
  are **skipped at load with `ValueError`** (they contain the literal phrase
  "system prompt", which trips `SkillParser._scan_injection`). This is
  pre-existing and already documented in `docs/ECC_DEBLOAT_REVIEW.md` and
  `docs/OPENCODE_CONTINUATION.md`. Their files are unmodified by this lane, and
  the scanner was **not** weakened. The new `prompt-injection-defense` skill
  deliberately avoids that phrase so it loads cleanly.
- No commits were made.
