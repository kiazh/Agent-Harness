---
name: langgraph
description: LangGraph orchestration — StateGraph, conditional edges, PostgresSaver checkpointing, streaming,
  subgraphs, human-in-the-loop, multi-agent graphs
triggers:
- langgraph
- stategraph
- checkpoint
- streaming
- subgraph
- human-in-the-loop
version: 1.0.0
schema_version: 2
skill_id: langgraph
task_families:
- backend-api
- maintenance
capabilities:
- api-design
- error-handling
- configuration
aliases:
- lang-graph
- stategraph
- langgraph-checkpointing
- agent-graph
stack:
  languages:
  - python
  frameworks:
  - langgraph
  - langchain
  - postgresql
  independent: false
preconditions:
- LangGraph is installed and graph code lives in this repository
- A checkpoint store such as PostgreSQL is available for persistence
- Agent nodes and transitions are defined or definable
required_tools:
- python
exclusions:
- No graph or state-machine orchestration is involved
- A simple linear chain is sufficient
- Only provider client plumbing (use llm-provider)
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# LangGraph Orchestration

## Core Skills

| Skill | Practice |
|---|---|
| StateGraph basics | 3-node graph: planner → tool → reflect |
| Conditional edges | If tool fails → retry, else → reflect |
| PostgresSaver | Agent state survives crashes |
| Streaming | Token-level output from graph nodes |
| Subgraphs | Compose complex agents from simple ones |
| Human-in-the-loop | Interrupt for approval |
| Multi-agent graphs | 2 agents sharing state |

## Key Resources
- [LangGraph docs](https://langchain-ai.github.io/langgraph/)
- [LangGraph tutorials](https://langchain-ai.github.io/langgraph/tutorials/)
- [LangGraph checkpointing](https://langchain-ai.github.io/langgraph/concepts/persistence/)

## Practice Project
Build a ReAct agent with LangGraph: Thought → Action → Observation loop, checkpointed to PostgreSQL, streaming tokens to terminal.
