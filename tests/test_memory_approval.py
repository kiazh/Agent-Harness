"""Tests for memory approval gate, secret redaction, and user profiles."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.memory.approval import (
    ApprovalStatus,
    MemoryApprovalGate,
    PendingMemory,
)
from machina.memory.redaction import (
    RedactionResult,
    SecretRedactor,
    redact_secrets,
    redact_value,
)
from machina.memory.user_profile import (
    UserProfile,
    UserProfileStore,
    _clean_preferences,
)


def _transactional_connection(mock_db):
    conn = AsyncMock()
    conn.fetchrow = mock_db.fetchrow
    conn.execute = mock_db.execute
    conn.transaction = MagicMock(return_value=AsyncMock())
    cm = AsyncMock()
    cm.__aenter__.return_value = conn
    mock_db.acquire = MagicMock(return_value=cm)


# ===========================================================================
# SecretRedactor Tests
# ===========================================================================


class TestSecretRedactor:
    """Tests for the SecretRedactor."""

    @pytest.fixture
    def redactor(self):
        return SecretRedactor()

    def test_redact_openai_key(self, redactor):
        text = "My API key is sk-abc123def456ghi789jkl012mno345"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "sk-abc123def456ghi789jkl012mno345" not in result.text
        assert "[REDACTED_OPENAI_KEY]" in result.text

    def test_redact_anthropic_key(self, redactor):
        text = "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "sk-ant-api03" not in result.text

    def test_redact_bearer_token(self, redactor):
        text = "Authorization: Bearer abcdef1234567890"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "abcdef1234567890" not in result.text
        assert "[REDACTED_TOKEN]" in result.text

    def test_redact_password(self, redactor):
        text = "password = supersecret123"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "supersecret123" not in result.text
        assert "[REDACTED_PASSWORD]" in result.text

    def test_redact_db_connection_string(self, redactor):
        text = "Connect to postgres://user:secretpass@localhost:5432/mydb"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "secretpass" not in result.text
        assert "[REDACTED_DB_PASSWORD]" in result.text

    def test_redact_aws_access_key(self, redactor):
        text = "AWS key: AKIAIOSFODNN7EXAMPLE"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in result.text

    def test_redact_github_token(self, redactor):
        text = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in result.text

    def test_redact_private_key_pem(self, redactor):
        text = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy...
-----END RSA PRIVATE KEY-----"""
        result = redactor.redact(text)
        assert result.was_redacted
        assert "RSA PRIVATE KEY" not in result.text
        assert "[REDACTED_PRIVATE_KEY]" in result.text

    def test_redact_jwt_token(self, redactor):
        text = "Token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "eyJhbGciOiJIUzI1NiJ9" not in result.text

    def test_redact_ssn(self, redactor):
        text = "SSN: 123-45-6789"
        result = redactor.redact(text)
        assert result.was_redacted
        assert "123-45-6789" not in result.text
        assert "[REDACTED_SSN]" in result.text

    def test_no_secrets_unchanged(self, redactor):
        text = "The user prefers dark mode and Python programming"
        result = redactor.redact(text)
        assert not result.was_redacted
        assert result.text == text
        assert result.redactions == []

    def test_multiple_secrets(self, redactor):
        text = "API key: sk-abc123def456ghi789jkl012mno345, password = mypassword123"
        result = redactor.redact(text)
        assert result.was_redacted
        assert len(result.redactions) >= 2

    def test_has_secrets_true(self, redactor):
        assert redactor.has_secrets("sk-abc123def456ghi789jkl012mno345")

    def test_has_secrets_false(self, redactor):
        assert not redactor.has_secrets("just a normal sentence")

    def test_redact_dict(self, redactor):
        data = {
            "api_key": "sk-abc123def456ghi789jkl012mno345",
            "name": "test",
        }
        result = redactor.redact_dict(data)
        assert result.was_redacted
        assert "sk-abc123" not in result.text

    def test_profile_preferences_redact_nested_keys(self):
        secret = "sk-abc123def456ghi789jkl012mno345"
        assert secret not in str(_clean_preferences({secret: {secret: "safe"}}))

    def test_redact_value_preserves_structural_keys(self, monkeypatch):
        monkeypatch.setattr("machina.security.secrets.known_secret_values", lambda: ["learning"])
        assert redact_value({"learning": "safe"}) == {"learning": "safe"}

    def test_convenience_function(self):
        result = redact_secrets("password = secret123")
        assert result.was_redacted

    def test_redaction_result_dataclass(self):
        result = RedactionResult(text="test", redactions=["api_key"])
        assert result.was_redacted
        assert result.text == "test"

    def test_redaction_result_no_redactions(self):
        result = RedactionResult(text="test", redactions=[])
        assert not result.was_redacted


# ===========================================================================
# MemoryApprovalGate Tests
# ===========================================================================


class TestMemoryApprovalGate:
    """Tests for the MemoryApprovalGate."""

    @pytest.fixture
    def gate(self):
        return MemoryApprovalGate(enabled=True)

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="INSERT 0 1")
        return mock

    def test_approval_status_enum(self):
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"

    def test_pending_memory_dataclass(self):
        pm = PendingMemory(
            id=uuid.uuid4(),
            memory_id=None,
            content="Test",
            category="fact",
        )
        assert pm.status == ApprovalStatus.PENDING
        assert pm.redactions == []

    def test_pending_memory_to_dict(self):
        pm = PendingMemory(
            id=uuid.uuid4(),
            memory_id=None,
            content="Test",
            category="fact",
            redactions=["api_key"],
        )
        d = pm.to_dict()
        assert d["content"] == "Test"
        assert d["status"] == "pending"
        assert "api_key" in d["redactions"]

    async def test_submit_creates_pending(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            pending = await gate.submit(
                content="Test memory",
                category="fact",
                importance=0.5,
            )
            assert pending.id is not None
            assert pending.status == ApprovalStatus.PENDING
            assert pending.content == "Test memory"

    async def test_submit_redacts_secrets(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            pending = await gate.submit(
                content="API key is sk-abc123def456ghi789jkl012mno345",
                category="fact",
            )
            assert pending.redactions != []
            assert "sk-abc123" not in pending.content

    async def test_approve_creates_memory(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            # Mock the pending record fetch
            mock_db.fetchrow = AsyncMock(
                return_value={
                    "id": uuid.uuid4(),
                    "content": "Test memory",
                    "category": "fact",
                    "importance": 0.5,
                    "agent_id": "harness",
                    "session_id": None,
                    "redactions": [],
                    "status": "pending",
                    "created_at": datetime.now(UTC),
                    "explicitly_important": False,
                    "base_strength": 1.0,
                }
            )
            # Mock memory_store.add
            mock_memory = AsyncMock()
            mock_memory.add = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
            with patch("machina.memory.approval.memory_store", mock_memory):
                mock_db.execute = AsyncMock(return_value="UPDATE 1")
                _transactional_connection(mock_db)
                # The mock may yield None; this only checks approve() doesn't raise.
                await gate.approve(uuid.uuid4())

    async def test_reject_pending(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={"status": "pending"})
            mock_db.execute = AsyncMock(return_value="UPDATE 1")
            _transactional_connection(mock_db)
            result = await gate.reject(uuid.uuid4())
            assert result is True

    async def test_reject_already_reviewed(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value={"status": "approved"})
            _transactional_connection(mock_db)
            result = await gate.reject(uuid.uuid4())
            assert result is False

    async def test_list_pending(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[])
            result = await gate.list_pending()
            assert result == []

    async def test_get_stats(self, gate, mock_db):
        with patch("machina.memory.approval.db", mock_db):
            mock_db.fetch = AsyncMock(
                return_value=[
                    {"status": "pending", "count": 5},
                    {"status": "approved", "count": 10},
                    {"status": "rejected", "count": 2},
                ]
            )
            stats = await gate.get_stats()
            assert stats["pending"] == 5
            assert stats["approved"] == 10
            assert stats["rejected"] == 2

    async def test_disabled_gate_auto_approves(self, mock_db):
        gate = MemoryApprovalGate(enabled=False)
        with patch("machina.memory.approval.db", mock_db):
            mock_db.execute = AsyncMock(return_value="INSERT 0 1")
            mock_db.fetchrow = AsyncMock(
                return_value={
                    "id": uuid.uuid4(),
                    "content": "Test",
                    "category": "fact",
                    "importance": 0.5,
                    "agent_id": "harness",
                    "session_id": None,
                    "redactions": [],
                    "status": "pending",
                    "created_at": datetime.now(UTC),
                    "explicitly_important": False,
                    "base_strength": 1.0,
                }
            )
            mock_memory = AsyncMock()
            mock_memory.add = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
            with patch("machina.memory.approval.memory_store", mock_memory):
                _transactional_connection(mock_db)
                pending = await gate.submit("Test", "fact")
                # When disabled, auto-approve is called
                assert pending is not None


# ===========================================================================
# UserProfile Tests
# ===========================================================================


class TestUserProfile:
    """Tests for the UserProfile dataclass."""

    def test_create_basic(self):
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id="test_user",
        )
        assert profile.user_id == "test_user"
        assert profile.interaction_count == 0
        assert profile.preferences == {}
        assert profile.topics == {}

    def test_record_interaction(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        profile.record_interaction(topic="python")
        assert profile.interaction_count == 1
        assert profile.topics["python"] == 1
        assert "python" in profile.last_topics

    def test_record_multiple_interactions(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        for _ in range(5):
            profile.record_interaction(topic="python")
        assert profile.interaction_count == 5
        assert profile.topics["python"] == 5

    def test_last_topics_ordering(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        profile.record_interaction(topic="python")
        profile.record_interaction(topic="rust")
        profile.record_interaction(topic="python")
        assert profile.last_topics[0] == "python"
        assert profile.last_topics[1] == "rust"

    def test_last_topics_max_10(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        for i in range(15):
            profile.record_interaction(topic=f"topic_{i}")
        assert len(profile.last_topics) == 10

    def test_set_preference(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        profile.set_preference("theme", "dark")
        assert profile.get_preference("theme") == "dark"

    def test_set_preference_redacts_secret_key(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        secret = "sk-abc123def456ghi789jkl012mno345"
        profile.set_preference(secret, "dark")
        assert secret not in str(profile.to_dict())

    def test_get_preference_uses_the_same_redacted_key_as_set(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        secret = "sk-abc123def456ghi789jkl012mno345"
        profile.set_preference(secret, "dark")
        assert profile.get_preference(secret) == "dark"

    def test_get_preference_default(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        assert profile.get_preference("nonexistent") is None
        assert profile.get_preference("nonexistent", "default") == "default"

    def test_get_top_topics(self):
        profile = UserProfile(id=uuid.uuid4(), user_id="test")
        profile.record_interaction(topic="python")
        profile.record_interaction(topic="python")
        profile.record_interaction(topic="rust")
        top = profile.get_top_topics(n=2)
        assert top[0] == ("python", 2)
        assert top[1] == ("rust", 1)

    def test_to_dict(self):
        profile = UserProfile(
            id=uuid.uuid4(),
            user_id="test",
            display_name="Test User",
            preferences={"theme": "dark"},
        )
        d = profile.to_dict()
        assert d["user_id"] == "test"
        assert d["display_name"] == "Test User"
        assert d["preferences"]["theme"] == "dark"

    def test_from_dict(self):
        data = {
            "id": str(uuid.uuid4()),
            "user_id": "test",
            "display_name": "Test",
            "preferences": {"theme": "dark"},
            "interaction_count": 5,
            "topics": {"python": 3},
            "last_topics": ["python"],
            "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "updated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        profile = UserProfile.from_dict(data)
        assert profile.user_id == "test"
        assert profile.interaction_count == 5
        assert profile.preferences["theme"] == "dark"


class TestUserProfileStore:
    """Tests for the UserProfileStore."""

    @pytest.fixture
    def store(self):
        return UserProfileStore()

    @pytest.fixture
    def mock_db(self):
        mock = AsyncMock()
        mock.fetch = AsyncMock(return_value=[])
        mock.fetchrow = AsyncMock(return_value=None)
        mock.fetchval = AsyncMock(return_value=0)
        mock.execute = AsyncMock(return_value="INSERT 0 1")
        return mock

    def _make_row(self, **overrides):
        row = {
            "id": uuid.uuid4(),
            "user_id": "test_user",
            "display_name": "Test User",
            "preferences": '{"theme": "dark"}',
            "interaction_count": 5,
            "topics": '{"python": 3}',
            "last_topics": '["python"]',
            "created_at": datetime.now(UTC).replace(tzinfo=None),
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
        }
        row.update(overrides)
        return row

    async def test_create_profile(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=self._make_row())
            profile = await store.create(user_id="test_user", display_name="Test User")
            assert profile.user_id == "test_user"
            assert profile.display_name == "Test User"

    async def test_get_by_user_id(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=self._make_row())
            profile = await store.get_by_user_id("test_user")
            assert profile is not None
            assert profile.user_id == "test_user"

    async def test_get_by_user_id_not_found(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=None)
            profile = await store.get_by_user_id("nonexistent")
            assert profile is None

    async def test_get_or_create_existing(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=self._make_row())
            profile = await store.get_or_create("test_user")
            assert profile.user_id == "test_user"

    async def test_get_or_create_new(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=self._make_row())
            profile = await store.get_or_create("new_user")
            assert profile is not None

    async def test_update_preferences(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=self._make_row())
            profile = await store.update_preferences(uuid.uuid4(), {"theme": "light"})
            assert profile is not None

    async def test_record_interaction(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetchrow = AsyncMock(return_value=self._make_row())
            profile = await store.record_interaction(uuid.uuid4(), topic="python")
            assert profile is not None

    async def test_list_all(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.fetch = AsyncMock(return_value=[self._make_row()])
            profiles = await store.list_all()
            assert len(profiles) == 1

    async def test_delete(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.execute = AsyncMock(return_value="DELETE 1")
            result = await store.delete(uuid.uuid4())
            assert result is True

    async def test_delete_not_found(self, store, mock_db):
        with patch("machina.memory.user_profile.db", mock_db):
            mock_db.execute = AsyncMock(return_value="DELETE 0")
            result = await store.delete(uuid.uuid4())
            assert result is False


# ===========================================================================
# R4-089 (b): approval gate wired into MemoryStore.add
# ===========================================================================


class TestApprovalGateWiring:
    """Enabled via config: memory writes route to ``pending_memories`` and the
    approve/reject surface completes them. Disabled (default): unchanged."""

    async def test_enabled_routes_add_to_pending_then_approve(self, db_pool, monkeypatch):
        from machina.core.config import config
        from machina.memory.approval import memory_approval_gate
        from machina.memory.store import MemoryStore

        for target in (
            "machina.memory.store.db",
            "machina.memory.identity.db",
            "machina.memory.approval.db",
        ):
            monkeypatch.setattr(target, db_pool)
        # raising=False so the RED run against the unwired code fails at the
        # semantic assertion (durable write instead of pending), not at the
        # config surface itself.
        monkeypatch.setattr(config, "memory_approval_enabled", True, raising=False)

        agent_id = f"r4-089-gate-{uuid.uuid4()}"
        try:
            placeholder = await MemoryStore().add(
                None, agent_id, "User prefers concise release notes", "preference"
            )

            pending = await db_pool.fetchrow(
                "SELECT id, status, content FROM pending_memories WHERE id = $1",
                placeholder.id,
            )
            assert pending is not None, "the write must land in pending_memories"
            assert pending["status"] == "pending"
            assert pending["content"] == "User prefers concise release notes"
            assert (
                await db_pool.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE agent_id = $1", agent_id
                )
                == 0
            ), "no durable memory may exist before approval"

            memory = await memory_approval_gate.approve(placeholder.id)
            assert memory is not None
            assert memory.content == "User prefers concise release notes"
            assert (
                await db_pool.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE agent_id = $1", agent_id
                )
                == 1
            )
            assert (
                await db_pool.fetchval(
                    "SELECT memory_id FROM pending_memories WHERE id = $1", placeholder.id
                )
                == memory.id
            )
        finally:
            await db_pool.execute("DELETE FROM pending_memories WHERE agent_id = $1", agent_id)
            await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent_id)

    async def test_enabled_reject_creates_no_memory(self, db_pool, monkeypatch):
        from machina.core.config import config
        from machina.memory.approval import memory_approval_gate
        from machina.memory.store import MemoryStore

        for target in (
            "machina.memory.store.db",
            "machina.memory.identity.db",
            "machina.memory.approval.db",
        ):
            monkeypatch.setattr(target, db_pool)
        monkeypatch.setattr(config, "memory_approval_enabled", True, raising=False)

        agent_id = f"r4-089-gate-{uuid.uuid4()}"
        try:
            placeholder = await MemoryStore().add(
                None, agent_id, "Ephemeral fact the user rejects", "fact"
            )
            assert (
                await db_pool.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE agent_id = $1", agent_id
                )
                == 0
            )

            assert await memory_approval_gate.reject(placeholder.id) is True
            assert (
                await db_pool.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE agent_id = $1", agent_id
                )
                == 0
            )
            assert (
                await db_pool.fetchval(
                    "SELECT status FROM pending_memories WHERE id = $1", placeholder.id
                )
                == "rejected"
            )
        finally:
            await db_pool.execute("DELETE FROM pending_memories WHERE agent_id = $1", agent_id)
            await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent_id)

    async def test_disabled_writes_directly_without_pending(self, db_pool, monkeypatch):
        """Default OFF preserves the pre-gate behavior exactly."""
        from machina.memory.store import MemoryStore

        for target in ("machina.memory.store.db", "machina.memory.identity.db"):
            monkeypatch.setattr(target, db_pool)

        agent_id = f"r4-089-gate-off-{uuid.uuid4()}"
        try:
            entry = await MemoryStore().add(None, agent_id, "Direct durable fact", "fact")
            assert entry.id is not None
            assert (
                await db_pool.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE agent_id = $1", agent_id
                )
                == 1
            )
            assert (
                await db_pool.fetchval(
                    "SELECT COUNT(*) FROM pending_memories WHERE agent_id = $1", agent_id
                )
                == 0
            )
        finally:
            await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent_id)
