#!/usr/bin/env python
"""Audit finding: eight concurrent replacements of the same one-chunk
document leave multiple chunks (DELETE+INSERT without source serialization).

Uses the dedicated machina_test database only.
"""
import asyncio
import sys
import uuid
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

DSN = "postgresql://postgres:postgres@localhost:5432/machina_test"

from machina.db.connection import db
from machina.rag.loaders import Document
from machina.rag.pipeline import RAGPipeline
from machina.rag.reranker import IdentityReranker

SOURCE = "audit-race.md"


async def main() -> None:
    db.dsn = DSN
    await db.connect()
    await db.initialize_schema()

    session_id = uuid.uuid4()
    await db.execute(
        "INSERT INTO sessions (id, title, agent_id) VALUES ($1, $2, $3)",
        session_id,
        "audit-rag-race",
        "harness",
    )
    pipeline = RAGPipeline(reranker=IdentityReranker())
    document = Document(
        content=("A" * 2000) + "\n", source=SOURCE, doc_type="text", metadata={}
    )

    await asyncio.gather(
        *(pipeline.index_document(document, session_id=session_id) for _ in range(8))
    )

    count = await db.fetchval(
        "SELECT COUNT(*) FROM context_chunks WHERE session_id = $1 AND document_source = $2",
        session_id,
        SOURCE,
    )
    print(f"8 concurrent replacements -> chunks stored for one document: {count}")

    await db.execute(
        "DELETE FROM context_chunks WHERE session_id = $1 AND document_source = $2",
        session_id,
        SOURCE,
    )
    await db.execute("DELETE FROM sessions WHERE id = $1", session_id)
    await db.close()

    confirmed = int(count) > 1
    print("CLAIM CONFIRMED (concurrent replacement left extra chunks)"
          if confirmed else "CLAIM NOT REPRODUCED in this run")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
