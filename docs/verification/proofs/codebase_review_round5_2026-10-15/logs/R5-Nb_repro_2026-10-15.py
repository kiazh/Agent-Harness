"""R5-Nb RED: RAG source credentials persist in context_chunks (finding 18 remainder).

Indexes one document whose ``source`` carries BOTH a URI userinfo password and
a short credential query value, then reads the real ``context_chunks`` row
(``document_source`` + msgpack payload) and the ``audit_events`` rows.

Exit 1 => RED reproduced: a raw credential survives in the stored row.
Exit 0 => GREEN: no credential in the stored row or the audit rows.

Run (isolated private schema; never the shared DB)::

    MACHINA_TEST_DATABASE_URL=... .venv/bin/python R5-Nb_repro_2026-10-15.py
"""

import asyncio
import json
import os

import msgpack

USERINFO_PASSWORD = "example-passphrase"
QUERY_SECRET = "short1"
SRC = "https://alice:example-passphrase@example.invalid/doc?api_key=short1&x=1"


def _blob(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


async def main() -> int:
    # --- unit seam: the source redactor the pipeline applies must remove both ---
    from machina.core.provider.audit import redact_url_credentials
    from machina.memory.redaction import redact_secrets
    from machina.rag import pipeline as _pipeline_mod

    # Unfixed code stores sources via ``_redact_or_failed`` alone; the fix
    # adds ``_redact_source`` (runs ``redact_url_credentials`` afterwards) and
    # routes every source path through it.
    _source_redactor = getattr(_pipeline_mod, "_redact_source", _pipeline_mod._redact_or_failed)
    storage_only = _pipeline_mod._redact_or_failed(SRC)
    applied = _source_redactor(SRC)
    print(f"source                   = {SRC!r}")
    print(f"redact_secrets (storage) = {redact_secrets(SRC).text!r}")
    print(f"url_credentials          = {redact_url_credentials(SRC)!r}")
    print(f"_redact_or_failed        = {storage_only!r}")
    print(f"applied source redactor  = {applied!r} ({_source_redactor.__name__})")
    unit_leak = (USERINFO_PASSWORD in applied) or (QUERY_SECRET in applied)

    # --- E2E: real index + context_chunks/audit_events row read ---
    from machina.db.connection import Database, db
    from tests.support.schema import isolated_schema

    dsn = os.environ["MACHINA_TEST_DATABASE_URL"]
    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            from machina.observability import audit as audit_mod
            from machina.rag import pipeline as pipeline_mod

            orig_pipe_db, orig_audit_db = pipeline_mod.db, audit_mod.db
            orig_pool, orig_dsn = db._pool, db.dsn
            pipeline_mod.db = database
            audit_mod.db = database
            db._pool, db.dsn = database._pool, database.dsn
            try:
                from machina.observability.audit import audit_persistence
                from machina.rag.loaders import Document
                from machina.rag.pipeline import RAGConfig, RAGPipeline

                session_id = await database.fetchval(
                    "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
                )
                audit_persistence.start()
                try:
                    pipe = RAGPipeline(
                        config=RAGConfig(enable_embeddings=False, enable_reranking=False)
                    )
                    await pipe.index_document(
                        Document(
                            content=("hello world content " * 20),
                            source=SRC,
                            doc_type="text",
                        ),
                        session_id,
                    )
                finally:
                    await audit_persistence.stop()

                rows = await database.fetch(
                    "SELECT document_source, payload_msgpack FROM context_chunks "
                    "WHERE session_id = $1",
                    session_id,
                )
                print(f"context_chunks rows: {len(rows)}")
                storage_leak = False
                for row in rows:
                    payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
                    meta = (payload.get("metadata") or {}).get("source")
                    fields = {
                        "document_source": row["document_source"],
                        "payload.metadata.source": meta,
                        "payload.source": payload.get("source"),
                    }
                    for name, value in fields.items():
                        blob = _blob(value)
                        has_pw = USERINFO_PASSWORD in blob
                        has_q = QUERY_SECRET in blob
                        storage_leak = storage_leak or has_pw or has_q
                        print(
                            f"  {name}: userinfo_leak={has_pw} query_leak={has_q}\n    {blob[:200]}"
                        )

                audit_rows = await database.fetch(
                    "SELECT event, payload FROM audit_events WHERE event LIKE 'rag_index%'"
                )
                print(f"audit rows: {len(audit_rows)}")
                audit_leak = False
                for row in audit_rows:
                    blob = _blob(row["payload"])
                    has_pw = USERINFO_PASSWORD in blob
                    has_q = QUERY_SECRET in blob
                    audit_leak = audit_leak or has_pw or has_q
                    print(
                        f"  event={row['event']} userinfo_leak={has_pw} query_leak={has_q}\n"
                        f"    {blob[:200]}"
                    )

                leak = unit_leak or storage_leak or audit_leak
                if leak:
                    which = []
                    if unit_leak:
                        which.append("unit-storage-redactor")
                    if storage_leak:
                        which.append("context_chunks")
                    if audit_leak:
                        which.append("audit_events")
                    print(f"RED REPRODUCED: raw credential survives in {', '.join(which)}")
                    return 1
                print("GREEN: no raw credential in storage or audit rows")
                return 0
            finally:
                pipeline_mod.db = orig_pipe_db
                audit_mod.db = orig_audit_db
                db._pool, db.dsn = orig_pool, orig_dsn
    finally:
        await administrator.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
