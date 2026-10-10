"""Bounded async persistence for sanitized audit events."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ah.core.metrics import metrics
from ah.db.connection import db

logger = logging.getLogger(__name__)


class AuditPersistence:
    def __init__(self, max_pending: int = 1000) -> None:
        self._max_pending = max_pending
        self._queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._task: asyncio.Task | None = None
        self._owners = 0

    def start(self) -> None:
        self._owners += 1
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue(maxsize=self._max_pending)
            self._task = asyncio.create_task(self._run(self._queue), name="ah-audit-writer")
            self._task.add_done_callback(self._clear_task)

    def _clear_task(self, task: asyncio.Task) -> None:
        if self._task is task:
            self._task = None
            self._queue = None

    def submit(self, entry: dict[str, Any]) -> None:
        if self._task is None or self._task.done() or self._queue is None:
            return
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            metrics.increment_counter("audit.queue.dropped")
            logger.error("Audit event queue is full; event was not persisted")

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

    async def _run(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        while True:
            entry = await queue.get()
            if entry is None:
                queue.task_done()
                return
            try:
                await db.execute(
                    "INSERT INTO audit_events (event, payload) VALUES ($1, $2::jsonb)",
                    entry["event"],
                    json.dumps(entry, default=str),
                )
                metrics.increment_counter("audit.persistence.succeeded")
            except Exception as error:
                metrics.increment_counter("audit.persistence.failed")
                logger.error("Could not persist audit event (%s)", type(error).__name__)
            finally:
                queue.task_done()


audit_persistence = AuditPersistence()
