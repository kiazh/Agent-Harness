# Bundled ECC subagents

AgentHarness ships all **71 canonical subagents** from
[ECC's agents directory](https://github.com/affaan-m/ECC/tree/b92f181b3b748d1abbc2afd94af6aa7d8c8fca29/agents),
pinned at `b92f181b3b748d1abbc2afd94af6aa7d8c8fca29`, alongside the three core
agents (`harness`, `researcher`, `coder`). This import contains agent definitions
only. It adds no ECC skills, hooks, commands, installations or MCP configuration.

## Use

Select **Delegate** in the slash menu, choose an agent, and enter its task.
`/delegate planner <task>` also works. `/agents` lists available definitions;
`/agents show planner` shows the prompt and tool scope. The model can discover
specialists with `list_agents` and use the existing `delegate` tool. Adding
this catalog does not launch agents automatically or insert all their prompts
into normal turns. Only the selected role's prompt goes into its agent session.

Definitions are built-in defaults with source `ecc`; they need no database
seeding, startup download, optional extra or installation step. They also remain
available when `AGENT_HARNESS_AGENTS_DIR` points to custom YAML agents. As with
the core agents, gateway save/delete operations cannot replace or remove these
built-in names. Existing database lookup precedence is unchanged.

## Harness adaptation

Original Markdown files are preserved byte-for-byte under
`ah/bundled_agents/ecc/`. `manifest.json` records the upstream revision, file
checksums, original model preferences and tool names. The original MIT notice
is included in `LICENSE` in that directory.

The adapter in `ah/core/agent_def.py` maps tool names to registered harness
tools and prepends a short integration note to each selected role's prompt:

| ECC tool | Harness tool |
| --- | --- |
| Read | read_file |
| Grep | search_files |
| Glob | list_files |
| Write, Edit | write_file |
| Bash | terminal |
| WebSearch | web_search |
| WebFetch | web_extract |

Upstream `opus`, `sonnet` and `haiku` preferences remain provenance, not forced
provider switches. ECC definitions use the harness's ordinary model/provider
resolution. Read-only definitions stay read-only; no empty allow-list silently
becomes all-tool access. Child tool authority intersects the parent's caps and
the ordinary permission broker still handles execution and approvals.

Some upstream agents reference Context7 or Playwright MCP tools. Those tools
are not registered by this import; the selected agent's integration note names
unavailable tools and tells it to report missing capabilities. References to
external ECC skills, slash commands or platform-specific paths are retained
in original prompts, but must be inspected rather than assumed installed.
Importing an agent definition is not proof that every external workflow in its
prompt can run on this machine.

## Reproduce and verify

From an unmodified local ECC checkout at the pinned revision:

```sh
.venv/bin/python -m tools.vendor_ecc_agents /path/to/ECC
```

The importer also accepts an agents-only downloaded snapshot containing the
canonical `agents/*.md`, `LICENSE`, and a `source.json` recording its `commit`.
It validates the revision and agent metadata, copies only agent Markdown and
the license, and generates checksums. It does not execute upstream code.
When deliberately updating the pin, update the import constant and coverage
expectations, inspect source changes and optional tool requirements, then verify
both the checkout and built wheel. Do not re-vendor the skills bundle to update
subagents: it has a separate pin and scope.

Tests in `tests/test_ecc_agents.py` cover the complete pinned inventory,
checksums/license, real registered tool scopes, independent custom directory,
gateway catalog, selected prompt/model construction, parent authority, and an
existing-orchestrator delegation using a real dedicated test database and a
deterministic fixture agent. The fixture test makes no paid model request.
Wheel inspection and an import from an extracted wheel outside this checkout
verify offline packaging. Live model quality and third-party MCP services are
not exercised by these checks.
