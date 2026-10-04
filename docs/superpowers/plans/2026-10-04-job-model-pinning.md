# Scheduled Job Model Pinning

**Goal:** A scheduled job can pin its provider and model so later global or
agent-definition changes do not silently change the backend used by the job.

**Design:** Persist optional `model` and `provider` on each job. The runner
passes those values to agent construction; job values take precedence over
agent-definition values. An omitted pin keeps existing behavior. Surface the
fields through the gateway, HTTP API, and terminal UI. Reject empty pins and
keep the current no-paid-fallback provider behavior.

**Verification:** First add failing database and runner tests, then migration
and implementation. Check legacy jobs, gateway/API inputs, the TypeScript UI,
and the full test suite. Deployment to the user's live Docker instance is a
separate operational step.
