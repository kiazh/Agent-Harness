"""R4-026 (E2E, H-DB + H-FS): `index_document` uses the canonical workspace resolver.

The permission broker normalizes/approves the target against
``execution_context``'s workspace root (``machina/permissions/policy.py``
``normalize_request``), but the shared pipeline's ``FileLoader`` used to
default to ``machina_home or cwd``. With ``workspace_root`` explicitly
configured to differ from both, approved absolute workspace paths are
rejected and relative paths silently index a same-named file under
``machina_home``. These tests drive the real tool → real shared pipeline →
real FileLoader → real ``search_documents`` against the real test database,
with ``workspace_root != machina_home != cwd``, and plant the old-base
lookalike to attack the divergence.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from machina.core.exceptions import ToolError
from machina.tools import rag as rag_tools
from machina.tools.agents import current_agent_id, current_session_id

WORKSPACE_CANARY = "unique-workspace-marker"
PLANTED_CANARY = "unique-planted-marker"
SHARED_NEEDLE = "sharedneedle"


@asynccontextmanager
async def _agent_scope(session_id):
    """Bind the caller scope exactly like the agent loop does per tool call."""
    session_token = current_session_id.set(session_id)
    agent_token = current_agent_id.set("harness")
    try:
        yield
    finally:
        current_session_id.reset(session_token)
        current_agent_id.reset(agent_token)


@pytest.fixture
def isolated_roots(tmp_path, monkeypatch):
    """workspace_root (project) != machina_home (planted) != cwd."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "x.md").write_text(
        f"# Workspace doc\n\n{WORKSPACE_CANARY} {SHARED_NEEDLE}\n", encoding="utf-8"
    )
    planted_home = tmp_path / "machina_home"
    (planted_home / "docs").mkdir(parents=True)
    (planted_home / "docs" / "x.md").write_text(
        f"# Planted doc\n\n{PLANTED_CANARY} {SHARED_NEEDLE}\n", encoding="utf-8"
    )
    from machina.core.config import config

    monkeypatch.setattr(config, "workspace_root", str(project))
    monkeypatch.setattr(config, "machina_home", str(planted_home))
    # Deterministic keyword-only pipeline: this proof exercises resolution,
    # never provider/model work. Force the shared pipeline to rebuild under
    # this configuration.
    monkeypatch.setattr(config, "embedding_backend", "off")
    monkeypatch.setattr(config, "reranker_backend", "none")
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)
    return project, planted_home


@pytest_asyncio.fixture
async def rag_db(db_pool, monkeypatch):
    """Real test DB wired into the pipeline and the session-ownership check."""
    monkeypatch.setattr("machina.rag.pipeline.db", db_pool)
    monkeypatch.setattr("machina.tools.agents.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    try:
        yield db_pool, session_id
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def _stored_sources(db_pool, session_id) -> set[str]:
    rows = await db_pool.fetch(
        "SELECT DISTINCT document_source FROM context_chunks WHERE session_id = $1",
        session_id,
    )
    return {row["document_source"] for row in rows}


async def test_absolute_workspace_path_indexes_that_file(isolated_roots, rag_db):
    """An approved absolute workspace path is indexed, not rejected."""
    project, _ = isolated_roots
    db_pool, session_id = rag_db
    target = (project / "docs" / "x.md").resolve()
    async with _agent_scope(session_id):
        result = await rag_tools.index_document(str(target), str(session_id))
        assert "Successfully indexed" in result
        output = await rag_tools.search_documents(SHARED_NEEDLE, str(session_id))
    assert WORKSPACE_CANARY in output
    assert PLANTED_CANARY not in output
    assert await _stored_sources(db_pool, session_id) == {str(target)}


async def test_relative_path_resolves_to_workspace_not_machina_home(isolated_roots, rag_db):
    """`docs/x.md` is the workspace file, never the machina_home lookalike."""
    project, planted_home = isolated_roots
    db_pool, session_id = rag_db
    async with _agent_scope(session_id):
        await rag_tools.index_document("docs/x.md", str(session_id))
        output = await rag_tools.search_documents(SHARED_NEEDLE, str(session_id))
    assert WORKSPACE_CANARY in output
    assert PLANTED_CANARY not in output
    stored = await _stored_sources(db_pool, session_id)
    assert stored == {str((project / "docs" / "x.md").resolve())}
    assert not any(str(planted_home) in source for source in stored)


async def test_planted_old_base_lookalike_is_never_indexed(isolated_roots, rag_db):
    """Adversarial: the old base's same-relative-path file must stay untouched."""
    _, planted_home = isolated_roots
    db_pool, session_id = rag_db
    async with _agent_scope(session_id):
        await rag_tools.index_document("docs/x.md", str(session_id))
        planted_output = await rag_tools.search_documents(PLANTED_CANARY, str(session_id))
    # The echoed query itself is not evidence; the stored sources are.
    assert str(planted_home) not in planted_output
    count = await db_pool.fetchval(
        "SELECT count(*) FROM context_chunks WHERE session_id = $1 AND document_source LIKE $2",
        session_id,
        f"{planted_home}%",
    )
    assert count == 0


async def test_absolute_path_outside_workspace_is_rejected(isolated_roots, rag_db):
    """Adversarial: a real file outside the workspace is never indexed."""
    _, planted_home = isolated_roots
    db_pool, session_id = rag_db
    outside = (planted_home / "docs" / "x.md").resolve()
    async with _agent_scope(session_id):
        with pytest.raises(ToolError, match="escapes the allowed base directory"):
            await rag_tools.index_document(str(outside), str(session_id))
    assert await _stored_sources(db_pool, session_id) == set()


async def test_symlink_escape_is_rejected(isolated_roots, rag_db):
    """Adversarial (H-FS): a workspace symlink to an outside file is rejected."""
    project, planted_home = isolated_roots
    db_pool, session_id = rag_db
    (project / "escape-link.md").symlink_to(planted_home / "docs" / "x.md")
    async with _agent_scope(session_id):
        with pytest.raises(ToolError, match="escapes the allowed base directory"):
            await rag_tools.index_document("escape-link.md", str(session_id))
    assert await _stored_sources(db_pool, session_id) == set()


async def test_dotdot_traversal_is_rejected(isolated_roots, rag_db):
    """Adversarial (H-FS): `..` cannot reach the planted tree outside the workspace."""
    db_pool, session_id = rag_db
    async with _agent_scope(session_id):
        with pytest.raises(ToolError, match="escapes the allowed base directory"):
            await rag_tools.index_document("docs/../../machina_home/docs/x.md", str(session_id))
    assert await _stored_sources(db_pool, session_id) == set()
