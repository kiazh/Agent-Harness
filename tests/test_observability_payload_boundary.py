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
