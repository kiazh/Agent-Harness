"""Fresh/upgrade/repeated migrations and actual PostgreSQL SQL contracts."""

import ast
import re
import uuid
from pathlib import Path

import asyncpg
import pytest

from tests.support.schema import isolated_schema

pytestmark = [pytest.mark.integration, pytest.mark.db]


async def test_schema_loader_preserves_utf8_sql(db_pool, tmp_path):
    async with isolated_schema(db_pool, initialize=False) as (_, database):
        source = tmp_path / "utf8.sql"
        source.write_text(
            "CREATE TABLE utf8_probe(value text); INSERT INTO utf8_probe VALUES ('café ✓');",
            encoding="utf-8",
        )
        await database.initialize_schema(str(source))
        assert await database.fetchval("SELECT value FROM utf8_probe") == "café ✓"


async def test_migration_failure_rolls_back_all_statements(db_pool, tmp_path):
    async with isolated_schema(db_pool, initialize=False) as (schema, database):
        source = tmp_path / "invalid.sql"
        source.write_text(
            "CREATE TABLE migration_probe(value text); SELECT 1 / 0;", encoding="utf-8"
        )
        with pytest.raises(asyncpg.DivisionByZeroError):
            await database.initialize_schema(str(source))
        assert (
            await database.fetchval("SELECT to_regclass($1)", schema + ".migration_probe") is None
        )


async def test_legacy_session_upgrade_is_idempotent_and_preserves_data(db_pool):
    async with isolated_schema(db_pool, initialize=False) as (_, database):
        await database.execute("""CREATE TABLE sessions (
            id uuid PRIMARY KEY, title text, agent_id text DEFAULT 'harness', state_msgpack bytea,
            status text DEFAULT 'active', goal text, model text, provider text,
            context_budget int DEFAULT 8000, created_at timestamptz DEFAULT now(),
            last_activity timestamptz DEFAULT now())""")
        sid = uuid.uuid4()
        await database.execute(
            "INSERT INTO sessions(id, title, status, state_msgpack) VALUES ($1, $2, 'running', $3)",
            sid,
            "legacy session",
            b"legacy data",
        )
        await database.initialize_schema()
        await database.initialize_schema()
        row = await database.fetchrow(
            "SELECT title, state_msgpack, status, execution_mode FROM sessions WHERE id = $1", sid
        )
        assert dict(row) == {
            "title": "legacy session",
            "state_msgpack": b"legacy data",
            "status": "active",
            "execution_mode": "ask",
        }
        for mode in ("ask", "workspace", "sandbox", "full"):
            await database.execute(
                "UPDATE sessions SET execution_mode = $2 WHERE id = $1", sid, mode
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await database.execute(
                "UPDATE sessions SET execution_mode = 'invalid' WHERE id = $1", sid
            )


async def test_fresh_schema_fks_indexes_and_projections(db_pool):
    async with isolated_schema(db_pool) as (schema, database):
        await database.initialize_schema()
        from ah.core.scheduler import _COLUMNS as job_columns
        from ah.permissions.store import GRANT_COLUMNS

        for table, columns in (("jobs", job_columns), ("permission_grants", GRANT_COLUMNS)):
            assert await database.fetch(f"SELECT {columns} FROM {table} LIMIT 0") == []
        fks = await database.fetch(
            """SELECT c.relname AS child, p.relname AS parent, k.confdeltype::text AS deletion
            FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_class p ON p.oid = k.confrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE k.contype = 'f' AND n.nspname = $1""",
            schema,
        )
        edges = {(row["child"], row["parent"], row["deletion"]) for row in fks}
        assert {
            ("context_chunks", "sessions", "c"),
            ("context_archive", "sessions", "c"),
            ("jobs", "sessions", "c"),
            ("memory_provenance", "memories", "c"),
            ("permission_approvals", "sessions", "c"),
            ("permission_grants", "sessions", "c"),
        } <= edges
        indexes = await database.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1", schema
        )
        names = {row["indexname"] for row in indexes}
        assert {
            "idx_sessions_claim",
            "idx_sessions_status_last_activity",
            "idx_context_chunks_session_type_created",
            "idx_context_chunks_embedding",
        } <= names
        sid = await database.fetchval(
            "INSERT INTO sessions(title) VALUES ('cascade probe') RETURNING id"
        )
        await database.execute(
            "INSERT INTO context_chunks(session_id, agent_id, chunk_type, payload_msgpack) VALUES ($1, 'harness', 'compression_summary', $2)",
            sid,
            b"payload",
        )
        await database.execute("DELETE FROM sessions WHERE id = $1", sid)
        assert (
            await database.fetchval(
                "SELECT count(*) FROM context_chunks WHERE session_id = $1", sid
            )
            == 0
        )


def static_queries():
    """Complete literal SQL passed to the database, excluding dynamic builders."""
    root = Path(__file__).resolve().parents[1] / "ah"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {}
        for assignment in tree.body:
            if isinstance(assignment, ast.Assign):
                try:
                    value = ast.literal_eval(assignment.value)
                except (ValueError, TypeError):
                    continue
                for target in assignment.targets:
                    if isinstance(target, ast.Name) and isinstance(value, str):
                        constants[target.id] = value
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not node.args
                or not isinstance(node.func, ast.Attribute)
            ):
                continue
            if node.func.attr not in {"execute", "fetch", "fetchrow", "fetchval"}:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.JoinedStr):
                pieces = []
                for part in argument.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        pieces.append(part.value)
                    elif (
                        isinstance(part, ast.FormattedValue)
                        and isinstance(part.value, ast.Name)
                        and part.value.id in constants
                    ):
                        pieces.append(constants[part.value.id])
                    else:
                        break
                else:
                    query = "".join(pieces)
                    if re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE)\b", query, re.I):
                        yield path.name + ":" + str(node.lineno), query
                continue
            try:
                query = ast.literal_eval(argument)
            except (ValueError, TypeError):
                continue
            if isinstance(query, str) and re.match(
                r"\s*(SELECT|INSERT|UPDATE|DELETE)\b", query, re.I
            ):
                yield path.name + ":" + str(node.lineno), query


async def test_static_python_sql_compiles_against_schema(db_pool):
    async with isolated_schema(db_pool) as (_, database):
        errors, count = [], 0
        async with database.acquire() as connection:
            for location, query in static_queries():
                try:
                    await connection.prepare(query)
                    count += 1
                except asyncpg.PostgresError as error:
                    errors.append(f"{location}: {type(error).__name__}")
        assert count >= 100, "SQL discovery stopped covering production queries"
        assert errors == [], errors
