"""User profile — persistent user modeling for personalized interactions.

Stores user preferences, interaction history, and derived insights.
Persisted to the database via the user_profiles table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ah.core.provider import audit_log
from ah.db.connection import db
from ah.memory.redaction import _default_redactor, redact_secrets


def _clean_text(value: str) -> str:
    """Redact secret-looking substrings from free-form profile text."""
    try:
        return redact_secrets(value).text
    except Exception:
        return value


def _clean_preferences(preferences: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact secret-looking values in a preferences mapping."""
    try:
        cleaned, _ = _default_redactor._redact_value(dict(preferences))
        return cleaned if isinstance(cleaned, dict) else {}
    except Exception:
        return preferences

__all__ = [
    "UserProfile",
    "UserProfileStore",
    "user_profile_store",
]

logger = logging.getLogger(__name__)


@dataclass
class UserProfile:
    """A persistent user profile for modeling user preferences and behavior.

    Attributes:
        id: Unique profile identifier.
        user_id: External user identifier (e.g., username, email).
        display_name: Human-readable name.
        preferences: Dict of preference key-value pairs.
        interaction_count: Total number of interactions.
        topics: Dict of topic -> frequency count.
        last_topics: Recently discussed topics (most recent first).
        created_at: Profile creation timestamp.
        updated_at: Last update timestamp.
    """

    id: uuid.UUID
    user_id: str
    display_name: str = ""
    preferences: dict[str, Any] = field(default_factory=dict)
    interaction_count: int = 0
    topics: dict[str, int] = field(default_factory=dict)
    last_topics: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Ensure mutable defaults are not shared."""
        if self.preferences is None:
            self.preferences = {}
        if self.topics is None:
            self.topics = {}
        if self.last_topics is None:
            self.last_topics = []

    def record_interaction(self, topic: str | None = None) -> None:
        """Record a new interaction, optionally with a topic."""
        self.interaction_count += 1
        self.updated_at = datetime.now(UTC)
        if topic:
            topic = _clean_text(topic)
            self.topics[topic] = self.topics.get(topic, 0) + 1
            # Maintain last 10 topics, most recent first
            if topic in self.last_topics:
                self.last_topics.remove(topic)
            self.last_topics.insert(0, topic)
            self.last_topics = self.last_topics[:10]

    def set_preference(self, key: str, value: Any) -> None:
        """Set a preference key-value pair."""
        try:
            cleaned, _ = _default_redactor._redact_value(value)
        except Exception:
            cleaned = value
        self.preferences[key] = cleaned
        self.updated_at = datetime.now(UTC)

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get a preference value by key."""
        return self.preferences.get(key, default)

    def get_top_topics(self, n: int = 5) -> list[tuple[str, int]]:
        """Get the top N topics by frequency."""
        sorted_topics = sorted(self.topics.items(), key=lambda x: x[1], reverse=True)
        return sorted_topics[:n]

    def to_dict(self) -> dict[str, Any]:
        """Serialize profile to a dict."""
        return {
            "id": str(self.id),
            "user_id": self.user_id,
            "display_name": self.display_name,
            "preferences": self.preferences,
            "interaction_count": self.interaction_count,
            "topics": self.topics,
            "last_topics": self.last_topics,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserProfile:
        """Deserialize profile from a dict."""
        return cls(
            id=uuid.UUID(data["id"]) if isinstance(data["id"], str) else data["id"],
            user_id=data["user_id"],
            display_name=data.get("display_name", ""),
            preferences=data.get("preferences", {}),
            interaction_count=data.get("interaction_count", 0),
            topics=data.get("topics", {}),
            last_topics=data.get("last_topics", []),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if isinstance(data.get("created_at"), str)
                else data.get("created_at", datetime.now(UTC))
            ),
            updated_at=(
                datetime.fromisoformat(data["updated_at"])
                if isinstance(data.get("updated_at"), str)
                else data.get("updated_at", datetime.now(UTC))
            ),
        )


class UserProfileStore:
    """CRUD for user profiles stored in PostgreSQL."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def create(
        self,
        user_id: str,
        display_name: str = "",
        preferences: dict[str, Any] | None = None,
    ) -> UserProfile:
        """Create a new user profile."""
        profile_id = uuid.uuid4()
        now = datetime.now(UTC)
        preferences_json = json.dumps(_clean_preferences(preferences or {}))
        display_name = _clean_text(display_name)
        user_id = _clean_text(user_id)

        row = await db.fetchrow(
            """
            INSERT INTO user_profiles (id, user_id, display_name, preferences, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $5)
            RETURNING id, user_id, display_name, preferences, interaction_count,
                      topics, last_topics, created_at, updated_at
            """,
            profile_id,
            user_id,
            display_name,
            preferences_json,
            now,
        )
        profile = self._row_to_profile(row)
        audit_log(
            "user_profile_create",
            profile_id=str(profile.id),
            user_id=user_id,
        )
        return profile

    async def get(self, profile_id: uuid.UUID) -> UserProfile | None:
        """Get a profile by ID."""
        row = await db.fetchrow(
            """
            SELECT id, user_id, display_name, preferences, interaction_count,
                   topics, last_topics, created_at, updated_at
            FROM user_profiles WHERE id = $1
            """,
            profile_id,
        )
        if row is None:
            return None
        return self._row_to_profile(row)

    async def get_by_user_id(self, user_id: str) -> UserProfile | None:
        """Get a profile by user_id."""
        row = await db.fetchrow(
            """
            SELECT id, user_id, display_name, preferences, interaction_count,
                   topics, last_topics, created_at, updated_at
            FROM user_profiles WHERE user_id = $1
            """,
            user_id,
        )
        if row is None:
            return None
        return self._row_to_profile(row)

    async def get_or_create(
        self,
        user_id: str,
        display_name: str = "",
    ) -> UserProfile:
        """Get existing profile or create a new one."""
        profile = await self.get_by_user_id(user_id)
        if profile:
            return profile
        return await self.create(user_id=user_id, display_name=display_name)

    async def update_preferences(
        self,
        profile_id: uuid.UUID,
        preferences: dict[str, Any],
    ) -> UserProfile | None:
        """Replace all preferences for a profile."""
        preferences_json = json.dumps(_clean_preferences(preferences))
        row = await db.fetchrow(
            """
            UPDATE user_profiles
            SET preferences = $2, updated_at = now()
            WHERE id = $1
            RETURNING id, user_id, display_name, preferences, interaction_count,
                      topics, last_topics, created_at, updated_at
            """,
            profile_id,
            preferences_json,
        )
        if row is None:
            return None
        profile = self._row_to_profile(row)
        audit_log(
            "user_profile_update_prefs",
            profile_id=str(profile.id),
            user_id=profile.user_id,
        )
        return profile

    async def record_interaction(
        self,
        profile_id: uuid.UUID,
        topic: str | None = None,
    ) -> UserProfile | None:
        """Record an interaction for a profile.

        Uses a single atomic UPDATE ... RETURNING query to avoid race conditions
        between concurrent interaction recordings.
        """
        if topic:
            topic = _clean_text(topic)
            row = await db.fetchrow(
                """
                UPDATE user_profiles
                SET interaction_count = interaction_count + 1,
                    topics = jsonb_set(
                        COALESCE(topics, '{}'::jsonb),
                        ARRAY[$2],
                        to_jsonb(COALESCE((topics->>$2)::int, 0) + 1)
                    ),
                    last_topics = COALESCE((
                        SELECT jsonb_agg(elem) FROM (
                            SELECT elem FROM (
                                SELECT to_jsonb($2) AS elem
                                UNION ALL
                                SELECT elem FROM jsonb_array_elements(last_topics) elem
                                WHERE elem != to_jsonb($2::text)
                            ) combined
                            LIMIT 10
                        ) limited
                    ), '[]'::jsonb),
                    updated_at = now()
                WHERE id = $1
                RETURNING id, user_id, display_name, preferences, interaction_count,
                          topics, last_topics, created_at, updated_at
                """,
                profile_id,
                topic,
            )
        else:
            row = await db.fetchrow(
                """
                UPDATE user_profiles
                SET interaction_count = interaction_count + 1,
                    updated_at = now()
                WHERE id = $1
                RETURNING id, user_id, display_name, preferences, interaction_count,
                          topics, last_topics, created_at, updated_at
                """,
                profile_id,
            )

        if row is None:
            return None
        return self._row_to_profile(row)

    async def list_all(self, limit: int = 100) -> list[UserProfile]:
        """List all user profiles."""
        rows = await db.fetch(
            """
            SELECT id, user_id, display_name, preferences, interaction_count,
                   topics, last_topics, created_at, updated_at
            FROM user_profiles
            ORDER BY updated_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_profile(row) for row in rows]

    async def delete(self, profile_id: uuid.UUID) -> bool:
        """Delete a profile. Returns True if deleted."""
        result = await db.execute(
            "DELETE FROM user_profiles WHERE id = $1",
            profile_id,
        )
        deleted = result != "DELETE 0"
        if deleted:
            audit_log("user_profile_delete", profile_id=str(profile_id))
        return deleted

    def _row_to_profile(self, row: Any) -> UserProfile:
        """Convert a database row to a UserProfile."""
        preferences = json.loads(row["preferences"]) if row["preferences"] else {}
        topics = json.loads(row["topics"]) if row["topics"] else {}
        last_topics = json.loads(row["last_topics"]) if row["last_topics"] else []

        return UserProfile(
            id=row["id"],
            user_id=row["user_id"],
            display_name=row["display_name"],
            preferences=preferences,
            interaction_count=row["interaction_count"],
            topics=topics,
            last_topics=last_topics,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# Global singleton
user_profile_store = UserProfileStore()
