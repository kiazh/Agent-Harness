"""Tests for low-severity code review fixes.

Covers:
1. TTLCache docstring fix (60s not 5s)
2. Redundant eviction logic simplification
3. Exception type consistency (ValueError for input validation)
4. Circular import fix in retriever
5. Dead code removal (fetchval_cached, execute_prepared, fetch_prepared)
6. Naming consistency (usage_count, not use_count)
"""
import asyncio
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.context import ContextManager
from ah.core.exceptions import ValidationError
from ah.core.provider import _validate_messages, _validate_params
from ah.core.session import SessionManager
from ah.db.connection import Database
from ah.memory.retriever import MemoryRetriever
from ah.skills.registry import Skill, SkillRegistry, SkillCurator, SkillHub


# ============================================================================
# 1. TTLCache Docstring Fix
# ============================================================================

class TestTTLCacheDocstring:
    """Verify TTLCache docstring matches actual TTL value."""

    def test_session_manager_docstring_says_60_seconds(self):
        """SessionManager docstring should mention 60-second cache."""
        docstring = SessionManager.__doc__
        assert "60-second" in docstring or "60 second" in docstring

    def test_session_manager_cache_ttl_is_60(self):
        """Verify the actual TTLCache TTL is 60 seconds."""
        manager = SessionManager()
        assert manager._cache.ttl == 60

    def test_session_manager_cache_maxsize(self):
        """Verify the TTLCache maxsize is 128."""
        manager = SessionManager()
        assert manager._cache.maxsize == 128

    def test_session_get_docstring_mentions_60_seconds(self):
        """The get() method docstring should mention 60 seconds."""
        docstring = SessionManager.get.__doc__
        assert "60" in docstring


# ============================================================================
# 2. Redundant Eviction Logic
# ============================================================================

class TestEvictionLogic:
    """Verify eviction logic works correctly after simplification."""

    @pytest.fixture
    def context_manager(self):
        return ContextManager()

    @pytest.mark.asyncio
    async def test_evict_old_chunks_no_limits(self, context_manager):
        """No eviction when no limits are set."""
        result = await context_manager.evict_old_chunks(
            uuid.uuid4(), max_tokens=None, max_chunks=None
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_evict_old_chunks_within_limits(self, context_manager):
        """No eviction when within limits."""
        session_id = uuid.uuid4()
        # Mock db calls
        with patch.object(context_manager, 'get_token_usage', new_callable=AsyncMock, return_value=100):
            with patch('ah.core.context.db') as mock_db:
                mock_db.fetchval = AsyncMock(return_value=5)
                mock_db.fetch = AsyncMock(return_value=[])
                result = await context_manager.evict_old_chunks(
                    session_id, max_tokens=1000, max_chunks=50
                )
                assert result == 0

    @pytest.mark.asyncio
    async def test_evict_old_chunks_over_token_limit(self, context_manager):
        """Eviction occurs when over token limit."""
        session_id = uuid.uuid4()
        # Create mock rows for eviction
        mock_rows = [
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-01"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-02"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-03"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-04"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-05"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-06"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-07"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-08"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-09"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-10"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-11"},
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": "2024-01-12"},
        ]
        with patch.object(context_manager, 'get_token_usage', new_callable=AsyncMock, return_value=1200):
            with patch('ah.core.context.db') as mock_db:
                mock_db.fetchval = AsyncMock(return_value=12)
                mock_db.fetch = AsyncMock(return_value=mock_rows)
                mock_db.execute = AsyncMock(return_value="DELETE 1")
                result = await context_manager.evict_old_chunks(
                    session_id, max_tokens=500, max_chunks=None
                )
                assert result > 0

    @pytest.mark.asyncio
    async def test_evict_old_chunks_over_chunk_limit(self, context_manager):
        """Eviction occurs when over chunk limit."""
        session_id = uuid.uuid4()
        mock_rows = [
            {"id": uuid.uuid4(), "token_count": 10, "chunk_type": "text", "created_at": f"2024-01-{i:02d}"}
            for i in range(1, 16)
        ]
        with patch.object(context_manager, 'get_token_usage', new_callable=AsyncMock, return_value=150):
            with patch('ah.core.context.db') as mock_db:
                mock_db.fetchval = AsyncMock(return_value=15)
                mock_db.fetch = AsyncMock(return_value=mock_rows)
                mock_db.execute = AsyncMock(return_value="DELETE 1")
                result = await context_manager.evict_old_chunks(
                    session_id, max_tokens=None, max_chunks=5
                )
                assert result > 0

    @pytest.mark.asyncio
    async def test_evict_old_chunks_preserves_recent(self, context_manager):
        """Eviction preserves the most recent 10 chunks."""
        session_id = uuid.uuid4()
        # 15 chunks, should preserve last 10, evict from first 5
        mock_rows = [
            {"id": uuid.uuid4(), "token_count": 100, "chunk_type": "text", "created_at": f"2024-01-{i:02d}"}
            for i in range(1, 16)
        ]
        with patch.object(context_manager, 'get_token_usage', new_callable=AsyncMock, return_value=1500):
            with patch('ah.core.context.db') as mock_db:
                mock_db.fetchval = AsyncMock(return_value=15)
                mock_db.fetch = AsyncMock(return_value=mock_rows)
                mock_db.execute = AsyncMock(return_value="DELETE 1")
                result = await context_manager.evict_old_chunks(
                    session_id, max_tokens=500, max_chunks=None
                )
                # Should evict 5 chunks (15 - 10 preserved = 5 evictable, need 1000 tokens freed)
                assert result == 5


# ============================================================================
# 3. Exception Type Consistency
# ============================================================================

class TestExceptionTypes:
    """Verify ValueError is used for input validation, not ValidationError."""

    def test_validate_messages_missing_content_raises_valueerror(self):
        """Missing 'content' field should raise ValueError."""
        messages = [{"role": "user"}]
        with pytest.raises(ValueError, match="missing 'content'"):
            _validate_messages(messages)

    def test_validate_messages_missing_role_raises_validationerror(self):
        """Missing 'role' field should raise ValidationError."""
        messages = [{"content": "hello"}]
        with pytest.raises(ValidationError, match="missing 'role'"):
            _validate_messages(messages)

    def test_validate_messages_empty_list_raises_validationerror(self):
        """Empty messages list should raise ValidationError."""
        with pytest.raises(ValidationError, match="non-empty list"):
            _validate_messages([])

    def test_validate_messages_not_a_list_raises_validationerror(self):
        """Non-list messages should raise ValidationError."""
        with pytest.raises(ValidationError, match="non-empty list"):
            _validate_messages("not a list")

    def test_validate_messages_message_not_dict_raises_validationerror(self):
        """Non-dict message should raise ValidationError."""
        with pytest.raises(ValidationError, match="must be a dict"):
            _validate_messages(["not a dict"])

    def test_validate_params_temperature_too_low(self):
        """Temperature below 0 should raise ValueError."""
        with pytest.raises(ValueError, match="temperature must be between"):
            _validate_params(-0.1, 100)

    def test_validate_params_temperature_too_high(self):
        """Temperature above 2.0 should raise ValueError."""
        with pytest.raises(ValueError, match="temperature must be between"):
            _validate_params(2.1, 100)

    def test_validate_params_max_tokens_too_low(self):
        """max_tokens below 1 should raise ValueError."""
        with pytest.raises(ValueError, match="max_tokens must be between"):
            _validate_params(0.5, 0)

    def test_validate_params_max_tokens_too_high(self):
        """max_tokens above 32768 should raise ValueError."""
        with pytest.raises(ValueError, match="max_tokens must be between"):
            _validate_params(0.5, 32769)

    def test_validate_params_valid_values(self):
        """Valid parameters should not raise."""
        _validate_params(0.7, 4096)  # Should not raise

    def test_validationerror_is_valueerror_subclass(self):
        """ValidationError should be a ValueError subclass."""
        assert issubclass(ValidationError, ValueError)


# ============================================================================
# 4. Circular Import Fix
# ============================================================================

class TestCircularImport:
    """Verify retriever module imports work correctly."""

    def test_retriever_imports_db_at_module_level(self):
        """MemoryRetriever should import db at module level, not in method."""
        import ah.memory.retriever as retriever_module
        source = inspect.getsource(retriever_module)
        # The import should be at the top of the file, not inside a method
        assert "from ah.db.connection import db" in source

    def test_retriever_class_exists(self):
        """MemoryRetriever class should be importable."""
        assert MemoryRetriever is not None

    def test_retriever_can_be_instantiated(self):
        """MemoryRetriever should be instantiable."""
        retriever = MemoryRetriever()
        assert retriever is not None
        assert retriever.top_k == 5
        assert retriever.rerank is True

    def test_retriever_has_keyword_search_method(self):
        """MemoryRetriever should have _keyword_search method."""
        retriever = MemoryRetriever()
        assert hasattr(retriever, '_keyword_search')
        assert callable(retriever._keyword_search)


# ============================================================================
# 5. Dead Code Removal
# ============================================================================

class TestDeadCodeRemoval:
    """Verify dead code methods have been removed from Database."""

    def test_fetchval_cached_removed(self):
        """fetchval_cached should not exist on Database."""
        assert not hasattr(Database, 'fetchval_cached')

    def test_execute_prepared_removed(self):
        """execute_prepared should not exist on Database."""
        assert not hasattr(Database, 'execute_prepared')

    def test_fetch_prepared_removed(self):
        """fetch_prepared should not exist on Database."""
        assert not hasattr(Database, 'fetch_prepared')

    def test_database_still_has_core_methods(self):
        """Core database methods should still exist."""
        assert hasattr(Database, 'execute')
        assert hasattr(Database, 'fetch')
        assert hasattr(Database, 'fetchrow')
        assert hasattr(Database, 'fetchval')
        assert hasattr(Database, 'connect')
        assert hasattr(Database, 'close')

    def test_database_method_count(self):
        """Database should have exactly the expected public methods."""
        # Get all public methods (not starting with _)
        public_methods = [
            name for name in dir(Database)
            if not name.startswith('_') and callable(getattr(Database, name))
        ]
        expected = {'connect', 'close', 'execute', 'executemany', 'fetch', 'fetchrow', 'fetchval', 'initialize_schema', 'reset', 'acquire'}
        assert set(public_methods) == expected


# ============================================================================
# 6. Naming Consistency (usage_count)
# ============================================================================

class TestNamingConsistency:
    """Verify usage_count is used consistently, not use_count."""

    def test_skill_has_usage_count_field(self):
        """Skill dataclass should have usage_count field."""
        skill = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        assert hasattr(skill, 'usage_count')
        assert skill.usage_count == 0

    def test_skill_does_not_have_use_count_field(self):
        """Skill dataclass should NOT have use_count field."""
        skill = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        assert not hasattr(skill, 'use_count')

    def test_record_use_increments_usage_count(self):
        """record_use should increment usage_count."""
        registry = SkillRegistry()
        registry._skills["test"] = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        registry.record_use("test")
        skill = registry.get("test")
        assert skill.usage_count == 1

    def test_record_use_does_not_set_use_count(self):
        """record_use should not set a use_count attribute."""
        registry = SkillRegistry()
        registry._skills["test"] = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        registry.record_use("test")
        skill = registry.get("test")
        assert not hasattr(skill, 'use_count')

    def test_get_telemetry_returns_usage_count(self):
        """get_telemetry should return usage_count, not use_count."""
        registry = SkillRegistry()
        registry._skills["test"] = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        registry.record_use("test")
        telemetry = registry.get_telemetry("test")
        assert "usage_count" in telemetry
        assert "use_count" not in telemetry
        assert telemetry["usage_count"] == 1

    def test_get_unused_skills_uses_usage_count(self):
        """get_unused_skills should check usage_count."""
        registry = SkillRegistry()
        registry._skills["used"] = Skill(
            name="used",
            description="used skill",
            triggers=[],
            content="test content",
            file_path="/tmp/used.md",
        )
        registry._skills["unused"] = Skill(
            name="unused",
            description="unused skill",
            triggers=[],
            content="test content",
            file_path="/tmp/unused.md",
        )
        registry.record_use("used")
        curator = SkillCurator(registry)
        unused = curator.get_unused_skills()
        assert len(unused) == 1
        assert unused[0].name == "unused"

    def test_get_top_skills_uses_usage_count(self):
        """get_top_skills should sort by usage_count."""
        registry = SkillRegistry()
        registry._skills["skill1"] = Skill(
            name="skill1",
            description="skill 1",
            triggers=[],
            content="test content",
            file_path="/tmp/skill1.md",
        )
        registry._skills["skill2"] = Skill(
            name="skill2",
            description="skill 2",
            triggers=[],
            content="test content",
            file_path="/tmp/skill2.md",
        )
        registry.record_use("skill1")
        registry.record_use("skill1")
        registry.record_use("skill2")
        curator = SkillCurator(registry)
        top = curator.get_top_skills(limit=2)
        assert top[0].name == "skill1"
        assert top[0].usage_count == 2
        assert top[1].name == "skill2"
        assert top[1].usage_count == 1

    def test_health_report_uses_usage_count(self):
        """Health report should use usage_count."""
        registry = SkillRegistry()
        registry._skills["test"] = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        registry.record_use("test")
        curator = SkillCurator(registry)
        report = curator.get_health_report()
        assert report["total_uses"] == 1
        assert report["never_used"] == 0
        assert report["top_skills"][0]["usage_count"] == 1
        assert "use_count" not in report["top_skills"][0]

    def test_skill_hub_publish_uses_usage_count(self):
        """SkillHub.publish should serialize usage_count."""
        import json
        import tempfile
        from pathlib import Path
        from ah.skills.registry import SkillHub

        registry = SkillRegistry()
        registry._skills["test"] = Skill(
            name="test",
            description="test skill",
            triggers=[],
            content="test content",
            file_path="/tmp/test.md",
        )
        registry.record_use("test")

        with tempfile.TemporaryDirectory() as tmpdir:
            hub = SkillHub(registry, hub_dir=tmpdir)
            entry = hub.publish("test")
            assert "usage_count" in entry
            assert "use_count" not in entry
            assert entry["usage_count"] == 1
