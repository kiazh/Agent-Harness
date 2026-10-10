"""Mock connection contracts match asyncpg's synchronous context managers."""

from contextlib import asynccontextmanager


def attach_connection(database):
    @asynccontextmanager
    async def transaction():
        yield

    class Connection:
        def __getattr__(self, name):
            return getattr(database, name)

        def transaction(self, **kwargs):
            return transaction()

    @asynccontextmanager
    async def acquire(**kwargs):
        yield Connection()

    database.acquire = acquire
    return database
