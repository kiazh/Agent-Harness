"""Final-review (2026-10-15) provider/config findings — RED-first evidence.

One evidence suite for the six confirmed findings in the final review:

* F1 (adversarial F1, P2): an embedded-``@`` DB-URL password
  (``postgres://alice:p@ss@db.example.com:5432/mydb``) leaked its tail
  (``ss``) because the DB-scheme pattern stopped at the first ``@`` and the
  later generic userinfo pass preserved the ``[REDACTED_DB_PASSWORD]`` marker.
* F2 (line_providers P3-3): ``audit.metadata()`` raised ``AttributeError`` on
  a non-string nested dict key (``key.lower()``).
* F3 (security_review S6, P3): ``audit_log``'s ``**sanitized_kwargs`` could
  override the reserved ``event`` / ``timestamp`` fields (audit spoofing).
* F4 (line_providers P3-4): the shared SSE parser did not strip a leading
  UTF-8 BOM, so a BOM-prefixed first frame (``event:`` or ``data:``) was
  dropped.
* F5 (line_providers P3-2): ``rate_limit_calls_per_minute = 0`` ("disables
  throttling") was unreachable — the config bound was ``(1, None)``.
* F6 (line_providers P2-1): ``runtime._detect_providers`` reported
  ``provider=anthropic`` as "unknown provider" (no PROVIDER_SPECS entry, no
  explicit arm).

Markers such as ``[REDACTED_DB_PASSWORD]`` / ``[REDACTED_URL_PASSWORD]`` are
stable and asserted unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# F1 — embedded-@ DB-URL passwords must be fully redacted
# ---------------------------------------------------------------------------


def test_embedded_at_db_password_tail_does_not_leak():
    """The whole password (including embedded ``@``) redacts to one marker."""
    from machina.core.provider.audit import _sanitize_value

    out = _sanitize_value("postgres://alice:p@ss@db.example.com:5432/mydb")
    assert "p@ss" not in out
    assert "@ss@" not in out
    assert "ss@db.example.com" not in out
    assert out == "postgres://alice:[REDACTED_DB_PASSWORD]@db.example.com:5432/mydb"


def test_multi_at_db_password_tail_does_not_leak():
    """``postgresql://u:se@cr@et@host/db`` redacts ``se@cr@et`` in full."""
    from machina.core.provider.audit import _sanitize_value

    out = _sanitize_value("postgresql://u:se@cr@et@host/db")
    assert "se@cr@et" not in out
    assert "cr@et" not in out
    assert out == "postgresql://u:[REDACTED_DB_PASSWORD]@host/db"


def test_single_at_db_marker_is_preserved_not_rewritten():
    """Control: the ordinary single-``@`` DSN keeps its existing marker."""
    from machina.core.provider.audit import _sanitize_value

    out = _sanitize_value("postgres://user:secretpass@localhost:5432/db")
    assert "secretpass" not in out
    assert "[REDACTED_DB_PASSWORD]" in out
    assert "[REDACTED_URL_PASSWORD]" not in out


def test_bare_db_userinfo_without_password_is_untouched():
    """Control: a bare ``user@host`` DSN is an identifier, not a credential."""
    from machina.core.provider.audit import _sanitize_value

    src = "postgres://user@localhost:5432/db"
    assert _sanitize_value(src) == src


def test_embedded_at_generic_scheme_still_redacts():
    """Control: the generic userinfo pass already covered non-DB schemes."""
    from machina.core.provider.audit import redact_url_credentials

    out = redact_url_credentials("https://alice:p@ss@example.invalid/doc")
    assert "p@ss" not in out
    assert out == "https://alice:[REDACTED_URL_PASSWORD]@example.invalid/doc"


# ---------------------------------------------------------------------------
# SEC-2 — presigned-URL signature credentials must be redacted
# ---------------------------------------------------------------------------

AZURE_SAS = (
    "https://acct.blob.core.windows.net/c/doc"
    "?sv=2024-11-04&se=2030-01-01T00%3A00%3A00Z&sp=r&sig=abc123%2B%2F%3D"
)

AWS_PRESIGNED = (
    "https://bucket.s3.amazonaws.com/key"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAEXAMPLE%2F20261015%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261015T000000Z"
    "&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=deadbeefcafef00d"
)


def test_azure_sas_sig_param_redacted_neighbors_intact():
    from machina.core.provider.audit import _sanitize_value

    out = _sanitize_value(AZURE_SAS)
    assert "abc123%2B%2F%3D" not in out
    assert "sig=[REDACTED_CREDENTIAL]" in out
    assert "se=2030-01-01T00%3A00%3A00Z" in out
    assert "sp=r" in out


def test_aws_sigv4_signature_and_credential_redacted():
    from machina.core.provider.audit import _sanitize_value

    out = _sanitize_value(AWS_PRESIGNED)
    assert "deadbeefcafef00d" not in out
    assert "AKIAEXAMPLE" not in out
    assert "X-Amz-Signature=[REDACTED_CREDENTIAL]" in out
    assert "X-Amz-Credential=[REDACTED_CREDENTIAL]" in out
    # Non-secret SigV4 bookkeeping survives for debuggability.
    assert "X-Amz-Date=20261015T000000Z" in out
    assert "X-Amz-Expires=3600" in out
    assert "X-Amz-SignedHeaders=host" in out


def test_gcs_x_goog_signature_redacted():
    from machina.core.provider.audit import redact_url_credentials

    src = (
        "https://storage.googleapis.com/b/o"
        "?X-Goog-Algorithm=GOOG4&X-Goog-Signature=abcdef123456&X-Goog-Expires=3600"
    )
    out = redact_url_credentials(src)
    assert "abcdef123456" not in out
    assert "X-Goog-Signature=[REDACTED_CREDENTIAL]" in out
    assert "X-Goog-Expires=3600" in out


def test_signature_like_benign_params_stay_intact():
    from machina.core.provider.audit import redact_url_credentials

    for src in (
        "https://example.invalid/api?signal=green&design=flat",
        "https://example.invalid/api?resig=1&insig=2&assigned=3",
        "https://example.invalid/api?X-Amz-Date=20261015T000000Z&X-Amz-Expires=3600",
        "https://example.invalid/api?X-Amz-SignedHeaders=host&X-Amz-Algorithm=AWS4-HMAC-SHA256",
    ):
        assert redact_url_credentials(src) == src, src


def test_is_credential_param_precise_sig_rules():
    from machina.core.provider.audit import _is_credential_param

    assert _is_credential_param("sig")
    assert _is_credential_param("X-Amz-Signature")
    assert _is_credential_param("X-Amz-Credential")
    assert _is_credential_param("X-Goog-Signature")
    assert not _is_credential_param("signal")
    assert not _is_credential_param("design")
    assert not _is_credential_param("resig")
    assert not _is_credential_param("insig")
    assert not _is_credential_param("assigned")


# ---------------------------------------------------------------------------
# SEC-3 — password-only userinfo must be redacted
# ---------------------------------------------------------------------------


def test_password_only_db_userinfo_redacted():
    from machina.core.provider.audit import _sanitize_value

    out = _sanitize_value("redis://:s3cr3t@host:6379/0")
    assert "s3cr3t" not in out
    assert out == "redis://:[REDACTED_DB_PASSWORD]@host:6379/0"

    out = _sanitize_value("postgresql://:hunter2x@db.internal:5432/app")
    assert "hunter2x" not in out
    assert out == "postgresql://:[REDACTED_DB_PASSWORD]@db.internal:5432/app"


def test_password_only_generic_userinfo_redacted():
    from machina.core.provider.audit import redact_url_credentials

    out = redact_url_credentials("https://:p@ssw0rd@example.invalid/doc")
    assert "p@ssw0rd" not in out
    assert out == "https://:[REDACTED_URL_PASSWORD]@example.invalid/doc"


def test_password_only_controls_stay_intact():
    from machina.core.provider.audit import redact_url_credentials

    for src in (
        "https://bob@example.invalid/plain",  # bare user, no password
        "https://example.com:8443/path",  # port, no userinfo
        "postgres://user@localhost:5432/db",  # bare DB user
    ):
        assert redact_url_credentials(src) == src, src


# ---------------------------------------------------------------------------
# F2 — audit metadata tolerates non-string nested dict keys
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_audit(monkeypatch):
    """Capture the sanitized entry handed to audit persistence."""
    from machina.observability import audit as audit_mod

    captured: list[dict] = []
    monkeypatch.setattr(audit_mod.audit_persistence, "submit", captured.append)
    return captured


def test_audit_log_tolerates_non_string_nested_dict_keys(captured_audit):
    """A structured payload with a non-str key must not crash ``audit_log``.

    Covers the metadata() ``key.lower()`` ``AttributeError`` (final-review
    P3-3). Keys stay JSON-serializable int/float/None (the tuple-key case is a
    separate ``json.dumps`` limitation, not this finding).
    """
    from machina.core.provider.audit import audit_log

    audit_log("non_str_keys", data={1: "y", 2.5: "z", None: "w"})
    entry = captured_audit[-1]
    assert entry["event"] == "non_str_keys"
    assert entry["data"] == {1: "y", 2.5: "z", None: "w"}


def test_audit_log_still_redacts_sensitive_nested_str_keys(captured_audit):
    """The non-string-key fix must not weaken sensitive-key redaction."""
    from machina.core.provider.audit import audit_log

    audit_log("sensitive", data={"api_key": "synthetic-value-1234567890"})
    entry = captured_audit[-1]
    assert entry["data"] == {"api_key": "[REDACTED]"}


# ---------------------------------------------------------------------------
# F3 — event/timestamp are authoritative over caller kwargs
# ---------------------------------------------------------------------------


def test_audit_log_reserved_keys_cannot_be_spoofed(captured_audit, caplog):
    """A caller-supplied ``event``/``timestamp`` cannot rewrite the record."""
    import logging

    from machina.core.provider.audit import audit_log

    before = time.time()
    with caplog.at_level(logging.INFO, logger="machina.audit"):
        audit_log("real_event", event="spoofed_event", timestamp=0.0, detail="kept")
    after = time.time()

    entry = captured_audit[-1]
    assert entry["event"] == "real_event"
    assert isinstance(entry["timestamp"], float)
    assert before <= entry["timestamp"] <= after
    assert entry["detail"] == "kept"

    # The emitted wire line carries the same authoritative identity.
    records = [record for record in caplog.records if record.name == "machina.audit"]
    payload = json.loads(records[-1].getMessage())
    assert payload["event"] == "real_event"
    assert payload["timestamp"] != 0.0


# ---------------------------------------------------------------------------
# F4 — shared SSE parser strips a single leading UTF-8 BOM
# ---------------------------------------------------------------------------

_BOM = "\ufeff"


def test_sse_field_strips_leading_bom():
    from machina.core.provider.sse import sse_data, sse_event, sse_field

    assert sse_data(f"{_BOM}data: {{}}") == "{}"
    assert sse_data(f"{_BOM}data:[DONE]") == "[DONE]"
    assert sse_event(f"{_BOM}event: message_start") == "message_start"
    assert sse_field(f"{_BOM}data", "data") == ""
    # Only ONE leading BOM is stripped; a second stays payload.
    assert sse_data(f"{_BOM}{_BOM}data: x") is None
    # A BOM in the middle of a line is not touched (not a leading BOM).
    assert sse_data("data: a\ufeffb") == "a\ufeffb"


class _MockStreamResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):
        for line in self._body.decode().splitlines():
            yield line


class _MockStreamContext:
    def __init__(self, body: bytes) -> None:
        self._response = _MockStreamResponse(body)

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


def _streaming_client(body: bytes) -> MagicMock:
    client = MagicMock()
    client.stream = MagicMock(return_value=_MockStreamContext(body))
    return client


def _direct_provider(client: MagicMock):
    from machina.core.provider.direct import DirectProvider
    from machina.core.provider.specs import ProviderSpec

    spec = ProviderSpec(
        "f4-bom", "https://f4-bom.invalid/v1", "F4_BOM_KEY", "m", requires_key=False
    )
    provider = DirectProvider.__new__(DirectProvider)
    provider.spec = spec
    provider.api_key = "k"
    provider.model = "m"
    provider._default_effort = ""
    provider._rate_limiter = AsyncMock()
    provider._rate_limit_key = spec.name
    provider.client = client
    return provider


async def test_first_sse_frame_with_leading_bom_is_not_dropped():
    """A BOM on the first frame must not drop its content (RED pre-fix)."""
    chunk = {"model": "m", "choices": [{"delta": {"content": "Hello"}}]}
    stop = {"model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]}
    body = (f"{_BOM}data: {json.dumps(chunk)}\ndata: {json.dumps(stop)}\ndata: [DONE]\n").encode()
    provider = _direct_provider(_streaming_client(body))

    events = [
        event async for event in provider.stream_complete([{"role": "user", "content": "hi"}])
    ]
    texts = [event.content for event in events if event.type == "text"]
    assert texts == ["Hello"], texts
    done = next(event for event in events if event.type == "done")
    assert done.response.content == "Hello"


# ---------------------------------------------------------------------------
# F5 — rate_limit_calls_per_minute = 0 is a supported (disable) value
# ---------------------------------------------------------------------------


def test_rate_limit_zero_is_accepted_by_config():
    from machina.core.config import validate_value

    assert validate_value("rate_limit_calls_per_minute", 0) == 0
    assert validate_value("rate_limit_calls_per_minute", "0") == 0


def test_rate_limit_negative_is_still_rejected():
    from machina.core.config import validate_value

    with pytest.raises(ValueError, match="rate_limit_calls_per_minute"):
        validate_value("rate_limit_calls_per_minute", -1)


def test_rate_limit_zero_builds_a_disabled_bucket():
    from machina.core.provider.rate_limit import _build_rate_limiter

    bucket = _build_rate_limiter(0)
    assert bucket.rate == 0.0
    assert bucket.capacity == 0


async def test_rate_limit_zero_acquire_returns_without_waiting():
    from machina.core.provider.rate_limit import AsyncTokenBucket

    bucket = AsyncTokenBucket(rate=0, capacity=0)
    await asyncio.wait_for(bucket.acquire(), timeout=1.0)


def test_config_zero_rate_limit_reaches_the_registry(monkeypatch):
    """config.get → reload_rate_limiters → a disabled live bucket."""
    from machina.core.config import config
    from machina.core.provider import rate_limit as rl

    monkeypatch.setattr(config, "_session_overrides", {"rate_limit_calls_per_minute": 0})
    assert config.get("rate_limit_calls_per_minute") == 0

    key = "f5-zero-rate"
    rl._RATE_LIMITERS[key] = rl._build_rate_limiter(60)
    try:
        rl.reload_rate_limiters()
        assert rl._RATE_LIMITERS[key].rate == 0.0
        assert rl._RATE_LIMITERS[key].capacity == 0
    finally:
        rl._RATE_LIMITERS.pop(key, None)


# ---------------------------------------------------------------------------
# F6 — anthropic readiness is honest
# ---------------------------------------------------------------------------


def test_runtime_reports_anthropic_ready_with_key(monkeypatch):
    from machina.core import runtime
    from machina.core.config import config

    monkeypatch.setattr(
        config,
        "_session_overrides",
        {"provider": "anthropic", "anthropic_api_key": "synthetic-anthropic-key"},
    )
    report = runtime._detect_providers()
    assert report["chat_provider_selected"] == "anthropic"
    assert report["chat_provider"] == "ready", report
    assert report["chat_provider_reason"] == ""


def test_runtime_reports_anthropic_missing_key(monkeypatch):
    from machina.core import runtime
    from machina.core.config import config

    monkeypatch.setattr(
        config,
        "_session_overrides",
        {"provider": "anthropic", "anthropic_api_key": ""},
    )
    report = runtime._detect_providers()
    assert report["chat_provider_selected"] == "anthropic"
    assert report["chat_provider"] == "unavailable", report
    assert report["chat_provider_reason"] == "ANTHROPIC_API_KEY not set"


def test_runtime_still_reports_unknown_for_removed_providers(monkeypatch):
    """Control: the anthropic arm must not swallow other unknown providers."""
    from machina.core import runtime
    from machina.core.config import config

    monkeypatch.setattr(config, "_session_overrides", {"provider": "definitely-not-a-provider"})
    report = runtime._detect_providers()
    assert report["chat_provider"] == "unavailable", report
    assert "unknown provider" in report["chat_provider_reason"], report


# ---------------------------------------------------------------------------
# F7 (line_gateway_obs_cli F5) — shutdown drains background work BEFORE it
# stops the audit writer, so audit events emitted by consolidation/learning
# tasks are persisted instead of dropped.
# ---------------------------------------------------------------------------


async def test_runtime_shutdown_drains_background_before_audit_stop(monkeypatch):
    """Structural pin: drain runs, then the audit writer stops."""
    from machina.core import runtime as rt
    from machina.observability import audit as audit_mod

    order: list[str] = []

    async def record_drain(timeout: float = 5.0) -> int:
        order.append("drain")
        return 0

    async def record_stop(*args, **kwargs) -> None:
        order.append("audit_stop")

    monkeypatch.setattr(rt, "drain_background_tasks", record_drain)
    monkeypatch.setattr(audit_mod.audit_persistence, "stop", record_stop)
    monkeypatch.setattr("machina.rag.embedder.close_shared_embedder", AsyncMock())
    monkeypatch.setattr("machina.db.connection.db.close", AsyncMock())

    runtime = rt.RuntimeServices(_started=True)
    await runtime.shutdown(timeout=1.0)
    assert order == ["drain", "audit_stop"], order


async def test_runtime_shutdown_persists_audit_from_drained_task(db_pool, monkeypatch):
    """Behavioral pin: an audit event from a still-running background task
    submitted during shutdown reaches ``audit_events`` (not dropped)."""
    from machina.core import runtime as rt
    from machina.core.background_tasks import pending_learning_reviews
    from machina.core.provider.audit import audit_log
    from machina.observability import audit as audit_mod

    monkeypatch.setattr(audit_mod, "db", db_pool)
    monkeypatch.setattr("machina.rag.embedder.close_shared_embedder", AsyncMock())
    monkeypatch.setattr("machina.db.connection.db.close", AsyncMock())

    audit_mod.audit_persistence.start()
    ran = asyncio.Event()

    async def worker() -> None:
        # Yield once so the drain is genuinely awaiting a live task.
        await asyncio.sleep(0)
        audit_log("shutdown_drain_probe", detail="kept")
        ran.set()

    task = asyncio.create_task(worker())
    pending_learning_reviews.add(task)
    try:
        runtime = rt.RuntimeServices(_started=True)
        await runtime.shutdown(timeout=5.0)
    finally:
        pending_learning_reviews.discard(task)
        while audit_mod.audit_persistence._owners:
            await audit_mod.audit_persistence.stop(timeout=0.05)

    assert ran.is_set()
    row = await db_pool.fetchrow(
        "SELECT payload FROM audit_events WHERE event = 'shutdown_drain_probe'"
    )
    assert row is not None, "audit event from a drained background task was dropped"
    await db_pool.execute("DELETE FROM audit_events WHERE event = 'shutdown_drain_probe'")
