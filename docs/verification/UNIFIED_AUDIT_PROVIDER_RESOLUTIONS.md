# Provider audit resolutions

Scope: F-01, F-02, F-03, F-05, F-06, F-13, F-20 and F-23 from the unified audit dated 2026-10-15.

| Finding | Source resolution | Static path reviewed |
| --- | --- | --- |
| F-01 | Normalization accepts canonical contextWindow as well as upstream aliases, preserving windows across fetch then save. | security/providers.py normalize -> fetch_models -> save_models; CLI and gateway catalog callers. |
| F-02 | Ollama terminal frames retain valid nonnegative supplied token counts and resolved model in both reservation settlement and final response. Missing counts remain absent; total is computed only when both counts exist. | core/provider/ollama.py stream terminal -> usage_store.finish -> LLMResponse. |
| F-03 | A stop header alone cannot complete Anthropic output. Completion requires a decoded object whose type is message_stop. | core/provider/anthropic.py SSE header -> decoded payload -> EOF terminal guard. |
| F-05 | Shared usage identity helper prefers configured ProviderSpec.name and falls back to concrete provider class for native transports. | core/usage.py complete wrapper and core/agent.py streaming reservation. |
| F-06 | Maintenance, HTTP context, gateway context and CLI context use one services.session_context_window helper. It resolves session and agent definition precedence before provider-scoped capacity lookup. | services.py effective session route -> context_window_for(model, provider); api/routes/sessions.py; gateway/features/sessions.py; cli/__init__.py. |
| F-13 | Gateway marks catalogs live only following a successful fetch and save. Successful empty catalogs replace cached rows in gateway and CLI; failures preserve cache with an error. | gateway/features/providers_feature.py named models; cli/providers.py add and refresh. |
| F-20 | Kimi now uses the documented low/high/max vocabulary. Provider-aware normalization covers constructor defaults, per-call overrides and global config; gateway validation returns the same supported set. Medium is rejected on explicit Kimi requests; invalid inherited config logs and delegates to the endpoint default. | core/provider/specs.py -> reasoning.py provider-aware normalizer -> direct.py effort payload; gateway provider-aware validation. |
| F-23 | Compose forwards Kimi key, OpenCode key/base URL, and custom endpoint key/base URL using empty defaults. | docker-compose.yml environment -> provider factory/spec configuration. |

Validation is static only: source inspection and AST parsing of edited Python files, plus whitespace diff checks. No application imports, tests, runtime probes, database/provider calls or network requests were performed. Vendor compatibility, live model capacities, stream behavior and container startup remain unexecuted. The Kimi vocabulary follows the existing README integration contract; external vendor behavior has not been independently verified.
