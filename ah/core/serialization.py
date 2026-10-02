"""Shared serialization utilities for database row ↔ model conversion.

Eliminates code duplication across context.py, pipeline.py, and store.py
for embedding string conversion, msgpack payload packing, and row-to-model
mapping.
"""
from __future__ import annotations

from typing import Any

import msgpack

from ah.core.models import ContextChunk

__all__ = [
    "row_to_chunk",
    "chunk_to_payload",
    "embedding_to_str",
    "str_to_embedding",
    "payload_to_msgpack",
]


def row_to_chunk(row: Any) -> ContextChunk:
    """Convert a database row (asyncpg.Record) to a ContextChunk.

    Handles msgpack payload unpacking and embedding string parsing.
    """
    payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
    embedding = None
    if row["embedding"] is not None:
        embedding = str_to_embedding(row["embedding"])
    return ContextChunk(
        id=row["id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        chunk_type=row["chunk_type"],
        payload=payload,
        token_count=row["token_count"],
        embedding=embedding,
        created_at=row["created_at"],
        accessed_at=row["accessed_at"],
    )


def chunk_to_payload(chunk: ContextChunk) -> dict[str, Any]:
    """Convert a ContextChunk to a payload dict suitable for msgpack packing."""
    return {
        "text": chunk.payload.get("text", ""),
        "metadata": chunk.payload.get("metadata", {}),
        "source": chunk.payload.get("source", ""),
    }


def embedding_to_str(embedding: list[float]) -> str:
    """Convert a list of floats to a pgvector-compatible string.

    Produces format: "[1.0,2.0,3.0]"
    """
    return "[" + ",".join(str(x) for x in embedding) + "]"


def str_to_embedding(embedding_str: str | Any) -> list[float]:
    """Parse a pgvector string back to a list of floats.

    Handles format: "[1.0,2.0,3.0]"
    """
    s = str(embedding_str)
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",")]


def payload_to_msgpack(payload: dict[str, Any]) -> bytes:
    """Pack a payload dict to msgpack bytes with binary type support."""
    return msgpack.packb(payload, use_bin_type=True)
