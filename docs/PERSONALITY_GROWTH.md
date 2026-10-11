# Personality growth and runtime self-improvement

Growth is enabled by default. The agent keeps its core system prompt/SoulSpec identity and gradually curates preferences, conversational style and verified lessons. These bounded notes belong to the agent, enter subsequent turns automatically, and survive restarts, new sessions and provider/model changes. The model saves them with `learn_personality` during ordinary tool use, without an extra reflection request or a permission prompt in the default risk policy. The general agent and built-in specialists support this; restrictive custom definitions must explicitly include the tool.

The design follows the separation between user preferences and reusable agent lessons in [Hermes memory](https://github.com/nousresearch/hermes-agent/blob/main/tools/memory_tool.py). This implementation uses our PostgreSQL, agent ownership, tools and permissions; no Hermes source was copied. Hermes's [license](https://github.com/nousresearch/hermes-agent/blob/main/LICENSE) is MIT.

## Use it

Set the personality you want with `/persona A warm, witty mentor who explains clearly and keeps answers short`. This saves `persona_description` in config and applies on the next turn, including existing sessions. Bare `/persona` opens the Add/Reset menu; `/persona --clear` restores the agent's default. The description steers tone and character while learned preferences and lessons grow within it. It applies globally to agents in this installation, independently of automatic memory/growth flags, and does not change core roles or tool permissions. Descriptions are limited to 2,000 characters and secret-looking text is redacted.

You can also use `ah config-set persona_description 'A warm, witty mentor' --persist`, the YAML setting `persona_description: "A warm, witty mentor"`, or `AGENT_HARNESS_PERSONA_DESCRIPTION`. Existing config precedence applies; an environment value overrides a saved setting.

Restart the harness after updating. Runtime startup applies only the additive growth-table migration; `ah init` also installs it on a fresh database. Migration failure reports growth unavailable and chat continues with ordinary retrieval. CLI growth commands apply the same migration before use.

In chat, share a durable preference, for example: “Remember that I prefer short explanations with one concrete example.” The model can curate a note. You can also teach one directly:

```text
/growth
/growth teach style verbosity Prefer short explanations with one concrete example
/growth teach lesson testing Run focused tests before reporting success
/growth feedback good
/growth feedback bad <episode-id>
/growth forget style:verbosity
/growth rollback
/growth rollback 0
```

Bare `/growth` immediately offers View progress, Give feedback, Teach, Forget and Restore. Teaching collects the kind, name and text interactively. Forget and Restore ask for an explicit decision with cancellation as the default. Argument commands still work. Restoring revision 0 recovers the original retained baseline; restoration creates a new revision.

`/growth feedback` rates whether the completed approach was useful. The rating informs memory retrieval, the chosen task strategy, actually loaded skills and offered tool ordering. Without an ID it selects the most recent completed, unrated decision in the current session. Each decision accepts one rating. An unfinished, paused or cancelled turn cannot receive feedback. Terminal provider errors and budget/iteration limits can be rated; completion is not assumed to mean task success.

Equivalent CLI commands use the selected session or the last active session:

```sh
ah growth show --session <session-id>
ah growth teach style verbosity 'Prefer short answers' --session <session-id>
ah growth feedback <episode-id> 1 --session <session-id>
ah growth forget style:verbosity --session <session-id>
ah growth rollback 0 --session <session-id>
ah growth export-policy live-policy.json --session <session-id>
ah growth import-policy memory-policy.json --session <session-id>
```

Gateway methods are `growth.get`, `growth.teach`, `growth.feedback`, `growth.forget`, `growth.installPolicy` and `growth.rollback`, all requiring `sessionId`. They use existing gateway authentication and the same authenticated HTTP `/api/v1/rpc` dispatch. This is agent-scoped persistence in the existing shared local account model, not separate per-user isolation. Use distinct agents for distinct personalities/users.

## What learns

The existing `MemoryPolicy` and offline `GroupRelativeTrainer` retain their six-action research API. Live `RetrievalPolicy` masks the policy to `retrieve` and `noop`, then samples whether to run automatic long-term memory search. A retrieval-biased cold start searches on approximately 92% of turns. Explicit recall tools and persistent personality notes remain available regardless of the sampled decision.

Actual human rewards from -1 to +1 update the chosen action with a contextual bandit policy gradient and bounded correction for delayed feedback. RSI also applies a weak operational update, scaled to one tenth of the proxy reward. When a human rates that episode, its directly applied automatic memory update is subtracted before the human update; unrelated intervening updates remain. Checkpoints record which pending updates they contain, so later ratings also work after restoration. Importing a manually selected policy starts fresh provenance. This is incremental correction, not a replay of counterfactual training history.

Successful replies alone produce no reward. Actual successful tool execution produces +0.2, tool failure/denial -0.25, and terminal provider/budget/iteration failure -0.5. Self-learning tools cannot reward their own saved notes. Shell status comes from the subprocess exit code, including failures that lack an `Error:` prefix. These proxies describe execution, not proven task quality. Stored episodes contain bounded hashed features and the action probability, not conversation text. The most recent 500 decisions per agent are retained. Counts, notes and policy weights persist in PostgreSQL; updates are atomic across concurrent sessions.

RSI uses those completed episodes to adjust relevant skill ranks and available tool order, with multipliers between 0.8 and 1.2. Relevance filters, explicit skill requests and tool allowlists remain authoritative. Automatic skill loading and successful manual `skill_read` calls both receive attribution. It chooses short fixed task guidance (direct, verify or plan) from domain-specific evidence. Informational questions receive no extra strategy instruction. No additional provider/classifier request is made.

Persona notes have immutable checkpoints. Human teaching, forgetting and policy installation establish a stable revision. Agent-written notes start a trial against the last stable baseline. Three human-rated completed episodes from that exact revision stabilize it when their average is nonnegative or restore the previous baseline when negative. Automatic evidence cannot promote a persona. New human choices, including confirmation of the same note and changes while RSI is disabled, supersede older trials. A turn's revision and note snapshot are read together to prevent concurrent edits from misattributing ratings. This evaluation is a heuristic, not proof of causal improvement.

At most 50 revision snapshots are retained, preserving the baseline of an active trial. Manual restoration uses owned retained revisions and restores notes and retrieval-policy weights. Routing evidence remains bounded experience history; restoring a persona does not erase observations. `/growth show` and `ah growth show` distinguish operational observations, human ratings, trial progress and revision metadata. Counts can overlap because a human rating replaces an episode's proxy rather than creating a second episode.

Exported live weights and offline trained JSON use the same six-action format. Offline training still requires scored counterfactual outcomes and never runs automatically in chat:

```sh
python -m ah.research.train_memory_policy rollouts.jsonl --output memory-policy.json
ah growth import-policy memory-policy.json --session <session-id>
```

Live imports require each weight to be finite and within -20 to +20. Imported weights never enable deletion, identity rewriting or additional tools. Personality adaptation comes from learned context; the RL policy learns when to retrieve memory. Neither changes the underlying LLM's weights. Long procedural skills still use the existing opt-in skill-review workflow (`learning_review_enabled` and `/skills proposals`).

Notes are limited to 500 characters each and 2,400 characters total, including keys. Secret-looking content is redacted before persistence; invalid/secret keys are rejected. Reusing a key corrects a note. Full storage returns a clear request to consolidate an existing key; the agent does not silently discard notes. Notes are fallible data subordinate to core identity and policy, not a source of execution authority. As with other model-curated memory, inspect and correct mistaken inferences with `/growth`.

RSI defaults on. Set `rsi_enabled: false` (or `AGENT_HARNESS_RSI_ENABLED=false`) to stop collecting operational evidence and applying RSI routing/strategy adaptation. Existing personality notes and explicit human memory feedback remain available. Human management still versions existing RSI history so an older trial cannot undo a later human choice.

Set `personality_growth_enabled` or `memory_enabled` to false to stop automatic growth, personality injection, decisions, teaching, feedback and policy installation. Inspection, export, explicit removal and restoration remain available. Turning it off preserves learned state for later use; existing explicit remember/recall tools retain their original behavior.

This implements recursive runtime improvement across the shared agent loop used by CLI, gateway/TUI, HTTP, jobs and delegation. It does not train LLM weights or rewrite/deploy application code. Learned procedural skill proposals retain their existing review workflow. The [design](plans/PERSONA_RSI_DESIGN.md) and [implementation/evidence plan](plans/PERSONA_RSI_PLAN.md) describe the boundaries and checks.

## Verification

`tests/test_personality_growth.py` covers real PostgreSQL persistence, concurrent note writes, one-time owned feedback, malformed imports, restart/new-session/model continuity through normal and streaming agent loops, disabled behavior and additive migration. `tests/test_rsi.py`, `tests/test_rsi_agent.py` and `tests/test_rsi_management.py` add revision evaluation/restoration, proxy replacement across restore/import and in-flight completion, human-choice protection, skill/tool adaptation, real task cancellation in both loop modes, and real CLI commands. UI tests cover the interactive menu and confirmed restoration; the real Node gateway restart test also checks persisted revision history and restoration.

Verified on 2026-10-11: **43 RSI tests passed**, full backend **2,092 passed / 5 skipped**, and `npm --prefix ui run check` passed (**138 UI tests / 5 environment-gated skips**, typecheck and lint). The real gateway/DB UI checks run through the backend test with isolated test configuration. Ruff check and formatting passed for 16 touched Python files, Bandit reported no findings in the RSI/growth modules, and `git diff --check` passed. Fresh review findings were reproduced with red regressions and fixed. Exact commands, transient failures and clean-HEAD comparison are recorded in the [implementation plan](plans/PERSONA_RSI_PLAN.md). Tests used synthetic providers and the dedicated test DB; no production database or live model request was used. No comparative benchmark establishes an overall quality or speed improvement yet.
