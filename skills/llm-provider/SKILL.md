---
name: llm-provider
description: LLM provider abstraction — OpenAI-compatible API, streaming, tool calling, token counting,
  retry logic, cost tracking, model fallback
triggers:
- provider
- openrouter
- ollama
- streaming
- tool calling
- token count
version: 1.0.0
schema_version: 2
skill_id: llm-provider
task_families:
- backend-api
- maintenance
capabilities:
- api-design
- error-handling
- observability
- configuration
aliases:
- provider-abstraction
- llm-client
- openai-compatible
- model-fallback
- streaming-client
- openrouter
- provider-retry
stack:
  languages:
  - python
  - typescript
  frameworks:
  - openai
  - litellm
  - openrouter
  - ollama
  independent: true
preconditions:
- Provider API keys or a local runtime are configured
- Target models and their context limits are known
- A token counting library is available
required_tools:
- tiktoken
exclusions:
- Provider access is already fully abstracted and only prompt text changes
- No outbound model calls are involved
- Model quality benchmarking rather than client plumbing
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

# LLM Provider Abstraction

## Core Skills

| Skill | Practice |
|---|---|
| OpenAI-compatible API | Works with OpenRouter, Nous, Ollama |
| Streaming | Token-level output |
| Tool calling | Function calling with JSON Schema |
| Token counting | tiktoken for accurate counts |
| Retry logic | Exponential backoff with jitter |
| Cost tracking | Track tokens in/out, estimate cost |
| Model fallback | Primary → secondary → local |

## Key Resources
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [OpenRouter docs](https://openrouter.ai/docs)
- [tiktoken](https://github.com/openai/tiktoken)
- [LiteLLM](https://github.com/BerriAI/litellm)

## Practice Project
Build a provider abstraction: LLMProvider interface with chat(), stream(), count_tokens(). Implement for OpenRouter and Ollama. Add retry + fallback.
