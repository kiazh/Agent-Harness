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
python -m ah.research.locomo path/to/locomo10.json --backend session-recall --test-db --k 5
python -m ah.research.locomo path/to/locomo10.json --backend title-only --test-db --k 5
```

The archive backend uses `AGENT_HARNESS_TEST_DATABASE_URL`, replays every
dialogue turn into temporary archive rows, searches using the production
`ContextManager.search_archive_text()` path, and deletes benchmark sessions.
The `title-only` backend runs the same replay but scores only session-title
matches (`SessionRecall` source `title`): chunk-level evidence is unreachable
from titles alone, so expected recall on the standard replay is ~0 because
every session carries the same generic title. It documents what transcript
search adds, not a competing design.

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
| Cross-session recall (archive, 50 turns/session) | 0.4259 | 0.4618 | 0.3457 | 922.3 |
| Title-only (`title-only`) | — (run pending) | — | — | — |

Both runs scored 1,977 questions. Five questions had unresolvable annotated
evidence references and were excluded with an explicit `skipped_questions`
count. Adversarial questions with no positive evidence do not enter recall.
This is **evidence retrieval**, not LoCoMo answer accuracy. The archive
result is slightly below the lexical baseline on this dataset. The first
archive run exposed overly strict AND matching; OR matching raised recall@5
on the first conversation from 0.0254 to 0.3693.

The cross-session run partitions archived turns into sessions under one
temporary agent per conversation and calls the production
`SessionRecall.discover()` service. On this local Windows PostgreSQL run its
mean query latency was 15.3 ms. The initially implemented AND query scored
0.0254 recall@5 on the first conversation; sharing the bounded OR query builder
with archive search raised that slice to 0.3693. The full cross-session run
matches the archive-only retrieval metrics. These results do not measure
answer quality, live-chunk ranking, multi-user scale, or restart recovery.
Raw metrics and dataset hash are in
[`research/results/locomo-evidence-2026-10-04.json`](../research/results/locomo-evidence-2026-10-04.json).

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

## Post-turn skill learning

When `AGENT_HARNESS_LEARNING_REVIEW_ENABLED=true`, completed turns containing
an explicit workflow cue can trigger one bounded review call. The reviewer
redacts known secret patterns before sending the turn, limits input length and
output to 512 tokens, and stops after three reviews per session by default.
Suggestions are stored in `learning_reviews`; approval through the gateway or
`/skills approve <id>` creates a skill only if the name is still unused.

The current tests cover staging, redaction, duplicate turns, rejected
injection text, approval/rejection, and overwrite prevention. They do not
measure suggestion precision or skill-use improvement. The reviewer runs as
a process-local background task; restart recovery is still required before
this can be called durable learning.

## Durable approval workflow trial

Install the optional `workflow-trial` extra, set
`AGENT_HARNESS_TEST_DATABASE_URL` to an isolated PostgreSQL database, then run:

```powershell
python -m ah.research.workflow_benchmark --iterations 5
```

The [2026-10-04 result](../research/results/workflow-trial-2026-10-04.json)
measured mean start-plus-approval latency of 3.618 ms for the native row-state
path and 18.608 ms for the LangGraph PostgreSQL checkpointer path (five runs
on Windows). A separate-process test proves that the graph can resume a
paused approval after reopening its checkpointer. Both paths used an
idempotent PostgreSQL insert as the effect. This narrow trial does not test
LLM calls, arbitrary external tools, gateway streaming, or usage/audit
parity; see the [decision gate](research-langgraph.md).

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
`TestSoulSpecConformance().validate_package()` now validate required manifest
fields, optional v0.5 fields, declared file paths, license allowlist, and
package file presence. Imported packages preserve unknown manifest fields and
the bytes of all declared auxiliary files on export. `allowedTools` remains
informational, as specified in the [official v0.5 document](https://github.com/clawsouls/soulspec/blob/main/soul-spec-v0.5.md).

Cross-framework portability still needs runs inside each target framework;
the current adapters are format converters, not compatibility certification.
