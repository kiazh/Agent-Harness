# Research evaluation

The research modules now have runnable, reproducible local evaluations. These
results measure the named implementation paths; they are not claims about
generated answer quality or a trained language model.

## LoCoMo evidence retrieval

Download the [official LoCoMo JSON](https://github.com/snap-research/locomo/blob/main/data/locomo10.json)
to a local path. The file used for the October 2026 run had SHA-256
`79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.

```powershell
python -m ah.research.locomo path/to/locomo10.json --k 5
python -m ah.research.locomo path/to/locomo10.json --backend archive --test-db --k 5
```

The archive backend uses `AGENT_HARNESS_TEST_DATABASE_URL`, replays every
dialogue turn into temporary archive rows, searches using the production
`ContextManager.search_archive_text()` path, and deletes benchmark sessions.
The test database must already have the project schema. `--db-url` can select
another initialized database explicitly. `--max-conversations 1` gives a
short smoke run.

| Backend | Evidence recall@5 | Hit rate@5 | MRR@5 | Mean retrieved bytes |
|---------|------------------:|-----------:|------:|---------------------:|
| Lexical baseline | 0.4395 | 0.4770 | 0.3540 | 1,078.6 |
| Archive full text search | 0.4259 | 0.4618 | 0.3457 | 922.3 |

Both runs scored 1,977 questions. Five questions had unresolvable annotated
evidence references and were excluded with an explicit `skipped_questions`
count. Adversarial questions with no positive evidence do not enter recall.
This is **evidence retrieval**, not LoCoMo answer accuracy. The archive
result is slightly below the lexical baseline on this dataset. The first
archive run exposed overly strict AND matching; OR matching raised recall@5
on the first conversation from 0.0254 to 0.3693.

## Memory action policy

`ah.memory.policy` implements a finite-action contextual softmax policy and
a group-relative trainer with normalized within-group rewards, clipped
probability ratios, and a reference KL term. It uses the existing six memory
actions and step, task, and persona rewards. It is a lightweight research
baseline, not 7B LLM fine-tuning.

Create a JSONL file with one state and scored outcome for every available
action. Each outcome needs `task_success` (boolean) and
`persona_consistency` (0–1); step rewards use `relevance`, `redundancy`, or
`staleness` as appropriate. Missing action outcomes are rejected.

```json
{"state":{"task":"remember a preference","conversation":[],"current_memory_count":0,"budget_remaining":4000},"outcomes":{"store":{"relevance":1,"task_success":true,"persona_consistency":1},"retrieve":{"relevance":0,"task_success":false,"persona_consistency":1},"noop":{"task_success":false,"persona_consistency":1}}}
```

```powershell
python -m ah.research.train_memory_policy rollouts.jsonl --output memory-policy.json --epochs 40 --seed 7
```

The command reports training and held-out expected reward. No policy is
silently activated in the runtime; that needs an online safety and rollback
evaluation using representative agent trajectories.

## Identity drift sequences

`IdentityGate.observe_transition()` records each proposed belief update,
rejects a change above the configured threshold, and quarantines cited
causal memories in the same transaction. The evaluator runs labeled benign
and injected sequences using temporary agent IDs:

```powershell
python -m ah.research.identity_eval research/identity_scenarios_example.json --test-db
```

The example yields attack rejection 1.0 and benign acceptance 1.0 over three
transitions; it is a **small synthetic check**, not evidence of real-world
adversarial robustness. A useful next study needs many independent scenarios,
multiple beliefs per agent, authentic shared-memory traces, and blinded
injection labels.

## Soul Spec v0.5

`SoulSpec.from_package()`, `write_package()`, and
`SoulSpecConformance.validate_package()` now validate required manifest
fields, optional v0.5 fields, declared file paths, license allowlist, and
package file presence. Imported packages preserve unknown manifest fields and
the bytes of all declared auxiliary files on export. `allowedTools` remains
informational, as specified in the [official v0.5 document](https://github.com/clawsouls/soulspec/blob/main/soul-spec-v0.5.md).

Cross-framework portability still needs runs inside each target framework;
the current adapters are format converters, not compatibility certification.
