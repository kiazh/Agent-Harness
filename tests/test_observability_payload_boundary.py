"""Diagnostics contain metadata, never conversation or action payloads."""

import json
import uuid

import pytest

from ah.core.provider import audit_log
from ah.gateway.server import Gateway


def test_audit_removes_prompt_completion_and_raw_tool_payload(caplog, monkeypatch):
    entries = []
    monkeypatch.setattr("ah.observability.audit.audit_persistence.submit", entries.append)
    with caplog.at_level("INFO", logger="ah.audit"):
        audit_log(
            "boundary",
            prompt="private-prompt",
            completion="private-completion",
            tool_args={"content": "private-file"},
            query_text="private-query",
            error="private-provider-response",
            session_id=str(uuid.uuid4()),
            tokens=7,
        )
    assert len(entries) == 1
    assert entries[0]["tokens"] == 7
    encoded = json.dumps(entries) + caplog.text
    for forbidden in (
        "private-prompt",
        "private-completion",
        "private-file",
        "private-query",
        "private-provider-response",
    ):
        assert forbidden not in encoded


async def test_invalid_gateway_request_does_not_log_its_payload(caplog):
    gateway = Gateway(lambda _: None, owns_db=False)
    await gateway.handle_line(json.dumps({"text": "private-prompt", "secret": "private-secret"}))
    assert "private-prompt" not in caplog.text
    assert "private-secret" not in caplog.text


def test_config_corruption_does_not_log_parser_source(tmp_path, caplog):
    from ah.core.config import Config

    path = tmp_path / "config.yaml"
    path.write_text("model: [private-prompt", encoding="utf-8")
    Config.load(path)
    assert "private-prompt" not in caplog.text


async def test_audit_writer_failure_has_metric_and_no_error_payload(monkeypatch, caplog):
    import asyncio

    from ah.core.metrics import metrics
    from ah.observability import audit

    started = asyncio.Event()

    async def fail(*args):
        started.set()
        raise RuntimeError("private-provider-response")

    monkeypatch.setattr(audit.db, "execute", fail)
    previous = metrics._counters["audit.persistence.failed"]
    writer = audit.AuditPersistence()
    writer.start()
    writer.submit({"event": "test-boundary"})
    try:
        await asyncio.wait_for(started.wait(), 1)
        await writer.stop()
        assert metrics._counters["audit.persistence.failed"] == previous + 1
        assert "private-provider-response" not in caplog.text
    finally:
        await writer.stop()


@pytest.mark.parametrize("source", ["environment", "mounted", "backend"])
def test_opaque_configured_secret_redacted_after_resolution(monkeypatch, tmp_path, caplog, source):
    import logging

    from ah.memory.redaction import StreamingSecretRedactor, redact_secrets
    from ah.security import secrets

    value = "opaquecredentialwithoutstandardprefix"
    if source == "environment":
        monkeypatch.setenv("SMOKE_API_KEY", value)
    elif source == "mounted":
        path = tmp_path / "secret"
        path.write_text(value, encoding="utf-8")
        monkeypatch.setenv("SMOKE_API_KEY_FILE", str(path))
    else:
        monkeypatch.setenv("AGENT_HARNESS_SECRET_BACKEND", "vault")
        monkeypatch.setattr(secrets, "_external_secret", lambda *args: value)
        monkeypatch.setattr(secrets, "_cache", {})
    assert secrets.get_secret("SMOKE_API_KEY") == value
    assert value not in redact_secrets("provider echoed " + value).text
    stream = StreamingSecretRedactor()
    output = "".join(stream.feed(char) for char in "echo " + value + " suffix") + stream.flush()
    assert value not in output and "[REDACTED" in output
    logging.getLogger("ah.smoke").warning("provider echoed %s", value)
    assert value not in caplog.text


async def test_gateway_redacts_tool_arguments_and_results(monkeypatch):
    from unittest.mock import AsyncMock

    from ah.core import turns
    from ah.core.models import AgentResponse, Session, StreamEvent
    from ah.core.session import session_manager

    value = "sk-" + "a" * 30
    session = Session(id=uuid.uuid4())
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr("ah.services.maybe_auto_compact", AsyncMock())

    class Agent:
        async def run_stream(self, *args, **kwargs):
            yield StreamEvent(type="tool_call", tool_name="echo", tool_args={"input": value})
            yield StreamEvent(type="tool_result", tool_name="echo", tool_result=value)
            yield StreamEvent(type="done", response=AgentResponse(content="done"))

    frames = []
    gateway = Gateway(frames.append, agent_factory=lambda *args: Agent(), owns_db=False)
    token = await turns.try_begin_turn(session.id)
    gateway._turn_tokens[str(session.id)] = token
    try:
        await gateway._run_turn(session.id, "sanitized-tool", "smoke")
        assert value not in json.dumps(frames)
        assert any(frame["params"]["type"] == "tool.complete" for frame in frames)
    finally:
        await turns.end_turn(session.id, token)
