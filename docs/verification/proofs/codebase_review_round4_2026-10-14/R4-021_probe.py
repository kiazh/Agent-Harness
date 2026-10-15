"""R4-021 probe: malformed 200 -> embed()/embed_query() behavior + cache state.

Run against the unfixed tree (RED) and the fixed tree (GREEN):
    cd <tree> && /Users/shadow/machina/.venv/bin/python <this file>
"""

import asyncio
import os

os.environ.setdefault("MACHINA_EMBEDDING_BACKEND", "openai")
os.environ.setdefault("MACHINA_EMBEDDING_BASE_URL", "http://stub.test/v1")

import httpx  # noqa: E402


def _malformed(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"model": "test-embed"})  # no "data"


async def main() -> None:
    from machina.memory.embeddings import embed_query
    from machina.rag.embedder import get_shared_embedder

    embedder = get_shared_embedder()
    assert embedder is not None, "stub endpoint + openai backend must build"
    await embedder._client.aclose()
    embedder._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_malformed), base_url=embedder._base_url
    )

    try:
        vector = await embedder.embed("probe text")
    except Exception as exc:  # noqa: BLE001 - probe reports the failure surface
        print(f"embed() raised {type(exc).__name__}: {exc}")
    else:
        print(
            f"embed() returned list(len={len(vector)}, all-zero={not any(vector)})"
        )
    print(f"cache_size after embed(): {embedder.cache_size}")

    query_embedding = await embed_query("probe text")
    if query_embedding is None:
        print("embed_query() -> None (honest keyword-only fallback)")
    else:
        print(
            "embed_query() -> list(len=%d, all-zero=%s) "
            "(a 'successful' zero-vector embedding)"
            % (len(query_embedding), not any(query_embedding))
        )

    # Second call for the same text: does it hit the poisoned cache?
    try:
        second = await embedder.embed("probe text")
    except Exception as exc:  # noqa: BLE001
        print(f"embed() second call raised {type(exc).__name__}: {exc}")
    else:
        print(f"embed() second call returned all-zero={not any(second)}")
    print(f"cache_size final: {embedder.cache_size}")
    await embedder.close()


asyncio.run(main())
