---
name: yaml-agent-config
description: YAML-declared agent configuration — YAML agent specs, hexagonal architecture, configuration
  merge
triggers:
- yaml
- agent config
- hexagonal
- declarative
- merge
version: 1.0.0
schema_version: 2
skill_id: yaml-agent-config
task_families:
- backend-api
- refactoring
capabilities:
- configuration
aliases:
- agent-config
- yaml-agents
- hexagonal
- config-merge
- declarative-agents
stack:
  languages:
  - yaml
  - python
  frameworks: []
  independent: false
preconditions:
- Agents or services defined declaratively
- A configuration inheritance or override model
required_tools:
- read
- write
exclusions:
- Runtime environment variables as the only config
- Prompt text authoring (use prompt-engineering)
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

# YAML-Declared Agent Configuration

## Core Skills

| Skill | Practice |
|---|---|
| YAML agent specs | Define agents in YAML with tools, prompts, constraints |
| Hexagonal architecture | Ports and adapters pattern |
| Configuration merge | Merge semantics for configuration inheritance |

## Key Resources
- [composable-agents](https://github.com/SoluDevTech/composable-agents) — YAML-declared agents with hexagonal architecture

## Practice Project
Build a YAML-based agent definition system with configuration merge and hexagonal architecture.
