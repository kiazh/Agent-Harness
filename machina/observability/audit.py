"""Bounded async persistence for sanitized audit events.

The writer is best effort by design (documented in ``OPERATIONS.md``): a full
queue or an unavailable database never blocks the security path. F-34 closes
the observability gap on the *drop* side without changing that contract: every
event the bounded channel cannot accept (writer not started, writer during or
after shutdown, queue full, or a failed SQL write) is also appended, one
sanitized JSON object per line, to a local spill file so it survives a restart:

    <machina_home>/logs/audit-spill.jsonl

Spilling is best effort and caller-safe: it performs only a small synchronous
append on an already-dropped event, never raises into ``submit``, and falls
back to the previous metrics/log-only behavior if the file cannot be written.
The file is bounded: once it reaches ``AUDIT_SPILL_MAX_BYTES`` the previous
generation is rotated to ``audit-spill.jsonl.1`` (one generation, so disk use
is capped at roughly twice the limit). A single record larger than the cap is
still written once so an event is never lost merely because it is large.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from machina.core.metrics import metrics
from machina.db.connection import db

logger = logging.getLogger(__name__)

#: Size cap for the audit spill file; the previous generation is rotated to
#: ``.1`` when a write would exceed it (one generation is retained).
AUDIT_SPILL_MAX_BYTES = 10 * 1024 * 1024

_SPILL_FILENAME = "audit-spill.jsonl"


def _spill_path() -> Path:
    """Local audit spill target under the Machina state ``logs`` directory.

    Honors the configured ``machina_home`` (settable via ``MACHINA_HOME``),
    matching the auth stores and the lifecycle log; an empty value falls back
    to ``~/.machina``.
    """
    from machina.core.config import config

    try:
        home = str(config.get("machina_home") or "").strip()
    except Exception:
        home = ""
    root = Path(home).expanduser() if home else Path.home() / ".machina"
    return root / "logs" / _SPILL_FILENAME


class AuditPersistence:
    def __init__(self, max_pending: int = 1000) -> None:
        self._max_pending = max_pending
        self._queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._task: asyncio.Task | None = None
        self._owners = 0
        self._ever_started = False

    def start(self) -> None:
        self._owners += 1
        self._ever_started = True
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue(maxsize=self._max_pending)
            self._task = asyncio.create_task(self._run(self._queue), name="machina-audit-writer")
            self._task.add_done_callback(self._clear_task)

    def _clear_task(self, task: asyncio.Task) -> None:
        if self._task is task:
            self._task = None
            self._queue = None

    def _drop(self, entry: dict[str, Any], reason: str) -> None:
        """Count, report, and best-effort spill an event no writer can accept."""
        metrics.increment_counter("audit.queue.dropped")
        if reason == "before-start":
            metrics.increment_counter("audit.queue.dropped_before_start")
            logger.error("Audit event dropped before the writer started; event was not persisted")
        else:
            metrics.increment_counter("audit.queue.dropped_shutdown")
            logger.error(
                "Audit event dropped during/after writer shutdown; event was not persisted"
            )
        self._spill(entry, reason)

    @staticmethod
    def _sanitize_for_spill(entry: dict[str, Any]) -> Any:
        """Sanitize exactly like the DB boundary; never propagate a failure."""
        try:
            from machina.core.provider.audit import _sanitize_value

            return _sanitize_value(entry)
        except Exception:
            return {
                "event": entry.get("event", "unknown") if isinstance(entry, dict) else "unknown",
                "payload": "[AUDIT_SPILL_SANITIZE_FAILED]",
            }

    @staticmethod
    def _rotate_spill_if_needed(path: Path) -> None:
        """Keep one previous generation once the current file reaches the cap."""
        try:
            if path.stat().st_size < AUDIT_SPILL_MAX_BYTES:
                return
        except FileNotFoundError:
            return
        rotated = path.with_name(path.name + ".1")
        try:
            rotated.unlink()
        except FileNotFoundError:
            pass
        path.replace(rotated)

    def _spill(self, entry: dict[str, Any], reason: str) -> None:
        """Append one sanitized JSON line for a dropped event; never raises.

        Runs only on the drop path (the event was already lost to persistence),
        so the small synchronous append cannot block a live security path.
        """
        try:
            record = {
                "spilled_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "entry": self._sanitize_for_spill(entry),
            }
            line = json.dumps(record, default=str) + "\n"
            path = _spill_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_spill_if_needed(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
            metrics.increment_counter("audit.spill.written")
        except Exception as error:
            # The drop behavior stays metrics/log-only; spilling must never
            # fail the caller or mask the primary drop.
            metrics.increment_counter("audit.spill.failed")
            logger.warning("Could not spill dropped audit event: %s", error)

    def submit(self, entry: dict[str, Any]) -> None:
        if self._task is None or self._task.done() or self._queue is None:
            self._drop(entry, "shutdown" if self._ever_started else "before-start")
            return
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            metrics.increment_counter("audit.queue.dropped")
            logger.error("Audit event queue is full; event was not persisted")
            self._spill(entry, "queue-full")

    async def stop(self, *, timeout: float = 5.0) -> None:
        if self._owners == 0:
            return
        self._owners -= 1
        if self._owners > 0 or self._task is None:
            return
        task = self._task
        queue = self._queue
        # A new owner must start a new writer while this generation drains.
        self._clear_task(task)
        try:
            async with asyncio.timeout(timeout):
                await queue.put(None)
                await asyncio.shield(task)
        except TimeoutError:
            logger.warning("Audit writer did not drain before shutdown; cancelling it")
            task.cancel()
            await asyncio.wait({task}, timeout=0.1)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task}, timeout=0.1)
            raise

    def reset(self) -> None:
        """Normalize to a clean, unstarted generation (recovery/test seam).

        Drops any owner count, writer task, and queue left by a previous
        generation and clears ``_ever_started`` so the singleton is
        indistinguishable from a freshly constructed one. This is the
        documented way for a harness (or a recovery path) to shed a leaked
        generation without reaching into private attributes.

        A task bound to a different (or already-dead) event loop cannot be
        awaited or drained here, so it is dropped, not joined: any events it
        still held were already lost with that loop. Logs when it discards
        live state so a leak stays visible rather than silently vanishing.
        """
        if self._owners or self._task is not None or self._queue is not None:
            logger.warning(
                "AuditPersistence.reset() discarding state "
                "(owners=%s, task=%s, queue=%s); any queued events were dropped",
                self._owners,
                self._task,
                self._queue,
            )
        self._owners = 0
        self._task = None
        self._queue = None
        self._ever_started = False

    async def _run(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        while True:
            entry = await queue.get()
            if entry is None:
                queue.task_done()
                return
            try:
                # R5-N (finding 18): re-sanitize at the persistence boundary so
                # a raw entry submitted directly (bypassing audit_log) can
                # never persist a URI userinfo password or credential-bearing
                # query/fragment value. Lazy import: provider.audit imports
                # this module lazily, so a top-level import would cycle.
                from machina.core.provider.audit import _sanitize_value

                try:
                    entry = _sanitize_value(entry)
                except Exception:
                    logger.exception("Could not sanitize audit event; persisting marker")
                    entry = {
                        "event": entry.get("event", "unknown")
                        if isinstance(entry, dict)
                        else "unknown",
                        "payload": "[AUDIT_SANITIZE_FAILED]",
                    }
                await db.execute(
                    "INSERT INTO audit_events (event, payload) VALUES ($1, $2::jsonb)",
                    entry["event"],
                    json.dumps(entry, default=str),
                )
                metrics.increment_counter("audit.persistence.succeeded")
            except Exception:
                metrics.increment_counter("audit.persistence.failed")
                logger.exception("Could not persist audit event")
                # F-34: a failed SQL write is a drop too; keep a durable local
                # record instead of losing the event with the queue.
                self._spill(entry, "persistence-failed")
            finally:
                queue.task_done()


audit_persistence = AuditPersistence()
