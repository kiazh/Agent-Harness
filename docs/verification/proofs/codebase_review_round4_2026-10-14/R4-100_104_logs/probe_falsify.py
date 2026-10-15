"""R4-100 falsification probe (evidence only; never collected as a test).

Deliberately violates each asserted fact through the same seam the tightened
test reads, so every tightened assertion must FAIL here. The identically
targeted run without ``-p probe_falsify`` passes — that pair is the proof the
replacements discriminate where the old ``or True`` / ``>= 0`` / ``isinstance``
forms could not.

Usage:
    PYTHONPATH=.../R4-100_104_logs .venv/bin/python -m pytest -p probe_falsify \
        <the five node ids> -q
"""

from __future__ import annotations

import dataclasses


def pytest_runtest_setup(item) -> None:
    name = item.name
    if name == "test_stage_activate_roundtrip_and_registry_pickup":
        # Simulate a registry pickup regression: content no longer served.
        from machina.skills.registry import SkillRegistry

        SkillRegistry.get_skill_content = lambda self, skill_name: None
    elif name == "test_reranker_passthrough_labeled":
        # Simulate an unlabeled/foreign reranker implementation.
        from machina.rag.reranker import IdentityReranker

        IdentityReranker.backend = "unknown"
    elif name == "test_models_list_chatgpt_returns_live_catalog":
        # Simulate the catalog wrongly advertising an ultra tier on luna.
        from machina.core.provider import catalog

        real_parse = catalog.parse_catalog

        def mutated_parse(payload, **kwargs):
            models = real_parse(payload, **kwargs)
            return [
                dataclasses.replace(m, advertised_efforts=tuple(m.advertised_efforts) + ("ultra",))
                if getattr(m, "slug", "") == "gpt-6-luna"
                else m
                for m in models
            ]

        catalog.parse_catalog = mutated_parse
    elif name == "test_eviction_with_archive_failure_falls_back_to_individual":
        # Simulate the regression the test names: the individual fallback
        # silently evicts nothing (old assertion ``evicted >= 0`` stayed green).
        from machina.core import context as ctx_mod

        async def evicts_nothing(self, *args, **kwargs):
            return 0

        ctx_mod.ContextManager.evict_old_chunks = evicts_nothing
    elif name == "test_retrieve_by_query":
        # Simulate a retriever regression returning no rows (old assertion
        # ``isinstance(results, list)`` stayed green).
        from machina.memory import retriever as retriever_mod

        async def retrieves_nothing(self, *args, **kwargs):
            return []

        retriever_mod.MemoryRetriever.retrieve = retrieves_nothing
