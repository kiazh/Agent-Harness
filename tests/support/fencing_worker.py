"""Small stdin-driven worker exercising production APIs against the test schema."""

import asyncio
import json
import os
import sys
import uuid

import asyncpg


async def main():
    from ah.core import turns
    from ah.core.scheduler import job_store
    from ah.core.session_mode import resolve_effective_mode, set_session_mode
    from ah.db.connection import db
    from ah.permissions.store import claim_execution

    db._pool = await asyncpg.create_pool(
        dsn=os.environ["AGENT_HARNESS_TEST_DATABASE_URL"],
        min_size=1,
        max_size=3,
        server_settings={"search_path": os.environ["AH_FENCING_TEST_SCHEMA"] + ",public"},
    )
    active = []
    entered = asyncio.Event()

    class HeldAgent:
        async def run_stream(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield  # Keep this an async generator; never produces effects.

    from ah.gateway.server import Gateway

    gateway = Gateway(lambda frame: None, agent_factory=lambda *args: HeldAgent(), owns_db=False)
    gateway._db_ready = True

    async def dispatch(request):
        op = request["operation"]
        sid = uuid.UUID(request["session_id"]) if "session_id" in request else None
        if op in {"gateway", "http", "turn", "mutation"}:
            entered.clear()
            if op == "gateway":
                from ah.gateway.errors import RpcError

                try:
                    await gateway._prompt_submit({"sessionId": str(sid), "text": "held test turn"})
                except RpcError:
                    return {"acquired": False}
                await asyncio.wait_for(entered.wait(), 5)
                token = gateway._turn_tokens[str(sid)]
            elif op == "http":
                from fastapi import HTTPException

                import ah.core.agent_factory as factory
                from ah.api import app as api

                async def fake_factory(*args, **kwargs):
                    return HeldAgent()

                factory.build_agent_for_session = fake_factory
                endpoint = next(
                    route.endpoint
                    for route in api.create_app().routes
                    if route.path == "/sessions/{session_id}/prompt"
                )
                try:
                    response = await endpoint(str(sid), api.PromptRequest(text="held test turn"))
                except HTTPException as error:
                    if error.status_code != 409:
                        raise
                    return {"acquired": False}

                async def consume():
                    async for _ in response.body_iterator:
                        pass

                active.append(asyncio.create_task(consume()))
                await asyncio.wait_for(entered.wait(), 5)
                token = turns._local_owner(sid)["token"]
            else:
                token = await (
                    turns.try_begin_turn(sid) if op == "turn" else turns.begin_mutation(sid)
                )
            return {"acquired": bool(token), "token": token}
        if op == "release":
            return {"released": await turns.end_turn(sid, request["token"])}
        if op == "mode":
            set_session_mode(str(sid), "full")  # Deliberately stale worker-local cache.
            return {"mode": await resolve_effective_mode(str(sid))}
        if op == "job":
            job = await job_store.claim_due()
            return {
                "acquired": bool(job),
                "id": str(job.id) if job else None,
                "token": str(job.claim_token) if job else None,
            }
        if op == "finish":
            return {
                "finished": await job_store.finish(
                    uuid.UUID(request["job_id"]), claim_token=uuid.UUID(request["token"])
                )
            }
        if op == "approval":
            return {
                "claimed": await claim_execution(
                    request["request_id"], digest=request["digest"], session_id=str(sid)
                )
            }
        raise ValueError("unknown test operation")

    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            try:
                response = await dispatch(request)
            except Exception as error:
                response = {"error_type": type(error).__name__}
            print(json.dumps(response), flush=True)
    finally:
        tasks = active + list(gateway._turns.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
