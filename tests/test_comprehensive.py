"""Comprehensive tests for AgentHarness."""
import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest
import pytest_asyncio
from typer.testing import CliRunner

# Import all modules to ensure clean imports
import ah
from ah.core.context import ContextManager, context_manager
from ah.core.assembler import PromptAssembler
from ah.core.models import (
    AgentResponse,
    ContextChunk,
    LLMResponse,
    Session,
    StreamEvent,
    ToolDefinition,
)
from ah.core.provider import (
    LLMProvider,
    OllamaProvider,
    OpenRouterProvider,
    get_provider,
)
from ah.core.session import SessionManager, session_manager
from ah.core.agent import ReActAgent, SYSTEM_PROMPT
from ah.db.connection import Database, db
from ah.skills.registry import Skill, SkillParser, SkillRegistry, skill_registry
from ah.tools.base import Tool, ToolRegistry, registry
from ah.tools import builtins  # noqa: F401 — registers web_search, web_extract, search_files
from ah.tools import file  # noqa: F401 — registers read_file, write_file, list_files
from ah.tools import terminal  # noqa: F401 — registers terminal
from ah.tools.file import read_file, write_file, list_files
from ah.cli import app


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_dir():
    """Create a temporary directory for file tests."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def sample_skill_dir(temp_dir):
    """Create a sample skills directory with SKILL.md files."""
    skill_dir = temp_dir / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-skill\n"
        "description: A test skill\n"
        "triggers:\n"
        "  - test\n"
        "  - testing\n"
        "version: 1.0.0\n"
        "---\n"
        "# Test Skill\n"
        "This is a test skill.\n",
        encoding="utf-8",
    )
    return temp_dir


@pytest.fixture
def mock_db():
    """Create a mock database object."""
    mock = AsyncMock()
    mock.fetch = AsyncMock(return_value=[])
    mock.fetchrow = AsyncMock(return_value=None)
    mock.fetchval = AsyncMock(return_value=0)
    mock.execute = AsyncMock(return_value="DELETE 0")
    return mock


# ============================================================================
# 1. Unit Tests — Pure Functions
# ============================================================================

class TestPromptAssembler:
    """Tests for PromptAssembler."""

    def test_assemble_basic(self):
        """Test basic prompt assembly."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt="You are a test agent.",
            goal="Test goal",
            recent_chunks=[{"type": "user_message", "payload": {"content": "hello"}}],
            retrieved_chunks=[],
            query="test query",
        )
        assert "You are a test agent." in prompt
        assert "Test goal" in prompt
        assert "test query" in prompt

    def test_assemble_no_goal(self):
        """Test assembly without goal."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt="System",
            goal=None,
            recent_chunks=[],
            retrieved_chunks=[],
            query="query",
        )
        assert "System" in prompt
        assert "query" in prompt
        assert "Current Goal" not in prompt

    def test_assemble_empty_recent_chunks(self):
        """Test assembly with empty recent chunks."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt="System",
            goal="Goal",
            recent_chunks=[],
            retrieved_chunks=[],
            query="query",
        )
        assert "Recent Activity" not in prompt

    def test_assemble_with_retrieved_chunks(self):
        """Test assembly with retrieved chunks."""
        assembler = PromptAssembler(session_budget=8000)
        chunk = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="test",
            chunk_type="memory",
            payload={"content": "remembered fact"},
        )
        prompt = assembler.assemble(
            system_prompt="System",
            goal="Goal",
            recent_chunks=[],
            retrieved_chunks=[(chunk, 0.95)],
            query="query",
        )
        assert "Relevant Context" in prompt
        assert "remembered fact" in prompt

    def test_assemble_token_budget(self):
        """Test that token budget is respected."""
        assembler = PromptAssembler(session_budget=100)
        prompt = assembler.assemble(
            system_prompt="System",
            goal="Goal",
            recent_chunks=[],
            retrieved_chunks=[],
            query="query",
        )
        # Should still include system + goal + query
        assert "System" in prompt
        assert "Goal" in prompt
        assert "query" in prompt

    def test_compress_chunk_tool_call(self):
        """Test compressing tool_call chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "tool_call",
            "payload": {"tool": "read_file", "args": {"path": "/tmp/test"}},
        })
        assert "tool_call" in result
        assert "read_file" in result
        assert "path=/tmp/test" in result

    def test_compress_chunk_user_message(self):
        """Test compressing user_message chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "user_message",
            "payload": {"content": "hello world"},
        })
        assert "user_message" in result
        assert "hello world" in result

    def test_compress_chunk_result(self):
        """Test compressing result chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "result",
            "payload": {"status": "ok", "result": "success"},
        })
        assert "ok" in result
        assert "success" in result

    def test_compress_chunk_result_truncation(self):
        """Test that long results are truncated."""
        assembler = PromptAssembler()
        long_result = "x" * 300
        result = assembler._compress_chunk({
            "type": "result",
            "payload": {"status": "ok", "result": long_result},
        })
        assert "..." in result
        assert len(result) < 250

    def test_compress_chunk_memory(self):
        """Test compressing memory chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "memory",
            "payload": {"content": "remembered"},
        })
        assert "memory" in result
        assert "remembered" in result

    def test_compress_chunk_heartbeat(self):
        """Test compressing heartbeat chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "heartbeat",
            "payload": {"prompt": "check status"},
        })
        assert "heartbeat" in result
        assert "check status" in result

    def test_compress_chunk_system(self):
        """Test compressing system chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "system",
            "payload": {"message": "system message"},
        })
        assert "system" in result
        assert "system message" in result

    def test_compress_chunk_user(self):
        """Test compressing user chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "user",
            "payload": {"content": "user input"},
        })
        assert "user" in result
        assert "user input" in result

    def test_compress_chunk_assistant(self):
        """Test compressing assistant chunk."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "assistant",
            "payload": {"content": "assistant response"},
        })
        assert "assistant" in result
        assert "assistant response" in result

    def test_compress_chunk_unknown(self):
        """Test compressing unknown chunk type."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({
            "type": "unknown_type",
            "payload": {"data": "value"},
        })
        assert "unknown_type" in result

    def test_estimate_tokens(self):
        """Test token estimation."""
        assembler = PromptAssembler()
        assert assembler._estimate_tokens("abcd") >= 1
        assert assembler._estimate_tokens("a" * 400) >= 50
        assert assembler._estimate_tokens("") == 0


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_register_sync_function(self):
        """Test registering a sync function."""
        reg = ToolRegistry()

        @reg.register(description="Test tool")
        def test_tool(x: int) -> int:
            return x * 2

        assert "test_tool" in reg.list_tools()
        tool = reg.get_tool("test_tool")
        assert tool is not None
        assert tool.description == "Test tool"
        assert not tool.is_async

    def test_register_async_function(self):
        """Test registering an async function."""
        reg = ToolRegistry()

        @reg.register(description="Async tool")
        async def async_tool(x: int) -> int:
            return x * 2

        assert "async_tool" in reg.list_tools()
        tool = reg.get_tool("async_tool")
        assert tool is not None
        assert tool.is_async

    def test_register_custom_name(self):
        """Test registering with custom name."""
        reg = ToolRegistry()

        @reg.register(name="custom_name", description="Custom")
        def original_name() -> str:
            return "ok"

        assert "custom_name" in reg.list_tools()
        assert "original_name" not in reg.list_tools()

    def test_register_custom_parameters(self):
        """Test registering with custom parameters."""
        reg = ToolRegistry()
        custom_params = {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        }

        @reg.register(parameters=custom_params)
        def tool_with_params(x: int) -> int:
            return x

        tool = reg.get_tool("tool_with_params")
        assert tool.parameters == custom_params

    def test_infer_schema(self):
        """Test schema inference from function signature."""
        reg = ToolRegistry()

        def sample_func(a: str, b: int, c: float = 1.0, d: bool = True) -> str:
            return "ok"

        schema = reg._infer_schema(sample_func)
        assert schema["type"] == "object"
        assert "a" in schema["properties"]
        assert "b" in schema["properties"]
        assert schema["properties"]["a"]["type"] == "string"
        assert schema["properties"]["b"]["type"] == "integer"
        assert "a" in schema["required"]
        assert "b" in schema["required"]
        assert "c" not in schema["required"]

    def test_infer_schema_with_list_dict(self):
        """Test schema inference with list and dict types."""
        reg = ToolRegistry()

        def func_with_types(items: list, mapping: dict) -> str:
            return "ok"

        schema = reg._infer_schema(func_with_types)
        assert schema["properties"]["items"]["type"] == "array"
        assert schema["properties"]["mapping"]["type"] == "object"

    def test_get_tool_definitions(self):
        """Test getting tool definitions for LLM."""
        reg = ToolRegistry()

        @reg.register(description="Test")
        def my_tool(x: int) -> int:
            return x

        defs = reg.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0].name == "my_tool"
        assert defs[0].description == "Test"
        assert defs[0].parameters is not None

    async def test_execute_sync_tool(self):
        """Test executing a sync tool."""
        reg = ToolRegistry()

        @reg.register()
        def add(a: int, b: int) -> int:
            return a + b

        result = await reg.execute("add", a=2, b=3)
        assert result == 5

    async def test_execute_async_tool(self):
        """Test executing an async tool."""
        reg = ToolRegistry()

        @reg.register()
        async def async_add(a: int, b: int) -> int:
            return a + b

        result = await reg.execute("async_add", a=2, b=3)
        assert result == 5

    async def test_execute_nonexistent_tool(self):
        """Test executing a non-existent tool raises error."""
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="not registered"):
            await reg.execute("nonexistent")

    def test_list_tools_empty(self):
        """Test listing tools in empty registry."""
        reg = ToolRegistry()
        assert reg.list_tools() == []

    def test_get_tool_names(self):
        """Test get_tool_names alias."""
        reg = ToolRegistry()

        @reg.register()
        def tool1() -> str:
            return "ok"

        assert reg.get_tool_names() == reg.list_tools()

    def test_get_tool_not_found(self):
        """Test getting a non-existent tool."""
        reg = ToolRegistry()
        assert reg.get_tool("nonexistent") is None


class TestSkillParser:
    """Tests for SkillParser."""

    def test_parse_with_frontmatter(self, temp_dir):
        """Test parsing SKILL.md with frontmatter."""
        skill_file = temp_dir / "SKILL.md"
        skill_file.write_text(
            "---\n"
            "name: my-skill\n"
            "description: My test skill\n"
            "triggers:\n"
            "  - test\n"
            "  - demo\n"
            "version: 2.0.0\n"
            "---\n"
            "# My Skill\n"
            "Content here.\n",
            encoding="utf-8",
        )
        skill = SkillParser.parse(skill_file)
        assert skill.name == "my-skill"
        assert skill.description == "My test skill"
        assert "test" in skill.triggers
        assert "demo" in skill.triggers
        assert skill.version == "2.0.0"
        assert "My Skill" in skill.content

    def test_parse_without_frontmatter(self, temp_dir):
        """Test parsing SKILL.md without frontmatter."""
        skill_file = temp_dir / "SKILL.md"
        skill_file.write_text("# Just content\nNo frontmatter.", encoding="utf-8")
        skill = SkillParser.parse(skill_file)
        assert skill.name == temp_dir.name
        assert skill.description == ""
        assert skill.triggers == []
        assert "Just content" in skill.content

    def test_parse_simple_yaml_basic(self):
        """Test simple YAML parsing with basic key-value."""
        import yaml
        yaml_text = "name: test\ndescription: A test\nversion: 1.0.0"
        result = yaml.safe_load(yaml_text)
        assert result["name"] == "test"
        assert result["description"] == "A test"
        assert result["version"] == "1.0.0"

    def test_parse_simple_yaml_list(self):
        """Test simple YAML parsing with list values."""
        import yaml
        yaml_text = "name: test\ntriggers:\n  - one\n  - two\n  - three"
        result = yaml.safe_load(yaml_text)
        assert result["name"] == "test"
        assert result["triggers"] == ["one", "two", "three"]

    def test_parse_simple_yaml_empty(self):
        """Test simple YAML parsing with empty input."""
        import yaml
        result = yaml.safe_load("")
        assert result is None or result == {}

    def test_parse_simple_yaml_mixed(self):
        """Test simple YAML parsing with mixed content."""
        import yaml
        yaml_text = "name: test\ndescription: A test\ntriggers:\n  - a\n  - b\nversion: 1.0"
        result = yaml.safe_load(yaml_text)
        assert result["name"] == "test"
        assert result["description"] == "A test"
        assert result["triggers"] == ["a", "b"]
        assert result["version"] == 1.0


class TestSkillRegistry:
    """Tests for SkillRegistry."""

    def test_load_all(self, sample_skill_dir):
        """Test loading all skills from directory."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        skills = reg.list_skills()
        assert len(skills) == 1
        assert skills[0].name == "test-skill"

    def test_load_all_empty_dir(self, temp_dir):
        """Test loading from empty directory."""
        reg = SkillRegistry(skills_dir=temp_dir)
        reg.load_all()
        assert reg.list_skills() == []

    def test_load_all_nonexistent_dir(self, temp_dir):
        """Test loading from non-existent directory."""
        reg = SkillRegistry(skills_dir=temp_dir / "nonexistent")
        reg.load_all()
        assert reg.list_skills() == []

    def test_get_skill(self, sample_skill_dir):
        """Test getting a skill by name."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        skill = reg.get("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"

    def test_get_skill_not_found(self, sample_skill_dir):
        """Test getting a non-existent skill."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        assert reg.get("nonexistent") is None

    def test_match_triggers(self, sample_skill_dir):
        """Test matching triggers."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        matched = reg.match_triggers("this is a test query")
        assert len(matched) == 1
        assert matched[0].name == "test-skill"

    def test_match_triggers_no_match(self, sample_skill_dir):
        """Test trigger matching with no matches."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        matched = reg.match_triggers("completely unrelated query")
        assert len(matched) == 0

    def test_get_skill_content(self, sample_skill_dir):
        """Test getting skill content."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        content = reg.get_skill_content("test-skill")
        assert content is not None
        assert "Test Skill" in content

    def test_get_skill_content_not_found(self, sample_skill_dir):
        """Test getting content of non-existent skill."""
        reg = SkillRegistry(skills_dir=sample_skill_dir)
        reg.load_all()
        assert reg.get_skill_content("nonexistent") is None


class TestDataclasses:
    """Tests for dataclass defaults and behavior."""

    def test_session_defaults(self):
        """Test Session dataclass defaults."""
        s = Session(id=uuid.uuid4())
        assert s.title is None
        assert s.agent_id == "harness"
        assert s.status == "active"
        assert s.state == {}
        assert s.goal is None
        assert s.model is None
        assert s.provider is None
        assert s.context_budget == 8000
        assert isinstance(s.created_at, datetime)
        assert isinstance(s.last_activity, datetime)

    def test_session_custom_values(self):
        """Test Session with custom values."""
        s = Session(
            id=uuid.uuid4(),
            title="Test",
            agent_id="custom",
            status="archived",
            state={"key": "value"},
            goal="Test goal",
            model="gpt-4",
            provider="openai",
            context_budget=4000,
        )
        assert s.title == "Test"
        assert s.agent_id == "custom"
        assert s.status == "archived"
        assert s.state == {"key": "value"}
        assert s.goal == "Test goal"
        assert s.model == "gpt-4"
        assert s.provider == "openai"
        assert s.context_budget == 4000

    def test_context_chunk_defaults(self):
        """Test ContextChunk dataclass defaults."""
        c = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="test",
            chunk_type="user_message",
            payload={"content": "hello"},
        )
        assert c.token_count == 0
        assert c.embedding is None
        assert c.accessed_at is None
        assert isinstance(c.created_at, datetime)

    def test_context_chunk_with_embedding(self):
        """Test ContextChunk with embedding."""
        c = ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="test",
            chunk_type="memory",
            payload={"content": "test"},
            token_count=10,
            embedding=[0.1, 0.2, 0.3],
        )
        assert c.token_count == 10
        assert c.embedding == [0.1, 0.2, 0.3]

    def test_llm_response_defaults(self):
        """Test LLMResponse dataclass defaults."""
        r = LLMResponse(content="test", model="gpt-4")
        assert r.usage == {}
        assert r.raw == {}
        assert r.tool_calls == []

    def test_tool_definition(self):
        """Test ToolDefinition dataclass."""
        td = ToolDefinition(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
        )
        assert td.name == "test_tool"
        assert td.description == "A test tool"

    def test_agent_response_defaults(self):
        """Test AgentResponse dataclass defaults."""
        r = AgentResponse(content="test")
        assert r.tool_calls == []
        assert r.tokens_used == 0
        assert r.iterations == 0

    def test_agent_response_with_tool_calls(self):
        """Test AgentResponse with tool calls."""
        r = AgentResponse(
            content="test",
            tool_calls=[{"tool": "read_file", "args": {"path": "/tmp/test"}}],
            tokens_used=100,
            iterations=3,
        )
        assert r.content == "test"
        assert len(r.tool_calls) == 1
        assert r.tokens_used == 100
        assert r.iterations == 3


# ============================================================================
# 2. Edge Case Tests
# ============================================================================

class TestEdgeCases:
    """Edge case tests for various inputs."""

    def test_prompt_assembler_empty_strings(self):
        """Test PromptAssembler with empty strings."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt="",
            goal="",
            recent_chunks=[],
            retrieved_chunks=[],
            query="",
        )
        assert isinstance(prompt, str)

    def test_prompt_assembler_unicode(self):
        """Test PromptAssembler with unicode characters."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt="You are 你好 🎉",
            goal="Goal with émojis 🚀",
            recent_chunks=[{"type": "user_message", "payload": {"content": "こんにちは"}}],
            retrieved_chunks=[],
            query="Query with ñ and ü",
        )
        assert "你好" in prompt
        assert "🚀" in prompt
        assert "こんにちは" in prompt

    def test_prompt_assembler_special_chars(self):
        """Test PromptAssembler with special characters."""
        assembler = PromptAssembler(session_budget=8000)
        prompt = assembler.assemble(
            system_prompt="System <prompt> & \"quotes\"",
            goal="Goal with\ttabs\nand\nnewlines",
            recent_chunks=[],
            retrieved_chunks=[],
            query="Query with {braces} and [brackets]",
        )
        assert "<prompt>" in prompt
        assert "quotes" in prompt

    def test_prompt_assembler_very_large_input(self):
        """Test PromptAssembler with very large input."""
        assembler = PromptAssembler(session_budget=100000)
        large_text = "x" * 10000
        prompt = assembler.assemble(
            system_prompt=large_text,
            goal=large_text,
            recent_chunks=[],
            retrieved_chunks=[],
            query=large_text,
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_compress_chunk_empty_payload(self):
        """Test compressing chunk with empty payload."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({"type": "unknown", "payload": {}})
        assert "unknown" in result

    def test_compress_chunk_missing_type(self):
        """Test compressing chunk with missing type."""
        assembler = PromptAssembler()
        result = assembler._compress_chunk({"payload": {"content": "test"}})
        assert "unknown" in result

    def test_estimate_tokens_unicode(self):
        """Test token estimation with unicode."""
        assembler = PromptAssembler()
        # Unicode characters may have different lengths
        tokens = assembler._estimate_tokens("你好世界")
        assert tokens >= 0

    def test_tool_registry_no_params(self):
        """Test tool with no parameters."""
        reg = ToolRegistry()

        @reg.register()
        def no_params() -> str:
            return "ok"

        tool = reg.get_tool("no_params")
        assert tool.parameters["properties"] == {}
        assert tool.parameters["required"] == []

    def test_skill_parser_empty_file(self, temp_dir):
        """Test parsing empty SKILL.md file."""
        skill_file = temp_dir / "SKILL.md"
        skill_file.write_text("", encoding="utf-8")
        skill = SkillParser.parse(skill_file)
        assert skill.content == ""
        assert skill.name == temp_dir.name

    def test_skill_parser_only_frontmatter(self, temp_dir):
        """Test parsing SKILL.md with only frontmatter."""
        skill_file = temp_dir / "SKILL.md"
        skill_file.write_text(
            "---\nname: test\ndescription: test\n---\n",
            encoding="utf-8",
        )
        skill = SkillParser.parse(skill_file)
        assert skill.name == "test"
        assert skill.content == ""


# ============================================================================
# 3. Error Handling Tests
# ============================================================================

class TestErrorHandling:
    """Tests for error handling."""

    def test_read_file_not_found(self, temp_dir):
        """Test read_file with non-existent file."""
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = read_file(str(temp_dir / "nonexistent.txt"))
        assert "Error" in result
        assert "not found" in result.lower() or "File not found" in result

    def test_read_file_directory(self, temp_dir):
        """Test read_file with directory path."""
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = read_file(str(temp_dir))
        assert "Error" in result

    def test_write_file_invalid_path(self):
        """Test write_file with invalid path."""
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("Access denied")):
            result = write_file("/some/path/test.txt", "content")
            assert "Error" in result

    def test_list_files_not_found(self, temp_dir):
        """Test list_files with non-existent directory."""
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = list_files(str(temp_dir / "nonexistent"))
        assert "Error" in result

    def test_list_files_file_path(self, temp_dir):
        """Test list_files with file path instead of directory."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("content")
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = list_files(str(test_file))
        assert "Error" in result

    def test_terminal_invalid_command(self):
        """Test terminal with invalid command."""
        from ah.tools.terminal import terminal as terminal_fn
        result = terminal_fn("this_command_does_not_exist_12345")
        assert "Error" in result or "not recognized" in result.lower() or "exit code" in result

    def test_terminal_timeout(self):
        """Test terminal with timeout."""
        from ah.tools.terminal import terminal as terminal_fn
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=1)
            result = terminal_fn("sleep 10", timeout=1)
            assert "timed out" in result.lower() or "Error" in result

    def test_tool_execute_nonexistent(self):
        """Test executing non-existent tool."""
        reg = ToolRegistry()
        with pytest.raises(ValueError):
            asyncio.run(reg.execute("nonexistent_tool"))

    def test_tool_execute_wrong_args(self):
        """Test executing tool with wrong arguments."""
        reg = ToolRegistry()

        @reg.register()
        def strict_tool(x: int) -> int:
            return x

        with pytest.raises(ValueError):
            asyncio.run(reg.execute("strict_tool", y="wrong"))

    def test_database_not_connected(self):
        """Test database operations when not connected."""
        db_obj = Database("postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = db_obj.pool

    def test_provider_unknown(self):
        """Test get_provider with unknown provider."""
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider("unknown_provider")

    def test_openrouter_no_api_key(self):
        """Test OpenRouterProvider without API key."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
                OpenRouterProvider()


# ============================================================================
# 4. Integration Tests — Mocked Database
# ============================================================================

class TestSessionManagerMocked:
    """Tests for SessionManager with mocked database."""

    @pytest.fixture
    def mock_session_row(self):
        """Create a mock session row."""
        return {
            "id": uuid.uuid4(),
            "title": "Test Session",
            "agent_id": "harness",
            "status": "active",
            "state_msgpack": b"\x80",  # msgpack for empty dict
            "goal": "Test goal",
            "model": "gpt-4",
            "provider": "openai",
            "context_budget": 8000,
            "created_at": datetime.utcnow(),
            "last_activity": datetime.utcnow(),
        }

    async def test_create_session(self, mock_session_row):
        """Test creating a session with mocked DB."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.fetchrow = AsyncMock(return_value=mock_session_row)
            session = await session_manager.create(title="Test Session")
            assert session.title == "Test Session"
            assert session.status == "active"

    async def test_get_session(self, mock_session_row):
        """Test getting a session with mocked DB."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.fetchrow = AsyncMock(return_value=mock_session_row)
            session = await session_manager.get(mock_session_row["id"])
            assert session is not None
            assert session.id == mock_session_row["id"]

    async def test_get_session_not_found(self):
        """Test getting a non-existent session."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.fetchrow = AsyncMock(return_value=None)
            session = await session_manager.get(uuid.uuid4())
            assert session is None

    async def test_list_sessions(self, mock_session_row):
        """Test listing sessions with mocked DB."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.fetch = AsyncMock(return_value=[mock_session_row])
            sessions = await session_manager.list_sessions()
            assert len(sessions) == 1
            assert sessions[0].title == "Test Session"

    async def test_update_state(self):
        """Test updating session state."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            await session_manager.update_state(uuid.uuid4(), {"key": "value"})
            mock_db.execute.assert_called_once()

    async def test_archive_session(self):
        """Test archiving a session."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            await session_manager.archive(uuid.uuid4())
            mock_db.execute.assert_called_once()

    async def test_set_goal(self):
        """Test setting session goal."""
        with patch("ah.core.session.db") as mock_db:
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            await session_manager.set_goal(uuid.uuid4(), "New goal")
            mock_db.execute.assert_called_once()


class TestContextManagerMocked:
    """Tests for ContextManager with mocked database."""

    @pytest.fixture
    def mock_chunk_row(self):
        """Create a mock context chunk row."""
        import msgpack
        return {
            "id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "agent_id": "harness",
            "chunk_type": "user_message",
            "payload_msgpack": msgpack.packb({"content": "hello"}, use_bin_type=True),
            "token_count": 5,
            "embedding": None,
            "created_at": datetime.utcnow(),
            "accessed_at": None,
        }

    async def test_add_chunk(self, mock_chunk_row):
        """Test adding a context chunk."""
        with patch("ah.core.context.db") as mock_db:
            mock_db.fetchrow = AsyncMock(return_value=mock_chunk_row)
            chunk = await context_manager.add_chunk(
                session_id=mock_chunk_row["session_id"],
                agent_id="harness",
                chunk_type="user_message",
                payload={"content": "hello"},
            )
            assert chunk.chunk_type == "user_message"
            assert chunk.payload == {"content": "hello"}

    async def test_get_chunks(self, mock_chunk_row):
        """Test getting context chunks."""
        with patch("ah.core.context.db") as mock_db:
            mock_db.fetch = AsyncMock(return_value=[mock_chunk_row])
            chunks = await context_manager.get_chunks(mock_chunk_row["session_id"])
            assert len(chunks) == 1
            assert chunks[0].chunk_type == "user_message"

    async def test_get_recent_context(self, mock_chunk_row):
        """Test getting recent context."""
        with patch("ah.core.context.db") as mock_db:
            mock_db.fetch = AsyncMock(return_value=[mock_chunk_row])
            context = await context_manager.get_recent_context(mock_chunk_row["session_id"])
            assert len(context) == 1
            assert context[0]["type"] == "user_message"

    async def test_delete_chunks(self):
        """Test deleting context chunks."""
        with patch("ah.core.context.db") as mock_db:
            mock_db.execute = AsyncMock(return_value="DELETE 5")
            count = await context_manager.delete_chunks(uuid.uuid4())
            assert count == 5

    async def test_get_token_usage(self):
        """Test getting token usage."""
        with patch("ah.core.context.db") as mock_db:
            mock_db.fetchval = AsyncMock(return_value=150)
            tokens = await context_manager.get_token_usage(uuid.uuid4())
            assert tokens == 150

    async def test_mark_accessed(self):
        """Test marking chunk as accessed."""
        with patch("ah.core.context.db") as mock_db:
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            await context_manager.mark_accessed(uuid.uuid4())
            mock_db.execute.assert_called_once()


# ============================================================================
# 5. CLI Tests
# ============================================================================

class TestCLI:
    """Tests for CLI commands using typer's CliRunner."""

    def test_version_command(self):
        """Test version command."""
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_doctor_command(self):
        """Test doctor command."""
        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "AgentHarness Doctor" in result.output

    def test_skills_command(self):
        """Test skills command."""
        runner = CliRunner()
        result = runner.invoke(app, ["skills"])
        # Should succeed even with no skills
        assert result.exit_code == 0

    def test_status_command_no_db(self):
        """Test status command fails gracefully when DB is not available."""
        runner = CliRunner()
        with patch("ah.cli.db") as mock_db:
            mock_db.connect = AsyncMock(side_effect=Exception("Connection refused"))
            mock_db.pool = None
            result = runner.invoke(app, ["status"])
            # CLI should handle DB errors gracefully (either exit non-zero or show error)
            assert result.exit_code != 0 or "error" in result.output.lower() or "unavailable" in result.output.lower()

    def test_chat_no_message(self):
        """Test chat command without message."""
        runner = CliRunner()
        with patch("ah.cli.db") as mock_db, \
             patch("ah.cli.session_manager") as mock_sm, \
             patch("ah.cli.context_manager") as mock_cm:
            mock_db.connect = AsyncMock()
            mock_db.close = AsyncMock()
            mock_sm.create = AsyncMock(return_value=Session(id=uuid.uuid4()))
            mock_sm.get_last_active = AsyncMock(return_value=None)
            mock_cm.get_recent_context = AsyncMock(return_value=[])
            result = runner.invoke(app, ["chat"])
            # Should exit with code 0 and show help message
            assert result.exit_code == 0 or "No message" in result.output

    def test_chat_help(self):
        """Test chat command help."""
        runner = CliRunner()
        result = runner.invoke(app, ["chat", "--help"])
        assert result.exit_code == 0

    def test_sessions_command_no_db(self):
        """Test sessions command without database."""
        runner = CliRunner()
        with patch("ah.cli.db") as mock_db, \
             patch("ah.core.session.db") as mock_session_db:
            mock_db.connect = AsyncMock()
            mock_db.close = AsyncMock()
            mock_session_db.fetch = AsyncMock(return_value=[])
            result = runner.invoke(app, ["sessions"])
            # Should handle DB failure gracefully
            assert result.exit_code == 0 or "connection failed" in result.output.lower()

    def test_context_command_no_args(self):
        """Test context command without arguments."""
        runner = CliRunner()
        with patch("ah.cli.db") as mock_db, \
             patch("ah.core.session.db") as mock_session_db, \
             patch("ah.core.context.db") as mock_context_db:
            mock_db.connect = AsyncMock()
            mock_db.close = AsyncMock()
            mock_session_db.fetchrow = AsyncMock(return_value=None)
            mock_context_db.fetch = AsyncMock(return_value=[])
            result = runner.invoke(app, ["context"])
            # Should handle no sessions gracefully
            assert result.exit_code == 0 or "No active sessions" in result.output

    def test_init_command_help(self):
        """Test init command help."""
        runner = CliRunner()
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0


# ============================================================================
# 6. Provider Tests — Mocked HTTP
# ============================================================================

class TestOpenRouterProvider:
    """Tests for OpenRouterProvider with mocked HTTP."""

    @pytest.fixture
    def provider(self):
        """Create an OpenRouterProvider with test API key."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            return OpenRouterProvider(api_key="test-key", model="test-model")

    async def test_complete_success(self, provider):
        """Test successful completion."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "Test response",
                    "tool_calls": [],
                }
            }],
            "model": "test-model",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            response = await provider.complete(messages=[{"role": "user", "content": "hello"}])
            assert response.content == "Test response"
            assert response.model == "test-model"
            assert response.usage["total_tokens"] == 15

    async def test_complete_with_tool_calls(self, provider):
        """Test completion with tool calls."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "test_tool",
                            "arguments": '{"x": 1}',
                        },
                    }],
                }
            }],
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            response = await provider.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[ToolDefinition(name="test_tool", description="Test", parameters={})],
            )
            assert len(response.tool_calls) == 1
            assert response.tool_calls[0]["function"]["name"] == "test_tool"

    async def test_complete_http_error(self, provider):
        """Test completion with HTTP error."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP 429")

        with patch.object(provider.client, "post", return_value=mock_response):
            with pytest.raises(Exception, match="HTTP 429"):
                await provider.complete(messages=[{"role": "user", "content": "hello"}])

    async def test_embed_success(self, provider):
        """Test successful embedding."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            embedding = await provider.embed("test text")
            assert embedding == [0.1, 0.2, 0.3]

    async def test_close(self, provider):
        """Test closing the provider."""
        with patch.object(provider.client, "aclose", new=AsyncMock()) as mock_close:
            await provider.close()
            mock_close.assert_called_once()


class TestOllamaProvider:
    """Tests for OllamaProvider with mocked HTTP."""

    @pytest.fixture
    def provider(self):
        """Create an OllamaProvider."""
        return OllamaProvider(model="llama3.1", base_url="http://localhost:11434")

    async def test_complete_success(self, provider):
        """Test successful completion."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {
                "content": "Test response",
                "tool_calls": [],
            },
            "model": "llama3.1",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            response = await provider.complete(messages=[{"role": "user", "content": "hello"}])
            assert response.content == "Test response"
            assert response.usage["total_tokens"] == 15

    async def test_complete_with_tool_calls(self, provider):
        """Test completion with tool calls."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "test_tool",
                        "arguments": {"x": 1},
                    },
                }],
            },
            "model": "llama3.1",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            response = await provider.complete(
                messages=[{"role": "user", "content": "hello"}],
                tools=[ToolDefinition(name="test_tool", description="Test", parameters={})],
            )
            assert len(response.tool_calls) == 1

    async def test_embed_success(self, provider):
        """Test successful embedding."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "embedding": [0.1, 0.2, 0.3],
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(provider.client, "post", return_value=mock_response):
            embedding = await provider.embed("test text")
            assert embedding == [0.1, 0.2, 0.3]

    async def test_close(self, provider):
        """Test closing the provider."""
        with patch.object(provider.client, "aclose", new=AsyncMock()) as mock_close:
            await provider.close()
            mock_close.assert_called_once()


class TestProviderFactory:
    """Tests for get_provider factory function."""

    def test_get_openrouter_provider(self):
        """Test getting OpenRouter provider."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            provider = get_provider("openrouter")
            assert isinstance(provider, OpenRouterProvider)

    def test_get_ollama_provider(self):
        """Test getting Ollama provider."""
        provider = get_provider("ollama")
        assert isinstance(provider, OllamaProvider)

    def test_get_unknown_provider(self):
        """Test getting unknown provider."""
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider("unknown")

    def test_get_provider_with_model(self):
        """Test getting provider with custom model."""
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            provider = get_provider("openrouter", model="custom-model")
            assert provider.model == "custom-model"


# ============================================================================
# 7. Agent Tests — Mocked Provider
# ============================================================================

class TestReActAgent:
    """Tests for ReActAgent with mocked provider."""

    @pytest.fixture
    def mock_provider(self):
        """Create a mock LLM provider."""
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=LLMResponse(
            content="Test response",
            model="test-model",
            usage={"total_tokens": 10},
        ))
        return provider

    @pytest.fixture
    def mock_session(self):
        """Create a mock session."""
        return Session(
            id=uuid.uuid4(),
            title="Test",
            context_budget=8000,
        )

    async def test_agent_run_no_tool_calls(self, mock_provider, mock_session):
        """Test agent run with no tool calls."""
        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=mock_provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test message", verbose=False)

                assert response.content == "Test response"
                assert response.iterations == 1
                assert response.tokens_used == 10

    async def test_agent_run_with_tool_calls(self, mock_provider, mock_session):
        """Test agent run with tool calls."""
        # First call returns tool call, second returns final answer
        tool_call_response = LLMResponse(
            content="",
            model="test-model",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test"}',
                },
            }],
        )
        final_response = LLMResponse(
            content="Final answer",
            model="test-model",
            usage={"total_tokens": 5},
        )

        mock_provider.complete = AsyncMock(side_effect=[tool_call_response, final_response])

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=mock_provider, max_iterations=5)
                response = await agent.run(mock_session.id, "test message", verbose=False)

                assert response.content == "Final answer"
                assert response.iterations == 2
                assert len(response.tool_calls) == 1

    async def test_agent_session_not_found(self, mock_provider):
        """Test agent run with non-existent session."""
        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=None)

            agent = ReActAgent(provider=mock_provider)
            with pytest.raises(ValueError, match="not found"):
                await agent.run(uuid.uuid4(), "test message")

    async def test_agent_max_iterations(self, mock_provider, mock_session):
        """Test agent reaching max iterations."""
        # Always return tool calls
        tool_call_response = LLMResponse(
            content="",
            model="test-model",
            usage={"total_tokens": 5},
            tool_calls=[{
                "id": "call_1",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test"}',
                },
            }],
        )
        mock_provider.complete = AsyncMock(return_value=tool_call_response)

        with patch("ah.core.agent.session_manager") as mock_sm:
            mock_sm.get = AsyncMock(return_value=mock_session)
            mock_sm.update_activity = AsyncMock()

            with patch("ah.core.agent.context_manager") as mock_cm:
                mock_cm.add_chunk = AsyncMock()
                mock_cm.get_recent_context = AsyncMock(return_value=[])

                agent = ReActAgent(provider=mock_provider, max_iterations=3)
                response = await agent.run(mock_session.id, "test message", verbose=False)

                assert "max iterations" in response.content.lower()
                assert response.iterations == 3


# ============================================================================
# 8. Built-in Tools Tests
# ============================================================================

class TestBuiltinTools:
    """Tests for built-in tools."""

    def test_read_file_success(self, temp_dir):
        """Test reading an existing file."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("line1\nline2\nline3", encoding="utf-8")
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = read_file(str(test_file))
        assert "line1" in result
        assert "line2" in result

    def test_read_file_with_offset(self, temp_dir):
        """Test reading file with offset."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("line1\nline2\nline3", encoding="utf-8")
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = read_file(str(test_file), offset=2)
        assert "line1" not in result
        assert "line2" in result

    def test_read_file_with_limit(self, temp_dir):
        """Test reading file with limit."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("line1\nline2\nline3", encoding="utf-8")
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = read_file(str(test_file), limit=2)
        assert "line3" not in result

    def test_write_file_success(self, temp_dir):
        """Test writing a file."""
        test_file = temp_dir / "output.txt"
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = write_file(str(test_file), "test content")
        assert "Written" in result or "Successfully" in result
        assert test_file.read_text() == "test content"

    def test_write_file_creates_dirs(self, temp_dir):
        """Test writing a file creates parent directories."""
        test_file = temp_dir / "subdir" / "output.txt"
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = write_file(str(test_file), "test content")
        assert test_file.exists()

    def test_list_files_success(self, temp_dir):
        """Test listing files."""
        (temp_dir / "file1.txt").write_text("content1")
        (temp_dir / "file2.txt").write_text("content2")
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = list_files(str(temp_dir))
        assert "file1.txt" in result
        assert "file2.txt" in result

    def test_list_files_with_pattern(self, temp_dir):
        """Test listing files with pattern."""
        (temp_dir / "file1.txt").write_text("content1")
        (temp_dir / "file2.py").write_text("content2")
        with patch("ah.tools.file._BASE_DIR", temp_dir):
            result = list_files(str(temp_dir), pattern="*.py")
        assert "file2.py" in result
        assert "file1.txt" not in result

    def test_search_files_success(self, temp_dir):
        """Test searching files."""
        (temp_dir / "file1.txt").write_text("hello world")
        (temp_dir / "file2.txt").write_text("goodbye world")
        result = builtins.search_files("hello", str(temp_dir))
        assert "hello" in result
        assert "file1.txt" in result

    def test_search_files_no_match(self, temp_dir):
        """Test searching files with no match."""
        (temp_dir / "file1.txt").write_text("hello world")
        result = builtins.search_files("nonexistent", str(temp_dir))
        assert "No matches" in result


# ============================================================================
# 9. Database Schema Tests
# ============================================================================

class TestDatabaseSchema:
    """Tests for database schema and connection."""

    def test_database_default_dsn(self):
        """Test default DSN."""
        with patch.dict(os.environ, {}, clear=True):
            db_obj = Database()
            assert "postgresql" in db_obj.dsn

    def test_database_custom_dsn(self):
        """Test custom DSN."""
        db_obj = Database("postgresql://user:pass@host:5432/db")
        assert db_obj.dsn == "postgresql://user:pass@host:5432/db"

    def test_database_pool_not_connected(self):
        """Test pool property when not connected."""
        db_obj = Database("postgresql://localhost/test")
        with pytest.raises(RuntimeError):
            _ = db_obj.pool

    async def test_database_connect(self):
        """Test database connection."""
        db_obj = Database("postgresql://localhost/test")
        with patch("asyncpg.create_pool", new=AsyncMock()) as mock_pool:
            mock_pool.return_value = AsyncMock()
            await db_obj.connect()
            assert db_obj._pool is not None
            await db_obj.close()

    async def test_database_close(self):
        """Test database close."""
        db_obj = Database("postgresql://localhost/test")
        db_obj._pool = AsyncMock()
        await db_obj.close()
        assert db_obj._pool is None


# ============================================================================
# 10. System Prompt Tests
# ============================================================================

class TestSystemPrompt:
    """Tests for system prompt."""

    def test_system_prompt_exists(self):
        """Test that system prompt is defined."""
        assert SYSTEM_PROMPT is not None
        assert len(SYSTEM_PROMPT) > 0

    def test_system_prompt_content(self):
        """Test system prompt content."""
        assert "AgentHarness" in SYSTEM_PROMPT
        assert "tools" in SYSTEM_PROMPT.lower()


# ============================================================================
# 11. LLMProvider Base Class Tests
# ============================================================================

class TestLLMProviderBase:
    """Tests for LLMProvider base class."""

    async def test_llm_provider_base_complete(self):
        """Test LLMProvider base class complete method."""
        provider = LLMProvider()
        with pytest.raises(NotImplementedError):
            await provider.complete(messages=[])

    async def test_llm_provider_base_embed(self):
        """Test LLMProvider base class embed method."""
        provider = LLMProvider()
        with pytest.raises(NotImplementedError):
            await provider.embed("test")


# ============================================================================
# Run tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
