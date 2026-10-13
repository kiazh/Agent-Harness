"""SoulSpec identity, human persona and growth form one effective runtime persona."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.soulspec.adapters import ClaudeCodeAdapter, CodexAdapter, MachinaAdapter
from machina.soulspec.merge import SoulSpecMerger
from machina.soulspec.schema import _NO_TOOLS_SENTINEL, SoulSpec


def authored():
    return SoulSpec(
        name="careful-helper",
        version="1.0.0",
        persona=SoulSpec.Persona(
            name="Careful Helper",
            description="A careful specialist",
            system_prompt="Keep specialist identity stable.",
            values=[SoulSpec.PersonaValue("accuracy", 0.9)],
            traits=[SoulSpec.PersonaTrait("curiosity", 0.7)],
            voice=SoulSpec.Voice("warm", "concise"),
        ),
        workflow=[
            SoulSpec.Workflow("verify", steps=["Verify observed outcomes."], tools=["read_file"])
        ],
    )


def test_structured_identity_survives_adapter_and_compiles():
    from machina.soulspec.runtime import resolve_persona

    original = authored()
    restored = MachinaAdapter.to_soulspec(original.to_agent_def())
    effective = resolve_persona(
        restored,
        requested_style="A patient mentor",
        notes={"lesson:test": "Verify before claiming success."},
    )
    prompt = effective.render_prompt()
    for text in [
        "specialist identity",
        "accuracy",
        "curiosity",
        "warm",
        "concise",
        "patient mentor",
        "Verify before claiming",
    ]:
        assert text in prompt
    assert restored.to_dict() == original.to_dict()
    assert effective.to_agent_def().tools == original.to_agent_def().tools
    assert "patient mentor" not in original.to_yaml()


@pytest.mark.parametrize("adapter", [CodexAdapter, ClaudeCodeAdapter])
def test_framework_exports_include_identity_body(adapter):
    assert "Keep specialist identity stable." in adapter.export(authored())


def test_permission_intersection_and_deny_all_roundtrip():
    first = SoulSpec(
        name="first",
        config=SoulSpec.Config(permissions=SoulSpec.Permissions(allowed_tools=["read_file"])),
    )
    second = SoulSpec(
        name="second",
        config=SoulSpec.Config(permissions=SoulSpec.Permissions(allowed_tools=["write_file"])),
    )
    merged = SoulSpecMerger.merge(first, second)
    definition = merged.to_agent_def()
    assert definition.tools == [_NO_TOOLS_SENTINEL]
    restored = MachinaAdapter.to_soulspec(definition).to_agent_def()
    assert restored.tools == [_NO_TOOLS_SENTINEL]
    assert json.loads(json.dumps(restored.to_dict()))["tools"]  # serializable and restricted


def test_package_roundtrip_preserves_effective_persona(tmp_path):
    from machina.soulspec.runtime import resolve_persona

    effective = resolve_persona(
        authored(),
        requested_style="Speak gently",
        notes={"preference:examples": "Use concrete examples"},
    )
    effective.write_package(tmp_path, author="Local user")
    restored = SoulSpec.from_package(tmp_path)
    assert restored.to_dict() == effective.to_dict()
    assert "Use concrete examples" in (tmp_path / "SOUL.md").read_text()
    assert "Speak gently" in restored.render_prompt()
    assert restored.to_agent_def().tools == ["read_file"]


async def test_stored_soul_factory_resume_growth_and_style(db_pool, monkeypatch):
    import uuid

    from machina.core import agent_def, agent_factory
    from machina.core.config import config
    from machina.core.models import Session
    from machina.memory.growth import GrowthStore

    owner = "soul-" + uuid.uuid4().hex[:12]
    sid = uuid.uuid4()
    spec = authored()
    spec.name = owner
    session = Session(id=sid, agent_id=owner)
    monkeypatch.setattr(agent_def, "db", db_pool)
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", AsyncMock(return_value=None))
    monkeypatch.setattr("machina.core.agent.session_manager.get", AsyncMock(return_value=session))
    for method in ("get_recent_context", "get_latest_images", "search_archive_text"):
        monkeypatch.setattr(
            "machina.core.agent.context_manager." + method, AsyncMock(return_value=[])
        )
    monkeypatch.setattr("machina.memory.embeddings.embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "machina.memory.retriever.MemoryRetriever.retrieve", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(config, "persona_description", "A patient mentor")
    monkeypatch.setattr(config, "rag_enabled", False)
    store = GrowthStore(db_pool)
    monkeypatch.setattr("machina.memory.growth.growth_store", store)
    await db_pool.execute("INSERT INTO sessions(id, agent_id) VALUES ($1, $2)", sid, owner)
    try:
        await agent_def.agent_registry.save(spec.to_agent_def())
        await store.teach(owner, "lesson", "verification", "Check observed results.")
        for _ in range(2):  # a fresh factory instance simulates resume/restart
            agent = await agent_factory.build_agent_for_session(session)
            _, messages = await agent._prepare_context(
                sid, "implement a feature", pending_chunks=[]
            )
            prompt = messages[0]["content"]
            for text in (
                "accuracy",
                "curiosity",
                "warm",
                "patient mentor",
                "Check observed results.",
            ):
                assert text in prompt
            assert agent.allowed_tools == ["read_file"]
            assert prompt.count("Check observed results.") == 1
        monkeypatch.setattr(config, "persona_description", "")
        agent = await agent_factory.build_agent_for_session(session)
        _, messages = await agent._prepare_context(sid, "another feature", pending_chunks=[])
        assert "patient mentor" not in messages[0]["content"]
        assert "warm" in messages[0]["content"]
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", sid)
        await db_pool.execute("DELETE FROM agents WHERE name = $1", owner)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


async def test_deny_all_survives_database_and_factory(db_pool, monkeypatch):
    import uuid

    from machina.core import agent_def, agent_factory
    from machina.core.models import Session

    owner = "soul-deny-" + uuid.uuid4().hex[:12]
    spec = SoulSpec(
        name=owner,
        persona=SoulSpec.Persona(name=owner, system_prompt="Read no tools."),
        config=SoulSpec.Config(permissions=SoulSpec.Permissions(deny_all=True)),
    )
    monkeypatch.setattr(agent_def, "db", db_pool)
    monkeypatch.setattr("machina.core.provider.get_provider", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(agent_factory, "_shared_rag_pipeline", AsyncMock(return_value=None))
    try:
        saved = await agent_def.agent_registry.save(spec.to_agent_def())
        assert saved.tools == [_NO_TOOLS_SENTINEL]
        agent = await agent_factory.build_agent_for_session(
            Session(id=uuid.uuid4(), agent_id=owner)
        )
        assert agent._tool_defs() == []
        assert MachinaAdapter.to_soulspec(saved).to_agent_def().tools == [_NO_TOOLS_SENTINEL]
    finally:
        await db_pool.execute("DELETE FROM agents WHERE name = $1", owner)


async def test_persona_export_import_and_rollback_keep_owner_authority(
    db_pool, monkeypatch, tmp_path
):
    import uuid

    from machina.core import agent_def
    from machina.core.config import config
    from machina.memory.growth import GrowthStore
    from machina.soulspec import lifecycle

    owner = "soul-owner-" + uuid.uuid4().hex[:12]
    spec = authored()
    spec.name = owner
    monkeypatch.setattr(agent_def, "db", db_pool)
    monkeypatch.setattr("machina.db.connection.db", db_pool)
    store = GrowthStore(db_pool)
    monkeypatch.setattr(lifecycle, "growth_store", store)
    monkeypatch.setattr(config, "persona_description", "Speak gently")
    try:
        await agent_def.agent_registry.save(spec.to_agent_def())
        await store.teach(owner, "lesson", "test", "Original verified lesson.")
        revision = (await store.rsi_status(owner))["revision"]
        public = tmp_path / "public"
        await lifecycle.export_persona(owner, public)
        assert "Original verified lesson" not in (public / "SOUL.md").read_text()
        snapshot = tmp_path / "snapshot"
        await lifecycle.export_persona(owner, snapshot, include_learned=True)
        assert "Original verified lesson" in (snapshot / "SOUL.md").read_text()
        await store.teach(owner, "lesson", "test", "Updated verified lesson.")
        await store.rollback(owner, revision)
        assert (await store.get(owner))["notes"]["lesson:test"] == "Original verified lesson."
        imported = await lifecycle.import_persona(owner, snapshot, restore_learned=True)
        assert imported.name == owner
        assert imported.tools == ["read_file"]
        restored = await lifecycle.effective_persona(owner, include_learned=True)
        assert "accuracy" in restored.render_prompt()
        assert "Original verified lesson" in restored.render_prompt()
        assert (await store.get("unrelated-owner"))["notes"] == {}
        await store.forget(owner, "lesson:test")
        assert (
            "Original verified lesson"
            not in (await lifecycle.effective_persona(owner, include_learned=True)).render_prompt()
        )
    finally:
        await db_pool.execute("DELETE FROM agents WHERE name = $1", owner)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


async def test_runtime_soul_migration_keeps_legacy_agent(db_pool):
    from tests.support.schema import isolated_schema

    async with isolated_schema(db_pool) as (_, database):
        await database.execute("ALTER TABLE agents DROP COLUMN soul_spec")
        await database.execute(
            "INSERT INTO agents(name, system_prompt) VALUES ('legacy-soul', 'Keep my identity')"
        )
        await database.initialize_runtime_schema()
        await database.initialize_runtime_schema()
        row = await database.fetchrow(
            "SELECT system_prompt, soul_spec FROM agents WHERE name = 'legacy-soul'"
        )
        assert row["system_prompt"] == "Keep my identity"
        assert row["soul_spec"] is None


@pytest.mark.parametrize("mutation", ["missing-state", "different-body", "invalid-persona"])
def test_invalid_package_extension_is_rejected_cleanly(tmp_path, mutation):
    spec = authored()
    spec.write_package(tmp_path, author="Local user")
    manifest_path = tmp_path / "soul.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "missing-state":
        manifest["x-machina"].pop("soulSpec")
    elif mutation == "invalid-persona":
        manifest["x-machina"]["soulSpec"]["persona"] = {"system_prompt": 123}
    else:
        (tmp_path / "SOUL.md").write_text("A conflicting identity")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        SoulSpec.from_package(tmp_path)


def test_merging_personas_preserves_style_lessons_and_resources():
    from machina.soulspec.runtime import resolve_persona

    base = resolve_persona(
        authored(), requested_style="Speak gently", notes={"lesson:check": "Check results"}
    )
    base.package_manifest = {"files": {"soul": "SOUL.md", "style": "STYLE.md"}}
    base.package_files = {"STYLE.md": b"Prefer short paragraphs"}
    merged = SoulSpecMerger.merge(
        base,
        SoulSpec(
            name="careful-helper",
            persona=SoulSpec.Persona(traits=[SoulSpec.PersonaTrait("patience", 0.8)]),
        ),
    )
    assert "Speak gently" in merged.render_prompt()
    assert "Check results" in merged.render_prompt()
    assert "Prefer short paragraphs" in merged.render_prompt()
    assert "patience" in merged.render_prompt()


async def test_persona_import_never_restores_stale_permissions(db_pool, monkeypatch, tmp_path):
    import uuid

    from machina.core import agent_def
    from machina.core.agent_def import AgentDef
    from machina.soulspec import lifecycle

    owner = "soul-race-" + uuid.uuid4().hex[:12]
    spec = authored()
    spec.write_package(tmp_path, author="Local user")
    monkeypatch.setattr(agent_def, "db", db_pool)
    monkeypatch.setattr("machina.db.connection.db", db_pool)
    original_get = agent_def.agent_registry.get
    await agent_def.agent_registry.save(AgentDef(name=owner, tools=["read_file", "write_file"]))

    async def narrow_after_read(name):
        definition = await original_get(name)
        await db_pool.execute(
            "UPDATE agents SET tools = '[\"read_file\"]'::jsonb WHERE name = $1", owner
        )
        return definition

    monkeypatch.setattr(agent_def.agent_registry, "get", narrow_after_read)
    try:
        imported = await lifecycle.import_persona(owner, tmp_path)
        assert imported.tools == ["read_file"]
        assert (await original_get(owner)).tools == ["read_file"]
    finally:
        await db_pool.execute("DELETE FROM agents WHERE name = $1", owner)


def test_legacy_provider_survives_soul_roundtrip():
    from machina.core.agent_def import AgentDef

    definition = AgentDef(
        name="custom", provider="ollama", model="local-model", tools=["read_file"]
    )
    restored = MachinaAdapter.to_soulspec(definition).to_agent_def()
    assert restored.provider == "ollama"
    assert restored.model == "local-model"


def test_structured_soul_yaml_loads_through_registry(tmp_path, monkeypatch):
    from machina.core.agent_def import AgentRegistry

    monkeypatch.setenv("MACHINA_AGENTS_DIR", str(tmp_path))
    (tmp_path / "careful-helper.yaml").write_text(authored().to_yaml())
    definition = AgentRegistry()._file_definitions()["careful-helper"]
    assert "accuracy" in MachinaAdapter.to_soulspec(definition).render_prompt()
    assert definition.tools == ["read_file"]


def test_malformed_or_mixed_yaml_cannot_widen_or_poison_registry(tmp_path, monkeypatch):
    from machina.core.agent_def import AgentRegistry

    monkeypatch.setenv("MACHINA_AGENTS_DIR", str(tmp_path))
    (tmp_path / "good.yaml").write_text(authored().to_yaml())
    (tmp_path / "mixed.yaml").write_text(
        "name: mixed\ntools: [read_file]\npersona: {name: mixed}\n"
    )
    (tmp_path / "broken.yaml").write_text(
        "name: broken\npersona: {name: broken}\nworkflow: [{description: missing name}]\n"
    )
    definitions = AgentRegistry()._file_definitions()
    assert list(definitions) == ["careful-helper"]


def test_merged_plain_souls_export_and_reload(tmp_path):
    merged = SoulSpecMerger.merge(authored(), authored())
    merged.write_package(tmp_path, author="Local user")
    assert SoulSpec.from_package(tmp_path).to_dict() == merged.to_dict()


async def test_real_cli_soul_export_import_roundtrip(db_pool, tmp_path):
    import asyncio
    import os
    import subprocess
    import sys
    import uuid

    from machina.memory.growth import GrowthStore

    owner = "soul-cli-" + uuid.uuid4().hex[:12]
    sid = uuid.uuid4()
    spec = authored()
    spec.name = owner
    await db_pool.execute(
        "INSERT INTO agents(name, description, system_prompt, tools, soul_spec) "
        "VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)",
        owner,
        spec.persona.description,
        spec.persona.system_prompt,
        json.dumps(["read_file"]),
        json.dumps(spec.to_storage_dict()),
    )
    await db_pool.execute("INSERT INTO sessions(id, agent_id) VALUES ($1, $2)", sid, owner)
    store = GrowthStore(db_pool)
    await store.teach(owner, "lesson", "verify", "Check observed outcomes")
    env = dict(
        os.environ,
        MACHINA_ENV_FILE=str(tmp_path / "absent.env"),
        DATABASE_URL=db_pool.dsn,
        MACHINA_DATABASE_URL=db_pool.dsn,
        MACHINA_PERSONALITY_GROWTH_ENABLED="true",
        MACHINA_MEMORY_ENABLED="true",
    )

    def invoke(command, path, *flags):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "machina",
                "growth",
                command,
                str(path),
                "--session",
                str(sid),
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    try:
        public = tmp_path / "public"
        private = tmp_path / "private"
        await asyncio.to_thread(invoke, "export-soul", public)
        assert SoulSpec.from_package(public).persona.learned_notes == {}
        await asyncio.to_thread(invoke, "export-soul", private, "--include-learned")
        assert "lesson:verify" in SoulSpec.from_package(private).persona.learned_notes
        await store.forget(owner, "lesson:verify")
        result = await asyncio.to_thread(invoke, "import-soul", private, "--restore-learned")
        assert result["agent_id"] == owner
        assert (await store.get(owner))["notes"]["lesson:verify"] == "Check observed outcomes"
        row = await db_pool.fetchrow("SELECT tools, soul_spec FROM agents WHERE name = $1", owner)
        assert json.loads(row["tools"]) == ["read_file"]
        assert (
            "accuracy" in SoulSpec.from_storage_dict(json.loads(row["soul_spec"])).render_prompt()
        )
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", sid)
        await db_pool.execute("DELETE FROM agents WHERE name = $1", owner)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)
