"""R5-N RED: RAG source credentials persist in audit_events (finding 18)."""

import asyncio
import json
import os
import uuid

USERINFO_SRC = "https://alice:example-passphrase@example.invalid/document"
QUERY_SRC = "https://example.invalid/doc?api_key=short1&x=1"


async def main() -> int:
    from machina.core.provider.audit import _sanitize_value

    # --- unit-level seam: sanitizer must not leak credentials ---
    for src in (USERINFO_SRC, QUERY_SRC):
        out = _sanitize_value({"source": src})["source"]
        leaked = ("example-passphrase" in out) or ("short1" in out)
        print(f"sanitizer outlet source={src!r}\n  -> {out!r} leaked={leaked}")

    # --- E2E: real index + audit-row read in an isolated schema ---
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
                        config=RAGConfig(
                            enable_embeddings=False, enable_reranking=False
                        )
                    )
                    await pipe.index_document(
                        Document(
                            content=("hello world content " * 20),
                            source=USERINFO_SRC,
                            doc_type="text",
                        ),
                        session_id,
                    )
                    await pipe.index_document(
                        Document(
                            content=("more content here " * 20),
                            source=QUERY_SRC,
                            doc_type="text",
                        ),
                        session_id,
                    )
                finally:
                    await audit_persistence.stop()
                rows = await database.fetch(
                    "SELECT event, payload FROM audit_events WHERE event LIKE 'rag_index%'"
                )
                print(f"audit rows: {len(rows)}")
                leak = False
                for row in rows:
                    blob = (
                        row["payload"]
                        if isinstance(row["payload"], str)
                        else json.dumps(row["payload"], default=str)
                    )
                    has_pw = "example-passphrase" in blob
                    has_q = "short1" in blob
                    leak = leak or has_pw or has_q
                    print(
                        f"  event={row['event']} userinfo_leak={has_pw} query_leak={has_q}\n"
                        f"    {blob[:220]}"
                    )
                print("RED REPRODUCED: credential persists in audit_events" if leak
                      else "NOT REPRODUCED: no credential in audit_events")
                return 0 if leak else 1
            finally:
                pipeline_mod.db = orig_pipe_db
                audit_mod.db = orig_audit_db
                db._pool, db.dsn = orig_pool, orig_dsn
    finally:
        await administrator.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
