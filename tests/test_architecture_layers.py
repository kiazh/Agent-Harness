"""Architecture layer guard (Stream A, A1).

Pins dependency direction between layers so new coupling cannot sneak in
unnoticed. Methodology (AST, stdlib only):

* "Module level" = an ``import`` / ``from`` statement with no function- or
  class-body ancestor (top-level ``if``/``try`` wrappers count: they execute
  at import time). Only absolute (``level == 0``) ``machina.*`` references
  are considered; intra-package relative imports are ignored.
* "Deferred" = an import statement with a function-body ancestor.
* Import counts are per statement (``from x import a, b`` = 1); line
  ceilings use ``wc -l`` semantics.

DEVIATION (evidence-backed): the literal "leaves import nothing from
``machina.core``" does not hold on the current tree — tools (21 stmts),
memory (14), permissions (1: shell.py), skills (4), rag (15), and soulspec
(4) all import ``machina.core.*`` at module level today, and ``machina.core``
itself imports leaves (agent.py, container.py) at module level. Enforcing
zero would fail on the current tree, so those packages are pinned as
ratchets instead: the per-package statement counts below may only shrink.
Genuinely clean boundaries keep literal assertions: connectors has zero
module-level ``machina.core`` imports, controller is limited to core+skills,
and repomap imports nothing from ``machina``.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "machina"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_machina_imports(path: Path) -> list[str]:
    """Absolute machina.* modules imported at module level (see methodology)."""
    found: list[str] = []

    def walk(node: ast.AST, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_nested = nested or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            if isinstance(child, ast.Import):
                if not nested:
                    for alias in child.names:
                        if alias.name == "machina" or alias.name.startswith("machina."):
                            found.append(alias.name)
            elif isinstance(child, ast.ImportFrom):
                if not nested and child.level == 0 and child.module:
                    if child.module == "machina":
                        found.extend(f"machina.{a.name}" for a in child.names)
                    elif child.module == "machina" or child.module.startswith("machina."):
                        found.append(child.module)
            walk(child, child_nested)

    walk(_tree(path), False)
    return found


def _facade_imports(path: Path) -> list[str]:
    """Imports (module level or deferred) that pull the provider facade itself.

    Used by the provider package guard: submodules must reach siblings only.
    """
    hits: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom) and child.level == 0:
                if child.module == "machina.core.provider":
                    hits.append("from machina.core.provider import ...")
                elif child.module == "machina.core" and any(
                    alias.name == "provider" for alias in child.names
                ):
                    hits.append("from machina.core import provider")
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name == "machina.core.provider":
                        hits.append("import machina.core.provider")
            walk(child)

    walk(_tree(path))
    return hits


def _is_core(mod: str) -> bool:
    return mod == "machina.core" or mod.startswith("machina.core.")


def _in_packages(mod: str, packages: tuple[str, ...]) -> bool:
    return any(mod == pkg or mod.startswith(pkg + ".") for pkg in packages)


def _deferred_import_count(path: Path) -> int:
    count = 0

    def walk(node: ast.AST, in_func: bool) -> None:
        nonlocal count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if in_func:
                    count += 1
            walk(child, in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    walk(_tree(path), False)
    return count


def _line_count(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def test_controller_module_imports_limited_to_core_and_skills():
    allowed = ("machina.core", "machina.skills", "machina.controller")
    bad: dict[str, list[str]] = {}
    for path in sorted((ROOT / "controller").rglob("*.py")):
        violations = [m for m in _module_machina_imports(path) if not _in_packages(m, allowed)]
        if violations:
            bad[str(path.relative_to(ROOT))] = violations
    assert not bad, f"controller imports outside core+skills at module level: {bad}"


def test_repomap_has_no_module_level_machina_imports():
    bad: dict[str, list[str]] = {}
    for path in sorted((ROOT / "repomap").rglob("*.py")):
        imports = [
            m for m in _module_machina_imports(path) if not _in_packages(m, ("machina.repomap",))
        ]
        if imports:
            bad[str(path.relative_to(ROOT))] = imports
    assert not bad, f"repomap imports machina at module level: {bad}"


# Current census of module-level machina.core import statements per leaf
# package (see DEVIATION above). Ratchets down, never up.
_CORE_IMPORT_CEILINGS = {
    "tools": 21,
    "memory": 14,
    "permissions": 1,
    "skills": 4,
    "rag": 15,
    "connectors": 0,
    "soulspec": 4,
    # stdio shim stays import-light: the agent runtime is imported lazily per
    # request, so its module-level core-import count is pinned at zero.
    "mcp_server": 0,
}

# Deferred (function-level) import cap, single-sourced for the test and for
# machina/research/benchmark.py's architecture section (round-2 review N6:
# the hand-mirrored literal drifted once already). Moves only with the delta
# table documented inside test_deferred_import_count_ratchet.
DEFERRED_IMPORT_CAP = 721


def test_module_level_core_imports_only_shrink():
    over: dict[str, str] = {}
    for package, ceiling in _CORE_IMPORT_CEILINGS.items():
        total = 0
        for path in sorted((ROOT / package).rglob("*.py")):
            total += sum(1 for mod in _module_machina_imports(path) if _is_core(mod))
        if total > ceiling:
            over[package] = f"{total} > {ceiling}"
    assert not over, f"new module-level machina.core imports: {over}"


def test_deferred_import_count_ratchet():
    # Ratchet history (guard-method census over machina/ only):
    #   658 = measured baseline at guard creation (the old 672 was a ceiling,
    #         not the count; wave-1 landings).
    #   677 = after wave-2 hook/isolation work: +19 total, itemized below.
    #         An earlier revision of this table attributed -3 to session_mode,
    #         but its deferred-import count did not change: the hoist that
    #         entry referred to is agent.py's turn_scope import, kept at
    #         module level (no cycle possible). The itemized deltas already
    #         sum to +19 (6+4+4+2+2+1+1-1+0):
    #         agent.py +6 (S3 wiring hooks, guarded), controller/wiring.py +4
    #         (new, guarded by purity tests), gateway/server.py +4 / usage.py
    #         +2 / orchestrator.py +2 / tools/agents.py +1 (S1+S2 observe-only
    #         hooks), container.py +1 (locator decoupling), repomap/scan.py -1
    #         (cargo-cult deferred import removed), api/app.py -45 balanced by
    #         api/routes/* +45 (split moved, not added).
    #   Wave-3 hotfix added +1 (permissions/broker.py guarded redaction
    #   import); wave-4 removed -1 net (agent.py -3: datetime/uuid/images
    #   hoisted to module level, +1 tool-trace hook; controller/wiring.py +2
    #   trace/evidence helpers). Net since wave-2: -1, so cap stays 677.
    #   Final wave (A5, 6a1131a): -1 (agent.py 31 -> 29 deferred imports after
    #   the turn_flow extraction; new core/turn_flow.py +1 guarded helper
    #   import). Census = 675. N1 (final pass): entry added to make the
    #   procedure table complete; the cap was already honest at 675 <= 677.
    #   Benchmark tool (research/benchmark.py, unified offline benchmark): +5
    #   (the four test-harness modules — replay/retrieval/prefix/architecture —
    #   are imported lazily per section because tests/ is not importable until
    #   sys.path setup and is absent from wheels; plus pytest for
    #   MonkeyPatch. The machina-side imports — version, locomo, db — are at
    #   module level, matching research/workflow_benchmark.py.) Census
    #   675 -> 680, so cap 677 -> 680.
    #   Auto-applied learning wave (measured on this worktree): median census
    #   at 7b0f83f was 677. Deltas since:
    #     +5  machina/mcp_server/ (landed commit a92c1e8, MCP shim lane):
    #         server, config, DB, and tool schemas are imported lazily per
    #         request so the stdio shim cannot pull the agent runtime at
    #         startup. That lane landed without its own ratchet update; the
    #         guard is tree-wide, so the +5 is counted here explicitly.
    #     +1  skills/learning.py (this change): ``_audit`` binds audit_log
    #         through a function-level core import so the skills module-level
    #         machina.core ceiling stays at 4, and the binding resolves per
    #         call so monkeypatching the provider audit module intercepts.
    #     -1  slop-cleanup wave (landed ca6fc05): pipeline._finish_search and
    #         env_file lost their function-level imports. Net verified by
    #         re-measuring the merged tree on 2026-10-14: census == 682.
    #   Census 677 -> 682, so cap 680 -> 682. Unreviewed growth beyond this
    #   still fails by design; itemize any further movement here.
    #   Interop self-setup wave (2026-10-14): +1 machina/interop.py — the
    #   module's only deferred import is ``_child_process`` inside the
    #   helper, binding the stdlib-only leaf lazily so importing
    #   ``machina.interop`` never pulls the agent runtime (same idiom as the
    #   mcp_server shim). Census 682 -> 683, so cap 682 -> 683.
    #   Provider wave (2026-10-14): +1 machina/core/runtime.py — the chatgpt
    #   readiness branch binds chatgpt_auth.status lazily so a status probe
    #   never imports the auth/provider modules unless chatgpt is selected.
    #   Census 683 -> 684, so cap 683 -> 684.
    #   Round-5 interop review (2026-10-14): +1 machina/interop.py — the
    #   enable/disable output tail embedded in ActionResult steps is now
    #   secret-redacted (N2'); the helper binds machina.memory.redaction
    #   lazily so importing machina.interop stays stdlib-only (same idiom as
    #   the mcp_server shim). The F2 extraction of run_bounded into
    #   machina/core/child_process.py added no deferred imports (stdlib
    #   imports, module level) and kept both callers' per-call
    #   ``_child_process`` bindings, so the census moves by exactly +1.
    #   Census 684 -> 685, so cap 684 -> 685.
    #   Model-catalog lane (2026-10-14, this change): +4 deferred imports,
    #   each a deliberate cycle/dependency-delaying binding:
    #     +1 machina/core/provider/catalog.py — chatgpt_auth is imported
    #        inside fetch_catalog_for_account so the catalog module (which
    #        context_windows imports) stays loadable without the auth stack.
    #     +1 machina/core/provider/factory.py — security.providers binds
    #        inside _named_provider so provider resolution works without the
    #        user store module unless a named provider is actually requested.
    #     +1 machina/core/provider/specs.py — security.providers binds inside
    #        _named_provider_names so dynamic PROVIDERS membership keeps a
    #        core->security edge only at call time.
    #     +1 machina/core/context_windows.py — catalog.window_for binds inside
    #        the chatgpt branch so the window lookup stays a light leaf for
    #        its many lazy importers. (All other new imports in this lane were
    #        hoisted to module level; the census moved by exactly +4.)
    #   Census 685 -> 689, so cap 685 -> 689.
    #   Round-3 review fixes (2026-10-14, this change): +0 deferred imports.
    #   The chatgpt multimodal converter, the direct-provider cache
    #   namespace, the endpoint-aware embedder id normalization and the
    #   child-process overflow kill all use module-level (stdlib) imports.
    #   Subscription-auth provider wave (2026-10-14, this change): +3 deferred
    #   imports, each a deliberate readiness-probe binding:
    #     +3 machina/core/runtime.py — claude/copilot/gemini readiness
    #        branches bind their security.*_auth status probes lazily so the
    #        provider probe never imports the auth stack unless selected.
    #   Census 689 -> 692, so cap 689 -> 692.
    #   Vendor-compliance removal (2026-10-14): -2 deferred imports — the
    #   claude/gemini readiness branches are gone with their subscription
    #   flows (owner-directed; vendor terms). Census measured at 689 on this
    #   tree after the removal, so cap 692 -> 689.
    #   R4-091 skills/soulspec cluster (2026-10-14, this change): +1 deferred
    #   import — skills/registry.py binds repo_install.remove_installed_skill
    #   inside delete_skill so the registry's top-level imports stay the
    #   ports+observability seam (test_architecture_pairs pins that set).
    #   Census 687 -> 688 over the committed base (measured); the cap is
    #   unchanged at 689 because it already binds the grown census.
    #   no-mistakes gate review rounds (2026-10-15, R1-R24): 689 -> 712
    #   (+23). agent.py +5 (joined teardown + premature-exit finalization),
    #   connectors/service.py +4, agent_factory.py +4 (provider ownership
    #   scope), api/routes/streaming.py +2 (relay closure), connectors/
    #   management.py +2, connectors/store.py +2 (layering: the core import
    #   moved from module scope into the two async wrappers), core/usage.py
    #   +2, core/turn_engine.py +1, tools/file.py +1.
    #   gate-reconciliation follow-up (2026-10-15): +1 agent_factory.py
    #   (cancel_and_join deferred import in close_agent_provider).
    #   harnesses feature (2026-10-15): +4 gateway/features/interop.py
    #   (lazy bindings to machina.interop for detect/enable/disable/
    #   run_task so the gateway module never imports the runner eagerly).
    #   holistic-review fix wave (2026-10-15): +4 net.
    #     core/scheduler.py +3 (job/session ownership reconciliation helper),
    #     core/turn_engine.py -1 (bounded source-close refactor),
    #     services.py +1 (effective session-route helper for explicit
    #     compression), memory/retriever.py +1 (shared
    #     prepare_query_embedding seam). New modules +0 (firecrawl_lifecycle
    #     imports at module level; streaming.py unchanged).
    #   Census 721, cap 721.
    # Procedure: this number may only move with a delta table like the above
    # in this comment; silent growth fails the suite by design.
    total = sum(_deferred_import_count(p) for p in ROOT.rglob("*.py"))
    assert total <= DEFERRED_IMPORT_CAP, (
        f"deferred (function-level) imports grew: {total} > {DEFERRED_IMPORT_CAP}"
    )


# God-file ceilings. Ratchets down, never up, except with a reviewed delta
# table like the deferred-import ratchet above:
#   core/provider.py 2212 -> 2381 (FM2 wave, audited fixes): response-cache
#     identity isolation + TTL/size bound + defensive copies, stream 429
#     retry parity for direct/anthropic/ollama, explicit mid-stream error
#     frames, Ollama missing-message guard, fallback-chain cache skip,
#     Retry-After HTTP-date, reasoning-effort config warning, runtime
#     rate-limiter reload; offset by removing the dead embed implementations
#     (-26) and a duplicated free-tier clamp (-1). No unreviewed growth may
#     land on top of this.
#   core/provider.py 2381 -> 2554 (E1 pilot wave): config-gated prompt-cache
#     breakpoints — payload-only markers on the system tail + last tool for
#     Anthropic, OpenRouter passthrough only for Anthropic-routed models,
#     direct/ollama no-ops, and OFF-path wire-byte pins; transport logic
#     unchanged.
#   core/provider.py 2554 -> 2576 (audit-remediation wave): SSE terminal-state
#     validation — saw_terminal tracking ([DONE] or non-null finish_reason)
#     across the DirectProvider loop and both OpenRouter parse sites; a stream
#     ending without terminal state now raises ProviderError before any done
#     event. Unreviewed growth beyond this is rejected.
#   core/provider.py 2576 -> core/provider/ package (W5 split): facade + 13
#     focused modules; ``__all__`` byte-identical; +228 lines are per-module
#     import headers and docstrings only (all 41 class/function bodies verified
#     byte-identical to the old module; the five function-level imports moved
#     verbatim, so the deferred-import census is unchanged). Per-file ceilings
#     are the measured split landing values; the package total is pinned
#     separately below.
#   core/provider/anthropic.py 462 -> 564, core/provider/ollama.py 369 -> 378
#     (R06/R08 wave): Anthropic native content-block preservation through
#     response parsing, streaming accumulation, and message assembly (signed
#     thinking/redacted_thinking blocks replay verbatim on tool continuations),
#     plus protocol terminal markers for both stream transports (``message_stop``
#     / ``done: true``) so clean EOF without them raises ProviderError. The
#     shared carrier key lives in core/prompt.py (not pinned here).
#     Final observed delta after the wave draft: anthropic/package +2 (564/2915).
#   core/provider chatgpt lane (2026-10-14): +596 lines — new
#     core/provider/chatgpt.py (590): ChatGPT-account Codex-protocol transport
#     (Responses SSE parsing with response.completed terminal discipline,
#     function-call accumulation, 401 -> refresh -> retry-once, 429 ->
#     RateLimitError, usage mapping); factory.py 39 -> 43 (chatgpt branch),
#     __init__.py 51 -> 52 (export), specs.py 103 -> 104 (PROVIDERS entry).
#     Package total 2915 -> 3511 by exactly this delta.
#   free-models lane (2026-10-14): +67 provider lines — the generic "custom"
#     OpenAI-compatible provider (factory.py 43 -> 87: dynamic spec from
#     custom_base_url with optional key, explicit model requirement, and the
#     opencode base-url override; specs.py 104 -> 125: ProviderSpec.requires_key
#     + OpenCode Zen preset + "custom" PROVIDERS entry; direct.py 337 -> 339:
#     keyless specs send no Authorization header). Package total 3511 -> 3578.
#   model-catalog lane (2026-10-14, this change): +622 provider lines —
#     new core/provider/catalog.py (508): live ChatGPT/Codex catalog fetch
#     (client_version newest-first, ChatGPT-Account-ID), TTL + negative-TTL
#     in-process cache, Codex CLI cache read-back, bundled verified fallback,
#     per-model effort/slug validation, context windows. chatgpt.py 590 -> 616:
#     catalog-validated model slug + per-model effort resolution replacing the
#     global normalize_reasoning_effort call. factory.py 87 -> 134: named
#     user-added providers resolve through machina.security.providers with a
#     cached-default-model fallback and an explicit no-models error.
#     specs.py 125 -> 166: dynamic PROVIDERS membership for named providers
#     (tuple subclass __contains__) + provider_names() listing helper.
#     Package total 3578 -> 4200 by exactly this delta.
#   review round 6 R6-1 (2026-10-14): +58 in the provider package — the
#     catalog's read helpers are now account-scoped (cached_catalog /
#     live_models_or_none / window_for / supported_efforts_for /
#     validate_model_slug / validate_effort take account_id+base_url; the
#     unknown-account fallback is now genuinely most-recent via
#     _most_recent_live) and chatgpt.py passes a best-effort _account_hint so
#     validation can never consult another account's live list. catalog.py
#     508 -> 553, chatgpt.py 616 -> 629. Package total 4200 -> 4258 by
#     exactly this delta.
#   Round-3 review fixes + subscription-auth provider wave (2026-10-14,
#   measured on this shared worktree; each lane owns its final numbers):
#     +49 core/provider/chatgpt.py — user multimodal content lists translate
#         to Responses content parts (input_text / input_image with a plain
#         image_url string) instead of a serialized text block (R3-F5).
#     +8  core/provider/direct.py — the response-cache namespace now carries
#         the endpoint and credential digest, so a named provider re-pointed
#         at another base URL cannot serve the previous host's cached body
#         within the TTL (R3-F6).
#     +1166 subscription-auth provider wave (claude / copilot / gemini
#         first-class providers): new core/provider/claude.py (109) — the
#         Claude Pro/Max transport (thin AnthropicProvider subclass with an
#         httpx auth flow: per-request Bearer resolution, 401 -> refresh ->
#         replay once, OAuth identity headers); new core/provider/copilot.py
#         (425) — the Copilot seat transport (OpenAI SSE with tool-call
#         accumulation, per-account model_not_supported -> typed error, 401
#         typed, no refresh); new core/provider/gemini.py (611) — the Cloud
#         Code Assist transport (loadCodeAssist/onboardUser project
#         resolution with persisted project, streamGenerateContent SSE,
#         finishReason terminal discipline, 401 -> refresh -> retry once);
#         __init__.py 52 -> 55 (three exports), anthropic.py 564 -> 577
#         (PROVIDER_NAME label so the shared transport can report itself as
#         claude, plus formatter rewraps of the long audit calls),
#         factory.py 134 -> 146 (three branches), specs.py
#         166 -> 179 (PROVIDERS tuple + comment). All new-file counts are
#         post-``ruff format`` measurements.
#     Package total 4258 -> 5501 by exactly the combined delta (57 round-3
#     fixes + 1186 subscription-auth wave). Unreviewed growth beyond this is
#     rejected.
#   Vendor-compliance removal (2026-10-14): -720 provider lines — deleted
#     core/provider/claude.py (109) and core/provider/gemini.py (611), the
#     subscription OAuth transports (vendor terms; owner-directed). The
#     factory branches/imports shrink factory.py 146 -> 138 and
#     __init__.py 55 -> 53; the same wave adds the Kimi Code spec + registry
#     comment, specs.py 179 -> 188 (+9). Package total 5501 -> 4780 by
#     exactly this delta (measured post-``ruff format``).
#   Round-4/5 review fixes (2026-10-15, measured post-``ruff format`` on the
#   settled tree; each lane owns its numbers):
#     +55 core/agent.py (2055 -> 2064 -> 2110): R4-025 removed the
#         max_iterations surface and added the PB-1 flush log-and-continue
#         (+9 net), then R5-F1 serialized the consolidation cursor
#         read+extract+checkpoint under the advisory lock (+46).
#     +103 core/agent.py (2110 -> 2213, final-review core/runtime fix lane):
#         HELD-55 F1 3-key consolidation cursor — reader 2-/3-tuple parse +
#         seq-aware persist + non-built-in since unpacking (+21);
#         F02 cancellation finalization — `_tool_entry` / `_record_tool_action`
#         extraction (Phase D body deduped, net +25), `_finalize_completed_tools`
#         (+27) and the Phase C/D cancellation guards (+10); core-runtime F2
#         `run()` immediate-flush cancellation wrapper mirroring `run_stream`
#         (+6); code_review #6 truthful flush contract — `bool` return +
#         confirmed-flush clearing in `_flush_pending_cancellation_safe` (+15).
#         Offset by the removed duplicated Phase D recording body. No
#         unreviewed growth may land on top of this.
#     Provider package +178:
#      +72 core/provider/audit.py (R5-N; URL-credential redaction — userinfo
#          passwords and credential query/fragment values, shared by audit
#          emission, the RAG pipeline and audit persistence).
#      +46 core/provider/sse.py (new; R5-GH shared SSE field parser with the
#          spec's optional space after the colon).
#      +41 core/provider/rate_limit.py (R5-GH RateLimitBinding: per-access
#          registry resolve, override setter, unconditional budget
#          validation in reload_rate_limiters).
#      +10 core/provider/ollama.py (R5-GH endpoint-scoped cache identity).
#      +3 core/provider/anthropic.py, +2 chatgpt.py, +2 copilot.py,
#          +1 direct.py, +1 openrouter.py (R5-GH shared-parser wiring).
#     Package total 4780 -> 4958 by exactly this delta.
#   Final-review redaction wave (2026-10-15): +44 provider lines —
#     +35 core/provider/audit.py (219 -> 254): URL/presigned-credential
#     redaction (Azure sig / AWS X-Amz-Signature / X-Goog forms), the DB
#     password group consumes to the last @, password-only userinfo, and
#     reserved event/timestamp kwargs. +9 core/provider/sse.py (46 -> 55):
#     one leading UTF-8 BOM is stripped in the shared field parser.
#     Package total 4958 -> 5002 by exactly this delta.
#   no-mistakes gate review rounds (2026-10-15, R1-R24): one joined-teardown
#   ownership contract across the agent/streaming/connectors boundaries.
#     +54 core/agent.py (2213 -> 2267): joined teardown, premature-exit
#         finalization (CancelledError/GeneratorExit/disconnect), flush
#         persistence joins, retirement result handling.
#     +4 core/provider/chatgpt.py (680 -> 684) and +4 copilot.py (427 -> 431):
#         relay closure joins around provider streams.
#     -7 core/provider/audit.py (254 -> 247): redundant signature entries
#         removed (suffix rules already cover them).
#     Package total 5002 -> 5003 by exactly this delta (-7 +4 +4).
#   holistic-review fix wave (2026-10-15): +14 lines across two files:
#     +2 core/agent.py (2267 -> 2269): configured temperature forwarded on
#         the completion and streaming paths (H11).
#     +12 core/provider/copilot.py (431 -> 443): HTTP-200 SSE error frames
#         recognized before choices and raised as typed ProviderError while
#         preserving partial-output accounting (H12).
#     Provider package total 5003 -> 5015 by exactly this delta.
GOD_FILE_CEILINGS: tuple[tuple[str, int], ...] = (
    ("core/agent.py", 2269),
    ("core/compression.py", 999),
    ("core/provider/__init__.py", 53),
    ("core/provider/_log.py", 10),
    ("core/provider/reasoning.py", 61),
    ("core/provider/specs.py", 188),
    ("core/provider/audit.py", 247),
    ("core/provider/rate_limit.py", 120),
    ("core/provider/caches.py", 97),
    ("core/provider/transport.py", 382),
    ("core/provider/base.py", 60),
    ("core/provider/sse.py", 55),
    ("core/provider/openrouter.py", 608),
    ("core/provider/ollama.py", 388),
    ("core/provider/direct.py", 348),
    ("core/provider/anthropic.py", 580),
    ("core/provider/chatgpt.py", 684),
    ("core/provider/factory.py", 138),
    ("core/provider/catalog.py", 553),
    ("core/provider/copilot.py", 443),
)


@pytest.mark.parametrize("relative_path,ceiling", GOD_FILE_CEILINGS)
def test_god_file_ceilings_only_fall(relative_path, ceiling):
    assert _line_count(ROOT / relative_path) <= ceiling, (
        f"{relative_path} grew past its ceiling of {ceiling} lines"
    )


# Provider package total (W5 split). Ratchets down, never up: keeps the split
# from silently regrowing into one god module or sprawling into new files.
# 2915 = 2804 at split landing + 111 for the reviewed R06/R08 delta above.
# 3511 = 2915 + 596 for the reviewed chatgpt-lane delta above.
# 3578 = 3511 + 67 for the reviewed free-models lane delta above.
# 4200 = 3578 + 622 for the reviewed model-catalog lane delta above
#        (new catalog.py + chatgpt/factory/specs growth).
# 4258 = 4200 + 58 for the reviewed round-6 R6-1 account-scoping delta above.
# 5501 = 4258 + 57 for the round-3 review fixes (chatgpt.py +49, direct.py
#        +8) + 1186 for the subscription-auth provider wave (claude/copilot/
#        gemini first-class providers; itemized in the delta table above).
# 4780 = 5501 - 720 for the vendor-compliance removal (claude.py + gemini.py
#        deleted) - 8 (factory.py branches/imports) - 2 (__init__.py exports)
#        + 9 (specs.py: the Kimi Code spec + registry comment, same wave).
_PROVIDER_PACKAGE_TOTAL_CEILING = 5015


def test_provider_package_total_only_falls():
    package = ROOT / "core" / "provider"
    total = sum(_line_count(p) for p in sorted(package.rglob("*.py")))
    assert total <= _PROVIDER_PACKAGE_TOTAL_CEILING, (
        f"core/provider package grew past its ceiling of "
        f"{_PROVIDER_PACKAGE_TOTAL_CEILING} lines: {total}"
    )


def test_provider_submodules_do_not_import_the_facade():
    """Provider submodules import siblings only — never the package facade.

    A submodule importing ``machina.core.provider`` would re-enter the
    partially initialized package and make import order load-bearing (the
    classic package-split cycle).
    """
    bad: dict[str, list[str]] = {}
    for path in sorted((ROOT / "core" / "provider").glob("*.py")):
        if path.name == "__init__.py":
            continue
        hits = _facade_imports(path)
        if hits:
            bad[path.name] = hits
    assert not bad, f"provider submodules must not import the facade: {bad}"
