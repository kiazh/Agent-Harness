# Personality growth and live memory RL

Growth is enabled by default. The agent keeps its core system prompt/SoulSpec identity and gradually curates preferences, conversational style and verified lessons. These bounded notes belong to the agent, enter subsequent turns automatically, and survive restarts, new sessions and provider/model changes. The model saves them with `learn_personality` during ordinary tool use, without an extra reflection request or a permission prompt in the default risk policy. The general agent and built-in specialists support this; restrictive custom definitions must explicitly include the tool.

The design follows the separation between user preferences and reusable agent lessons in [Hermes memory](https://github.com/nousresearch/hermes-agent/blob/main/tools/memory_tool.py). This implementation uses our PostgreSQL, agent ownership, tools and permissions; no Hermes source was copied. Hermes's [license](https://github.com/nousresearch/hermes-agent/blob/main/LICENSE) is MIT.

## Use it

Set the personality you want with `/persona A warm, witty mentor who explains clearly and keeps answers short`. This saves `persona_description` in config and applies on the next turn, including existing sessions. `/persona` shows it; `/persona --clear` restores the agent's default. The description steers tone and character while learned preferences and lessons grow within it. It applies globally to agents in this installation, independently of automatic memory/growth flags, and does not change core roles or tool permissions. Descriptions are limited to 2,000 characters and secret-looking text is redacted.

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
```

`/growth feedback` rates whether the memory-search decision was useful, rather than overall answer quality. Without an ID it selects the most recent completed, unrated decision in the current session. Each decision accepts one rating. An unfinished or cancelled turn cannot receive feedback. Terminal provider errors and budget/iteration limits can be rated; completion is not assumed to mean task success.

Equivalent CLI commands use the selected session or the last active session:

```sh
ah growth show --session <session-id>
ah growth teach style verbosity 'Prefer short answers' --session <session-id>
ah growth feedback <episode-id> 1 --session <session-id>
ah growth forget style:verbosity --session <session-id>
ah growth export-policy live-policy.json --session <session-id>
ah growth import-policy memory-policy.json --session <session-id>
```

Gateway methods are `growth.get`, `growth.teach`, `growth.feedback`, `growth.forget` and `growth.installPolicy`, all requiring `sessionId`. They use existing gateway authentication. This is agent-scoped persistence in the existing shared local account model, not separate per-user isolation. Use distinct agents for distinct personalities/users.

## What learns

The existing `MemoryPolicy` and offline `GroupRelativeTrainer` retain their six-action research API. Live `RetrievalPolicy` masks the policy to `retrieve` and `noop`, then samples whether to run automatic long-term memory search. A retrieval-biased cold start searches on approximately 92% of turns. Explicit recall tools and persistent personality notes remain available regardless of the sampled decision.

Actual human rewards from -1 to +1 update the chosen action with a contextual bandit policy gradient and bounded correction for delayed feedback. Successful replies alone produce no reward. Stored episodes contain bounded hashed features and the action probability, not conversation text. The most recent 500 decisions per agent are retained. Counts, notes and policy weights persist in PostgreSQL; updates are atomic across concurrent sessions.

Exported live weights and offline trained JSON use the same six-action format. Offline training still requires scored counterfactual outcomes and never runs automatically in chat:

```sh
python -m ah.research.train_memory_policy rollouts.jsonl --output memory-policy.json
ah growth import-policy memory-policy.json --session <session-id>
```

Live imports require each weight to be finite and within -20 to +20. Imported weights never enable deletion, identity rewriting or additional tools. Personality adaptation comes from learned context; the RL policy learns when to retrieve memory. Neither changes the underlying LLM's weights. Long procedural skills still use the existing opt-in skill-review workflow (`learning_review_enabled` and `/skills proposals`).

Notes are limited to 500 characters each and 2,400 characters total, including keys. Secret-looking content is redacted before persistence; invalid/secret keys are rejected. Reusing a key corrects a note. Full storage returns a clear request to consolidate an existing key; the agent does not silently discard notes. Notes are fallible data subordinate to core identity and policy, not a source of execution authority. As with other model-curated memory, inspect and correct mistaken inferences with `/growth`.

Set `personality_growth_enabled` or `memory_enabled` to false to stop automatic growth, personality injection, decisions, teaching, feedback and policy installation. Inspection, export and explicit removal remain available. Turning it off preserves learned state for later use; existing explicit remember/recall tools retain their original behavior.

## Verification

`tests/test_personality_growth.py` covers real PostgreSQL persistence, concurrent note writes, one-time owned feedback, malformed imports, restart/new-session/model continuity through normal and streaming agent loops, disabled behavior and additive migration. UI command tests cover teaching, rating and correction without confirmation dialogs.

Verified on 2026-10-10: focused growth/gateway/CLI checks **131 passed**, including a real Node UI client restarting the Python gateway. Full Python suite: **1,780 passed, 3 failed, 5 skipped**. The remaining pre-existing failures are time-sensitive importance-score equality, the missing `research/identity_scenarios_example.json` fixture, and the workflow benchmark's missing research table. Full UI suite: **97 passed, 1 failed, 4 skipped**; the existing selected-row background-fill assertion still fails. The new real-gateway growth test runs through pytest with the isolated test database rather than being skipped there. Python lint, changed-file formatting, UI typecheck and UI lint pass. Fresh code review findings were reproduced and fixed.
