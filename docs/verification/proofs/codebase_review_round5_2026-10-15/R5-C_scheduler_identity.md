# R5-C · Scheduler agent identity (finding 7, P2) — evidence

## Finding

`JobRunner._build_agent` passed the owned session unchanged to
`build_agent_for_session`, which selects `session.agent_id`
(`machina/core/agent_factory.py:102`). `job.agent_name` was used only in
the session-unresolvable fallback (`machina/core/scheduler.py:1099`).
A job configured for a restricted specialist (e.g. `researcher`) on a
harness-owned session therefore executed the **harness** definition/tools
while job metadata reported the specialist.

## RED (reproduced, not refuted)

New test file `tests/test_r5_007_scheduler_identity.py` (sole new file).
Pre-fix run: 2 failed / 1 passed (unit) —

- `test_job_specialist_identity_honored_on_owned_session`:
  `AssertionError: assert 'harness' == 'researcher'` (real
  `build_agent_for_session`, only the network/provider edge stubbed).
- `test_job_identity_keeps_session_authority_caps`: same root cause.
- `test_matching_identity_unchanged` passed (no-change guard).

Fix reverted temporarily to check the E2E too: all 3 identity tests
(including `test_e2e_scheduler_tick_runs_job_agent_identity`) fail
pre-fix, pass post-fix. Full log:
`logs/R5-C_pytest.txt` (post-fix: 4 passed).

## Fix (GREEN)

`machina/core/scheduler.py::_build_agent` (+17 lines, no other prod file
touched — `agent_factory.py` needed no change; its contract "session
identity governs" is correct for chat/REST/delegation, the scheduler was
the only caller dropping the job identity):

- After the fresh session read, when `agent_name != session.agent_id`,
  pass `dataclasses.replace(session, agent_id=agent_name)` to the shared
  factory. Only the identity is replaced.
- Retained session authority: `delegation_authority` caps are still
  intersected inside the factory (verified: `researcher` ∩
  `{"tools": ["read_file","write_file"]}` → `["read_file"]`),
  `execution_mode` and model/provider settings stay the session's,
  unknown specialists still fail closed (`AgentDefinitionError`).
- Matching identity → session passed unchanged (zero behavior change).
- Fallback (unresolvable session) unchanged.

Decision per plan (documented): **honor-with-restrictions**, no
creation-time rejection of job/session pairs and no `agent_override`
plumbing in the shared factory (smallest safe change; job identity is
authoritative for the *definition*, the session contributes authority
*caps* + model/provider defaults). Note the reverse direction is
intentional: a `harness` job on a specialist-owned session runs harness —
the job explicitly selects the definition; caps still apply.

## Verification

- `tests/test_r5_007_scheduler_identity.py`: 4 passed (3 unit no-DB +
  1 E2E on self-managed `isolated_schema` private schema against
  `machina_test`: real claim → turn → build → run → finish; only the LLM
  call `ReActAgent.run` stubbed, which is the allowed network/provider
  boundary).
- Regressions, all green:
  `test_scheduler.py`, `test_agent_factory_memory.py`,
  `test_review_scheduler_clock.py`, `test_scheduler_model_pin.py`,
  `test_scheduler_usage.py` (24 passed);
  `test_scheduler_coverage.py`, `test_scheduler_scripts.py`,
  `test_deep_scheduler.py`, `test_fm4_scheduler.py` (61 passed).
- Behavior/API change: scheduled runs now execute the job's named agent
  definition on owned sessions (the reported bug); nothing else changes.
  No commit (coordinator commits).

## Files changed

- `machina/core/scheduler.py` (+17)
- `tests/test_r5_007_scheduler_identity.py` (new)
- evidence only: this file + `logs/R5-C_pytest.txt`
