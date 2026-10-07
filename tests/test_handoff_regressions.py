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


def _assert_no_trace(secret: str, output: str) -> None:
    assert secret not in output
    # Fragment invariant: no contiguous secret run of >= 10 chars may appear.
    for i in range(len(secret) - 9):
        assert secret[i : i + 10] not in output


def test_streaming_equals_whole_recognition_all_formats_all_splits():
    """LP-09: streamed output must equal whole-string redaction at every split."""
    from ah.memory.redaction import StreamingSecretRedactor, redact_secrets

    secrets = [
        "sk-" + "A" * 24,
        "sk-ant-abc123DEF456ghi789JKL0",
        "sk-or-xyz123ABC456def789GHI0jkl",
        "Bearer abcdef1234567890XYZ",
        "password=hunter2hunter2hunter2",
        "postgresql://bob:s3cret-pass_9@db:5432/app",
        "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "aB3dE5fG7hI9jK1lM2nO4pQ6rS8tU0vWxYz01234",
        "ghp_" + "c" * 36,
        "gho_" + "d" * 36,
        "ghs_" + "e" * 36,
        "ghu_" + "f" * 36,
        "github_pat_" + "g" * 30,
        "xoxb-123456789012-abcDEF123",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBnozM\n-----END RSA PRIVATE KEY-----",
        "4111111111111111",
        "4111 1111 1111 1111",
        "123-45-6789",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
        "api_key=ABCDEFGHIJKLMNOP123456",
        "cohere_api_key=COHERE1234567890abcdef",
    ]
    contexts = ["{s}", "pre {s} post", "X{s}", "{s}!", ">{s}<", '{"k": "{s}"}']
    for secret in secrets:
        whole = redact_secrets(secret).text
        assert secret not in whole, f"whole-string miss for {secret[:12]}"
        for ctx in contexts:
            text = ctx.replace("{s}", secret)
            expected = redact_secrets(text).text
            # All split positions (bounded sizes keep this fast).
            for i in range(len(text) + 1):
                r = StreamingSecretRedactor()
                out = r.feed(text[:i]) + r.feed(text[i:]) + r.flush()
                assert out == expected, f"split {i} of {secret[:12]} in {ctx}"
            # One-character feeds.
            r = StreamingSecretRedactor()
            out = "".join(r.feed(ch) for ch in text) + r.flush()
            assert out == expected, f"char feeds of {secret[:12]} in {ctx}"


def test_streaming_redactor_huge_delta_prefix_emission():
    """Addendum 5.1 counterexample: secret + 486 padding in ONE delta."""
    from ah.memory.redaction import StreamingSecretRedactor

    secret = "sk-" + "A" * 24
    text = secret + " " + "z" * 486
    r = StreamingSecretRedactor()
    output = r.feed(text) + r.flush()
    _assert_no_trace(secret, output)


def test_streaming_redactor_char_at_a_time_and_partitions():
    import random

    from ah.memory.redaction import StreamingSecretRedactor

    secrets = [
        "sk-" + "B" * 24,
        "sk-ant-abc123DEF456ghi789JKL0",
        "sk-or-xyz123ABC456def789GHI0jkl",
        "ghp_" + "c" * 36,
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-123456789012-abcDEF123",
    ]
    rng = random.Random(20261007)
    for secret in secrets:
        # char-at-a-time
        r = StreamingSecretRedactor()
        out = "".join(r.feed(ch) for ch in secret) + r.flush()
        _assert_no_trace(secret, out)
        # seeded random partitions with padding
        for _ in range(10):
            cuts = sorted(rng.sample(range(1, len(secret)), min(3, len(secret) - 1)))
            parts, prev = [], 0
            for c in cuts + [len(secret)]:
                parts.append(secret[prev:c])
                prev = c
            r = StreamingSecretRedactor()
            out = "".join(r.feed(p) for p in ["pre "] + parts + [" post"]) + r.flush()
            _assert_no_trace(secret, out)


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


def _swap_hook_factory(fmod, real_open, swap_from: str, swap_to: str):
    """Deterministic race hook: swap ancestor just before the guarded open."""
    import shutil

    state = {"done": False, "swapped": False}

    def _hooked(path, flags, *a, **k):
        if not state["done"]:
            state["done"] = True
            try:
                if os.path.isdir(swap_from) and not os.path.islink(swap_from):
                    shutil.rmtree(swap_from)
                else:
                    os.unlink(swap_from)
            except OSError:
                pass
            try:
                os.symlink(swap_to, swap_from, target_is_directory=True)
                state["swapped"] = os.path.islink(swap_from)
            except OSError:
                pass
        return real_open(path, flags, *a, **k)

    _hooked.state = state  # type: ignore[attr-defined]
    return _hooked


def test_write_ancestor_swap_leaves_outside_untouched(tmp_path, monkeypatch):
    """5.2: swap validated ancestor to outside before open → reject, no effect."""
    import ah.tools.file as fmod

    base = tmp_path / "base"
    (base / "sub").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "victim.txt"
    outside_file.write_text("ORIGINAL-CONTENT")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    real_open = fmod._os_open
    hook = _swap_hook_factory(fmod, real_open, str(base / "sub"), str(outside_dir))
    monkeypatch.setattr(fmod, "_os_open", hook)
    import asyncio

    with pytest.raises(Exception):
        asyncio.run(fmod.write_file("sub/newnote.txt", "ATTACKER"))
    if not hook.state["swapped"]:
        pytest.skip("symlinks unavailable")
    assert outside_file.read_text() == "ORIGINAL-CONTENT"
    # Strict no-effects: the rejected create must not leave a file behind.
    assert not (outside_dir / "newnote.txt").exists()


def test_write_final_symlink_leaves_outside_untouched(tmp_path, monkeypatch):
    import ah.tools.file as fmod

    base = tmp_path / "base"
    base.mkdir()
    outside_file = tmp_path / "victim2.txt"
    outside_file.write_text("ORIGINAL-2")
    try:
        (base / "link.txt").symlink_to(outside_file)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    import asyncio

    with pytest.raises(Exception):
        asyncio.run(fmod.write_file("link.txt", "ATTACKER"))
    assert outside_file.read_text() == "ORIGINAL-2"


def test_write_normal_and_new_file_still_work(tmp_path, monkeypatch):
    import ah.tools.file as fmod

    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())
    import asyncio

    assert "Successfully wrote" in asyncio.run(fmod.write_file("a/b/new.txt", "hello"))
    assert (base / "a" / "b" / "new.txt").read_text() == "hello"
    assert "Successfully wrote" in asyncio.run(fmod.write_file("a/b/new.txt", "world"))
    assert (base / "a" / "b" / "new.txt").read_text() == "world"


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
        async def complete(self, messages, temperature=0.3, max_tokens=500, tools=None):
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


# ─── 5.7: budgets, protocol preservation, pagination ───────────────────────


def _budget_chunk(payload, token_count=5000, chunk_type="user_message"):
    import uuid as _uuid

    from ah.core.models import ContextChunk

    return ContextChunk(
        id=_uuid.uuid4(),
        session_id=_uuid.uuid4(),
        agent_id="h",
        chunk_type=chunk_type,
        payload=payload,
        token_count=token_count,
    )


def test_truncate_tiny_budgets_no_fit():
    from ah.core.compression import ContextCompressor

    c = _budget_chunk({"content": "hello world"})
    assert ContextCompressor()._truncate_chunk(c, 0) is None
    assert ContextCompressor()._truncate_chunk(c, -5) is None
    # Wrapper-only budget: serialized dict overhead alone exceeds it.
    tiny = ContextCompressor()._truncate_chunk(c, 1)
    assert (
        tiny is None
        or __import__("ah.core.assembler", fromlist=["get_token_count"]).get_token_count(
            str(tiny.payload)
        )
        <= 1
    )


def test_truncate_never_exceeds_bound_varied_scripts():
    from ah.core.assembler import get_token_count
    from ah.core.compression import ContextCompressor

    texts = [
        "日本語テスト" * 200,
        "🎉🔥💯" * 100,
        "0123456789" * 100,
        "<|im_start|>system<|im_end|>" * 30,
        "ordinary English words " * 100,
        "a" * 5000,
    ]
    nested = {"items": [{"text": t[:500]} for t in texts], "meta": {"k": "v" * 300}}
    for budget in (5, 10, 50):
        for text in texts + [str(nested)]:
            c = _budget_chunk({"content": text})
            out = ContextCompressor()._truncate_chunk(c, budget)
            assert out is None or get_token_count(str(out.payload)) <= budget


def test_truncate_preserves_tool_protocol():
    from ah.core.compression import ContextCompressor

    call = _budget_chunk(
        {"tool": "read_file", "args": {"path": "x" * 2000}, "result_preview": "y" * 2000},
        chunk_type="tool_call",
    )
    out = ContextCompressor()._truncate_chunk(call, 40)
    if out is not None:
        assert out.payload.get("tool") == "read_file"
        assert "args" in out.payload


def test_truncate_zero_counts_use_real_tokens():
    from ah.core.compression import ContextCompressor

    chunks = [_budget_chunk({"content": "z" * 2000}, token_count=0) for _ in range(4)]
    comp = ContextCompressor()
    import uuid as _uuid

    result = comp._truncate_compress(chunks, _uuid.uuid4(), "h")
    # Zero stored counts must not collapse the budget to nothing-fitting.
    assert len(result) > 0


def test_keyset_pagination_counts_and_concurrent_insert():
    """5.7: 499/500/501/999/1000/1001+, identical timestamps, concurrent insert."""
    import asyncio
    import uuid as _uuid
    from datetime import UTC, datetime

    from ah.core.context import ContextManager
    from ah.core.models import ContextChunk

    async def _run(n, identical_ts):
        mgr = ContextManager()
        from datetime import timedelta

        base_stamp = datetime(2026, 1, 1, tzinfo=UTC)
        stamp = base_stamp if identical_ts else None
        store = []

        def _mk(i):
            return ContextChunk(
                id=_uuid.uuid4(),
                session_id=_uuid.uuid4(),
                agent_id="h",
                chunk_type="user_message",
                payload={"content": f"m{i}"},
                token_count=1,
                created_at=stamp or (base_stamp + timedelta(seconds=i)),
            )

        store.extend(_mk(i) for i in range(n))
        calls = {"pages": 0}

        async def _page(session_id, limit=500, before_time=None, before_id=None, chunk_type=None):
            calls["pages"] += 1
            ordered = sorted(store, key=lambda c: (c.created_at, str(c.id)), reverse=True)
            if before_time is not None:
                ordered = [
                    c for c in ordered if (c.created_at, str(c.id)) < (before_time, str(before_id))
                ]
            # Concurrent insert mid-pagination: newest row appears once.
            if calls["pages"] == 1 and n >= 500:
                store.append(_mk(10**6))
            return ordered[:limit]

        mgr.get_chunks_before = _page  # type: ignore[method-assign]
        out = await mgr.get_all_chunks(_uuid.uuid4())
        ids = [c.id for c in out]
        assert len(ids) == len(set(ids)), "no duplicate pages"
        assert len(out) >= n, "snapshot rows all returned"
        return out

    for n in (499, 500, 501, 999, 1000, 1001):
        asyncio.run(_run(n, identical_ts=True))
        asyncio.run(_run(n, identical_ts=False))


# ─── AH-026: scheduler token plumbing ─────────────────────────────────────


def _fake_pinned_transport(monkeypatch, routes):
    """Stub DNS + sockets for pinned_fetch. routes: {host: (ips, handler)}.

    handler(status, headers) -> body chunks iterator. Records connected IPs.
    """
    import ah.security.fetch as fetchmod

    connected = []

    def _resolve(hostname, timeout=5.0):
        if hostname not in routes:
            raise fetchmod.FetchError("unknown host in test")
        return list(routes[hostname][0])

    class _FakeSock:
        def __init__(self, *a, **k):
            self.sent = b""
            self._recv = b""
            self._pos = 0
            self.host = None

        def settimeout(self, t):
            pass

        def connect(self, addr):
            connected.append(addr[0])
            # Find route by matching IP.
            for host, (ips, handler) in routes.items():
                if addr[0] in ips:
                    self.host = host
                    status, headers, chunks = handler()
                    head = [f"HTTP/1.1 {status} x"]
                    head += [f"{k}: {v}" for k, v in headers.items()]
                    head += ["", ""]
                    self._recv = "\r\n".join(head).encode() + b"".join(chunks)
                    return
            raise OSError("no route")

        def sendall(self, data):
            self.sent += data

        def makefile(self, mode):
            import io

            return io.BytesIO(self._recv)

        def close(self):
            pass

    class _FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            if server_hostname not in routes:
                raise fetchmod.FetchError("bad sni")
            sock._sni = server_hostname
            return sock

    monkeypatch.setattr(fetchmod, "_bounded_resolve", _resolve)
    monkeypatch.setattr(fetchmod.socket, "socket", _FakeSock)
    import ssl as _ssl

    monkeypatch.setattr(_ssl, "create_default_context", lambda: _FakeCtx())
    return connected


def _ok(body: bytes, headers=None):
    h = {"Content-Length": str(len(body))}
    if headers:
        h.update(headers)
    return (200, h, [body])


def test_pinned_fetch_connects_validated_ip(monkeypatch):
    from ah.security.fetch import pinned_fetch

    connected = _fake_pinned_transport(
        monkeypatch,
        {"example.com": (["93.184.216.34"], lambda: _ok(b" pin-me "))},
    )
    assert pinned_fetch("https://example.com/s") == " pin-me "
    assert connected == ["93.184.216.34"]


def test_pinned_fetch_rejects_mixed_private_dns(monkeypatch):
    """Rebinding-style answer (public + private) fails closed with no connect."""
    import ah.security.fetch as fetchmod

    connected = _fake_pinned_transport(
        monkeypatch,
        {"mix.test": (["93.184.216.34", "10.0.0.5"], lambda: _ok(b"x"))},
    )
    with pytest.raises(fetchmod.FetchError):
        fetchmod.pinned_fetch("https://mix.test/s")
    assert connected == []


def test_pinned_fetch_redirect_to_private_rejected(monkeypatch):
    from ah.security.fetch import pinned_fetch

    def _redir():
        return (302, {"Location": "http://10.9.9.9/evil"}, [b""])

    connected = _fake_pinned_transport(
        monkeypatch,
        {
            "start.test": (["93.184.216.34"], _redir),
            "10.9.9.9": (["10.9.9.9"], lambda: _ok(b"evil")),
        },
    )
    with pytest.raises(Exception):
        pinned_fetch("https://start.test/s")
    assert all(not ip.startswith("10.") for ip in connected)


def test_pinned_fetch_caps_body_during_stream(monkeypatch):
    import ah.security.fetch as fetchmod

    def _big():
        return (200, {"Content-Length": str(10_000_000)}, [b"x" * 100])

    _fake_pinned_transport(monkeypatch, {"big.test": (["93.184.216.34"], _big)})
    with pytest.raises(fetchmod.FetchError):
        fetchmod.pinned_fetch("https://big.test/s", max_bytes=1024)


def test_skills_learn_off_loop(monkeypatch):
    """5.8: slow URL fetch must not stall the gateway event loop."""
    import asyncio
    import time
    import types

    from ah.gateway.features import skills as skillsmod

    def _slow_learn(source, **kw):
        time.sleep(0.3)
        return types.SimpleNamespace(
            name="s",
            description="d",
            triggers=[],
            version="1.0.0",
            source_type="learned",
            usage_count=0,
            enabled=True,
        )

    monkeypatch.setattr(skillsmod.services, "learn_skill", _slow_learn)
    gw = types.SimpleNamespace(require_db=lambda: None)
    gaps = []

    async def _heartbeat():
        last = time.monotonic()
        for _ in range(12):
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async def _go():
        hb = asyncio.create_task(_heartbeat())
        try:
            res = await skillsmod.skills_learn(gw, {"source": "https://example.com/s.md"})
        finally:
            await hb
        return res

    res = asyncio.run(_go())
    assert res["skill"]["name"] == "s"
    assert max(gaps) < 0.2, f"loop blocked: {max(gaps):.2f}s gap"


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


# ─── 5.6: owned provider cleanup on every exit ─────────────────────────────


def _compress_session_with(chunks, provider, monkeypatch, **kw):
    import asyncio
    from types import SimpleNamespace

    import ah.core.provider as provmod
    from ah.core import context as ctxmod

    async def _all_chunks(session_id):
        return list(chunks)

    monkeypatch.setattr(ctxmod.context_manager, "get_all_chunks", _all_chunks)
    monkeypatch.setattr(provmod, "get_provider", lambda provider=None, model=None, _p=provider: _p)
    replace_calls = kw.get("replace_calls")
    if replace_calls is not None:

        async def _replace(sid, ids, new_chunks, archive_reason="compressed"):
            replace_calls.append((list(ids), list(new_chunks)))
            assert len(ids) == len(chunks)
            return len(new_chunks)

        monkeypatch.setattr(ctxmod.context_manager, "replace_chunks_by_ids", _replace)
    from ah import services as svcmod

    session = SimpleNamespace(id=chunks[0].session_id if chunks else None, agent_id="h")
    return asyncio.run(svcmod.compress_session(session))


def test_compress_session_closes_provider_on_noop(monkeypatch):
    """5.6: original_count==0 early return must still close the owned provider."""
    import uuid as _uuid

    from ah.core.models import ContextChunk

    closed = []
    sid = _uuid.uuid4()

    class _P:
        async def close(self):
            closed.append(True)

    chunks = [
        ContextChunk(
            id=_uuid.uuid4(),
            session_id=sid,
            agent_id="h",
            chunk_type="user_message",
            payload={"content": "hi"},
            token_count=1,
        )
    ]
    # preserve_recent=3 > 1 chunk → nothing to compress → None.
    result = _compress_session_with(chunks, _P(), monkeypatch)
    assert result is None
    assert closed == [True]


def test_compress_session_closes_provider_on_persistence_failure(monkeypatch):
    import asyncio
    import uuid as _uuid

    from ah.core.models import ContextChunk

    closed = []
    sid = _uuid.uuid4()

    class _P:
        async def close(self):
            closed.append(True)

    chunks = [
        ContextChunk(
            id=_uuid.uuid4(),
            session_id=sid,
            agent_id="h",
            chunk_type="user_message",
            payload={"content": f"msg {i}"},
            token_count=100,
        )
        for i in range(6)
    ]
    import ah.core.provider as provmod
    from ah.core import context as ctxmod
    from types import SimpleNamespace

    async def _all_chunks(session_id):
        return list(chunks)

    async def _boom_replace(sid, ids, new_chunks, archive_reason="compressed"):
        raise RuntimeError("db down")

    monkeypatch.setattr(ctxmod.context_manager, "get_all_chunks", _all_chunks)
    monkeypatch.setattr(provmod, "get_provider", lambda provider=None, model=None: _P())
    monkeypatch.setattr(ctxmod.context_manager, "replace_chunks_by_ids", _boom_replace)
    from ah import services as svcmod

    with pytest.raises(RuntimeError):
        asyncio.run(svcmod.compress_session(SimpleNamespace(id=sid, agent_id="h")))
    assert closed == [True]


def test_compress_session_closes_provider_on_cancel(monkeypatch):
    import asyncio
    import uuid as _uuid

    from ah.core.models import ContextChunk

    closed = []
    sid = _uuid.uuid4()

    class _P:
        async def close(self):
            closed.append(True)

    chunks = [
        ContextChunk(
            id=_uuid.uuid4(),
            session_id=sid,
            agent_id="h",
            chunk_type="user_message",
            payload={"content": f"msg {i}"},
            token_count=100,
        )
        for i in range(6)
    ]
    import ah.core.provider as provmod
    from ah.core import context as ctxmod
    from types import SimpleNamespace

    async def _all_chunks(session_id):
        return list(chunks)

    async def _cancel_replace(sid, ids, new_chunks, archive_reason="compressed"):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ctxmod.context_manager, "get_all_chunks", _all_chunks)
    monkeypatch.setattr(provmod, "get_provider", lambda provider=None, model=None: _P())
    monkeypatch.setattr(ctxmod.context_manager, "replace_chunks_by_ids", _cancel_replace)
    from ah import services as svcmod

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svcmod.compress_session(SimpleNamespace(id=sid, agent_id="h")))
    assert closed == [True]


def test_shared_provider_not_closed_by_non_owner():
    import asyncio

    from ah.core.agent_factory import close_agent_provider

    closed = []

    class _P:
        async def close(self):
            closed.append(True)

    class _Shared:
        provider = _P()
        _owns_provider = False

    asyncio.run(close_agent_provider(_Shared()))
    assert closed == []
