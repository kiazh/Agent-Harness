---
name: langgraph
description: "LangGraph orchestration — StateGraph, conditional edges, PostgresSaver checkpointing, streaming, subgraphs, human-in-the-loop, multi-agent graphs"
triggers:
  - "langgraph"
  - "stategraph"
  - "checkpoint"
  - "streaming"
  - "subgraph"
  - "human-in-the-loop"
version: 1.0.0
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
