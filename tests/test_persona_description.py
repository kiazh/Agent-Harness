import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.config import Config, config, validate_value


def test_persona_description_validation():
    assert (
        validate_value("persona_description", "  Warm, witty and concise  ")
        == "Warm, witty and concise"
    )
    assert validate_value("persona_description", "") == ""
    for value in [123, None, "x" * 2001]:
        with pytest.raises(ValueError):
            validate_value("persona_description", value)


def test_persona_description_environment_and_redaction(monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_PERSONA_DESCRIPTION", "A playful tutor")
    assert (
        Config(persona_description="A formal assistant").get("persona_description")
        == "A playful tutor"
    )
    secret = "sk-or-v1-" + "a" * 50
    assert secret not in validate_value("persona_description", "Never share " + secret)


def test_persona_description_survives_config_reload(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("ah.core.config")

    monkeypatch.delenv("AGENT_HARNESS_PERSONA_DESCRIPTION", raising=False)
    monkeypatch.setattr(module, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    original = Config()
    original.set("persona_description", "A patient mentor with dry humor", persist=True)
    restored = Config.load()
    assert restored.get("persona_description") == "A patient mentor with dry humor"
    restored.set("persona_description", "", persist=True)
    assert restored.get("persona_description") == ""


@pytest.mark.asyncio
async def test_persona_applies_live_without_growth_and_clears(monkeypatch):
    from types import SimpleNamespace

    from ah.core.agent import BaseReActAgent
    from ah.core.models import Session

    session = Session(id=uuid.uuid4())
    monkeypatch.delenv("AGENT_HARNESS_PERSONA_DESCRIPTION", raising=False)
    monkeypatch.setattr(config, "personality_growth_enabled", False)
    monkeypatch.setattr(config, "memory_enabled", False)
    monkeypatch.setattr(config, "rag_enabled", False)
    monkeypatch.setattr(config, "persona_description", "A patient mentor with dry humor")
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr(
        "ah.core.agent.context_manager.get_recent_context", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "ah.core.agent.context_manager.get_latest_images", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "ah.core.agent.context_manager.search_archive_text", AsyncMock(return_value=[])
    )
    agent = BaseReActAgent(
        provider=SimpleNamespace(), system_prompt="CORE IDENTITY", allowed_tools=[]
    )
    _, messages = await agent._prepare_context(session.id, "hello", pending_chunks=[])
    prompt = messages[0]["content"]
    assert "CORE IDENTITY" in prompt
    assert "A patient mentor with dry humor" in prompt
    assert "permission" in prompt
    monkeypatch.setattr(config, "persona_description", "")
    _, messages = await agent._prepare_context(session.id, "hello", pending_chunks=[])
    assert "A patient mentor with dry humor" not in messages[0]["content"]
