"""Durable, authorized tool-artifact storage (token-efficiency T2, B0).

Oversized tool results are persisted once, verbatim, so the model can work
from a bounded view while the retained original stays recoverable. Guarantees:

- **Store before publish.** A lossy model-visible view is only produced after
  the original is durably retained; a failed store means the caller keeps its
  existing (non-recovery-attributed) behavior instead.
- **Honest completeness.** Every new record carries ``complete``,
  ``upstream_truncated``, ``stored_truncated``, and ``content_revision``.
  A producer that truncated its own output before the store saw it is
  recorded as ``upstream_truncated=true``/``complete=false`` (C07); unknown
  provenance stays NULL — never silently complete. Legacy rows read as
  unknown on every field.
- **Reauthorized reads.** Every read resolves the caller's session ancestry
  against the artifact's session ancestry; handles are opaque UUIDs, not
  bearer tokens, and authorization is re-checked per call. Expiry and missing
  rows answer "unavailable" honestly.
- **Bound cursors.** Paged reads and searches continue from cursors that bind
  the artifact revision, source version, scope, filters, order, scan window,
  and retention observed at issue time; a cursor that no longer matches is
  rejected explicitly instead of silently paging a different evidence basis
  (§14.6/C09). Legacy offset/limit calls keep working unchanged.
- **Bounded.** Per-artifact size, per-session count, and retention days are
  configuration; expired rows are purged opportunistically and quotas refuse
  new stores (they never evict live evidence silently).
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
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
_MAX_TOKEN_CHARS = 256
# Verified-view summary bound for the request projector lookup (B1 seam).
_LOOKUP_VIEW_CHARS = 500
# Session search scans at most this many newest readable artifacts per page.
_SEARCH_SCAN_LIMIT = 50
_MAX_SEARCH_SCAN = 200
_SEARCH_ORDER = "created_at_desc,id_desc"
_CURSOR_VERSION = 1
# Opportunistic purge throttling: at most one sweep per session-store burst.
_PURGE_INTERVAL_SECONDS = 600.0
_last_purge_at: float = 0.0


class ArtifactError(Exception):
    """Typed artifact failure: ``code`` is a stable machine-readable label."""

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


def artifact_disclosure(
    artifact_id: str,
    total_chars: int,
    *,
    upstream_truncated: bool | None = False,
    complete: bool | None = True,
) -> str:
    """Model-visible pointer to a retained original (stable, parseable).

    The claim is graded by what the store actually knows (C07): a producer
    that had already truncated its own output never gets an "exact original"
    claim, and unknown completeness is stated instead of implied.
    """
    if upstream_truncated is True:
        claim = (
            "the producing tool had already truncated its own output; "
            "artifact_read pages the retained post-truncation result"
        )
    elif complete is False:
        claim = "artifact_read pages the retained copy, which is itself incomplete"
    elif complete is None:
        claim = (
            "artifact_read pages the retained result; its completeness relative to "
            "the tool's full output is unverified"
        )
    else:
        claim = "artifact_read pages the exact original"
    return f"\n[evidence retained as artifact {artifact_id} ({total_chars} chars); {claim}]"


def _setting(name: str, default: Any) -> Any:
    try:
        value = config.get(name)
    except Exception:
        return default
    return default if value is None else value


def _clean_token(value: Any, *, limit: int = _MAX_TOKEN_CHARS) -> str | None:
    """Normalize an opaque version/revision token; unusable input means unknown."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned


def _clean_flag(value: Any) -> bool | None:
    """Completeness flags are tri-state: True/False are claims, else unknown."""
    return value if isinstance(value, bool) else None


def _clean_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _derive_content_revision(content: str) -> str:
    """Deterministic content-addressed revision for every new record."""
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest[:16]}"


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor:
        raise ArtifactError("cursor must be a non-empty string", code="invalid_cursor")
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception as exc:
        raise ArtifactError("cursor is malformed", code="invalid_cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise ArtifactError(
            "cursor version is not supported; issue a new cursor", code="invalid_cursor"
        )
    return payload


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
        content_revision: str | None = None,
        status: str | None = None,
        summary: str | None = None,
        complete: bool | None = None,
        upstream_truncated: bool | None = None,
        stored_truncated: bool = False,
        record_count: int | None = None,
    ) -> dict[str, Any] | None:
        """Persist *content* and return its handle metadata, or None.

        None means "no artifact for this result" — the store is disabled, the
        database is unavailable, or a quota refused the write. Callers must
        fall back to their existing behavior, marked as unrecoverable; they
        must never publish a lossy view attributed as recoverable without a
        handle.

        Completeness metadata (spec §14.2) is recorded from the caller's
        audited knowledge and never inferred into a positive claim: an
        unusable flag/token stores NULL (unknown), ``complete`` defaults to
        ``not upstream_truncated and not stored_truncated`` only when the
        caller states upstream truncation, and ``content_revision`` defaults
        to a deterministic hash of the stored content.
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
        version = _clean_token(source_version)
        revision = _clean_token(content_revision) or _derive_content_revision(content)
        complete_flag = _clean_flag(complete)
        upstream_flag = _clean_flag(upstream_truncated)
        stored_flag = stored_truncated if isinstance(stored_truncated, bool) else False
        if complete_flag is None and upstream_flag is not None:
            complete_flag = (not upstream_flag) and (not stored_flag)
        if stored_flag:
            complete_flag = False
        count = _clean_count(record_count)
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
                    count_rows = await conn.fetchval(
                        "SELECT COUNT(*) FROM tool_artifacts WHERE session_id = $1 "
                        "AND (expires_at IS NULL OR expires_at > now())",
                        session_id,
                    )
                    max_session = int(_setting("artifact_max_per_session", 500))
                    if max_session and int(count_rows or 0) >= max_session:
                        metrics.increment_counter("artifacts.store_refused_quota")
                        return None
                    retention_days = int(_setting("artifact_retention_days", 30))
                    created = await conn.fetchrow(
                        "INSERT INTO tool_artifacts "
                        "(session_id, agent_id, kind, source_version, content_revision, "
                        "status, summary, content, content_bytes, token_estimate, complete, "
                        "upstream_truncated, stored_truncated, record_count, expires_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, "
                        "$14, "
                        "CASE WHEN $15 > 0 THEN now() + make_interval(days => $15) ELSE NULL END) "
                        "RETURNING id, content_bytes, complete, upstream_truncated, "
                        "stored_truncated, record_count, content_revision, expires_at",
                        session_id,
                        agent_id,
                        kind,
                        version,
                        revision,
                        status,
                        summary,
                        content,
                        len(content.encode("utf-8", "replace")),
                        len(content) // 4,
                        complete_flag,
                        upstream_flag,
                        stored_flag,
                        count,
                        retention_days,
                    )
        except Exception as error:
            # Storage failure must never break the turn; the caller falls
            # back and loses nothing it did not already have.
            record_failure("artifacts.store", error)
            metrics.increment_counter("artifacts.store_failed")
            return None
        metrics.increment_counter("artifacts.stored")
        if upstream_flag:
            metrics.increment_counter("artifacts.stored_upstream_truncated")
        await self._maybe_purge()
        return {
            "artifact_id": str(created["id"]),
            "kind": kind,
            "content_bytes": int(created["content_bytes"]),
            "expires_at": created["expires_at"],
            "source_version": version,
            "content_revision": created["content_revision"],
            "complete": created["complete"],
            "upstream_truncated": created["upstream_truncated"],
            "stored_truncated": created["stored_truncated"],
            "record_count": created["record_count"],
        }

    async def _authorized(self, artifact_id: uuid.UUID, session_id: uuid.UUID) -> dict | None:
        """Return the artifact row when *session_id* may read it, else None.

        Access follows the delegation tree: a session may read artifacts of
        its ancestors and of its descendants (both carry each other in their
        ancestry snapshots), never siblings or unrelated sessions.
        """
        row = await db.fetchrow(
            "SELECT a.id, a.session_id, a.agent_id, a.kind, a.status, "
            "a.source_version, a.content_revision, a.summary, a.content_bytes, "
            "a.token_estimate, a.complete, a.upstream_truncated, a.stored_truncated, "
            "a.record_count, a.created_at, a.expires_at, a.access_count, "
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
            "content_revision": row["content_revision"],
            "summary": row["summary"],
            "content_bytes": int(row["content_bytes"]),
            "token_estimate": int(row["token_estimate"]),
            "complete": row["complete"],
            "upstream_truncated": row["upstream_truncated"],
            "stored_truncated": row["stored_truncated"],
            "record_count": (int(row["record_count"]) if row["record_count"] is not None else None),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "access_count": int(row["access_count"]),
        }

    async def lookup(
        self, artifact_id: str | uuid.UUID, *, session_id: uuid.UUID, agent_id: str
    ) -> dict[str, Any] | None:
        """Verified-view lookup for the request projector (B1 seam).

        Returns the projector contract ``{available, complete, handle,
        revision, summary_view}`` only when the artifact is currently readable
        (ACL and retention are re-checked here) *and* positively complete.
        Unknown, partial, expired, denied, or failed artifacts answer None —
        an unavailable artifact never degrades into a weaker claim (C06/C07),
        and errors never propagate as fabricated availability.
        """
        try:
            key = uuid.UUID(str(artifact_id))
        except ValueError:
            return None
        if not db.connected:
            return None
        try:
            row = await self._authorized(key, session_id)
            if row is None or self._expired(row) or row["complete"] is not True:
                return None
            preview = await db.fetchrow(
                "SELECT substr(content, 1, $2) AS head, length(content) AS total_chars "
                "FROM tool_artifacts WHERE id = $1",
                key,
                _LOOKUP_VIEW_CHARS,
            )
        except Exception as error:
            record_failure("artifacts.lookup", error)
            return None
        return {
            "available": True,
            "complete": True,
            "handle": str(key),
            "revision": row["content_revision"],
            "summary_view": self._summary_view(row, preview),
        }

    @staticmethod
    def _summary_view(row: dict[str, Any], preview: dict[str, Any] | None) -> str:
        """Bounded deterministic view text; never implies absent content."""
        summary = row.get("summary")
        if isinstance(summary, str) and summary:
            return summary
        head = str((preview or {}).get("head") or "")
        total = int((preview or {}).get("total_chars") or 0)
        if not head:
            # The row is complete, so empty content is a real stored value,
            # not a missing read; say exactly that instead of returning "".
            return "(retained artifact has no text content)"
        if len(head) < total:
            return f"{head}\n…[{total - len(head)} more chars in the retained original]"
        return head

    async def read(
        self,
        artifact_id: str | uuid.UUID,
        *,
        session_id: uuid.UUID,
        agent_id: str,
        offset: int = 0,
        limit: int = 4000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """A bounded page of the retained result.

        Offset/limit are character positions on the stored text. The response
        states the true total and carries the version binding
        (``source_version``/``content_revision``/``expires_at``) plus a
        ``next_cursor`` when more remains.

        A ``cursor`` resumes at its bound offset and is re-validated against
        the row: a cursor for another artifact, or one whose revision/version/
        retention no longer matches, is rejected with a typed error instead of
        silently paging a different evidence basis. Legacy offset/limit calls
        keep working unchanged.
        """
        if offset < 0:
            raise ArtifactError("offset must be >= 0", code="invalid")
        if limit < 1 or limit > _MAX_READ_CHARS:
            raise ArtifactError(f"limit must be in 1..{_MAX_READ_CHARS}", code="invalid")
        decoded: dict[str, Any] | None = None
        if cursor is not None:
            decoded = _decode_cursor(cursor)
            bound_offset = decoded.get("offset")
            if (
                decoded.get("t") != "read"
                or isinstance(bound_offset, bool)
                or not isinstance(bound_offset, int)
                or bound_offset < 0
            ):
                raise ArtifactError("cursor is not a valid read cursor", code="invalid_cursor")
            if offset not in (0, bound_offset):
                raise ArtifactError("offset conflicts with cursor", code="invalid")
        meta = await self.describe(artifact_id, session_id=session_id, agent_id=agent_id)
        key = uuid.UUID(meta["artifact_id"])
        if decoded is not None:
            if decoded.get("artifact") != meta["artifact_id"]:
                raise ArtifactError(
                    "cursor belongs to a different artifact; issue a new read",
                    code="invalid_cursor",
                )
            self._validate_binding(decoded, meta)
            offset = int(decoded["offset"])
        try:
            row = await db.fetchrow(
                "UPDATE tool_artifacts SET access_count = access_count + 1 "
                "WHERE id = $1 RETURNING substr(content, $2, $3) AS page, "
                "length(content) AS total_chars, kind, status, source_version, "
                "content_revision, expires_at",
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
        next_offset = offset + len(page)
        truncated = next_offset < total
        next_cursor = None
        if truncated:
            next_cursor = _encode_cursor(
                {
                    "v": _CURSOR_VERSION,
                    "t": "read",
                    "artifact": str(key),
                    "revision": row["content_revision"],
                    "source_version": row["source_version"],
                    "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                    "offset": next_offset,
                    "total_chars": total,
                }
            )
        return {
            "artifact_id": str(key),
            "kind": row["kind"],
            "status": row["status"],
            "offset": offset,
            "limit": limit,
            "total_chars": total,
            "returned_chars": len(page),
            "truncated": truncated,
            "content": page,
            "source_version": row["source_version"],
            "content_revision": row["content_revision"],
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _validate_binding(decoded: dict[str, Any], meta: dict[str, Any]) -> None:
        """Reject a cursor whose bound revision/version/retention drifted."""
        if (
            decoded.get("revision") != meta.get("content_revision")
            or decoded.get("source_version") != meta.get("source_version")
            or decoded.get("expires_at") != meta.get("expires_at")
        ):
            raise ArtifactError(
                "cursor revision no longer matches the retained artifact; issue a new read",
                code="stale_cursor",
            )

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
        fixed context window around each match. Returns [] for no matches in
        the scanned scope; use :meth:`search_page` when the scan window and
        continuation disclosure matter.
        """
        page = await self.search_page(
            query,
            session_id=session_id,
            agent_id=agent_id,
            artifact_id=artifact_id,
            limit=limit,
        )
        return page["matches"]

    async def search_page(
        self,
        query: str,
        *,
        session_id: uuid.UUID,
        agent_id: str,
        artifact_id: str | uuid.UUID | None = None,
        limit: int = 5,
        cursor: str | None = None,
        scan_limit: int = _SEARCH_SCAN_LIMIT,
    ) -> dict[str, Any]:
        """Bounded search with a bound continuation cursor (§14.6/C09).

        A cursor binds the query, scope, order, scan window, and the head
        candidate's revision/version; a resume that no longer matches is
        rejected explicitly instead of silently answering from a different
        evidence basis. The response reports ``scanned``/``scan_limit`` and
        ``scan_truncated`` so an empty ``matches`` list is never presented as
        a definitive absence when the window was cut short.
        """
        if not isinstance(query, str) or not query.strip():
            raise ArtifactError("query must be a non-empty string", code="invalid")
        query = query.strip()
        if len(query) > _MAX_QUERY_CHARS:
            raise ArtifactError(
                f"query must be at most {_MAX_QUERY_CHARS} characters", code="invalid"
            )
        limit = max(1, min(int(limit), 20))
        scan_limit = max(1, min(int(scan_limit), _MAX_SEARCH_SCAN))
        artifact_ref = str(artifact_id) if artifact_id is not None else None
        decoded: dict[str, Any] | None = None
        if cursor is not None:
            decoded = _decode_cursor(cursor)
            if decoded.get("t") != "search":
                raise ArtifactError("cursor is not a search cursor", code="invalid_cursor")
            if (
                decoded.get("query") != query
                or decoded.get("order") != _SEARCH_ORDER
                or decoded.get("scan_limit") != scan_limit
                or decoded.get("session") != str(session_id)
                or decoded.get("artifact") != artifact_ref
            ):
                raise ArtifactError(
                    "cursor does not match this search (query, scope, order, or window "
                    "changed); issue a new search",
                    code="invalid_cursor",
                )
        scope = {
            "kind": "artifact" if artifact_ref is not None else "session",
            "session_id": str(session_id),
            "artifact_id": artifact_ref,
        }
        if artifact_ref is not None:
            meta = await self.describe(artifact_ref, session_id=session_id, agent_id=agent_id)
            match = await self._match_one(uuid.UUID(meta["artifact_id"]), query)
            metrics.increment_counter("artifacts.search")
            return {
                "matches": [match] if match is not None else [],
                "query": query,
                "scope": scope,
                "order": _SEARCH_ORDER,
                "scanned": 1,
                "scan_limit": scan_limit,
                "scan_truncated": False,
                "next_cursor": None,
            }

        head: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        if decoded is not None:
            await self._verify_search_head(decoded, session_id)
            raw_head = decoded.get("head")
            if isinstance(raw_head, dict):
                head = raw_head
            raw_after = decoded.get("after")
            if isinstance(raw_after, dict):
                after = raw_after
        after_created: _dt.datetime | None = None
        after_id: uuid.UUID | None = None
        if after is not None:
            try:
                after_created = _dt.datetime.fromisoformat(str(after.get("created_at")))
                after_id = uuid.UUID(str(after.get("id")))
            except (TypeError, ValueError) as exc:
                raise ArtifactError(
                    "cursor continuation position is malformed", code="invalid_cursor"
                ) from exc
        try:
            if after_created is not None and after_id is not None:
                rows = await db.fetch(
                    "SELECT a.id, a.source_version, a.content_revision, a.created_at "
                    "FROM tool_artifacts a "
                    "WHERE (a.session_id = $1 "
                    "  OR a.session_id = ANY("
                    "    COALESCE((SELECT ancestor_session_ids FROM sessions WHERE id = $1), "
                    "'{}')) "
                    "  OR a.session_id IN ("
                    "    SELECT id FROM sessions WHERE ancestor_session_ids @> ARRAY[$1::uuid])) "
                    "AND (a.expires_at IS NULL OR a.expires_at > now()) "
                    "AND (a.created_at < $2 OR (a.created_at = $2 AND a.id < $3)) "
                    "ORDER BY a.created_at DESC, a.id DESC LIMIT $4",
                    session_id,
                    after_created,
                    after_id,
                    scan_limit + 1,
                )
            else:
                rows = await db.fetch(
                    "SELECT a.id, a.source_version, a.content_revision, a.created_at "
                    "FROM tool_artifacts a "
                    "WHERE (a.session_id = $1 "
                    "  OR a.session_id = ANY("
                    "    COALESCE((SELECT ancestor_session_ids FROM sessions WHERE id = $1), "
                    "'{}')) "
                    "  OR a.session_id IN ("
                    "    SELECT id FROM sessions WHERE ancestor_session_ids @> ARRAY[$1::uuid])) "
                    "AND (a.expires_at IS NULL OR a.expires_at > now()) "
                    "ORDER BY a.created_at DESC, a.id DESC LIMIT $2",
                    session_id,
                    scan_limit + 1,
                )
        except Exception as error:
            record_failure("artifacts.search", error)
            raise ArtifactError("artifact store unavailable", code="store_unavailable") from error
        candidates = rows[:scan_limit]
        beyond_window = len(rows) > scan_limit
        if head is None and candidates:
            head = {
                "id": str(candidates[0]["id"]),
                "revision": candidates[0]["content_revision"],
                "source_version": candidates[0]["source_version"],
            }
        matches: list[dict[str, Any]] = []
        scanned = 0
        last_scanned = None
        for candidate in candidates:
            scanned += 1
            last_scanned = candidate
            match = await self._match_one(candidate["id"], query)
            if match is not None:
                matches.append(match)
                if len(matches) >= limit:
                    break
        has_more = beyond_window or scanned < len(candidates)
        next_cursor = None
        if has_more and last_scanned is not None:
            next_cursor = _encode_cursor(
                {
                    "v": _CURSOR_VERSION,
                    "t": "search",
                    "query": query,
                    "session": str(session_id),
                    "artifact": None,
                    "order": _SEARCH_ORDER,
                    "scan_limit": scan_limit,
                    "after": {
                        "created_at": last_scanned["created_at"].isoformat(),
                        "id": str(last_scanned["id"]),
                    },
                    "head": head,
                }
            )
        metrics.increment_counter("artifacts.search")
        return {
            "matches": matches,
            "query": query,
            "scope": scope,
            "order": _SEARCH_ORDER,
            "scanned": scanned,
            "scan_limit": scan_limit,
            "scan_truncated": bool(has_more),
            "next_cursor": next_cursor,
        }

    async def _verify_search_head(self, decoded: dict[str, Any], session_id: uuid.UUID) -> None:
        """Re-validate a search cursor's head revision binding."""
        head = decoded.get("head")
        if not isinstance(head, dict):
            return
        try:
            head_id = uuid.UUID(str(head.get("id")))
        except (TypeError, ValueError) as exc:
            raise ArtifactError("cursor head binding is malformed", code="invalid_cursor") from exc
        try:
            row = await self._authorized(head_id, session_id)
        except Exception as error:
            record_failure("artifacts.search", error)
            raise ArtifactError("artifact store unavailable", code="store_unavailable") from error
        if row is None or self._expired(row):
            raise ArtifactError(
                "search cursor evidence basis is no longer available; issue a new search",
                code="stale_cursor",
            )
        if row["content_revision"] != head.get("revision") or row["source_version"] != head.get(
            "source_version"
        ):
            raise ArtifactError(
                "search cursor evidence basis changed; issue a new search", code="stale_cursor"
            )

    async def _match_one(self, key: uuid.UUID, query: str) -> dict[str, Any] | None:
        """Earliest match for one artifact, with version binding fields."""
        context = 160
        try:
            row = await db.fetchrow(
                "SELECT id, kind, status, length(content) AS total_chars, "
                "strpos(lower(content), lower($2)) AS hit, "
                "substr(content, GREATEST(strpos(lower(content), lower($2)) - $3, 1), $4) "
                "AS excerpt, content_revision, source_version "
                "FROM tool_artifacts WHERE id = $1",
                key,
                query,
                context,
                context * 3,
            )
        except Exception as error:
            record_failure("artifacts.search", error)
            raise ArtifactError("artifact store unavailable", code="store_unavailable") from error
        if row is None or not row["hit"]:
            return None
        excerpt = str(row["excerpt"] or "")
        if not excerpt:
            return None
        return {
            "artifact_id": str(row["id"]),
            "kind": row["kind"],
            "status": row["status"],
            "total_chars": int(row["total_chars"] or 0),
            "position": int(row["hit"]) - 1,
            "excerpt": excerpt,
            "content_revision": row["content_revision"],
            "source_version": row["source_version"],
        }

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
