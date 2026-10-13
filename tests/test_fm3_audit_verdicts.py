"""FM3-F8: broker-emitted permission verdict audit events (sanitized)."""

from __future__ import annotations

import pytest

from machina.core.config import config
from machina.permissions import store as st
from machina.permissions.broker import (
    ApprovalDenied,
    NeedsApproval,
    permission_broker,
    set_approval_handler,
)
from machina.permissions.policy import build_request


@pytest.fixture
def captured(monkeypatch):
    import machina.core.provider as provider

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        provider, "audit_log", lambda event_type, **kwargs: events.append((event_type, kwargs))
    )
    return events


@pytest.fixture
def memory_store(monkeypatch):
    async def no_db():
        return None

    monkeypatch.setattr(st, "_db", no_db)
    st._mem.approvals.clear()
    st.clear_grant_cache()
    try:
        yield
    finally:
        st._mem.approvals.clear()
        st.clear_grant_cache()


@pytest.mark.asyncio
async def test_allowed_verdict_emits_audit(captured, memory_store, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    req = build_request(
        operation="file.read",
        targets=[str(tmp_path / "a.txt")],
        mode="ask",
        session_id="s",
        agent_id="h",
    )
    await permission_broker.guard(req)
    assert captured
    event_type, payload = captured[-1]
    assert event_type == "permission_allowed"
    assert payload["request_id"] == req.request_id
    assert payload["digest_prefix"] == req.digest[:16]
    assert len(payload["digest_prefix"]) == 16
    assert payload["session_id"] == "s"
    assert payload["operation"] == "file.read"
    assert payload["mode"] == "ask"


@pytest.mark.asyncio
async def test_pending_verdict_emits_audit(captured, memory_store, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    req = build_request(
        operation="file.write",
        targets=[str(tmp_path.parent / "outside.txt")],
        content="x",
        mode="ask",
        session_id="s",
        agent_id="h",
    )
    with pytest.raises(NeedsApproval):
        await permission_broker.guard(req)
    assert any(
        event_type == "permission_pending" and payload["request_id"] == req.request_id
        for event_type, payload in captured
    ), captured


@pytest.mark.asyncio
async def test_denied_verdict_reason_is_redacted(captured, memory_store):
    secret = "sk-ant-api03-" + "q" * 30
    req = build_request(
        operation="process.exec",
        argv=["git", "reset", "--hard"],
        mode="ask",
        session_id="s",
        agent_id="h",
    )

    async def hostile_handler(card):
        return secret  # a transport must never leak a secret into the audit trail

    set_approval_handler(hostile_handler, principal="tui", turn_id="t1")
    try:
        with pytest.raises(ApprovalDenied):
            await permission_broker.guard(req)
    finally:
        set_approval_handler(None)
    denials = [payload for event_type, payload in captured if event_type == "permission_denied"]
    assert denials, captured
    assert secret not in str(denials[-1])
    assert denials[-1]["reason"]


@pytest.mark.asyncio
async def test_parent_authority_denial_emits_audit(captured, memory_store):
    from machina.core.agent_factory import parent_authority_var

    req = build_request(
        operation="file.read",
        targets=["/tmp/x.txt"],
        mode="ask",
        session_id="s",
        agent_id="h",
        tool="write_file",
    )
    token = parent_authority_var.set({"tools": ["read_file"]})
    try:
        with pytest.raises(ApprovalDenied):
            await permission_broker.guard(req)
    finally:
        parent_authority_var.reset(token)
    denials = [payload for event_type, payload in captured if event_type == "permission_denied"]
    assert denials, captured
    assert "write_file" in denials[-1]["reason"]
