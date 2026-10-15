"""H06 repro/verify: reviewer diagnostic #2 (synthetic failing keyword store).

Before the fix `retrieve` returned `[]` (a valid empty result). After the fix
every attempted branch failing raises the typed `MemoryRetrievalError`.
"""
import asyncio

from machina.memory.retriever import MemoryRetrievalError, MemoryRetriever


async def main() -> None:
    retriever = MemoryRetriever(rerank=False)

    async def failing_keyword(**kwargs):
        raise ConnectionError("synthetic store unavailable")

    retriever._keyword_search = failing_keyword  # type: ignore[method-assign]
    try:
        result = await retriever.retrieve("anything")
    except MemoryRetrievalError as error:
        print("Raised typed error:", type(error).__name__, "-", error)
        print("Reported as valid empty result:", False)
        return
    print("Result returned:", result)
    print("Reported as valid empty result:", True)


asyncio.run(main())
