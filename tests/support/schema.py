"""Private schemas keep migration and worker tests out of shared test data."""

import os
import uuid
from contextlib import asynccontextmanager

import asyncpg

from ah.db.connection import Database


@asynccontextmanager
async def isolated_schema(administrator, *, initialize=True):
    name = "harness_test_" + uuid.uuid4().hex
    # Only this generated identifier is interpolated; never a caller's schema.
    await administrator.execute(f'CREATE SCHEMA "{name}"')
    database = Database(dsn=os.environ["AGENT_HARNESS_TEST_DATABASE_URL"])
    try:
        database._pool = await asyncpg.create_pool(
            dsn=database.dsn,
            min_size=1,
            max_size=3,
            server_settings={"search_path": name + ",public"},
        )
        if initialize:
            await database.initialize_schema()
        yield name, database
    finally:
        await database.close()
        await administrator.execute(f'DROP SCHEMA "{name}" CASCADE')
