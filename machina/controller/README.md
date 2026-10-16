# TurnController foundation (Stream B) — deterministic, no model calls

Pure-Python triage helpers. Nothing here is wired into the agent loop yet.

## Interface

- `machina.core.intent.classify_task(query) -> str` — regex/keyword task
  class: `conversational | explain | learn-directory | review-readonly |
  debug | implement | ambiguous`. Reuses and extends `conversational()`;
  bare follow-ons (`continue`, `just write the file`) return `ambiguous`
  instead of guessing.
- `machina.controller.triage.resolve_task(query, task_state=None, default="implement")`
  — resolves `ambiguous` against explicit caller state
  (`{"active_class": ...}`); `default=None` preserves `ambiguous`.
- `machina.controller.complexity.score_complexity(query, repo_stats=None, error_count=0)`
  — `{"level": direct|single|subagents, "reasons": [...], "score": int}`
  from static signals only (task-verb weights, named files, rediscovery /
  multi-file markers, repo size, error counts). Thresholds
  (`SCORE_DIRECT_MAX`, `SCORE_SUBAGENTS_MIN`, weights, caps) live in
  `complexity.py` — the single tuning point.
- `machina.controller.preselect.SkillPreselectCache(skills)` — per-turn
  cache over the shared canonical selector (`skills.ports.automatic_skills`,
  i.e. `search_skills` minus disabled/hard-excluded); identical normalized
  queries rank once, `invalidate()` clears. Metadata ranking only — skill
  bodies still load on demand, preserving progressive disclosure.

## Deferred wiring point (follow-up, not this stream)

`Agent._prepare_context` (`machina/core/agent.py`) is the intended entry:
triage → complexity → preselect would run where `automatic_skill_context`
is currently built, selecting turn depth before any model call. No import
or call from `agent.py` to this package exists yet, by design.
