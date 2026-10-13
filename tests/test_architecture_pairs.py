"""Stream B architecture pairs: skills seam + TurnScope/grant-cache contracts.

Direction rules (AST, no imports needed) lock the Module/Seam/Depth/Locality
vocabulary from improve-codebase-architecture: ``core``/``controller``
consumers reach skills ONLY through ``machina.skills.ports``, and the broker
turn-id reads live in exactly ONE seam function per module.

Seam contracts (mocked DB, no providers) lock behavior: single resolve per
turn, invalidation paths, copies in/out, bypass without a turn, the 1024
caps, and ranking parity (interface vs direct calls on fixed queries).

Deletion test per seam: if ``skills.ports`` vanished, ranking/disclosure
complexity would reappear across every consumer; if the turn caches vanished,
verdicts would be identical at N-times the SELECT cost.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MACHINA = REPO / "machina"


def _top_level_machina_imports(path: pathlib.Path) -> set[str]:
    """Module-level (top statement) machina deps of *path*.

    Only direct ``import``/``from`` statements in the module body count —
    deferred (function-level) imports are not module-level cycle edges.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if (node.level or 0) == 0 and (node.module or "").startswith("machina"):
                deps.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("machina"):
                    deps.add(alias.name)
    return deps


def _skill_imports(path: pathlib.Path) -> set[str]:
    return {d for d in _top_level_machina_imports(path) if d.startswith("machina.skills")}


def _functions_referencing(path: pathlib.Path, names: set[str]) -> set[str]:
    """Function names whose body references any Name id in *names*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
            if node.id in names and self.stack:
                found.add(self.stack[-1])

    Visitor().visit(tree)
    return found


# ─── B1: skills seam direction ───────────────────────────────────────────────


def test_ports_module_has_no_machina_imports():
    """The seam itself is dependency-free: no module-level machina edges."""
    assert _top_level_machina_imports(MACHINA / "skills" / "ports.py") == set()


def test_controller_skills_deps_flow_only_through_ports():
    """Every controller module reaches skills ONLY via skills.ports."""
    package = MACHINA / "controller"
    assert package.is_dir()
    checked = 0
    for path in sorted(package.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for dep in _skill_imports(path):
            assert dep == "machina.skills.ports", f"{path.name} bypasses the seam: {dep}"
        checked += 1
    assert checked >= 1


def test_session_mode_and_execution_context_have_no_direct_skill_deps():
    for name in ("core/session_mode.py", "core/execution_context.py"):
        for dep in _skill_imports(MACHINA / name):
            assert dep == "machina.skills.ports", f"{name} bypasses the seam: {dep}"


def test_preselect_uses_the_ports_rank_dispatch():
    """preselect.rank_skills IS ports.rank_skills (no reimplementation)."""
    import machina.controller.preselect as preselect_mod
    from machina.skills import ports

    assert preselect_mod.rank_skills is ports.rank_skills


def test_registry_declares_skill_source_conformance(tmp_path):
    from machina.skills import ports
    from machina.skills.registry import SkillRegistry

    assert issubclass(SkillRegistry, ports.SkillSource)
    assert isinstance(SkillRegistry(tmp_path), ports.SkillSource)
    for method in ("load_all", "list_skills", "get", "match_triggers", "get_skill_content"):
        expected = inspect.signature(getattr(ports.SkillSource, method))
        actual = inspect.signature(getattr(SkillRegistry, method))
        assert [p.name for p in expected.parameters.values()] == [
            p.name for p in actual.parameters.values()
        ], method
        assert [p.kind for p in expected.parameters.values()] == [
            p.kind for p in actual.parameters.values()
        ], method
        assert [p.default for p in expected.parameters.values()] == [
            p.default for p in actual.parameters.values()
        ], method


# ─── B2/B3: no new module-level cycle edges ──────────────────────────────────


def test_no_back_edge_from_broker_closure_to_session_mode():
    """session_mode -> broker is safe: broker's module-level closure never
    imports core.session_mode / core.execution_context (no cycle edge)."""
    closure = {
        "permissions/broker.py",
        "permissions/store.py",
        "permissions/policy.py",
        "permissions/__init__.py",
        "observability/diagnostics.py",
    }
    for rel in closure:
        deps = _top_level_machina_imports(MACHINA / rel)
        assert "machina.core.session_mode" not in deps, rel
        assert "machina.core.execution_context" not in deps, rel


def test_store_keeps_broker_import_deferred():
    """store -> broker must stay function-level (broker imports store at
    module level; a top-level back-edge would partially-initialize)."""
    assert _top_level_machina_imports(MACHINA / "permissions" / "store.py") == set()


def test_registry_preselect_new_edges_are_seam_only():
    assert _top_level_machina_imports(MACHINA / "skills" / "registry.py") == {
        "machina.observability.diagnostics",
        "machina.skills.ports",
    }
    assert _top_level_machina_imports(MACHINA / "controller" / "preselect.py") == {
        "machina.skills.ports",
    }


def test_broker_turn_read_lives_in_one_seam_function_per_file():
    """The turn-id read has exactly one home per module (seam locality)."""
    assert _functions_referencing(
        MACHINA / "core" / "session_mode.py", {"_permissions_broker"}
    ) == {"_broker_turn"}
    assert _functions_referencing(MACHINA / "permissions" / "store.py", {"_broker"}) == {
        "_current_turn"
    }


# ─── fixtures: mocked DB, no providers ───────────────────────────────────────


@pytest.fixture
def clean_seams():
    from machina.core import session_mode
    from machina.permissions import broker, store

    broker.set_approval_handler(None)
    session_mode.reset_for_tests()
    store.clear_grant_cache()
    try:
        yield
    finally:
        broker.set_approval_handler(None)
        session_mode.reset_for_tests()
        store.clear_grant_cache()


class _ModeStubDb:
    connected = True

    def __init__(self, mode: str = "workspace") -> None:
        self.mode = mode
        self.fetchrows = 0

    async def fetchrow(self, query, _sid):
        self.fetchrows += 1
        assert "execution_mode" in query
        return {"execution_mode": self.mode}


def _grant_row(grant_id: str, session_id: str) -> dict:
    return {
        "id": grant_id,
        "session_id": session_id,
        "agent_id": "harness",
        "mode": "full",
        "capability": "session",
        "scope_path": None,
        "scope_type": "file",
        "grant_kind": "session",
        "digest": "full-mode-session",
        "expires_at": None,
        "revoked": False,
    }


class _GrantStubDb:
    def __init__(self, grants: list[dict]) -> None:
        self.grants = grants
        self.grant_selects = 0
        self.approval_inserts = 0

    async def fetch(self, query, *_args):
        assert "permission_grants" in query
        self.grant_selects += 1
        return [dict(g) for g in self.grants]

    async def execute(self, query, *_args):
        assert "permission_grants" in query
        return "UPDATE 1"

    def acquire(self):
        outer = self

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, query, *_args):
                if "permission_approvals" in query:
                    outer.approval_inserts += 1
                    return "INSERT 1"
                return await outer.execute(query, *_args)

            def transaction(self):
                return self

            async def fetchrow(self, query, *_args):
                if "permission_approvals" in query:
                    if "FOR UPDATE" in query:
                        return {
                            "status": "pending",
                            "consumed": False,
                            "agent_id": "someone-else",
                        }
                    return {"request_id": "r1", "status": "approved"}
                raise AssertionError(f"unexpected fetchrow: {query}")

        return _Conn()


# ─── B2: TurnScope contracts ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_scope_resolves_mode_once_per_turn(clean_seams, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = _ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        for _ in range(5):
            assert await session_mode.resolve_effective_mode(sid) == "workspace"
    assert stub.fetchrows == 1


@pytest.mark.asyncio
async def test_implicit_broker_turn_caches_and_new_turn_reresolves(
    clean_seams, monkeypatch
):
    from machina.core import session_mode
    from machina.permissions import broker

    sid = str(uuid.uuid4())
    stub = _ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-T1")
    try:
        for _ in range(3):
            assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 1
        broker.set_approval_handler(None, principal="tui", turn_id="PAIR-T2")
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 2
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_mode_change_invalidates_turn_cache(clean_seams, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = _ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        session_mode.set_session_mode(sid, "full")
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 2
        session_mode.clear_session_mode(sid)
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.fetchrows == 3


@pytest.mark.asyncio
async def test_db_failure_never_poisons_turn_cache(clean_seams, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())

    class Flaky:
        connected = True
        calls = 0

        async def fetchrow(self, query, _sid):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("database unavailable")
            return {"execution_mode": "workspace"}

    stub = Flaky()
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        with pytest.raises(RuntimeError, match="database unavailable"):
            await session_mode.resolve_effective_mode(sid)
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        assert stub.calls == 2


@pytest.mark.asyncio
async def test_implicit_mode_cap_is_exact_and_evicts_oldest(clean_seams, monkeypatch):
    from machina.core import session_mode
    from machina.permissions import broker

    assert session_mode._IMPLICIT_MODE_CAP == 1024
    for i in range(1024):
        session_mode._implicit_modes[(f"OLD-{i}", f"sid-{i}")] = "ask"
    assert len(session_mode._implicit_modes) == 1024
    stub = _ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-CAP")
    try:
        sid = str(uuid.uuid4())
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
    finally:
        broker.set_approval_handler(None)
    assert len(session_mode._implicit_modes) == 1024
    assert ("OLD-0", "sid-0") not in session_mode._implicit_modes


def test_broker_turn_seam_prefers_get_current_turn_then_falls_back(
    clean_seams, monkeypatch
):
    from machina.core import session_mode
    from machina.permissions import broker, store

    broker.set_approval_handler(None, principal="tui", turn_id="HANDLER-TURN")
    try:
        assert session_mode._broker_turn() == "HANDLER-TURN"
        assert store._current_turn() == "HANDLER-TURN"
        monkeypatch.setattr(broker, "get_current_turn", lambda: "ACCESSOR-TURN")
        assert session_mode._broker_turn() == "ACCESSOR-TURN"
        assert store._current_turn() == "ACCESSOR-TURN"

        def _boom():
            raise RuntimeError("turn unavailable")

        monkeypatch.setattr(broker, "get_current_turn", _boom)
        assert session_mode._broker_turn() == ""
        assert store._current_turn() == ""
    finally:
        broker.set_approval_handler(None)


# ─── B3: grant-cache contracts ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_cache_single_select_per_turn_session(clean_seams, monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    stub = _GrantStubDb([_grant_row(str(uuid.uuid4()), sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-G1")
    try:
        first = await store.list_grants(sid)
        second = await store.list_grants(sid)
        assert first == second and first is not second
        assert stub.grant_selects == 1
        broker.set_approval_handler(None, principal="tui", turn_id="PAIR-G2")
        await store.list_grants(sid)
        assert stub.grant_selects == 2
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_grant_cache_bypassed_without_turn(clean_seams, monkeypatch):
    from machina.permissions import store

    sid = str(uuid.uuid4())
    stub = _GrantStubDb([_grant_row(str(uuid.uuid4()), sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    await store.list_grants(sid)
    await store.list_grants(sid)
    assert stub.grant_selects == 2


@pytest.mark.asyncio
async def test_grant_cache_returns_copies_not_aliases(clean_seams, monkeypatch):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    stub = _GrantStubDb([_grant_row(str(uuid.uuid4()), sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-G3")
    try:
        first = await store.list_grants(sid)
        first[0]["mode"] = "MUTATED"
        first.append({"id": "INJECTED"})
        second = await store.list_grants(sid)
        assert stub.grant_selects == 1
        assert all(g["mode"] != "MUTATED" for g in second)
        assert all(g.get("id") != "INJECTED" for g in second)
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_grant_cache_invalidated_on_save_revoke_decision(
    clean_seams, monkeypatch
):
    from machina.permissions import broker, store

    sid = str(uuid.uuid4())
    request_id = uuid.uuid4().hex
    stub = _GrantStubDb([_grant_row(str(uuid.uuid4()), sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    store._mem.approvals[request_id] = {
        "request_id": request_id,
        "session_id": sid,
        "status": "pending",
        "consumed": False,
        "agent_id": "someone-else",
    }
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-G4")
    try:
        await store.list_grants(sid)
        assert stub.grant_selects == 1
        await store.save_grant(
            {
                "session_id": sid,
                "agent_id": "harness",
                "mode": "ask",
                "capability": "op",
                "digest": "pair-d2",
            }
        )
        await store.list_grants(sid)
        assert stub.grant_selects == 2
        await store.revoke_grants(sid)
        await store.list_grants(sid)
        assert stub.grant_selects == 3
        resolved = await store.resolve_decision(request_id, "approved", principal="human")
        assert resolved is not None
        await store.list_grants(sid)
        assert stub.grant_selects == 4
    finally:
        broker.set_approval_handler(None)
        store._mem.approvals.pop(request_id, None)


@pytest.mark.asyncio
async def test_approval_rows_never_touch_grant_cache(clean_seams, monkeypatch):
    from machina.permissions import broker, store
    from machina.permissions.policy import build_request

    sid = str(uuid.uuid4())
    stub = _GrantStubDb([_grant_row(str(uuid.uuid4()), sid)])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-G5")
    try:
        await store.list_grants(sid)
        assert stub.grant_selects == 1
        req = build_request(
            operation="process.exec",
            argv=["echo", "hi"],
            mode="ask",
            session_id=sid,
            agent_id="harness",
        )
        await store.create_approval(req, principal="tui")
        await store.list_grants(sid)
        assert stub.grant_selects == 1
    finally:
        broker.set_approval_handler(None)


@pytest.mark.asyncio
async def test_grant_cache_cap_is_exact_and_evicts_oldest(clean_seams, monkeypatch):
    from machina.permissions import broker, store

    assert store._GRANT_CACHE_CAP == 1024
    for i in range(1024):
        store._grant_cache[(f"OLD-{i}", f"sid-{i}")] = []
    stub = _GrantStubDb([])

    async def fake_db():
        return stub

    monkeypatch.setattr(store, "_db", fake_db)
    broker.set_approval_handler(None, principal="tui", turn_id="PAIR-GCAP")
    try:
        await store.list_grants(str(uuid.uuid4()))
    finally:
        broker.set_approval_handler(None)
    assert len(store._grant_cache) == 1024
    assert ("OLD-0", "sid-0") not in store._grant_cache


# ─── B1: ranking parity (interface vs direct, fixed queries) ─────────────────


def _fixture_registry(tmp_path):
    from machina.skills.registry import SkillRegistry

    reg = SkillRegistry(tmp_path)
    reg.create_skill(
        "pair-python-testing",
        "Python testing with pytest",
        "Run pytest with coverage.",
        triggers=["pytest", "python testing"],
    )
    reg.create_skill(
        "pair-deploy",
        "Deploy services with docker",
        "Build and push docker images.",
        triggers=["deploy", "docker"],
    )
    reg.create_skill(
        "pair-review",
        "Review code carefully",
        "Read diffs before approving.",
        triggers=["review", "code review"],
    )
    return reg


_FIXED_QUERIES = [
    "how do I run python testing with pytest?",
    "deploy the docker service",
    "review this code",
    "pytest coverage for the deploy pipeline",
]


def test_rank_parity_interface_vs_direct_on_fixed_queries(tmp_path):
    from machina.skills import ports
    from machina.skills.runtime import rank_skills as direct_rank

    reg = _fixture_registry(tmp_path)
    for query in _FIXED_QUERIES:
        via_seam = [s.name for s in ports.rank_skills(reg, query)]
        direct = [s.name for s in direct_rank(reg, query)]
        assert via_seam == direct
        weighted = {"pair-deploy": 1.2}
        assert [s.name for s in ports.rank_skills(reg, query, weights=weighted)] == [
            s.name for s in direct_rank(reg, query, weights=weighted)
        ]


def test_preselect_cache_matches_direct_rank_and_ranks_once(tmp_path):
    from machina.controller.preselect import SkillPreselectCache
    from machina.skills import ports
    from machina.skills.registry import SkillRegistry

    reg = _fixture_registry(tmp_path)
    assert isinstance(reg, ports.SkillSource)
    cache = SkillPreselectCache(reg)
    for query in _FIXED_QUERIES:
        ranked = cache.rank(query)
        assert [s.name for s in ranked] == [
            s.name for s in ports.rank_skills(reg, query)
        ]
    assert cache.rank_calls == len(_FIXED_QUERIES)
    cache.rank(_FIXED_QUERIES[0])
    assert cache.rank_calls == len(_FIXED_QUERIES)
