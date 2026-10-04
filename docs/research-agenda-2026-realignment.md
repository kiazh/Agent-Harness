# AgentHarness Research Agenda — Realigning to 5 Novel Gaps

> This document re-aligns the project with the original 5 research gaps, integrating findings from the 2026 deep research reports (memory, production, technical) and mapping each gap to concrete implementation requirements in the current codebase.

---

## Executive Summary

AgentHarness has built a solid production system (TUI, gateway, API, multi-agent, scheduler, usage tracking) but has drifted from the original research vision. The 5 research gaps identified in September 2026 remain unsolved and highly relevant. This document maps each gap to specific implementation plans that leverage the existing infrastructure and 2026 research findings.

**Core Insight**: The existing `llm_usage` + `context_chunks` + `memories` infrastructure provides the foundation for all 5 gaps. What's missing is the *novel* contribution that makes each gap publishable.

---

## Gap 1: Token-Efficient Context Representation with Reversible Eviction

### Original Problem
Fixed-length memory buffers destroy information permanently when overwritten. Existing reversible approaches are lossy or untested at scale. No system stores structured context with zero-loss eviction.

### Current State Assessment

| Component | Status | Gap |
|-----------|--------|-----|
| `ContextManager` (ah/core/context.py) | LRU cache + token-based eviction | Eviction is lossy — chunks deleted permanently |
| `PromptAssembler` (ah/core/assembler.py) | Greedy packing with token budget | No resurrection mechanism |
| `ContextChunk` (ah/core/models.py) | MessagePack payloads + pgvector | No archive tier or resurrection path |
| `RollingCompaction` | Dead code | Never implemented |

### Research Findings (from 2026 reports)

- **A-MEM** (NeurIPS 2025): Dynamic linking + memory evolution achieves 35% improvement over static memory
- **Focus** (arXiv:2601.07190): Agent self-regulates context, consolidates into persistent "Knowledge" block — 22.7% token reduction
- **Blast Radius**: Reversible eviction via archive — dead context stored byte-exact, resummable on demand
- **ByteRover**: LLM-curated knowledge operations with 5-tier progressive retrieval
- **TOON format**: Reduces LLM token consumption by 40-60% vs raw text

### Concrete Implementation Plan

#### Phase 1A: Two-Tier Eviction with Archive

```
Current: context_chunks (hot) → DELETE on eviction (lossy)
Proposed: context_chunks (hot) → context_archive (cold, byte-exact) → resurrection via embedding similarity
```

**Schema changes:**
```sql
CREATE TABLE context_archive (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    chunk_id UUID NOT NULL,  -- original chunk reference
    payload_msgpack BYTEA NOT NULL,  -- byte-exact MessagePack
    embedding vector(1536),
    archived_at TIMESTAMPTZ NOT DEFAULT NOW(),
    archive_reason TEXT,  -- 'evicted', 'consolidated', 'compressed'
    resurrection_count INT DEFAULT 0,
    last_resurrected TIMESTAMPTZ
);
```

**Implementation:**
1. `ContextManager.evict_old_chunks()` → archive before delete
2. `ContextManager.resurrect_context()` → query archive by embedding similarity
3. `PromptAssembler` → include resurrection path when context is full
4. Evaluate on LoCoMo benchmark

#### Phase 1B: TOON Serialization for Context Chunks

Replace MessagePack with TOON (Token-Optimized Object Notation) for context storage:
- 40-60% token reduction vs raw text
- Preserves structure (typed payloads)
- Compatible with existing pgvector embeddings

#### Phase 1C: Adaptive Compression via Focus Pattern

When context is full:
1. **Observation masking**: Remove intermediate tool outputs, keep final results
2. **Knowledge consolidation**: LLM summarizes key facts into persistent "Knowledge" block
3. **Trigger-based**: Consolidate on context bloat detection, not just timer

### Novel Contribution
**Reversible Eviction with Resurrection Probability**: A context management system that (a) stores structured typed payloads as MessagePack/TOON in PostgreSQL, (b) uses 2-tier eviction (hot → archive, byte-exact), (c) retrieves archived context via embedding similarity to current task, and (d) measures resurrection probability as a function of time since eviction and semantic similarity to current context.

**Evaluation**: LoCoMo + SWE-bench Verified, measuring token efficiency, task success rate, and resurrection hit rate.

---

## Gap 2: Multi-Agent Shared Memory with Identity Propagation Defense

### Original Problem
When agents share context, identity drift propagates via shared context. No system contains identity drift or validates shared memories.

### Current State Assessment

| Component | Status | Gap |
|-----------|--------|-----|
| `agent_messages` table | Cross-agent messaging | No identity gating or validation |
| `AgentDef` (ah/core/agent_def.py) | Static persona definitions | No belief model or consistency checking |
| `Orchestrator` (ah/core/orchestrator.py) | Sequential/parallel delegation | No shared memory bus |
| `memory_store` | Per-agent memories | No cross-agent validation |

### Research Findings

- **ID-RAG**: Identity drift causes self-perpetuating hallucinations in shared-context multi-agent systems
- **CogniPair**: Pairwise agent collaboration with explicit belief alignment
- **A2A protocol** (Linux Foundation): Agent-to-agent communication standard with capability discovery
- **MCP** (Anthropic): Tool access standard

### Concrete Implementation Plan

#### Phase 2A: Agent Identity Model (Belief Graph)

```python
@dataclass
class AgentBelief:
    """An agent's current belief state for identity consistency checking."""
    agent_id: str
    known_facts: dict[str, float]  # fact -> confidence
    traits: dict[str, float]  # trait -> strength
    values: dict[str, float]  # value -> importance
    updated_at: datetime

class IdentityGate:
    """Validates shared memories against agent's belief model."""
    
    async def validate_incoming(
        self, 
        agent_id: str, 
        memory: MemoryEntry
    ) -> ValidationResult:
        """
        Check:
        1. Consistency with existing beliefs
        2. Provenance via signed memory entries
        3. Semantic relevance via embedding threshold
        """
```

#### Phase 2B: Shared Memory Bus with Per-Agent Gating

```
Current: agents write to shared context_chunks (no gating)
Proposed: agents write to shared bus → IdentityGate validates → agent accepts/rejects
```

**Schema:**
```sql
CREATE TABLE agent_beliefs (
    agent_id TEXT PRIMARY KEY,
    belief JSONB NOT NULL,  -- {known_facts, traits, values}
    version INT DEFAULT 1,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE memory_provenance (
    memory_id UUID PRIMARY KEY,
    source_agent TEXT NOT NULL,
    signature TEXT NOT NULL,  -- HMAC of memory content
    parent_memory_id UUID,  -- for lineage
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### Phase 2C: Drift Detection and Containment

1. **Drift detection**: Compare agent beliefs before/after shared memory access
2. **Containment**: If drift detected, quarantine affected memories and alert
3. **Repair**: Agent can request belief realignment via explicit validation

### Novel Contribution
**Identity-Propagation-Resistant Shared Memory**: A shared PostgreSQL context bus where each agent maintains an explicit identity model (knowledge graph of beliefs/traits/values). Before incorporating a shared memory, the agent validates: (a) consistency with existing beliefs, (b) provenance via signed memory entries, (c) semantic relevance via embedding threshold. Evaluate drift propagation vs. baseline shared memory.

**Evaluation**: Multi-agent LoCoMo with identity drift injection, measuring drift rate, containment success, and task performance.

---

## Gap 3: Persona-Conditioned Memory with Emotion Topology

### Original Problem
Current memory systems store facts neutrally. Agents retrieve surface facts but cannot reconstruct persona-conditioned interpretations.

### Current State Assessment

| Component | Status | Gap |
|-----------|--------|-----|
| `memories` table | Neutral facts with importance score | No persona conditioning |
| `ImportanceScorer` | Multi-factor scoring (category, recency, frequency) | No emotion topology |
| `ForgettingModel` | Ebbinghaus decay | No emotion-modulated decay |
| `user_profile.py` | User preferences | Not agent persona |

### Research Findings

- **RoleMemo/DualMem**: Dual-stream memory (factual + persona-conditioned insight) achieves 40% improvement on role-playing tasks
- **P-GEM**: Personality-emotion warping mechanism for memory retrieval
- **Deep Persona**: 685B+ parameter models fail when persona cues are diluted over long context
- **Mem0**: Four-layer hierarchy (interaction → session → user → global) with 26% improvement

### Concrete Implementation Plan

#### Phase 3A: Dual-Stream Memory Architecture

```python
@dataclass
class PersonaMemory:
    """Persona-conditioned interpretation of a factual memory."""
    fact_id: UUID  # reference to factual memory
    persona_id: str  # which persona this interpretation belongs to
    interpretation: str  # what this fact means FOR this persona's goals
    emotional_valence: float  # -1.0 to 1.0
    emotional_arousal: float  # 0.0 to 1.0
    confidence: float  # 0.0 to 1.0
    created_at: datetime
```

**Schema:**
```sql
CREATE TABLE persona_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id UUID REFERENCES memories(id) ON DELETE CASCADE,
    persona_id TEXT NOT NULL,
    interpretation TEXT NOT NULL,
    emotional_valence FLOAT DEFAULT 0.0,
    emotional_arousal FLOAT DEFAULT 0.0,
    confidence FLOAT DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### Phase 3B: Emotion Topology for Memory Retrieval

```python
class EmotionTopology:
    """Maps emotional states to memory retrieval strategies."""
    
    # Plutchik's wheel of emotions → retrieval weights
    EMOTION_PROFILES = {
        'joy': {'recency_weight': 0.8, 'importance_weight': 0.4, 'valence_bias': 0.6},
        'sadness': {'recency_weight': 0.3, 'importance_weight': 0.7, 'valence_bias': -0.4},
        'anger': {'recency_weight': 0.6, 'importance_weight': 0.8, 'valence_bias': -0.6},
        'fear': {'recency_weight': 0.7, 'importance_weight': 0.6, 'valence_bias': -0.3},
        # ... 8 basic emotions
    }
    
    def retrieve_for_emotion(
        self, 
        query: str, 
        current_emotion: str,
        persona_id: str
    ) -> list[RetrievedMemory]:
        """Retrieve memories weighted by emotional state."""
```

#### Phase 3C: Fine-Tuned Persona Model

Use a small (4B) model to generate persona-conditioned interpretations:
- Input: factual memory + persona system prompt
- Output: interpretation, emotional_valence, emotional_arousal, confidence
- Train on LoCoMo + synthetic persona benchmarks

### Novel Contribution
**Persona-Conditioned Dual-Stream Memory with Emotion Topology**: A memory architecture with (a) factual store (raw events/observations as structured MessagePack), (b) insight store (persona-conditioned interpretations generated by a fine-tuned 4B model), and (c) emotion-modulated retrieval that weights memories by current emotional state using Plutchik's wheel. Evaluate role-playing quality on LoCoMo + synthetic persona benchmarks.

**Evaluation**: LoCoMo with persona-specific questions, measuring persona consistency, factual accuracy, and interpretation quality.

---

## Gap 4: End-to-End RL for Joint Memory Management and Task Performance

### Original Problem
Memory-R1 trains Memory Manager and Answer Agent separately because sparse rewards destabilize joint training. No paper has achieved stable end-to-end RL where memory operations and task answering are jointly optimized.

### Current State Assessment

| Component | Status | Gap |
|-----------|--------|-----|
| `MemoryConsolidator` | Rule-based extraction → scoring → dedup → write | Not learned |
| `ImportanceScorer` | Fixed weights (category, recency, frequency) | Not RL-trained |
| `ForgettingModel` | Ebbinghaus decay with fixed lambda | Not RL-trained |
| `PromptAssembler` | Greedy packing | Not RL-trained |

### Research Findings

- **Memory-R1** (arXiv:2508.19828): PPO/GRPO for memory ops (ADD/UPDATE/DELETE/NOOP), 152 training examples achieve SOTA
- **Agentic Memory** (Yu et al., 2026): Three-stage RL pipeline with step-wise GRPO, outperforms all baselines on 5 benchmarks
- **Nous** (arXiv:2606.22030): Bayesian belief update as training signal
- **ReMemR1**: RL with Multi-Level Rewards (RLMLR) — trajectory + step-level rewards

### Concrete Implementation Plan

#### Phase 4A: Define Memory Operations as RL Actions

```python
class MemoryAction(Enum):
    STORE = "store"      # Add to memory
    UPDATE = "update"    # Update existing memory
    DELETE = "delete"    # Delete memory
    SUMMARIZE = "summarize"  # Consolidate into summary
    RETRIEVE = "retrieve"  # Search and inject
    NOOP = "noop"        # No memory operation

@dataclass
class MemoryState:
    """State representation for RL."""
    conversation: list[dict]  # recent messages
    current_memory: list[MemoryEntry]  # current memory state
    task: str  # current task/goal
    budget_remaining: int  # token budget

@dataclass
class MemoryDecision:
    action: MemoryAction
    target_id: UUID | None  # memory to update/delete
    content: str | None  # content to store
    importance: float  # importance score if storing
```

#### Phase 4B: Multi-Level Reward Design

```python
class MemoryReward:
    """Multi-level reward for memory operations."""
    
    def compute_step_reward(
        self, 
        action: MemoryAction, 
        state: MemoryState, 
        outcome: Any
    ) -> float:
        """Dense step-level reward for memory operation correctness."""
        if action == MemoryAction.STORE:
            # Reward for storing relevant, non-redundant facts
            return self._relevance_score(outcome) - self._redundancy_penalty(outcome)
        elif action == MemoryAction.DELETE:
            # Reward for deleting irrelevant/stale memories
            return self._staleness_score(outcome)
        # ...
    
    def compute_trajectory_reward(
        self, 
        task_success: bool, 
        memory_operations: list[MemoryDecision]
    ) -> float:
        """Sparse trajectory-level reward for task completion."""
        return 1.0 if task_success else -0.1
    
    def compute_consistency_reward(
        self, 
        persona_consistency: float
    ) -> float:
        """Intermediate persona-consistency reward."""
        return persona_consistency * 0.1
```

#### Phase 4C: GRPO Training Pipeline

1. **Data collection**: Run agent on LoCoMo with random memory policies
2. **Reward labeling**: Score each memory operation with multi-level rewards
3. **GRPO training**: Train 7B model with step-wise GRPO
4. **Evaluation**: LoCoMo + LongMemEval + MSC

### Novel Contribution
**End-to-End Joint RL for Memory and Task**: A training pipeline that jointly optimizes memory operations and task answering using GRPO with multi-level rewards: (a) dense step-level rewards for memory operation correctness, (b) sparse trajectory rewards for task completion, (c) intermediate persona-consistency rewards. Leverage Nous's Bayesian belief update as a training signal. The key novelty is demonstrating stable end-to-end training where memory operations and task answering are jointly optimized, overcoming the reward sparsity that forced Memory-R1 to train separately.

**Evaluation**: LoCoMo + LongMemEval + MSC, measuring task success rate, memory F1, and training stability vs. separate training baseline.

---

## Gap 5: Open Standard for Agent Configuration (SoulSpec)

### Original Problem
No standardized structure for agent configuration. AGENTS.md files, identity definitions, voice, and behavioral guidelines are scattered across ad-hoc system prompts.

### Current State Assessment

| Component | Status | Gap |
|-----------|--------|-----|
| `AgentDef` (ah/core/agent_def.py) | Basic persona (name, description, system_prompt, tools) | No merge semantics, no conformance testing |
| `Skill` (ah/skills/registry.py) | SKILL.md with YAML frontmatter | No cross-framework compatibility |
| `BUILTIN_AGENTS` | Hardcoded harness/researcher/coder | Not portable |

### Research Findings

- **SoulSpec** (arXiv:2510.21413): Proposes format for AGENTS.md consolidation
- **AGENTS.md** standard: De facto standard, 75.5% cross-org propagation
- **SoulSpec v0.5**: Persona + workflow + skill manifests with 3-level progressive disclosure
- **Supply chain risk**: Agent configs are undeclared shared components (<1% permission declarations)

### Concrete Implementation Plan

#### Phase 5A: SoulSpec Schema

```yaml
# soulspec.yaml
name: "Harness Agent"
version: "1.0.0"
persona:
  name: "Harness"
  description: "General-purpose AI agent"
  values:
    - name: "accuracy"
      weight: 0.9
    - name: "helpfulness"
      weight: 0.8
  traits:
    - name: "analytical"
      strength: 0.8
  voice:
    tone: "professional"
    style: "concise"
  system_prompt: |
    You are {name}. {description}
    Core values: {values}
    Communication style: {voice.style}

workflow:
  - name: "code-review"
    description: "Review code for issues"
    steps:
      - "read_file"
      - "analyze"
      - "suggest_fixes"
    tools: ["read_file", "search_files"]
  
skills:
  - name: "python-expert"
    triggers: ["python", "pytest", "django"]
    path: "skills/python-expert.md"
    progressive_disclosure:
      level: 2  # load on demand

config:
  model: "anthropic/claude-3.5-sonnet"
  max_iterations: 10
  context_budget: 8000
```

#### Phase 5B: Merge Semantics

```python
class SoulSpecMerger:
    """Merge multiple SoulSpec configurations."""
    
    def merge(self, base: SoulSpec, override: SoulSpec) -> SoulSpec:
        """
        Merge rules:
        1. Scalar fields: override wins
        2. Lists: union with deduplication
        3. Dicts: recursive merge
        4. System prompts: concatenate with separator
        5. Permissions: intersection (most restrictive wins)
        """
```

#### Phase 5C: Conformance Test Suite

```python
class SoulSpecConformance:
    """Test that a SoulSpec conforms to the standard."""
    
    def validate_schema(self, spec: SoulSpec) -> ValidationResult:
        """Validate against JSON Schema."""
    
    def test_merge_semantics(self) -> None:
        """Test that merge works correctly."""
    
    def test_progressive_disclosure(self) -> None:
        """Test that skills load at correct disclosure level."""
    
    def test_cross_framework_portability(self) -> None:
        """Test that config can be exported to other frameworks."""
```

#### Phase 5D: Adapters

```python
class AgentHarnessAdapter:
    """Export AgentHarness config to SoulSpec."""
    
    def to_soulspec(self, agent_def: AgentDef) -> SoulSpec:
        """Convert AgentDef to SoulSpec YAML."""
    
    def from_soulspec(self, spec: SoulSpec) -> AgentDef:
        """Import SoulSpec YAML to AgentDef."""

class ClaudeCodeAdapter:
    """Export SoulSpec to Claude Code CLAUDE.md format."""

class CodexAdapter:
    """Export SoulSpec to Codex AGENTS.md format."""
```

### Novel Contribution
**SoulSpec: Open Standard for Agent Configuration**: Formalize SoulSpec as an open standard with: (a) YAML/JSON schema for agent configuration (name, voice, values, behavioral guidelines, AGENTS.md consolidation), (b) merge semantics for configuration inheritance/mixins, (c) conformance test suite (rule-based + LLM-judge), (d) adapters for Hermes, Claude Code, Codex, AgentHarness. Empirically measure configuration portability across frameworks.

**Evaluation**: Configuration portability test across 5 frameworks, measuring migration cost, feature coverage, and conformance test pass rate.

---

## Integration with Existing Infrastructure

### What We Have (Leverage)

| Infrastructure | Research Gap | How It Helps |
|----------------|-------------|--------------|
| `context_chunks` + pgvector | Gap 1 | Foundation for reversible eviction |
| `llm_usage` + advisory locks | Gap 1, 4 | Token tracking for RL rewards |
| `memories` + importance scoring | Gap 3 | Factual store for dual-stream memory |
| `AgentDef` + registry | Gap 5 | Foundation for SoulSpec |
| `Skill` + triggers | Gap 5 | Progressive disclosure model |
| `Orchestrator` | Gap 2 | Multi-agent coordination base |
| `agent_messages` | Gap 2 | Cross-agent messaging |
| `audit_events` | Gap 2 | Provenance tracking |
| `usage_store` budgets | Gap 4 | Reward signal for RL |

### What We Build (Novel)

| Component | Research Gap | Novelty |
|-----------|-------------|---------|
| `context_archive` + resurrection | Gap 1 | Reversible eviction with resurrection probability |
| TOON serialization | Gap 1 | Token-optimized context storage |
| `AgentBelief` + `IdentityGate` | Gap 2 | Identity propagation defense |
| `persona_memories` + `EmotionTopology` | Gap 3 | Persona-conditioned dual-stream memory |
| `MemoryAction` + GRPO training | Gap 4 | End-to-end joint RL for memory + task |
| `SoulSpec` schema + merge + adapters | Gap 5 | Open standard for agent configuration |

---

## Implementation Roadmap

### Phase 1: Foundation (Weeks 1-4)
- [ ] Gap 1A: Two-tier eviction with archive
- [ ] Gap 5A: SoulSpec schema definition
- [ ] Gap 2A: Agent identity model data structures

### Phase 2: Core Systems (Weeks 5-8)
- [ ] Gap 1B: TOON serialization
- [ ] Gap 2B: Shared memory bus with per-agent gating
- [ ] Gap 3A: Dual-stream memory architecture
- [ ] Gap 5B: Merge semantics

### Phase 3: Advanced Features (Weeks 9-12)
- [ ] Gap 1C: Adaptive compression
- [ ] Gap 2C: Drift detection and containment
- [ ] Gap 3B: Emotion topology
- [ ] Gap 4A: RL action space definition
- [ ] Gap 5C: Conformance test suite

### Phase 4: Training & Evaluation (Weeks 13-16)
- [ ] Gap 3C: Fine-tuned persona model
- [ ] Gap 4B: Multi-level reward design
- [ ] Gap 4C: GRPO training pipeline
- [ ] Gap 5D: Cross-framework adapters

### Phase 5: Publication (Weeks 17-20)
- [ ] LoCoMo + SWE-bench evaluation (Gap 1)
- [ ] Multi-agent drift evaluation (Gap 2)
- [ ] Persona benchmark evaluation (Gap 3)
- [ ] RL training stability evaluation (Gap 4)
- [ ] Configuration portability evaluation (Gap 5)

---

## Connection to Original Research Papers

| Gap | Key Papers | What We Take | What's New |
|-----|-----------|--------------|------------|
| 1 | Blast Radius, Focus, MemAgent | 2-tier eviction, LLM summarization | Resurrection probability, TOON format |
| 2 | ID-RAG, SAMEP, CogniPair | Belief models, provenance | Identity gate, drift containment |
| 3 | RoleMemo/DualMem, P-GEM | Dual-stream, emotion warping | Emotion topology, Plutchik's wheel |
| 4 | Memory-R1, Nous, ReMemR1 | RL for memory, multi-level rewards | End-to-end joint optimization |
| 5 | SoulSpec, AGENTS.md | Configuration format | Merge semantics, conformance testing |

---

## Risk Assessment

| Gap | Risk | Mitigation |
|-----|------|------------|
| 1 | Resurrection may not improve task success | Evaluate on LoCoMo first; fall back to simpler eviction |
| 2 | Identity gate may block legitimate memories | Start with permissive threshold, tighten based on evaluation |
| 3 | Persona model may not generalize | Use multiple personas; evaluate on diverse benchmarks |
| 4 | RL training may be unstable | Start with supervised warm-start; use conservative GRPO settings |
| 5 | Standard may not be adopted | Focus on adapters; demonstrate portability value |

---

## Success Criteria

Each gap must demonstrate:
1. **Novelty**: A clear contribution beyond existing work
2. **Evaluation**: Quantitative improvement on standard benchmarks
3. **Reproducibility**: Open-source implementation with tests
4. **Impact**: Measurable improvement over baseline systems

---

*This document is a living blueprint. Update as research progresses and implementation reveals new insights.*
