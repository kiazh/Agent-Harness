"""H08 follow-up repro — a pending vector survives a model switch.

Submits a vector while embedder A is configured, switches the shared embedder
to B (same 1536 width), then approves the pending row. Before the follow-up,
the pending row carried no submission space, so the completed memory row was
tagged with the embedder configured at approval time (B) even though its
vector was produced by A.

Exit 0 = the completed row keeps the submission space (fixed);
exit 1 = the completed row is tagged with the post-switch space (bug).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import machina.memory.approval as approval_module  # noqa: E402
import machina.memory.identity as identity_module  # noqa: E402
import machina.memory.store as store_module  # noqa: E402
import machina.rag.embedder as rag_embedder  # noqa: E402
from machina.db.connection import Database  # noqa: E402
from machina.memory.approval import MemoryApprovalGate  # noqa: E402
from tests.support.schema import isolated_schema  # noqa: E402

UNIT_E0 = [1.0] + [0.0] * 1535


class _FakeEmbedder:
    """Minimal embedder stand-in: the space identity is name + width."""

    def __init__(self, model: str, dimensions: int = 1536) -> None:
        self._model = model
        self._dimensions = dimensions

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions


def use_embedder(model: str) -> None:
    fake = _FakeEmbedder(model)
    rag_embedder.get_shared_embedder = lambda: fake


async def main() -> int:
    dsn = os.environ["MACHINA_TEST_DATABASE_URL"]
    admin = Database(dsn=dsn)
    await admin.connect()
    try:
        async with isolated_schema(admin) as (_schema, database):
            store_module.db = database
            identity_module.db = database
            approval_module.db = database
            gate = MemoryApprovalGate(enabled=True)

            use_embedder("model-a")
            pending = await gate.submit(
                content="repro pending fact",
                category="fact",
                embedding=list(UNIT_E0),
            )

            # Config switch while the write is still pending: same width,
            # different model -> a different embedding space.
            use_embedder("model-b")
            memory = await gate.approve(pending.id)
            assert memory is not None, "approval must produce a memory row"
            stored = await database.fetchval(
                "SELECT embedding_space FROM memories WHERE id=$1", memory.id
            )
            try:
                pending_space = await database.fetchval(
                    "SELECT embedding_space FROM pending_memories WHERE id=$1", pending.id
                )
            except Exception as error:  # noqa: BLE001 — reported as evidence
                pending_space = f"<column unavailable: {type(error).__name__}>"

            print("submitted under        : fake:model-a:1536")
            print(f"pending row space      : {pending_space}")
            print(f"completed memory space : {stored}")
            if stored and "model-a" in stored and pending_space == stored:
                print("RESULT: OK — completed row keeps the submission space")
                return 0
            print("RESULT: BUG — completed row was tagged with the post-switch space")
            return 1
    finally:
        await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
