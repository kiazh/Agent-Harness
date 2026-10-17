---
name: prompt-engineering
description: Prompt engineering — system-instruction design, few-shot examples, context assembly, prompt
  templates, prompt testing
triggers:
- prompt
- system instructions
- few-shot
- context assembly
- template
version: 1.0.0
schema_version: 2
skill_id: prompt-engineering
task_families:
- research
capabilities:
- test-authoring
aliases:
- prompts
- system-prompts
- few-shot
- prompt-templates
- prompt-testing
stack:
  languages: []
  frameworks: []
  independent: true
preconditions:
- An LLM or agent whose instructions you can edit
- Example tasks or a benchmark to compare variants
required_tools: []
exclusions:
- Fine-tuning or training model weights
- Hardening against adversarial input (use prompt-injection-defense)
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

# Prompt Engineering

## Core Skills

| Skill | Practice |
|---|---|
| System instructions | Write instruction sections for different agent roles |
| Few-shot examples | Design few-shot examples for tool calling |
| Context assembly | Implement prompt assembler with token budget |
| Prompt templates | Reusable template library for common patterns |
| Prompt testing | A/B test prompts on benchmark tasks |

## Key Resources
- [OpenAI Prompt Engineering Guide](https://platform.openai.com/docs/guides/prompt-engineering)
- [Anthropic Prompt Engineering](https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering)

## Practice Project
Build a prompt testing framework: define prompt variants, run against benchmark tasks, measure quality metrics.
