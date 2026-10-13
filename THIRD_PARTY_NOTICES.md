# Third-party notices

## AI Research Skills (Orchestra Research)

Source: https://github.com/orchestra-research/AI-research-SKILLs
Retrieved: 2026-10-13 (shallow clone of `main`; no pinned revision recorded)
License: MIT (each skill carries `license: MIT` in its SKILL.md frontmatter)

98 AI research/engineering skills (vLLM, TRL/GRPO, FAISS, DSPy, evaluation
harnesses, paper writing, and others) installed as `skills/<name>/` with
upstream directory contents preserved (SKILL.md plus references/scripts/assets
where shipped). Five skills (`grpo-rl-training`, `sglang`, `evolving-ai-agents`,
`nemo-guardrails`, `prompt-guard`) trip the harness's own prompt-injection
scanner on benign documentation strings and are therefore skipped at registry
load time (fail-closed); see the install report. No upstream hooks, services or
executable installers are activated; skill scripts run only through authorized
tools.

## ECC (Everything Claude Code)

Source: https://github.com/affaan-m/ECC
Revision: 4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2
Copyright (c) 2026 Affaan Mustafa
License: MIT; the complete license is retained in `skills/ecc/LICENSE`.

A curated set of 42 canonical skills and retained reference dependencies are copied unchanged under `skills/ecc/` (88 upstream files). This is a subset, not the complete ECC distribution. The selection is in `tools/vendor_ecc.py`; the manifest records omitted references. Harness-specific routing and adapter code lives outside that directory. Files with additional source-level notices retain those notices. Bundling does not install upstream hooks or third-party services.

## ECC subagents

The 71 canonical subagent definitions are separately pinned to revision
`b92f181b3b748d1abbc2afd94af6aa7d8c8fca29` of the same ECC repository.
Original Markdown and MIT license are retained under `machina/bundled_agents/ecc/`.
The manifest records source paths and SHA-256 hashes. See
[the integration notes](docs/ECC_AGENTS.md) for tool mappings and limitations.
No upstream hooks, services or executable installers are activated.
