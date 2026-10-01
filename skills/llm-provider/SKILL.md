---
name: llm-provider
description: "LLM provider abstraction — OpenAI-compatible API, streaming, tool calling, token counting, retry logic, cost tracking, model fallback"
triggers:
  - "provider"
  - "openrouter"
  - "ollama"
  - "streaming"
  - "tool calling"
  - "token count"
  - "retry"
version: 1.0.0
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
