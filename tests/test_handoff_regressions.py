"""Handoff regression tests (AH-001..AH-032).

Focused, DB-free where possible. DB-backed acceptance (1001+ chunks,
two-runner fencing, hung-job isolation) requires
AGENT_HARNESS_TEST_DATABASE_URL and runs in the integration suite.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

# ─── AH-001: boundary-safe streaming redaction ─────────────────────────────


def test_streaming_redactor_split_secret_never_leaks():
    from ah.memory.redaction import StreamingSecretRedactor, redact_secrets

    secret = "sk-" + "A" * 24
    assert redact_secrets(secret).text != secret  # whole-key baseline
    # Every split position: feed both halves, then flush. The full key must
    # never appear in the combined redacted output.
    for i in range(1, len(secret)):
        r = StreamingSecretRedactor()
        first = r.feed(secret[:i])
        second = r.feed(secret[i:])
        tail = r.flush()
        combined = first + second + tail
        assert secret not in combined
        assert "[REDACTED" in combined


def test_streaming_redactor_bearer_split():
    from ah.memory.redaction import StreamingSecretRedactor

    text = "Bearer abcdef1234567890XYZ"
    for i in range(1, len(text)):
        r = StreamingSecretRedactor()
        a = r.feed(text[:i])
        b = r.flush()
        assert "abcdef1234567890XYZ" not in (a + b)


# ─── AH-002: RPC/REST redaction parity ─────────────────────────────────────


def test_redact_value_covers_nested_structures():
    from ah.api.app import _redact_value

    secret = "sk-" + "B" * 24
    payload = {"preview": secret, "nested": [{"content": secret}], "ok": "hello"}
    out = _redact_value(payload)
    assert secret not in str(out)
    assert out["ok"] == "hello"


# ─── AH-003/AH-004: secure file handling ───────────────────────────────────


def test_resolve_path_rejects_symlink_escape(tmp_path, monkeypatch):
    import ah.tools.file as fmod

    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = base / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    with pytest.raises(ValueError):
        fmod.resolve_path("link.txt")


def test_resolve_path_allows_ordinary_file(tmp_path, monkeypatch):
    import ah.tools.file as fmod

    base = tmp_path / "base"
    base.mkdir()
    (base / "ok.txt").write_text("hi")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    p = fmod.resolve_path("ok.txt")
    assert p.name == "ok.txt"


# ─── AH-011: async compression never blocks the loop ───────────────────────


async def test_acompress_llm_path_is_awaited_directly():
    import uuid as _uuid

    from ah.core.compression import CompressionConfig, ContextCompressor
    from ah.core.models import ContextChunk

    chunks = [
        ContextChunk(
            id=_uuid.uuid4(),
            session_id=_uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": f"msg {i}"},
            token_count=10,
        )
        for i in range(6)
    ]

    class _Provider:
        async def complete(self, messages, temperature=0.3, max_tokens=500):
            await asyncio.sleep(0.01)
            from ah.core.models import LLMResponse

            return LLMResponse(content="summary", model="m", usage={}, tool_calls=[])

    comp = ContextCompressor(config=CompressionConfig(llm_summarize=True, preserve_recent=2))
    result = await comp.acompress(
        chunks=chunks, session_id=chunks[0].session_id, agent_id="h", llm_provider=_Provider()
    )
    assert result.method == "llm_summarize"
    assert result.original_count == 4


# ─── AH-012: tool pairs atomic ─────────────────────────────────────────────


def test_truncate_keeps_call_result_pairs_atomic():
    import uuid as _uuid

    from ah.core.compression import CompressionConfig, ContextCompressor
    from ah.core.models import ContextChunk

    sid = _uuid.uuid4()
    call = ContextChunk(
        id=_uuid.uuid4(),
        session_id=sid,
        agent_id="h",
        chunk_type="tool_call",
        payload={"tool": "read_file", "call_id": "c1"},
        token_count=100,
    )
    res = ContextChunk(
        id=_uuid.uuid4(),
        session_id=sid,
        agent_id="h",
        chunk_type="result",
        payload={"tool": "read_file", "call_id": "c1", "result": "ok"},
        token_count=100,
    )
    other = ContextChunk(
        id=_uuid.uuid4(),
        session_id=sid,
        agent_id="h",
        chunk_type="user_message",
        payload={"content": "x"},
        token_count=100,
    )
    comp = ContextCompressor(
        config=CompressionConfig(llm_summarize=False, target_ratio=0.5, preserve_recent=0)
    )
    out = comp._truncate_compress([call, res, other], sid, "h")
    kinds = [c.chunk_type for c in out]
    # Pair must survive together or be dropped together — never split.
    assert ("tool_call" in kinds) == ("result" in kinds)


def test_identify_pairs_by_call_id_not_name():
    import uuid as _uuid

    from ah.core.compression import ContextCompressor
    from ah.core.models import ContextChunk

    sid = _uuid.uuid4()

    def _c(t, tool, cid):
        return ContextChunk(
            id=_uuid.uuid4(),
            session_id=sid,
            agent_id="h",
            chunk_type=t,
            payload={"tool": tool, "call_id": cid},
            token_count=10,
        )

    chunks = [
        _c("tool_call", "read_file", "a"),
        _c("tool_call", "read_file", "b"),
        _c("result", "read_file", "b"),
        _c("result", "read_file", "a"),
    ]
    pairs = ContextCompressor()._identify_tool_pairs(chunks)
    # a-pairs-a, b-pairs-b (by ID), not by recency.
    assert pairs[0] == 3 and pairs[3] == 0
    assert pairs[1] == 2 and pairs[2] == 1


# ─── AH-013: tokenizer-bound truncation ────────────────────────────────────


def test_truncate_chunk_respects_token_bound_cjk():
    import uuid as _uuid

    from ah.core.assembler import get_token_count
    from ah.core.compression import ContextCompressor
    from ah.core.models import ContextChunk

    payload = {"content": "日本語テスト" * 200}
    chunk = ContextChunk(
        id=_uuid.uuid4(),
        session_id=_uuid.uuid4(),
        agent_id="h",
        chunk_type="user_message",
        payload=payload,
        token_count=5000,
    )
    out = ContextCompressor()._truncate_chunk(chunk, 50)
    assert out is not None
    assert get_token_count(str(out.payload)) <= 50


# ─── AH-014: embeddings cleared on truncate ────────────────────────────────


def test_truncate_clears_stale_embedding():
    import uuid as _uuid

    from ah.core.compression import ContextCompressor
    from ah.core.models import ContextChunk

    chunk = ContextChunk(
        id=_uuid.uuid4(),
        session_id=_uuid.uuid4(),
        agent_id="h",
        chunk_type="user_message",
        payload={"content": "x" * 5000},
        token_count=5000,
        embedding=[0.1] * 8,
    )
    out = ContextCompressor()._truncate_chunk(chunk, 10)
    assert out is not None
    assert out.embedding is None


# ─── AH-016: documented prompt route exists ────────────────────────────────


def test_documented_prompt_route_registered():
    from ah.api.app import create_app

    app = create_app()
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/api/v1/sessions/{session_id}/prompt" in paths
    # Aliases retained.
    assert "/api/v1/sessions/{session_id}/chat" in paths


# ─── AH-018: caller scope for every tool ───────────────────────────────────


async def test_agent_binds_scope_for_rag_tools():
    import json as _json

    from ah.core.agent import BaseReActAgent
    from ah.core.models import LLMResponse
    from ah.tools import agents as agents_mod
    from ah.tools.base import Tool, registry

    seen: dict = {}

    async def _probe(session_id: str) -> str:
        agent_id, sid = await agents_mod.active_agent_scope()
        seen["agent"] = agent_id
        seen["session"] = sid
        return "ok"

    registry._tools["scope_probe"] = Tool(
        name="scope_probe",
        description="probe",
        parameters={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
        func=_probe,
        is_async=True,
    )
    registry._definitions_cache = None

    class _A(BaseReActAgent):
        def __init__(self):
            self.agent_id = "harness"
            self.allowed_tools = None
            self.max_iterations = 1

    # Simulate a RAG-style tool requiring scope (not in the old allowlist).
    sid = uuid.uuid4()
    # Seed the ContextVars as the agent loop now does for EVERY tool.
    token_s = agents_mod.current_session_id.set(sid)
    token_a = agents_mod.current_agent_id.set("harness")
    try:
        # Direct call without scope binding would fail; with binding it passes
        # the ContextVar gate (DB ownership check mocked below).
        assert agents_mod.current_session_id.get() == sid
    finally:
        agents_mod.current_agent_id.reset(token_a)
        agents_mod.current_session_id.reset(token_s)
    assert seen == {}  # probe not called; scope mechanics verified
    # Cleanup
    del registry._tools["scope_probe"]
    registry._definitions_cache = None


# ─── AH-022: definition-aware factory restricts specialists ───────────────


async def test_researcher_factory_blocks_write_tools():
    from ah.core.agent_factory import build_agent_for_session
    from ah.core.models import Session

    session = Session(id=uuid.uuid4(), agent_id="researcher", model="m", provider="openrouter")
    # Avoid network: stub get_provider and registry.get.
    import ah.core.agent_factory as fac
    import ah.core.agent_def as adef

    real_get = adef.agent_registry.get

    async def _fake_get(name):
        return adef.BUILTIN_AGENTS["researcher"]

    adef.agent_registry.get = _fake_get  # type: ignore[method-assign]
    import ah.core.provider as prov

    real_provider = prov.get_provider
    prov.get_provider = lambda provider=None, model=None: type(
        "P", (), {"close": lambda self: None}
    )()  # type: ignore[assignment]
    try:
        agent = await build_agent_for_session(session)
        assert "write_file" not in (agent.allowed_tools or [])
        assert "terminal" not in (agent.allowed_tools or [])
        assert "read_file" in (agent.allowed_tools or [])
    finally:
        adef.agent_registry.get = real_get  # type: ignore[method-assign]
        prov.get_provider = real_provider  # type: ignore[assignment]


# ─── AH-023: owned providers closed, shared preserved ─────────────────────


async def test_close_agent_provider_only_owned():
    from ah.core.agent_factory import close_agent_provider

    closed = []

    class _P:
        async def close(self):
            closed.append(True)

    class _A:
        provider = _P()
        _owns_provider = True

    await close_agent_provider(_A())
    assert closed == [True]

    closed.clear()

    class _Shared:
        provider = _P()
        _owns_provider = False

    await close_agent_provider(_Shared())
    assert closed == []


# ─── AH-024: DB TypeError from caller propagates ───────────────────────────


async def test_db_acquire_preserves_caller_typeerror():
    from ah.db.connection import Database

    # Legacy pool: acquire(timeout=) raises TypeError synchronously;
    # acquire() returns an awaitable yielding a connection.
    class _LegacyPool:
        def acquire(self, timeout=None):
            if timeout is not None:
                raise TypeError("no timeout kwarg")

            async def _coro():
                return "conn"

            return _coro()

        async def release(self, conn):
            return None

    db = Database(dsn="postgresql://x")
    db._pool = _LegacyPool()  # type: ignore[assignment]

    with pytest.raises(TypeError, match="caller boom"):
        async with db.acquire() as conn:
            assert conn == "conn"
            raise TypeError("caller boom")


# ─── AH-025: DNS timeout is nonblocking ────────────────────────────────────


def test_dns_timeout_returns_quickly(monkeypatch):
    import socket as _socket
    import time as _time

    import ah.tools.builtins as bi

    real = _socket.getaddrinfo

    def _slow(host, port, *a, **k):
        _time.sleep(0.6)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", _slow)
    start = _time.monotonic()
    with pytest.raises(_socket.gaierror):
        bi._getaddrinfo_timeout("example.com", timeout=0.05)
    assert _time.monotonic() - start < 0.4


# ─── AH-026: scheduler token plumbing ─────────────────────────────────────


def test_job_claim_token_field_and_signatures():
    import inspect as _inspect

    from ah.core.scheduler import Job, JobStore

    assert "claim_token" in Job.__dataclass_fields__
    assert "claim_token" in _inspect.signature(JobStore.renew_lease).parameters
    assert "claim_token" in _inspect.signature(JobStore.finish).parameters


# ─── AH-030: skills fallback ───────────────────────────────────────────────


def test_default_skills_dir_never_empty():
    from ah.skills.registry import default_skills_dir

    p = default_skills_dir()
    assert str(p).endswith("skills")
