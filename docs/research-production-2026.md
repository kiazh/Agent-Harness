# Production Agent Systems Research Report (2024–2026)

**Date:** 2026-10-05  
**Scope:** Industry research on production agent systems, usage tracking, observability, evaluation, security, and configuration — mapped to AgentHarness's architecture.

---

## Executive Summary

This report synthesizes research across five critical areas for production AI agent systems. The agent infrastructure landscape has matured significantly from 2024 to 2026, with clear patterns emerging around cost optimization, observability, evaluation, security, and configuration management. AgentHarness already has foundational components in each area; this report identifies specific gaps and actionable recommendations.

**Key findings:**
- **Cost optimization** is now a $9.2B market (2025) growing at 26.7% CAGR; 40–60% of enterprise LLM spend is waste
- **Observability** has consolidated around 6 major platforms; OpenTelemetry GenAI conventions are the wire standard
- **Evaluation** has moved from research to CI/CD gates; trajectory-level scoring is the differentiator
- **Security** threats have escalated (EchoLeak CVE-2025-32711, 55% of agent attacks are indirect prompt injection)
- **Configuration** is converging on AGENTS.md + Skills patterns with progressive disclosure

---

## 1. LLM Usage Tracking and Cost Optimization

### 1.1 Industry Landscape

The LLM cost optimization market reached $863.7M in 2025 and is projected to hit $9.2B by 2035 (CAGR 26.7%). Model selection and routing alone capture 41.8% of this market. Key insight: **40–60% of enterprise LLM spend is pure waste** — redundant calls, over-specified models, no caching, and no budget controls.

### 1.2 Usage Tracking Patterns

**OpenRouter Analytics API** (2026) is the most complete reference implementation:
- Per-request usage automatically included in every API response (prompt tokens, completion tokens, reasoning tokens, cached tokens, cost)
- Analytics API with management keys for spend analysis grouped by model, API key, app, user, workspace
- Metrics: `total_usage`, `request_count`, `tokens_total`, `cache_hit_rate`, `blended_cost_per_mtok`
- Dimensions: model, API key, app, user, workspace, origin, country, data region, finish reason, context length, session
- Granularity: minute, hour, day, week, month
- Cost components: `usage_upstream` (raw inference), `usage_cache` (savings), `usage_data` (discounts), `usage_web`, `usage_file` (surcharges)

**OpenRouter State of AI 2025** (100T token study):
- Weak price elasticity: 10% price decrease → only 0.5–0.7% usage increase
- Four usage-cost archetypes: Premium leaders (Claude Sonnet), Efficient giants (Gemini Flash, DeepSeek), Long tail (Qwen, Granite), Premium specialists (GPT-4/5 Pro)
- Key operator insight: **cost-per-successful-outcome** matters more than cost-per-token

**LLMFlow** (open-source): Local observability proxy that tracks costs, tokens, latency for OpenAI, Anthropic, Gemini, Ollama. Uses OpenTelemetry for trace collection. SQLite backend keeps data local.

**toolkit-cost-optimizer** (PyPI): Ingests gateway/trace/usage exports, prices every request by token class (input, cache reads/writes, output, reasoning), joins per-request quality scores, finds cheapest routing that keeps quality above a floor. Produces cost/quality Pareto frontier and LiteLLM proxy config.

**AiFinOps** (open-source): Self-hosted OpenAI-compatible gateway for cost governance. Every call logged to Postgres before response returns. Provider/model allow-list gate. Cost attribution headers (tenant, application, module, user, transaction, region, environment).

**Obol**: No-SDK cost tracking across 9 providers. $5/month flat. Forecasts month-end bill with exponential smoothing. MAD + CUSUM anomaly detection. Cache savings tracker.

### 1.3 Cost Optimization Techniques

| Technique | Typical Savings | Implementation Effort | Best For |
|---|---|---|---|
| Prompt optimization | 20–40% | Low (hours) | Everyone, immediate wins |
| Response caching | 30–70% | Low-Medium (days) | Repetitive queries |
| Model routing | 40–60% | Medium (weeks) | Mixed complexity traffic |
| Batching | 20–50% | Medium (weeks) | High-volume, latency-tolerant |
| Prompt caching | 50–90% on cached | Low (hours) | Long system prompts, RAG |
| Self-hosting | 60–90% | High (months) | >1M queries/month |

**RouteLLM** (ICLR 2025): Well-trained complexity router achieves 95% of GPT-4 performance while routing only 14–26% of requests to the expensive model — **75–85% cost reduction** on routed workloads.

**Model routing approaches:**
- **Rule-based**: Simple but brittle; misroutes queries that break assumptions
- **Classifier-based**: Trains lightweight model to predict optimal LLM per query; approaches best-single-model performance at significantly lower cost
- **Cascading**: Starts with smallest model, escalates on low confidence; adds latency per escalation
- **Cache-aware routing**: Routes similar queries to same replica to maximize cache hit rates (3x P90 latency improvement)

**Production pitfalls that kill routing systems:**
1. No structured logging of routing decisions → flying blind
2. No multi-provider fallback → single point of failure
3. Cold start on new query types → misclassification
4. Sequential cascade latency → worse than direct routing
5. Confidence miscalibration at the tail → errors on rare inputs

**Multi-agent cost guardrails** (critical for AgentHarness):
- Rate limiting at agent level — hard cap on requests per unit time
- Real-time cost monitoring with automatic API key blocking on anomalous spend
- Budget-aware agent design — each agent knows its spend limit before escalating
- Cost kill-switch: soft warn at 70% of budget, hard halt at 100%
- Idempotent run IDs for safe retries

### 1.4 Recommendations for AgentHarness

**Current state:** AgentHarness has `ah/observability/metrics.py` and `ah/gateway/` with durable `llm_usage` accounting and enforced session/agent budgets, but no dollar cost attribution or model routing.

**Priority actions:**

1. **Implement per-request usage tracking** (Week 1–2)
   - Add `UsageRecord` dataclass: `session_id`, `agent_id`, `model`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `cost_usd`, `latency_ms`, `timestamp`, `cache_hit`
   - Log to structured JSONL with rotation (pattern from AiFinOps)
   - Add cost attribution headers to gateway requests

2. **Add cost aggregation dashboard** (Week 3–4)
   - Daily summary table: `date`, `agent_id`, `model`, `total_tokens`, `total_cost`, `request_count`, `cache_hit_rate`
   - Per-agent, per-model, per-session cost breakdown
   - Anomaly detection: MAD for spikes, CUSUM for drift

3. **Implement model routing** (Week 5–8)
   - Start with rule-based routing: dev/staging → cheap model, prod → premium
   - Add complexity classifier for production traffic
   - Implement cascade architecture with confidence thresholds
   - Cache-aware routing for repeated queries

4. **Add budget controls** (Week 9–10)
   - Per-agent daily/monthly spend caps
   - Soft warn at 70%, hard halt at 100%
   - Gateway-level spend policies (pattern from LangSmith LLM Gateway)
   - Concurrent request limits per agent

5. **Enable prompt caching** (Week 11–12)
   - Ensure system prompts are cache-stable (static prefix)
   - Version prompts and bust cache on deploy
   - Track cache hit rates per model

---

## 2. Agent Observability and Monitoring

### 2.1 Industry Landscape

By April 2026, six platforms own the production agent observability conversation:

| Platform | Type | Pricing | Best For |
|---|---|---|---|
| **LangSmith** | Framework-native | $39/seat/mo + usage | LangChain/LangGraph teams |
| **Langfuse** | Open-source | Free self-host; $59/seat cloud | OSS-first, multi-framework |
| **Arize Phoenix** | ML-grade | Open-source + enterprise | OpenTelemetry-native stacks |
| **Helicone** | Drop-in proxy | Free; $79/mo Pro | Simplest install, minimal changes |
| **Datadog LLM** | Enterprise APM | $31+/host/mo + LLM add-on | Datadog shops |
| **Honeycomb LLM** | Event-based | Enterprise | Honeycomb-native shops |

**Key insight from LangChain's 2026 State of AI Agents report:** 89% of teams have observability in place, but only 52% run offline evals before deployment. Quality is the #1 barrier to deployment at 32%.

### 2.2 Observability Patterns

**OpenTelemetry GenAI Semantic Conventions** (exited experimental early 2026):
- Standardized trace shape: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.reasoning.output_tokens`
- Agent spans: `invoke_agent`, `execute_tool`, `create_agent`
- Framework spans stable in practice through Q1 2026
- Arize's OpenInference aligned its span-kind taxonomy with the spec

**LangSmith's approach:**
- Hierarchical traces with node-by-node state diffs
- Insights Agent: automated clustering of similar traces to discover usage/error patterns
- Online evaluations: production traffic scored in real-time with LLM-as-judge
- SmithDB: purpose-built database for long, nested agent traces (12–15x faster, P50 trace tree loads ~92ms)
- Polly prompt agent: auto-improvement agent that iterates on prompts

**Langfuse v3** (2026):
- Rebuilt around OpenTelemetry
- Immutable events table
- Self-hostable, MIT-licensed
- Widest framework coverage among OSS options

**Production monitoring metrics that matter:**
- Token usage, latency percentiles (P50, P99), error rates, cost breakdowns
- Tool call failure rates, run count by tool
- User satisfaction, whether agent is being used for intended purposes
- Cache hit rates, model routing distribution

**Two-layer observability pattern:**
- LLM observability platform (LangSmith, Langfuse, Arize) for agent traces, eval, LLM-specific metrics
- Infrastructure observability (Datadog, Honeycomb, New Relic) for host metrics, app errors, deployment health
- Most production deployments need both

### 2.3 Recommendations for AgentHarness

**Current state:** AgentHarness has `ah/observability/tracing.py`, `metrics.py`, and `audit.py`. The existing research doc identifies gaps in distributed tracing, metrics aggregation, structured log schema, cost tracking, and alerting.

**Priority actions:**

1. **Adopt OpenTelemetry GenAI conventions** (Week 1–2)
   - Map existing traces to `gen_ai.*` semantic conventions
   - Add `trace_id` / `span_id` correlation to all audit events
   - Ensure tool calls are captured as `execute_tool` spans

2. **Implement structured log envelope** (Week 3–4)
   - Consistent envelope: `timestamp`, `trace_id`, `span_id`, `event_type`, `session_id`, `agent_id`, `model`, `tokens`, `cost`, `latency_ms`, `metadata`
   - Add PII redaction at ingest (tool_args, result_preview)
   - File rotation with gzip for old logs

3. **Add metrics aggregation** (Week 5–6)
   - Token counts per agent, per model, per session
   - Latency percentiles (P50, P90, P99)
   - Cost per request, cost per session
   - Cache hit rates
   - Tool call success/failure rates

4. **Implement production monitoring** (Week 7–8)
   - Custom dashboards for business-critical metrics
   - Alerts on: latency regression, cost spikes, error rate increases, tool call failures
   - Webhook/PagerDuty integration for critical alerts
   - Insights Agent pattern: automated clustering of similar traces

5. **Add replay mechanism** (Week 9–10)
   - Given a trace ID, reconstruct exact prompt, tool responses, and decisions
   - JSONL file per run: one line per event (timestamp, trace_id, event_type, payload)
   - Gzip old files, store for 30+ days

---

## 3. Agent Evaluation and Testing

### 3.1 Industry Landscape

Agent evaluation has become its own discipline, distinct from LLM evaluation. Key insight: **you score trajectories — plan, tool calls, arguments, final answer — not just text.**

**Four eval layers every production agent needs:**

| Layer | What it tests | When it runs | Cost |
|---|---|---|---|
| **Unit evals** | Individual components (tool call, classifier, extraction) | Every commit | Fast (<5s), cheap |
| **Trajectory evals** | Full agent on fixed dataset (50–200 real prompts) | Every PR | Medium |
| **Outcome evals** | Final output correctness (LLM-as-judge or ground truth) | Nightly | Expensive |
| **Online evals** | 1–5% live production traffic, async grading | Continuous | Ongoing |

**Key evaluation frameworks:**

- **DeepEval**: 50+ research-backed metrics, Pytest-style, CI/CD regression testing. `@observe` decorator captures component-level traces. `deepeval test run` works as Pytest plugin.
- **Braintrust**: Eval-first with CI/CD blocking, datasets, experiments. Native GitHub Action posts results to PRs and blocks merges. One-click production-to-eval conversion. Loop AI for automated scorer/dataset generation.
- **LangSmith**: Native LangChain integration, trajectory evaluators (deterministic match + LLM-as-judge), annotation queues, online evals.
- **Langfuse**: OSS, self-hosted, programmatic dataset runs.
- **Promptfoo**: Config-as-code, YAML/JSON, security testing.

**Trajectory evaluation metrics:**
- **TrajectoryAccuracy**: How closely agent's step sequence matches golden path
- **ToolCallAccuracy**: Was the right tool called with the right arguments?
- **TrajectoryEfficiency**: Was the path the shortest one that worked?
- **PlanQuality**: Did the agent decide on a sensible sequence before acting?
- **PlanAdherence**: Did the agent stick to its plan?
- **StepEfficiency**: Did the agent finish in reasonable steps without redundant retries?

**Statistical pass/fail for non-deterministic agents:**
- Define thresholds on aggregate scores, not individual assertions
- Use `num_repetitions` to average out single-run noise
- Compare against named baseline, not absolute thresholds
- Gate on dimensions separately (safety, correctness, relevance)

**Production-to-eval flywheel:**
1. Production failure captured as trace
2. Trace's tool inputs/outputs recorded
3. Agent re-run against frozen tool responses (deterministic regression test)
4. Failed trace added to regression dataset
5. Eval suite grows stronger with every production incident

**Sierra's tau-bench** introduced `pass^k` reliability metric: decays exponentially, exposes gap between "sometimes works" and "reliably works."

**AgentAssay** (arXiv 2603.02601): Sampling-based approaches for statistical confidence without exhaustive re-evaluation.

### 3.2 Recommendations for AgentHarness

**Current state:** AgentHarness has `skills/testing-qa/` and `docs/research-testing.md` with LoCoMo evidence retrieval, memory-policy training, and identity-drift evaluations (`ah/research/`), but no CI eval gate.

**Priority actions:**

1. **Build unit eval layer** (Week 1–2)
   - Test individual tools, classifiers, extraction prompts
   - Fast (<5s), runs on every commit
   - Block merges on regressions

2. **Build trajectory eval layer** (Week 3–6)
   - Curate 50–200 representative tasks with known-good trajectories
   - Implement trajectory evaluators: deterministic match + LLM-as-judge
   - Run on every PR, block merge if trajectory score drops below threshold
   - Use statistical thresholds (aggregate averages, not individual assertions)

3. **Build outcome eval layer** (Week 7–8)
   - LLM-as-judge or ground-truth labels on curated 100-prompt set
   - Run nightly, track accuracy delta over time
   - Metrics: correctness, faithfulness, helpfulness, refusal correctness

4. **Build online eval layer** (Week 9–10)
   - Sample 1–5% of live production traffic
   - Grade asynchronously with LLM-as-judge
   - Alert on accuracy drops over 24-hour window
   - Production failures automatically become regression cases

5. **Implement production-to-eval flywheel** (Week 11–12)
   - Capture failed traces with full tool inputs/outputs
   - Freeze tool responses for deterministic replay
   - Auto-add to regression dataset
   - Track eval suite growth from real production failures

---

## 4. Agent Security Patterns

### 4.1 Threat Landscape

**EchoLeak (CVE-2025-32711)**: Zero-click indirect prompt injection in Microsoft 365 Copilot. Exfiltrated internal documents from a single crafted email. CVSS 9.3. Every enterprise rebuilt their agent threat model after this.

**OWASP Top 10 for Agentic Applications** (December 2025):
- Prompt injection holds the top spot
- New categories: System Prompt Leakage, Vector and Embedding Weaknesses
- Excessive Agency rewritten for tool-using agents

**Attack statistics (2025–2026):**
- Indirect prompt injection: 55% of all observed agent attacks
- Multi-hop attacks growing 70% year over year
- 62% of successful enterprise exploits pass through injected document/email/tool output
- AgentDojo: 97 realistic tasks, 629 security test cases
- InjecAgent: ReAct-prompted GPT-4 vulnerable to indirect prompt injection 24% of the time
- AgentVigil: 71% success on AgentDojo, 70% on VWA-adv against o3-mini and GPT-4o

**Real-world CVEs:**
- CVE-2025-32711 (EchoLeak): M365 Copilot zero-click exfiltration
- CVE-2025-53355: MCP server indirect prompt injection → arbitrary command execution
- CVE-2025-54073: MCP server command execution
- Antigravity sandbox escape: `find_by_name` tool `-X` flag injection → arbitrary code execution

### 4.2 Security Patterns

**Design patterns for securing LLM agents** (arXiv 2506.08837):

1. **Input/output detection systems**: Heuristic/AI-based filters for prompt injection attempts. Raise the bar but cannot guarantee prevention.

2. **Isolation mechanisms**: Constrain agent capabilities when handling untrusted input. Predefined tool sets, system controller disables all others.

3. **System-level isolation**: Sandboxing, capability control, information-flow labels.

**Production security architecture** (from Sandboxing and Capability Control paper):

- **Policy-enforced tool brokers**: All tool calls pass through a policy engine
- **Object-capability grants**: Fine-grained permissions per tool, per resource
- **Information-flow labels**: Track data sensitivity through the system
- **Isolated execution sandboxes**: Code execution in containers with no network access
- **Scoped identities**: Per-tool credentials with minimal permissions
- **Egress control**: Network egress filtering
- **Approval gates**: Human-in-the-loop for high-risk actions
- **Adversarial evaluation**: Treat agent as compromised until proven otherwise

**Scoped permissions and sandboxing** (OWASP ASI03, ASI05):
- Scoped credentials for every tool
- Isolated runtime for code execution
- Blast radius capping: what can the agent physically reach if it decides to act?

**Multi-agent security:**
- Cross-agent manipulation is an emerging threat
- Trust boundaries between agents must be explicit
- Agent-to-agent communication needs authentication and authorization

### 4.3 Recommendations for AgentHarness

**Current state:** AgentHarness has `ah/security/secrets.py` and `skills/security-sandboxing/` with the Docker terminal sandbox shipped, secrets redaction, and prompt-injection screening in skills; comprehensive tool-abuse prevention and injection defense in depth remain open.

**Priority actions:**

1. **Implement input sanitization** (Week 1–2)
   - Strip control characters, system tag injections
   - Deny obvious dangerous patterns (sudo, rm -rf, drop table)
   - Length caps on user input
   - Validate structured output

2. **Add prompt injection detection** (Week 3–4)
   - Heuristic filters for common injection patterns
   - LLM-based detection for sophisticated attacks
   - Fence untrusted text (delimiters, XML tags)
   - Treat retrieved content as data, not instructions

3. **Implement tool broker with policy enforcement** (Week 5–8)
   - All tool calls pass through policy engine
   - Per-tool risk ratings (low/medium/high)
   - Per-tool scoped credentials
   - Approval gates for high-risk actions
   - Rate limiting per tool

4. **Add sandboxing for code execution** (Week 9–10)
   - Containerized execution with no network access
   - Resource limits (CPU, memory, disk, time)
   - Filesystem isolation (read-only except designated workspace)
   - No runtime package installation

5. **Implement audit and monitoring** (Week 11–12)
   - Log all tool calls with full context
   - Alert on anomalous patterns (unusual tool sequences, repeated failures, privilege escalation attempts)
   - Regular adversarial testing (AgentDojo-style)
   - Incident response playbook

---

## 5. Agent Configuration and Skill Systems

### 5.1 Industry Landscape

**AGENTS.md** has emerged as the de facto standard for project-level agent instructions:
- Open standard, tool-agnostic
- Root of repository, loaded at session start
- Converts generic agent into project-aware agent
- Keep to ~100 lines as pointer map; knowledge lives in `docs/`
- Nested AGENTS.md files for subprojects (closest takes precedence)
- Codex: root-down concatenation, `AGENTS.override.md` for replacements, 32 KiB combined limit
- Claude Code: cwd-up walk, `CLAUDE.md` with `@AGENTS.md` reference

**Agent configuration as managed supply chain** (arXiv 2606.26924):
- 10,008 public GitHub repos studied; 6,145 agent config files found
- 10.1% exact-duplicate rate (cross-org)
- 75.5% of clone pairs cross organizational boundaries
- 58% single-commit; 0.4 commits/month (vs 0.6 for comparator files)
- <1% declare permission boundaries (vs 33% for GitHub Actions workflows)
- Four control-plane mechanisms: content addressing, permission declaration, state-machine promotion, drift detection

**SoulSpec v0.5** (open standard for agent personas):
- `soul.json` manifest + `SOUL.md` personality + `AGENTS.md` workflows + `STYLE.md` + `HEARTBEAT.md`
- Three-level progressive disclosure: Level 1 (quick scan: `soul.json` only), Level 2 (full read: `SOUL.md` + `IDENTITY.md`), Level 3 (deep dive: `AGENTS.md`, `STYLE.md`, `HEARTBEAT.md`, examples)
- `allowedTools` for tool transparency
- `recommendedSkills` with version constraints and required/optional flags
- `compatibility.frameworks` for multi-framework support
- SoulScan: checks for prompt injection, secret leaks, 50+ patterns

**Claude Agent Skills** (Anthropic, open standard):
- Directory with `SKILL.md` (YAML frontmatter: `name`, `description`)
- Progressive disclosure: name+description in system prompt, full body loaded on demand
- Additional files (scripts, references) loaded as needed
- Code execution environment with filesystem access, bash, pre-installed packages
- No network access, no runtime package installation
- Skills vs AGENTS.md: AGENTS.md = project orientation, Skills = task knowledge

**Production system prompt architecture** (from 102K-char production prompt analysis):
- XML-tagged concern isolation (~25 named sections)
- Temporal grounding at head (cache-stable)
- Static tool definitions with runtime masking
- Skills as pointers (not inlined content)
- Discrete safety/compliance blocks
- Runtime parameters at tail (cache-optimal)
- Cache stability: any change to earlier token invalidates all cached content

### 5.2 Configuration Patterns

**Layered instruction scopes:**
- Global defaults → project-level files → directory overrides
- Most specific rule always wins
- `AGENTS.override.md` for temporary replacements

**Prompt governance:**
- Store as plain markdown in git
- PRs for behavior changes
- Content-addressed pinning (SHA-256)
- Permission declaration (tools, paths, network)
- State-machine promotion (dev → staging → prod)
- Drift detection

**Context engineering patterns:**
- Just-in-time context loading (lightweight identifiers, dynamic retrieval)
- Compaction (summarize conversation nearing context limit)
- Structured note-taking (persistent memory outside context)
- Sub-agent architectures (specialized agents with clean context)
- Hybrid strategy (some data up front, rest on demand)

### 5.3 Recommendations for AgentHarness

**Current state:** AgentHarness has `ah/skills/registry.py` and `skills/` directory with 18+ skills. Uses SKILL.md format with YAML frontmatter.

**Priority actions:**

1. **Adopt SoulSpec v0.5 manifest** (Week 1–2)
   - Add `soul.json` to each agent configuration
   - Implement three-level progressive disclosure
   - Add `allowedTools` for tool transparency
   - Add `recommendedSkills` with version constraints
   - Add `compatibility.frameworks` for multi-framework support

2. **Implement skill registry with versioning** (Week 3–4)
   - Content-addressed skills (SHA-256 pinning)
   - Version constraints and required/optional flags
   - Drift detection (compare pinned hash vs runtime bytes)
   - State-machine promotion (dev → staging → prod)
   - SoulScan integration for security auditing

3. **Optimize prompt architecture** (Week 5–6)
   - XML-tag concern isolation for system prompts
   - Temporal grounding at head (cache-stable)
   - Static tool definitions with runtime masking
   - Skills as pointers (not inlined content)
   - Runtime parameters at tail

4. **Implement context engineering** (Week 7–8)
   - Just-in-time context loading
   - Compaction for long conversations
   - Structured note-taking for persistent memory
   - Sub-agent architectures for complex tasks

5. **Add configuration governance** (Week 9–10)
   - PR-based workflow for agent config changes
   - Permission declaration in config files
   - Audit trail for all config changes
   - Rollback capability

---

## 6. Implementation Roadmap

### Phase 1: Foundation (Weeks 1–4)
- Per-request usage tracking
- OpenTelemetry GenAI conventions
- Structured log envelope
- Input sanitization
- SoulSpec v0.5 manifest

### Phase 2: Visibility (Weeks 5–8)
- Cost aggregation dashboard
- Metrics aggregation
- Model routing (rule-based)
- Prompt injection detection
- Tool broker with policy enforcement
- Skill registry with versioning

### Phase 3: Quality (Weeks 9–12)
- Unit eval layer
- Trajectory eval layer
- Budget controls
- Sandboxing for code execution
- Prompt architecture optimization

### Phase 4: Production Hardening (Weeks 13–16)
- Outcome eval layer
- Online eval layer
- Production-to-eval flywheel
- Audit and monitoring
- Context engineering
- Configuration governance

---

## 7. Key Citations

### Cost Optimization
- OpenRouter Analytics API: https://openrouter.ai/docs/cookbook/administration/analytics-cost-control
- OpenRouter State of AI 2025: https://openrouter.ai/state-of-ai
- RouteLLM (ICLR 2025): https://lmsys.org/blog/2024-07-01-routellm/
- toolkit-cost-optimizer: https://pypi.org/project/toolkit-cost-optimizer/
- AiFinOps: https://github.com/ankurngm/AiFinOps
- LLM cost optimization market: https://market.us/report/llm-cost-optimization-market/

### Observability
- LangSmith: https://www.langchain.com/langsmith/observability
- Langfuse: https://langfuse.com/
- Arize Phoenix: https://github.com/Arize-ai/phoenix
- Helicone: https://www.helicone.ai/
- Agent observability 2026 comparison: https://www.digitalapplied.com/blog/agent-observability-platforms-langsmith-langfuse-arize-2026
- LangChain production monitoring: https://www.langchain.com/blog/production-monitoring

### Evaluation
- Evaluation and Benchmarking of LLM Agents: A Survey (KDD 2025): https://arxiv.org/abs/2507.21504
- Unified Framework for LLM Agentic Capabilities: https://arxiv.org/html/2605.27898v1
- LogicHunter: Testing LLM Agent Frameworks: https://arxiv.org/pdf/2607.06195
- IBM agent-evaluation: https://github.com/awslabs/agent-evaluation
- Agent evaluation pipeline 2026: https://bestaiweb.ai/how-to-build-an-agent-evaluation-pipeline-with-langsmith-braintrust-and-deepeval-in-2026
- CI/CD for agents: https://klementgunndu1.hashnode.dev/your-unit-tests-pass-but-your-ai-agent-broke-in-production-heres-what-cicd-for-a

### Security
- Sandboxing and Capability Control for Tool-Using Autonomous Agents: https://richards.ai/papers/security-sandboxing-and-capability-control-for-tool-using-autonomous-agen
- Design Patterns for Securing LLM Agents (arXiv 2506.08837): https://arxiv.org/pdf/2506.08837v2
- SoK: The Attack Surface of Agentic AI: https://arxiv.org/pdf/2603.22928
- EchoLeak CVE-2025-32711: https://reactify-solutions.com/articles/ai-agent-security-2026
- OWASP Top 10 for Agentic Applications: https://theagentecosystem.com/blog/ai-agent-security-sandboxing-permissions

### Configuration
- SoulSpec: https://soulspec.org/
- AGENTS.md: https://agents.md/
- Claude Agent Skills: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- Agent config as managed supply chain (arXiv 2606.26924): https://agentpatterns.ai/instructions/agent-config-as-managed-supply-chain/
- Production system prompt architecture: https://agentpatterns.ai/instructions/production-system-prompt-architecture/
- Effective context engineering: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

---

## 8. Summary of Recommendations

| Area | Current State | Priority | Impact |
|---|---|---|---|
| Usage Tracking | Durable `llm_usage` + audit events | P1 | High |
| Cost Optimization | None | P0 | High |
| Observability | Basic tracing + metrics | P0 | High |
| Evaluation | LoCoMo/policy/identity evals exist; no CI gate | P1 | High |
| Security | Secrets backend + Docker sandbox + redaction | P2 | High |
| Configuration | Basic skill registry | P2 | Medium |

**Top 5 immediate actions:**
1. Implement per-request usage tracking with cost attribution
2. Adopt OpenTelemetry GenAI conventions for all traces
3. Build unit + trajectory eval layers with CI/CD integration
4. Add input sanitization and prompt injection detection
5. Implement SoulSpec v0.5 manifests with progressive disclosure

