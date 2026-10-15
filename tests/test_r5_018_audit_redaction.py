"""R5-N (finding 18): RAG source credentials must not persist in audit events.

RED: indexing ``https://alice:example-passphrase@example.invalid/document``
(or a URL carrying a short ``?api_key=`` value) persisted the credential
verbatim in ``audit_events`` — the audit string sanitizer knew token formats
and DB URLs but not URI userinfo or generic credential query parameters.

GREEN: the shared audit sanitizer redacts URI userinfo passwords and
credential-bearing query/fragment values (any length/format); the RAG
pipeline redacts the source before emission; persistence re-sanitizes at the
INSERT boundary so a raw direct ``submit()`` cannot persist a credential.
"""

from __future__ import annotations

import json
import os

import pytest

from machina.core.provider.audit import _sanitize_value, redact_url_credentials

USERINFO_PASSWORD = "example-passphrase-r5n"
USERINFO_SRC = f"https://alice:{USERINFO_PASSWORD}@example.invalid/document"
QUERY_SECRET = "short1-r5n"
QUERY_SRC = f"https://example.invalid/doc?api_key={QUERY_SECRET}&x=1"
FRAGMENT_SECRET = "fragtok-r5n"
FRAGMENT_SRC = f"https://example.invalid/cb#access_token={FRAGMENT_SECRET}&state=1"


def test_userinfo_password_redacted_username_and_host_kept():
    out = redact_url_credentials(USERINFO_SRC)
    assert USERINFO_PASSWORD not in out
    assert "//alice:" in out and "example.invalid/document" in out
    assert "[REDACTED_URL_PASSWORD]" in out


def test_bare_userinfo_without_password_is_preserved():
    src = "https://bob@example.invalid/plain"
    assert redact_url_credentials(src) == src


def test_credential_query_values_redacted_neighbors_intact():
    out = redact_url_credentials(QUERY_SRC)
    assert QUERY_SECRET not in out
    assert "x=1" in out and "example.invalid/doc" in out


def test_long_credential_query_value_uses_url_marker():
    out = _sanitize_value("https://example.invalid/doc?api_key=SUPERSECRETVALUE12345678&x=1")
    assert "SUPERSECRETVALUE12345678" not in out
    assert "x=1" in out


def test_fragment_credential_redacted():
    out = redact_url_credentials(FRAGMENT_SRC)
    assert FRAGMENT_SECRET not in out
    assert "state=1" in out


def test_non_credential_params_and_safe_strings_untouched():
    assert redact_url_credentials("https://example.com/guide?page=2") == (
        "https://example.com/guide?page=2"
    )
    assert redact_url_credentials("https://example.com:8443/path") == (
        "https://example.com:8443/path"
    )
    assert redact_url_credentials("Hello world") == "Hello world"


def test_existing_db_marker_is_preserved_not_rewritten():
    out = _sanitize_value("postgres://user:secretpass@localhost:5432/db")
    assert "secretpass" not in out
    assert "[REDACTED_DB_PASSWORD]" in out
    assert "[REDACTED_URL_PASSWORD]" not in out


def test_sanitizer_redacts_both_vectors_end_to_end():
    for src, secret in (
        (USERINFO_SRC, USERINFO_PASSWORD),
        (QUERY_SRC, QUERY_SECRET),
        (FRAGMENT_SRC, FRAGMENT_SECRET),
    ):
        out = _sanitize_value({"source": src})["source"]
        assert secret not in out, src


@pytest.fixture
async def isolated_audit_db(monkeypatch):
    """Wire pipeline + audit-persistence DB handles to a private schema (G3)."""
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import Database, db
    from tests.support.schema import isolated_schema

    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            from machina.observability import audit as audit_mod
            from machina.rag import pipeline as pipeline_mod

            monkeypatch.setattr(pipeline_mod, "db", database)
            monkeypatch.setattr(audit_mod, "db", database)
            original_pool, original_dsn = db._pool, db.dsn
            db._pool, db.dsn = database._pool, database.dsn
            try:
                yield database
            finally:
                db._pool, db.dsn = original_pool, original_dsn
    finally:
        await administrator.close()


def _payload_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, default=str)


async def test_index_with_credentialed_sources_persists_no_secrets(isolated_audit_db):
    """E2E: real index → flush writer → audit-row read shows no credentials."""
    from machina.observability.audit import audit_persistence
    from machina.rag.loaders import Document
    from machina.rag.pipeline import RAGConfig, RAGPipeline

    database = isolated_audit_db
    session_id = await database.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    audit_persistence.start()
    try:
        pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
        for source in (USERINFO_SRC, QUERY_SRC, FRAGMENT_SRC):
            await pipeline.index_document(
                Document(
                    content=("hello world content " * 20),
                    source=source,
                    doc_type="text",
                ),
                session_id,
            )
    finally:
        await audit_persistence.stop()
    rows = await database.fetch(
        "SELECT event, payload FROM audit_events WHERE event LIKE 'rag_index%'"
    )
    assert len(rows) == 6  # start + complete per indexed source
    for row in rows:
        blob = _payload_text(row["payload"])
        assert USERINFO_PASSWORD not in blob
        assert QUERY_SECRET not in blob
        assert FRAGMENT_SECRET not in blob
    # Debuggability survives: redacted rows still identify host and user.
    blobs = " ".join(_payload_text(row["payload"]) for row in rows)
    assert "example.invalid" in blobs
    assert "//alice:" in blobs


async def test_persistence_boundary_sanitizes_raw_direct_submit(isolated_audit_db):
    """Defense in depth: a raw entry bypassing audit_log is still scrubbed."""
    from machina.observability.audit import audit_persistence

    database = isolated_audit_db
    audit_persistence.start()
    try:
        audit_persistence.submit(
            {"event": "r5n_direct_probe", "source": USERINFO_SRC, "next": QUERY_SRC}
        )
    finally:
        await audit_persistence.stop()
    row = await database.fetchrow(
        "SELECT payload FROM audit_events WHERE event = 'r5n_direct_probe'"
    )
    assert row is not None
    blob = _payload_text(row["payload"])
    assert USERINFO_PASSWORD not in blob
    assert QUERY_SECRET not in blob
    assert "example.invalid" in blob
