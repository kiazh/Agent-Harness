# Shell execution and model context limits

User report (2026-10-11): `echo "shell ok" && uname -s && date -u && pwd`
fails with “dangerous characters”; after a few calls the run reports a maximum
token budget. Both conflict with the requested risk-based, long-context harness.

Implement in place on main; no commits, pushes, operator config edits or
production database access. Verification uses disposable home/env isolation and
only agentharness_test. Follow diagnosis, TDD and verification skills.

## Shell execution

- Share one parser between permission mapping and terminal execution. Ordinary
  commands retain exact argv execution without a shell. POSIX shell grammar gets
  an explicit `/bin/sh -c` argv and exact shell payload in the approval identity.
  Do not use login/interactive shells or read shell startup files.
- Safely recognized sequential chains (`&&`, `;`, pipelines) whose every stage
  passes the existing routine-project policy may execute without new prompts.
  Expansions, redirects, backgrounding, compound syntax and unknown commands
  require explicit broker approval. Sensitive or out-of-workspace operands
  never inherit routine permission from a safe first stage.
- A broker-authorized command must execute the exact approved argv/cwd/backend/
  payload. Legacy direct calls retain their narrow restrictions. No approved
  command is rejected merely for shell characters or blocked command options.
  Existing sandbox isolation, sanitized environment, timeout/output limits and
  process-tree cancellation remain enforced.
- Regression first: exact reported chains, pipe/redirect/substitution,
  changed-after-approval identity, explicit-denial/no-launch, standalone literal
  metacharacters, sensitive later stages, approved options, sandbox command shape.

## Model context

- Remove the 50k ceiling and cumulative per-turn spend stop. Cumulative provider
  and delegated usage remains accounting; repeated prompt transmission does not
  consume a model's simultaneous context window.
- Resolve the selected model's actual context window, preferring cached provider
  metadata; use curated documented fallback only when metadata is unavailable.
  OpenRouter `/models` provides context_length. Unknown models have an honest
  conservative fallback; never assume every model accepts one million tokens.
- Assemble prompts and trigger compaction against the model window. Preserve the
  old context_budget field for compatibility, but do not use its 8k default as a
  hidden spend/prompt cap. Use the actual request's messages, tools and reserved
  output when checking context capacity in streamed and ordinary paths.
- Preserve explicit user-defined usage-ledger limits and provider output limits;
  context capacity and maximum generated output are separate API constraints.
- Regressions first: million-token provider window; legacy session default does
  not clip retrieval at 8k; cumulative usage beyond old caps continues with a
  small current request; true overflowing request stops before provider call;
  tool schemas/output reservation count; model switching/metadata offline fallback;
  delegation usage still attributed without an implicit parent spend cutoff.

## Completion

Run focused regression suites, full backend suite and native UI check; triage
failures against clean HEAD. Check Ruff/format, Bandit on changed permission/tool
scope and diff whitespace. Record behavior, commands, results, exact safety
boundaries and limitations in README and docs/HANDOFF.md. Review security and
context boundaries before reporting completion; no live provider spending needed.
