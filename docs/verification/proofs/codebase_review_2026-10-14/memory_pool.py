import asyncio
import importlib
import time
import uuid
from unittest.mock import patch
import asyncpg
from machina.db.connection import Database
from machina.memory.consolidator import MemoryConsolidator
consolidator = importlib.import_module('machina.memory.consolidator')
context = importlib.import_module('machina.core.context')
async def main():
    database = Database(dsn='postgresql://postgres:postgres@localhost:5432/machina_test')
    database._pool = await asyncpg.create_pool(database.dsn, min_size=1, max_size=1)
    sid = uuid.uuid4()
    try:
        with patch.object(consolidator, 'db', database), patch.object(context, 'db', database):
            worker = MemoryConsolidator()
            control = await asyncio.wait_for(worker._consolidate_session_impl(sid, 'harness'), timeout=0.5)
            print('CONTROL WITHOUT HELD LOCK CONNECTION:', control.status, flush=True)
            start = time.monotonic()
            try:
                await asyncio.wait_for(worker.consolidate_session(sid, 'harness'), timeout=0.25)
            except TimeoutError:
                print('REAL SINGLE-CONNECTION POOL:', 'consolidate_session timed out', 'elapsed=', round(time.monotonic()-start, 3), flush=True)
    finally:
        await database.close()
asyncio.run(main())
