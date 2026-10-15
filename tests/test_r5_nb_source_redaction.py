"""R5-Nb (finding 18 remainder): RAG source credentials never persist in
``context_chunks``.

The R5-N lane made audit *emission* and audit *persistence* redact URI
credentials via the shared ``redact_url_credentials``. The findi18 remainder
is the RAG **storage** path: ``index_document`` stored ``doc.source`` through
``_redact_or_failed`` (shared ``redact_secrets``) alone, which is blind to URI
userinfo passwords (``https://alice:pass@host``) and to credential query
values of any length (``?api_key=short1``).

RED (unfixed): indexing a source carrying both vectors persisted the raw
password and the raw query value verbatim in ``document_source`` and in the
stored msgpack payload (``metadata.source`` / ``source``).

GREEN: ``_redact_source()`` runs ``_redact_or_failed`` then the shared
``redact_url_credentials``; every source path — ``document_source``, payload
metadata ``source``, payload ``source``, and audit emission — shares that one
authoritative implementation. Host, username, path, and benign params stay
readable; a redactor failure fails closed to ``REDACTION_FAILED``.
"""

from __future__ import annotations

import json
import os

import msgpack
import pytest

from machina.rag.pipeline import REDACTION_FAILED

USERINFO_PASSWORD = "example-passphrase"
QUERY_SECRET = "short1"
COMBINED_SRC = "https://alice:example-passphrase@example.invalid/doc?api_key=short1&x=1"
REDACTED_SRC = (
    "https://alice:[REDACTED_URL_PASSWORD]@example.invalid/doc?api_key=[REDACTED_CREDENTIAL]&x=1"
)
BENIGN_SRC = "https://example.invalid/guide?page=2"


# ---------------------------------------------------------------------------
# Unit: the shared source redactor
# ---------------------------------------------------------------------------


def test_source_redactor_removes_both_vectors_and_keeps_context():
    from machina.rag import pipeline as pipeline_mod

    out = pipeline_mod._redact_source(COMBINED_SRC)
    assert USERINFO_PASSWORD not in out
    assert QUERY_SECRET not in out
    # Debuggability: username, host, path, and benign parameter survive.
    assert "//alice:" in out
    assert "example.invalid/doc" in out
    assert "x=1" in out
    assert "[REDACTED_URL_PASSWORD]" in out
    assert "[REDACTED_CREDENTIAL]" in out


def test_source_redactor_leaves_benign_source_untouched():
    from machina.rag import pipeline as pipeline_mod

    assert pipeline_mod._redact_source(BENIGN_SRC) == BENIGN_SRC


def test_source_redactor_preserves_bare_userinfo_identifier():
    from machina.rag import pipeline as pipeline_mod

    src = "https://bob@example.invalid/plain"
    assert pipeline_mod._redact_source(src) == src


def test_source_redactor_fails_closed_when_url_redactor_raises(monkeypatch):
    from machina.rag import pipeline as pipeline_mod

    def _boom(_text):
        raise RuntimeError("url redaction failed")

    monkeypatch.setattr(pipeline_mod, "redact_url_credentials", _boom)
    assert pipeline_mod._redact_source(COMBINED_SRC) == REDACTION_FAILED


def test_source_redactor_fails_closed_when_secret_detector_raises(monkeypatch):
    from machina.rag import pipeline as pipeline_mod

    def _boom(_text):
        raise RuntimeError("secret detection failed")

    monkeypatch.setattr(pipeline_mod, "redact_secrets", _boom)
    out = pipeline_mod._redact_source(COMBINED_SRC)
    assert out == REDACTION_FAILED
    assert USERINFO_PASSWORD not in out and QUERY_SECRET not in out


# ---------------------------------------------------------------------------
# E2E: real index → real context_chunks / audit_events rows (isolated schema)
# ---------------------------------------------------------------------------


@pytest.fixture
async def isolated_rag_db(monkeypatch):
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


def _blob(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


async def _index_combined_source(database):
    from machina.rag.loaders import Document
    from machina.rag.pipeline import RAGConfig, RAGPipeline

    session_id = await database.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    await pipeline.index_document(
        Document(content=("hello world content " * 20), source=COMBINED_SRC, doc_type="text"),
        session_id,
    )
    return session_id


async def test_index_persists_no_source_credentials(isolated_rag_db):
    """Every stored field of the row is credential-free; context survives."""
    from machina.observability.audit import audit_persistence

    database = isolated_rag_db
    audit_persistence.start()
    try:
        session_id = await _index_combined_source(database)
    finally:
        await audit_persistence.stop()

    rows = await database.fetch("SELECT * FROM context_chunks WHERE session_id = $1", session_id)
    assert rows
    document_sources = set()
    for row in rows:
        assert USERINFO_PASSWORD not in _blob(row["document_source"])
        assert QUERY_SECRET not in _blob(row["document_source"])
        document_sources.add(row["document_source"])
        payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
        assert USERINFO_PASSWORD not in _blob(payload)
        assert QUERY_SECRET not in _blob(payload)
        assert USERINFO_PASSWORD not in _blob(row["search_text"])
        assert QUERY_SECRET not in _blob(row["search_text"])
        # Redacted identifiers are retained, not blanked out.
        assert payload["metadata"]["source"] == REDACTED_SRC
        assert payload["source"] == REDACTED_SRC
    assert document_sources == {REDACTED_SRC}
    assert "//alice:" in REDACTED_SRC and "x=1" in REDACTED_SRC


async def test_index_audit_rows_carry_no_source_credentials(isolated_rag_db):
    """Audit rows agree with storage and keep host/user for debuggability."""
    from machina.observability.audit import audit_persistence

    database = isolated_rag_db
    audit_persistence.start()
    try:
        await _index_combined_source(database)
    finally:
        await audit_persistence.stop()

    rows = await database.fetch(
        "SELECT event, payload FROM audit_events WHERE event LIKE 'rag_index%'"
    )
    assert len(rows) == 2  # start + complete
    blobs = " ".join(_blob(row["payload"]) for row in rows)
    assert USERINFO_PASSWORD not in blobs
    assert QUERY_SECRET not in blobs
    assert "example.invalid" in blobs
    assert "//alice:" in blobs
    assert "x=1" in blobs
