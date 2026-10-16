"""Durable, authorized tool-artifact storage (token-efficiency T2).

Oversized tool results are persisted once, verbatim, so the model can work
from a bounded view while the exact original stays recoverable. Guarantees:

- **Store before publish.** A lossy model-visible view is only produced after
  the original is durably retained; a failed store means the caller keeps its
  existing (non-lossy-attributed) behavior instead.
- **Reauthorized reads.** Every read resolves the caller's session ancestry
  against the artifact's session ancestry; handles are opaque UUIDs, not
  bearer tokens, and authorization is re-checked per call. Expiry and missing
  rows answer "unavailable" honestly.
- **Bounded.** Per-artifact size, per-session count, and retention days are
  configuration; expired rows are purged opportunistically and quotas refuse
  new stores (they never evict live evidence silently).
"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from typing import Any

from machina.core.config import config
from machina.core.metrics import metrics
from machina.db.lazy import LazyDb, parse_command_count
from machina.observability.diagnostics import record_failure

db = LazyDb()

__all__ = ["ArtifactStore", "artifact_store", "ArtifactError", "artifact_disclosure"]

_MAX_READ_CHARS = 20_000
_MAX_QUERY_CHARS = 200
# Opportunistic purge throttling: at most one sweep per session-store burst.
_PURGE_INTERVAL_SECONDS = 600.0
_last_purge_at: float = 0.0


class ArtifactError(Exception):
    """Typed artifact failure: ``code`` is a stable machine-readable label."""

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


def artifact_disclosure(artifact_id: str, total_chars: int) -> str:
    """Model-visible pointer to a retained original (stable, parseable)."""
    return (
        f"\n[evidence retained as artifact {artifact_id} ({total_chars} chars); "
        f"artifact_read pages the exact original]"
    )


def _setting(name: str, default: Any) -> Any:
    try:
        value = config.get(name)
    except Exception:
        return default
    return default if value is None else value


class ArtifactStore:
    """PostgreSQL-backed immutable artifact storage with session ACLs."""

    async def store(
        self,
        kind: str,
        content: str,
        *,
        session_id: uuid.UUID,
        agent_id: str,
        source_version: str | None = None,
        status: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist *content* and return its handle metadata, or None.

        None means "no artifact for this result" — the store is disabled, the
        database is unavailable, or a quota refused the write. Callers must
        fall back to their existing behavior in that case; they must never
        publish a lossy view attributed as recoverable without a handle.
        """
        if not bool(_setting("artifacts_enabled", True)):
            return None
        if not isinstance(content, str) or not content:
            return None
        max_chars = int(_setting("artifact_max_chars", 2_000_000))
        if len(content) > max_chars:
            # Refuse rather than silently store a differently-sized original:
            # a lossy view over a truncated "original" would lie about
            # recovery. The caller keeps its non-artifact behavior.
            metrics.increment_counter("artifacts.store_refused_oversize")
            return None
        if not db.connected:
            return None
        try:
            # Quota check + insert are serialized per session: parallel tool
            # batches (up to _MAX_PARALLEL_TOOLS stores at once) must not each
            # pass the check and collectively exceed artifact_max_per_session.
            async with db.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        f"tool_artifacts:{session_id}",
                    )
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM tool_artifacts WHERE session_id = $1 "
                        "AND (expires_at IS NULL OR expires_at > now())",
                        session_id,
                    )
                    max_session = int(_setting("artifact_max_per_session", 500))
                    if max_session and int(count or 0) >= max_session:
                        metrics.increment_counter("artifacts.store_refused_quota")
                        return None
                    retention_days = int(_setting("artifact_retention_days", 30))
                    created = await conn.fetchrow(
                        "INSERT INTO tool_artifacts "
                        "(session_id, agent_id, kind, source_version, status, summary, content, "
                        "content_bytes, token_estimate, expires_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, "
                        "CASE WHEN $10 > 0 THEN now() + make_interval(days => $10) ELSE NULL END) "
                        "RETURNING id, content_bytes, expires_at",
                        session_id,
                        agent_id,
                        kind,
                        source_version,
                        status,
                        summary,
                        content,
                        len(content.encode("utf-8", "replace")),
                        len(content) // 4,
                        retention_days,
                    )
        except Exception as error:
            # Storage failure must never break the turn; the caller falls
            # back and loses nothing it did not already have.
            record_failure("artifacts.store", error)
            metrics.increment_counter("artifacts.store_failed")
            return None
        metrics.increment_counter("artifacts.stored")
        await self._maybe_purge()
        return {
            "artifact_id": str(created["id"]),
            "kind": kind,
            "content_bytes": int(created["content_bytes"]),
            "expires_at": created["expires_at"],
        }

    async def _authorized(self, artifact_id: uuid.UUID, session_id: uuid.UUID) -> dict | None:
        """Return the artifact row when *session_id* may read it, else None.

        Access follows the delegation tree: a session may read artifacts of
        its ancestors and of its descendants (both carry each other in their
        ancestry snapshots), never siblings or unrelated sessions.
        """
        row = await db.fetchrow(
            "SELECT a.id, a.session_id, a.agent_id, a.kind, a.status, "
            "a.source_version, a.summary, a.content_bytes, a.token_estimate, "
            "a.created_at, a.expires_at, a.access_count, "
            "s.ancestor_session_ids AS session_ancestors "
            "FROM tool_artifacts a JOIN sessions s ON s.id = a.session_id "
            "WHERE a.id = $1",
            artifact_id,
        )
        if row is None:
            return None
        if row["session_id"] == session_id:
            return row
        session_ancestors = await db.fetchval(
            "SELECT ancestor_session_ids FROM sessions WHERE id = $1", session_id
        )
        artifact_ancestors = list(row["session_ancestors"] or [])
        caller_ancestors = list(session_ancestors or [])
        if row["session_id"] in caller_ancestors or session_id in artifact_ancestors:
            return row
        return None

    @staticmethod
    def _expired(row: dict[str, Any]) -> bool:
        expires = row.get("expires_at")
        if expires is None:
            return False
        now = _dt.datetime.now(_dt.UTC)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_dt.UTC)
        return expires < now

    async def describe(
        self, artifact_id: str | uuid.UUID, *, session_id: uuid.UUID, agent_id: str
    ) -> dict[str, Any]:
        """Metadata for one artifact; raises ArtifactError when not readable."""
        try:
            key = uuid.UUID(str(artifact_id))
        except ValueError as exc:
            raise ArtifactError("artifact_id must be a UUID", code="invalid") from exc
        if not db.connected:
            raise ArtifactError("artifact store unavailable", code="store_unavailable")
        try:
            row = await self._authorized(key, session_id)
        except Exception as error:
            record_failure("artifacts.describe", error)
            raise ArtifactError("artifact store unavailable", code="store_unavailable") from error
        if row is None:
            metrics.increment_counter("artifacts.denied")
            raise ArtifactError("artifact not found or not accessible", code="denied")
        if self._expired(row):
            raise ArtifactError("artifact expired", code="expired")
        return {
            "artifact_id": str(row["id"]),
            "kind": row["kind"],
            "status": row["status"],
            "session_id": str(row["session_id"]),
            "agent_id": row["agent_id"],
            "source_version": row["source_version"],
            "summary": row["summary"],
            "content_bytes": int(row["content_bytes"]),
            "token_estimate": int(row["token_estimate"]),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "access_count": int(row["access_count"]),
        }

    async def read(
        self,
        artifact_id: str | uuid.UUID,
        *,
        session_id: uuid.UUID,
        agent_id: str,
        offset: int = 0,
        limit: int = 4000,
    ) -> dict[str, Any]:
        """A bounded page of the exact original.

        Offset/limit are character positions on the stored text. The response
        states the true total so callers can page without guessing.
        """
        if offset < 0:
            raise ArtifactError("offset must be >= 0", code="invalid")
        if limit < 1 or limit > _MAX_READ_CHARS:
            raise ArtifactError(f"limit must be in 1..{_MAX_READ_CHARS}", code="invalid")
        meta = await self.describe(artifact_id, session_id=session_id, agent_id=agent_id)
        key = uuid.UUID(meta["artifact_id"])
        try:
            row = await db.fetchrow(
                "UPDATE tool_artifacts SET access_count = access_count + 1 "
                "WHERE id = $1 RETURNING substr(content, $2, $3) AS page, "
                "length(content) AS total_chars, kind, status",
                key,
                offset + 1,  # substr is 1-indexed
                limit,
            )
        except Exception as error:
            record_failure("artifacts.read", error)
            raise ArtifactError("artifact store unavailable", code="store_unavailable") from error
        if row is None:
            raise ArtifactError("artifact not found or not accessible", code="denied")
        metrics.increment_counter("artifacts.read")
        page = row["page"] or ""
        total = int(row["total_chars"] or 0)
        return {
            "artifact_id": str(key),
            "kind": row["kind"],
            "status": row["status"],
            "offset": offset,
            "limit": limit,
            "total_chars": total,
            "returned_chars": len(page),
            "truncated": offset + len(page) < total,
            "content": page,
        }

    async def search(
        self,
        query: str,
        *,
        session_id: uuid.UUID,
        agent_id: str,
        artifact_id: str | uuid.UUID | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search within one artifact or the session.

        Deterministic: newest artifacts first, earliest match per artifact,
        fixed context window around each match. Returns [] for no matches.
        """
        if not isinstance(query, str) or not query.strip():
            raise ArtifactError("query must be a non-empty string", code="invalid")
        query = query.strip()
        if len(query) > _MAX_QUERY_CHARS:
            raise ArtifactError(
                f"query must be at most {_MAX_QUERY_CHARS} characters", code="invalid"
            )
        limit = max(1, min(int(limit), 20))
        if artifact_id is not None:
            meta = await self.describe(artifact_id, session_id=session_id, agent_id=agent_id)
            candidates = [uuid.UUID(meta["artifact_id"])]
        else:
            try:
                rows = await db.fetch(
                    "SELECT a.id FROM tool_artifacts a "
                    "WHERE (a.session_id = $1 "
                    "  OR a.session_id = ANY("
                    "    COALESCE((SELECT ancestor_session_ids FROM sessions WHERE id = $1), '{}')) "
                    "  OR a.session_id IN ("
                    "    SELECT id FROM sessions WHERE ancestor_session_ids @> ARRAY[$1::uuid])) "
                    "AND (a.expires_at IS NULL OR a.expires_at > now()) "
                    "ORDER BY a.created_at DESC LIMIT 50",
                    session_id,
                )
            except Exception as error:
                record_failure("artifacts.search", error)
                raise ArtifactError(
                    "artifact store unavailable", code="store_unavailable"
                ) from error
            candidates = [row["id"] for row in rows]
        matches: list[dict[str, Any]] = []
        context = 160
        for key in candidates:
            try:
                row = await db.fetchrow(
                    "SELECT id, kind, status, length(content) AS total_chars, "
                    "strpos(lower(content), lower($2)) AS hit, "
                    "substr(content, GREATEST(strpos(lower(content), lower($2)) - $3, 1), $4) "
                    "AS excerpt "
                    "FROM tool_artifacts WHERE id = $1",
                    key,
                    query,
                    context,
                    context * 3,
                )
            except Exception as error:
                record_failure("artifacts.search", error)
                raise ArtifactError(
                    "artifact store unavailable", code="store_unavailable"
                ) from error
            if row is None or not row["hit"]:
                continue
            excerpt = str(row["excerpt"] or "")
            if not excerpt:
                continue
            matches.append(
                {
                    "artifact_id": str(row["id"]),
                    "kind": row["kind"],
                    "status": row["status"],
                    "total_chars": int(row["total_chars"] or 0),
                    "position": int(row["hit"]) - 1,
                    "excerpt": excerpt,
                }
            )
            if len(matches) >= limit:
                break
        metrics.increment_counter("artifacts.search")
        return matches

    async def purge_expired(self) -> int:
        """Delete expired artifacts; returns the number removed."""
        if not db.connected:
            return 0
        try:
            result = await db.execute(
                "DELETE FROM tool_artifacts WHERE expires_at IS NOT NULL "
                "AND expires_at < now() AND id IN ("
                "SELECT id FROM tool_artifacts WHERE expires_at IS NOT NULL "
                "AND expires_at < now() LIMIT 1000)"
            )
        except Exception as error:
            record_failure("artifacts.purge", error)
            return 0
        removed = parse_command_count(result)
        if removed:
            metrics.increment_counter("artifacts.purged", removed)
        return removed

    async def _maybe_purge(self) -> None:
        """Throttled opportunistic purge after a successful store.

        ``_last_purge_at`` is a process-global monotonic stamp: at most one
        sweep per interval across all sessions in this process.
        """
        global _last_purge_at
        now = time.monotonic()
        if now - _last_purge_at < _PURGE_INTERVAL_SECONDS:
            return
        _last_purge_at = now
        await self.purge_expired()


artifact_store = ArtifactStore()
