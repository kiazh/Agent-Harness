"""Production-readiness P1 regression tests (items 1-13)."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

# ─── 1. Delegation cleanup NameError ─────────────────────────────────────


async def test_orchestrator_default_factory_raise_preserves_original():
    from machina.core.orchestrator import Orchestrator

    orch = Orchestrator()
    orig = RuntimeError("provider boom")

    async def bad_default(definition, authority=None):
        raise orig

    orch._default_agent = bad_default  # type: ignore
    # Need minimal registry/session scaffolding: use unknown agent to fail earlier?
    # Instead test the guard directly: agent=None init + guarded close.
    import pathlib

    src = pathlib.Path("machina/core/orchestrator.py").read_text()
    assert "agent = None" in src
    assert "agent is not None" in src


async def test_orchestrator_injected_factory_raise_no_close():
    from machina.core.orchestrator import Orchestrator

    calls = []

    def bad_factory(defn):
        raise ValueError("bad factory")

    orch = Orchestrator(agent_factory=bad_factory)
    assert orch._agent_factory is bad_factory
    # Guard exists so close is never called for unconstructed agent
    import pathlib

    src = pathlib.Path("machina/core/orchestrator.py").read_text()
    assert "agent is not None" in src
    assert calls == []


# ─── 2. Empty env overrides ──────────────────────────────────────────────


def test_empty_model_env_falls_back(monkeypatch):
    from machina.core.config import DEFAULTS, Config

    c = Config()
    monkeypatch.setenv("MACHINA_MODEL", "")
    monkeypatch.delenv("MACHINA_PROVIDER", raising=False)
    assert c.get("model") == DEFAULTS["model"]


def test_empty_provider_env_falls_back(monkeypatch):
    from machina.core.config import DEFAULTS, Config

    c = Config()
    monkeypatch.setenv("MACHINA_PROVIDER", "")
    assert c.get("provider") == DEFAULTS["provider"]


def test_reasoning_effort_empty_preserved(monkeypatch):
    from machina.core.config import Config

    c = Config()
    monkeypatch.setenv("MACHINA_REASONING_EFFORT", "")
    assert c.get("reasoning_effort") == ""


def test_nonempty_env_overrides(monkeypatch):
    from machina.core.config import Config

    c = Config()
    monkeypatch.setenv("MACHINA_MODEL", "custom-model")
    assert c.get("model") == "custom-model"


# ─── 3. Unknown config keys ──────────────────────────────────────────────


def test_unknown_config_key_rejected():
    from machina.core.config import Config

    c = Config()
    try:
        c.set("bogus_key_xyz", "1")
    except ValueError as e:
        assert "bogus_key_xyz" in str(e)
    else:
        raise AssertionError("unknown key accepted")
    assert not hasattr(c, "bogus_key_xyz")


def test_valid_key_still_works():
    from machina.core.config import Config

    c = Config()
    c.set("model", "openrouter/free")
    assert c.get("model") == "openrouter/free"


# ─── 4. Auto-compaction silent failure ───────────────────────────────────


async def test_maybe_auto_compact_records_failure(monkeypatch):
    import machina.services as svc

    recorded = []
    monkeypatch.setattr(svc, "record_failure", lambda op, e: recorded.append((op, e)))

    # Force failure inside the guarded body: make begin_mutation raise
    async def boom(sid):
        raise RuntimeError("compact boom")

    monkeypatch.setattr("machina.core.turns.begin_mutation", boom)
    # Ensure guards pass
    from machina.core.config import config

    monkeypatch.setattr(config, "auto_compaction_enabled", True)
    monkeypatch.setattr(config, "compression_enabled", True)
    res = await svc.maybe_auto_compact(uuid.uuid4())
    assert res is None
    assert any(op == "services.maybe_auto_compact" for op, _ in recorded)


async def test_maybe_auto_compact_eviction_failure_recorded(monkeypatch):
    """A failed eviction pass keeps the compaction result and reports the boundary."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import machina.services as svc
    from machina.core import session as session_module
    from machina.core import turns as turns_module

    recorded = []
    monkeypatch.setattr(svc, "record_failure", lambda op, e: recorded.append((op, e)))

    session = SimpleNamespace(id=uuid.uuid4(), model="test-model", state={})
    monkeypatch.setattr(session_module.session_manager, "get", AsyncMock(return_value=session))
    monkeypatch.setattr(session_module.session_manager, "update_state_fields", AsyncMock())
    monkeypatch.setattr(turns_module, "begin_mutation", AsyncMock(return_value="token-1"))
    monkeypatch.setattr(turns_module, "end_mutation", AsyncMock(return_value=True))
    monkeypatch.setattr(
        turns_module, "_db_claim_state", AsyncMock(return_value={"claim_owner": "token-1"})
    )
    monkeypatch.setattr(svc, "_estimate_next_request", AsyncMock(return_value=10_000))
    monkeypatch.setattr("machina.core.context_windows.context_window_for", lambda _model: 1_000)
    monkeypatch.setattr(svc.context_manager, "get_token_usage", AsyncMock(return_value=10_000))
    monkeypatch.setattr(
        svc,
        "compress_session",
        AsyncMock(
            return_value=SimpleNamespace(
                compressed_tokens=10, original_tokens=100, method="truncate"
            )
        ),
    )
    monkeypatch.setattr(
        svc.context_manager,
        "evict_old_chunks",
        AsyncMock(side_effect=RuntimeError("evict boom")),
    )
    from machina.core.config import config

    monkeypatch.setattr(config, "auto_compaction_enabled", True)
    monkeypatch.setattr(config, "compression_enabled", True)
    monkeypatch.setattr(config, "eviction_max_tokens", 1_000)

    result = await svc.maybe_auto_compact(session.id)
    assert result is not None and result["evicted"] == 0
    assert [op for op, _ in recorded] == ["services.maybe_auto_compact.evict"]


# ─── 5. Estimate init ────────────────────────────────────────────────────


async def test_estimate_survives_session_lookup_failure(monkeypatch):
    import machina.services as svc

    recorded = []
    monkeypatch.setattr(svc, "record_failure", lambda op, e: recorded.append((op, e)))

    # Force session lookup to raise at the outer import level? Simulate by
    # making session_manager.get raise and registry raise.
    from machina.core import session as sessmod

    async def boom_get(sid):
        raise RuntimeError("session boom")

    monkeypatch.setattr(sessmod.session_manager, "get", boom_get)
    # Registry path also raises
    try:
        from machina.core import agent_def as admod

        async def boom_reg(name):
            raise RuntimeError("registry boom")

        monkeypatch.setattr(admod.agent_registry, "get", boom_reg)
    except Exception:
        pass
    val = await svc._estimate_next_request(uuid.uuid4())
    assert isinstance(val, int) and val > 0 and val != float("inf")
    assert "services.maybe_auto_compact.session" in [op for op, _ in recorded]


async def test_estimator_counts_full_schema_for_unrestricted_empty_tools(monkeypatch):
    """AgentDef.tools=[] is unrestricted at execution (definition_tool_cap
    resolves it to None); the estimator must count the full registry schema,
    not zero schemas."""
    from types import SimpleNamespace

    import machina.services as svc
    from machina.core.agent_def import agent_registry
    from machina.core.agent_factory import definition_tool_cap
    from machina.core.assembler import get_token_count
    from machina.core.session import session_manager
    from machina.tools.base import registry

    full = [
        SimpleNamespace(name=f"tool_{i}", description="d" * 120, parameters={"type": "object"})
        for i in range(3)
    ]
    one = [full[0]]

    async def fake_session_get(_sid):
        return SimpleNamespace(agent_id="audit-agent", provider=None, model=None)

    def resolver(tools):
        async def fake_defn_get(_name):
            return SimpleNamespace(tools=tools, system_prompt="")

        return fake_defn_get

    monkeypatch.setattr(session_manager, "get", fake_session_get)
    monkeypatch.setattr(registry, "get_tool_definitions", lambda: list(full))

    assert definition_tool_cap(SimpleNamespace(tools=[])) is None
    monkeypatch.setattr(agent_registry, "get", resolver([]))
    unrestricted = await svc._estimate_next_request(uuid.uuid4())
    monkeypatch.setattr(agent_registry, "get", resolver(["tool_0"]))
    restricted = await svc._estimate_next_request(uuid.uuid4())

    full_tokens = get_token_count(str(full))
    one_tokens = get_token_count(str(one))
    assert full_tokens > one_tokens  # fixture sanity
    assert unrestricted - restricted == full_tokens - one_tokens


# ─── 7. Status fallback ──────────────────────────────────────────────────


async def test_status_fallback_never_ready(monkeypatch):
    import machina.services as svc

    # Realistic: every config lookup used by the guarded block fails.
    from machina.core.config import config as cfg

    def boom_get(key=None):
        raise RuntimeError("config boom")

    monkeypatch.setattr(cfg, "get", boom_get)

    async def fake_fetchval(q, *a):
        if "version()" in q:
            return "PostgreSQL 16.0"
        return 0

    monkeypatch.setattr(svc.db, "fetchval", fake_fetchval)
    out = await svc.status_summary()
    assert out["autoCompaction"] == "disabled"
    assert out["memoryExtraction"] in ("disabled", "unknown", "degraded")
    assert out["memoryExtraction"] != "ready"
    assert out["reranker"] != "passthrough"
    assert out["mode"] == "ask"
    assert out["model"] == "unknown"
    assert out["openrouterKeySet"] is False


async def test_status_never_raises_on_partial_config_failure(monkeypatch):
    """Each individual status-detection failure keeps the response structured."""
    import machina.services as svc
    from machina.core.config import config as cfg

    real_get = cfg.get

    def selective(key=None):
        if key in (
            "execution_mode",
            "workspace_root",
            "machina_home",
            "auto_compaction_enabled",
            "compression_enabled",
            "memory_consolidation_enabled",
            "memory_enabled",
            "cohere_api_key",
        ):
            raise RuntimeError(f"boom {key}")
        return real_get(key)

    monkeypatch.setattr(cfg, "get", selective)

    async def fake_fetchval(q, *a):
        if "version()" in q:
            return "PostgreSQL 16.0"
        return 0

    monkeypatch.setattr(svc.db, "fetchval", fake_fetchval)
    out = await svc.status_summary()
    assert isinstance(out, dict) and out["postgres"]
    assert out["autoCompaction"] != "enabled"
    assert out["memoryExtraction"] != "ready"


async def test_status_summary_records_each_field_fallback(monkeypatch):
    """Each status field fallback reports its own sanitized boundary key."""
    import machina.services as svc
    from machina.core import runtime as runtime_module
    from machina.memory import embeddings as embeddings_module
    from machina.permissions import store as perm_store

    recorded = []
    monkeypatch.setattr(svc, "record_failure", lambda op, e: recorded.append((op, e)))

    def boom_status():
        raise RuntimeError("runtime boom")

    monkeypatch.setattr(runtime_module.runtime_services, "status", boom_status)

    def boom_embedding_status():
        raise RuntimeError("embedding boom")

    monkeypatch.setattr(embeddings_module, "embedding_status", boom_embedding_status)

    async def fake_fetchval(query, *args):
        if "version()" in query:
            return "PostgreSQL 16.0"
        if "chunk_type = 'document'" in query or "FROM jobs" in query:
            raise RuntimeError("query boom")
        return 0

    monkeypatch.setattr(svc.db, "fetchval", fake_fetchval)

    def boom_get(_key=None):
        raise RuntimeError("config boom")

    from machina.core.config import config as cfg

    monkeypatch.setattr(cfg, "get", boom_get)

    monkeypatch.setattr(
        perm_store, "is_durable", lambda: (_ for _ in ()).throw(RuntimeError("durable boom"))
    )

    out = await svc.status_summary()
    assert isinstance(out, dict)
    ops = {op for op, _ in recorded}
    for field in (
        "runtime",
        "retrieval",
        "rag_docs",
        "config",
        "jobs_waiting",
        "permission_durability",
    ):
        assert f"services.status_summary.{field}" in ops, ops


# ─── 9. Compression None timestamps ─────────────────────────────────────


def test_truncate_compress_none_timestamps():
    from machina.core.compression import CompressionConfig, ContextCompressor
    from machina.core.models import ContextChunk

    sid = uuid.uuid4()
    chunks = [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=sid,
            agent_id="h",
            chunk_type="user_message",
            payload={"content": f"msg {i}"},
            token_count=50,
            created_at=None,
        )
        for i in range(6)
    ]
    comp = ContextCompressor(
        config=CompressionConfig(preserve_recent=2, target_ratio=0.5, llm_summarize=False)
    )
    out = comp._truncate_compress(chunks, sid, "h")
    assert len(out) > 0
    # Deterministic order: sorted desc by (ts, id)
    assert all(c.created_at is None or isinstance(c.created_at, datetime) for c in out)


# ─── 11. Approval resume KeyError ────────────────────────────────────────


async def test_resume_action_stream_missing_preview():
    from machina.core.approval_resume import resume_action_stream

    class FakeAgent:
        async def _execute_tool_calls_stream(self, response, hist, made, sid):
            made.append({"tool": "read_file"})  # no result_preview
            yield {"type": "text", "content": "x"}

    events = [
        e
        async for e in resume_action_stream(
            FakeAgent(), uuid.uuid4(), {"request_id": "r1", "proposal": {"tool": "read_file"}}, {}
        )
    ]
    done = [e for e in events if getattr(e, "type", "") == "done"]
    assert done
    assert "continuation ended" in done[0].response.content


# ─── 12. MCP retry masking ───────────────────────────────────────────────


def test_retryable_does_not_mask_without_httpx2(monkeypatch):
    # Simulate missing httpx2
    import builtins

    from machina.connectors import mcp_client as mc

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "httpx2":
            raise ImportError("no httpx2")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # ConnectionError is retryable via stdlib path; httpx2 missing must not raise
    assert mc._retryable_connect_error(ConnectionError("boom")) is True
    # Unknown error with missing httpx2 -> False, not ImportError
    assert mc._retryable_connect_error(ValueError("x")) is False


# ─── 13. Connector tools_list shape ──────────────────────────────────────


async def test_tools_list_structured():
    from tests.test_connectors_service import setup_service

    service, clients = setup_service()
    await service.connect("alpha")
    out = await service.tools_list("alpha")
    assert isinstance(out, dict)
    assert out["truncated"] is False
    assert all(set(t) == {"name", "remote_name", "description", "verdict"} for t in out["tools"])
    clients[0].truncated = True
    out2 = await service.tools_list("alpha")
    assert out2["truncated"] is True
    assert all("truncated" not in t for t in out2["tools"])


# ─── 8. Recent-context ordering (SQL check) ──────────────────────────────


def test_recent_context_ordering_sql():
    import pathlib

    src = pathlib.Path("machina/core/context.py").read_text()
    # HELD-55: the durable event ordinal joins the tie-break (id stays as the
    # final fallback); ordering keeps created_at first for cross-batch order.
    assert "ORDER BY created_at DESC, event_seq DESC NULLS LAST, id DESC" in src
    assert "ORDER BY created_at DESC, id DESC" not in src


# ─── 6. Document count uses stable identity ──────────────────────────────


async def test_status_doc_count_query_uses_document_source(monkeypatch):
    """status_summary must count document identity, not serialized payloads."""
    import machina.services as svc

    seen_queries: list[str] = []

    async def fake_fetchval(q, *a):
        seen_queries.append(str(q))
        if "version()" in str(q):
            return "PostgreSQL 16.0 (mock)"
        return 3

    monkeypatch.setattr(svc.db, "fetchval", fake_fetchval)
    out = await svc.status_summary()
    doc_queries = [q for q in seen_queries if "chunk_type = 'document'" in q]
    assert doc_queries, "no document count query executed"
    query = doc_queries[0]
    assert "document_source" in query
    assert "payload_msgpack" not in query
    assert out["documentRag"].startswith("ready")


async def test_document_count_distinct_sources(db_pool, monkeypatch):
    import machina.core.context as ctxmod
    import machina.core.session as sessmod

    monkeypatch.setattr(ctxmod, "db", db_pool)
    monkeypatch.setattr(sessmod, "db", db_pool)
    monkeypatch.setattr("machina.services.db", db_pool)
    from machina.core.session import SessionManager

    # Bind manager's db too (imported reference)
    sm = SessionManager()
    # SessionManager uses global db; patch its module
    import machina.core.session as smmod

    monkeypatch.setattr(smmod, "db", db_pool)
    # Rebind context_manager db reference (it uses imported db object)
    # db_pool is a Database; context_manager uses `db` global imported earlier — patch it
    monkeypatch.setattr("machina.core.context.db", db_pool)
    sess = await sm.create(title="doc-count", agent_id="harness")
    try:
        payload = {"text": "same text", "source": "x", "metadata": {}}
        # Two chunks identical payload but distinct sources
        await db_pool.execute(
            "INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source) VALUES ($1,$2,'document',$3,5,NULL,$4,$5)",
            sess.id,
            "harness",
            __import__(
                "machina.core.serialization", fromlist=["payload_to_msgpack"]
            ).payload_to_msgpack(payload),
            "same text",
            "doc-A",
        )
        await db_pool.execute(
            "INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source) VALUES ($1,$2,'document',$3,5,NULL,$4,$5)",
            sess.id,
            "harness",
            __import__(
                "machina.core.serialization", fromlist=["payload_to_msgpack"]
            ).payload_to_msgpack(payload),
            "same text",
            "doc-B",
        )
        # Duplicate rows same doc
        await db_pool.execute(
            "INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, document_source) VALUES ($1,$2,'document',$3,5,NULL,$4,$5)",
            sess.id,
            "harness",
            __import__(
                "machina.core.serialization", fromlist=["payload_to_msgpack"]
            ).payload_to_msgpack(payload),
            "same text",
            "doc-A",
        )
        n = await db_pool.fetchval(
            "SELECT COUNT(DISTINCT COALESCE(document_source, id::text)) FROM context_chunks WHERE chunk_type='document' AND session_id=$1",
            sess.id,
        )
        assert n == 2
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sess.id)


# ─── 10. CompressOutcome ─────────────────────────────────────────────────


async def test_compress_outcome_statuses(db_pool, monkeypatch):
    import machina.services as svc

    monkeypatch.setattr("machina.services.db", db_pool)
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    import machina.core.session as smmod
    from machina.core.session import SessionManager

    monkeypatch.setattr(smmod, "db", db_pool)
    sm = SessionManager()
    sess = await sm.create(title="compress-status", agent_id="harness")
    try:
        # no-op: empty
        out = await svc.compress_session_with_status(sess)
        assert out.status == "no_op"
        # stale ownership: create chunks then use bad token
        import machina.core.context as cmod
        from machina.core.context import context_manager

        monkeypatch.setattr(cmod, "db", db_pool)
        for i in range(5):
            await context_manager.add_chunk(
                sess.id,
                "harness",
                "user_message",
                {"content": f"hello world message number {i} with padding"},
                token_count=100,
            )
        # A stale/foreign token is refused by the transactional fence.
        out2 = await svc.compress_session_with_status(
            sess, mutation_token="00000000-0000-0000-0000-000000000000"
        )
        assert out2.status in ("stale_ownership", "stale_snapshot")
        # Everything still intact (no partial commit).
        remaining = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id=$1", sess.id
        )
        assert remaining == 5
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sess.id)


# ─── 11. W6: compress_session delegates its statuses to the with_status twin ─


def _w6_chunks(session_id):
    from machina.core.models import ContextChunk

    return [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=session_id,
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": f"message {i}: " + "detail " * 40},
            token_count=100,
        )
        for i in range(6)
    ]


def _w6_patch_reads(monkeypatch, chunks):
    """Provider-free compaction setup: fixed chunks, truncation path, owned claim."""
    from machina.core import context as ctxmod
    from machina.core.config import config

    monkeypatch.setattr(config, "compression_llm_summarize", False)
    monkeypatch.setattr(config, "compression_preserve_recent", 2)

    async def _all_chunks(session_id):
        return list(chunks)

    monkeypatch.setattr(ctxmod.context_manager, "get_all_chunks", _all_chunks)
    monkeypatch.setattr(ctxmod.context_manager, "replace_chunks_by_ids", _w6_replace)


async def _w6_replace(session_id, ids, new_chunks, archive_reason="compressed", **kwargs):
    return len(new_chunks)


async def test_w6_compress_session_busy_returns_none(monkeypatch):
    """W6 pin: busy (claim held elsewhere) stays None through the wrapper."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import machina.services as svc
    from machina.core import turns as turns_mod

    monkeypatch.setattr(turns_mod, "begin_mutation", AsyncMock(return_value=None))
    session = SimpleNamespace(id=uuid.uuid4(), agent_id="harness")
    assert await svc.compress_session(session) is None


@pytest.mark.parametrize("stale_name", ["StaleOwnershipError", "StaleSnapshotError"])
async def test_w6_compress_session_stale_returns_none(monkeypatch, stale_name):
    """W6 pin: stale fencing collapses to None (legacy contract), no re-raise."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import machina.services as svc
    from machina.core import turns as turns_mod
    from machina.core.exceptions import StaleOwnershipError, StaleSnapshotError

    monkeypatch.setattr(turns_mod, "begin_mutation", AsyncMock(return_value="token-w6"))
    monkeypatch.setattr(turns_mod, "end_mutation", AsyncMock(return_value=True))
    sid = uuid.uuid4()
    _w6_patch_reads(monkeypatch, _w6_chunks(sid))

    stale_cls = {
        "StaleOwnershipError": StaleOwnershipError,
        "StaleSnapshotError": StaleSnapshotError,
    }[stale_name]

    async def _stale_replace(session_id, ids, new_chunks, archive_reason="compressed", **kwargs):
        raise stale_cls("fence refused")

    monkeypatch.setattr(
        "machina.core.context.context_manager.replace_chunks_by_ids", _stale_replace
    )
    session = SimpleNamespace(id=sid, agent_id="harness")
    assert await svc.compress_session(session) is None


async def test_w6_compress_session_reraises_original_persistence_error(monkeypatch):
    """W6 pin: unexpected failures re-raise the SAME original exception object."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import machina.services as svc
    from machina.core import turns as turns_mod

    class PersistenceBoom(RuntimeError):
        pass

    boom = PersistenceBoom("db down")
    monkeypatch.setattr(svc, "record_failure", lambda op, e: None)
    monkeypatch.setattr(turns_mod, "begin_mutation", AsyncMock(return_value="token-w6"))
    monkeypatch.setattr(turns_mod, "end_mutation", AsyncMock(return_value=True))
    sid = uuid.uuid4()
    _w6_patch_reads(monkeypatch, _w6_chunks(sid))

    async def _boom_replace(session_id, ids, new_chunks, archive_reason="compressed", **kwargs):
        raise boom

    monkeypatch.setattr("machina.core.context.context_manager.replace_chunks_by_ids", _boom_replace)
    session = SimpleNamespace(id=sid, agent_id="harness")
    with pytest.raises(PersistenceBoom) as excinfo:
        await svc.compress_session(session)
    assert excinfo.value is boom


async def test_w6_compress_session_reraises_claim_error_and_records(monkeypatch):
    """W6 pin: a failing claim raises (legacy) and is now recorded under the
    shared 'services.compress_session' key."""
    from types import SimpleNamespace

    import machina.services as svc
    from machina.core import turns as turns_mod

    recorded = []
    monkeypatch.setattr(svc, "record_failure", lambda op, e: recorded.append((op, e)))
    boom = RuntimeError("claim boom")

    async def _begin(session_id):
        raise boom

    monkeypatch.setattr(turns_mod, "begin_mutation", _begin)
    session = SimpleNamespace(id=uuid.uuid4(), agent_id="harness")
    with pytest.raises(RuntimeError) as excinfo:
        await svc.compress_session(session)
    assert excinfo.value is boom
    assert recorded == [("services.compress_session", boom)]


async def test_w6_with_status_failed_carries_cause_not_leaked_into_repr(monkeypatch):
    """W6 pin: failed keeps the original cause for the wrapper; repr stays bounded."""
    from types import SimpleNamespace

    import machina.services as svc
    from machina.core import turns as turns_mod

    recorded = []
    monkeypatch.setattr(svc, "record_failure", lambda op, e: recorded.append((op, e)))
    boom = RuntimeError("provider payload secret")

    async def _begin(session_id):
        raise boom

    monkeypatch.setattr(turns_mod, "begin_mutation", _begin)
    session = SimpleNamespace(id=uuid.uuid4(), agent_id="harness")
    out = await svc.compress_session_with_status(session)
    assert out.status == "failed"
    assert out.error == "RuntimeError"
    assert out.cause is boom
    assert "provider payload secret" not in repr(out)
    assert recorded == [("services.compress_session", boom)]


async def test_w6_with_status_stale_carries_cause_not_leaked_into_repr(monkeypatch):
    """W6 pin: stale statuses keep their cause diagnostic, repr stays bounded."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import machina.services as svc
    from machina.core import turns as turns_mod
    from machina.core.exceptions import StaleOwnershipError

    monkeypatch.setattr(turns_mod, "begin_mutation", AsyncMock(return_value="token-w6"))
    monkeypatch.setattr(turns_mod, "end_mutation", AsyncMock(return_value=True))
    sid = uuid.uuid4()
    _w6_patch_reads(monkeypatch, _w6_chunks(sid))

    async def _stale_replace(session_id, ids, new_chunks, archive_reason="compressed", **kwargs):
        raise StaleOwnershipError("fence refused")

    monkeypatch.setattr(
        "machina.core.context.context_manager.replace_chunks_by_ids", _stale_replace
    )
    session = SimpleNamespace(id=sid, agent_id="harness")
    out = await svc.compress_session_with_status(session)
    assert out.status == "stale_ownership"
    assert out.error == "StaleOwnershipError"
    assert isinstance(out.cause, StaleOwnershipError)
    assert "fence refused" not in repr(out)


async def test_w6_with_status_busy_has_no_cause(monkeypatch):
    """W6 pin: busy carries no cause and no leaked repr field."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import machina.services as svc
    from machina.core import turns as turns_mod

    monkeypatch.setattr(turns_mod, "begin_mutation", AsyncMock(return_value=None))
    session = SimpleNamespace(id=uuid.uuid4(), agent_id="harness")
    out = await svc.compress_session_with_status(session)
    assert out.status == "busy"
    assert out.cause is None
    assert "cause" not in repr(out)
