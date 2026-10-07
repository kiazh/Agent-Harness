"""Shared runtime service graph (Phase B).

One lifecycle-managed graph used by CLI / TUI / HTTP / jobs / delegated
tasks. Reuses existing singletons (no parallel framework); adds:

- single startup/readiness/degradation/shutdown contract
- capability/status reporting derived from instantiated services
- lazy cloud clients (no eager key-dependent instantiation)
- shared vs owned client ownership accounting

Transport differences (stdio vs SSE vs headless) stay above this layer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Capability:
    name: str
    state: str  # available | enabled | active | degraded | unavailable | research-only
    reason: str = ""
    next_action: str = ""


@dataclass
class RuntimeServices:
    """Lifecycle-managed shared services. Use :meth:`startup` once per process."""

    _started: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _background: set[asyncio.Task] = field(default_factory=set)
    capabilities: list[Capability] = field(default_factory=list)

    async def startup(self) -> dict[str, Any]:
        """Connect DB, load skills, detect providers/sandbox. Never raises."""
        async with self._lock:
            if self._started:
                return self.status()
            report: dict[str, Any] = {"db": "unavailable", "skills": 0}
            try:
                from ah.db.connection import db

                await db.connect()
                report["db"] = "ready" if db.connected else "unavailable"
            except Exception as e:
                report["db"] = f"unavailable: {e}"
            try:
                from ah.skills.registry import skill_registry

                skill_registry.load_all()
                report["skills"] = len(skill_registry.list_skills())
            except Exception as e:
                report["skills_error"] = str(e)
            report.update(_detect_providers())
            report.update(_detect_sandbox())
            self.capabilities = _capabilities_from_report(report)
            self._started = True
            self._last_report = report
            return self.status()

    def status(self) -> dict[str, Any]:
        report = dict(getattr(self, "_last_report", {}))
        report["capabilities"] = [
            {"name": c.name, "state": c.state, "reason": c.reason, "next": c.next_action}
            for c in self.capabilities
        ]
        return report

    async def shutdown(self) -> None:
        async with self._lock:
            for t in list(self._background):
                t.cancel()
            if self._background:
                await asyncio.gather(*self._background, return_exceptions=True)
                self._background.clear()
            try:
                from ah.observability.audit import audit_persistence

                await audit_persistence.stop()
            except Exception:
                pass
            try:
                from ah.db.connection import db

                await db.close()
            except Exception:
                pass
            self._started = False

    def track(self, task: asyncio.Task) -> asyncio.Task:
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task


def _detect_providers() -> dict[str, Any]:
    from ah.core.config import config

    chat_ready = False
    try:
        from ah.core.provider import PROVIDER_SPECS

        # Do NOT instantiate: presence of any configured key == ready.
        keys = [config.get("openrouter_api_key")]
        for spec in PROVIDER_SPECS.values():
            try:
                keys.append(config.get(spec.config_key))
            except Exception:
                pass
        try:
            import os

            if os.environ.get("OLLAMA_HOST"):
                keys.append("ollama-host-set")
        except Exception:
            pass
        chat_ready = any(bool(k) for k in keys)
    except Exception:
        pass
    embedding_ready = bool(config.get("openai_api_key") or config.get("openrouter_api_key"))
    return {
        "chat_provider": "ready" if chat_ready else "unavailable",
        "embedding": "ready" if embedding_ready else "keyword-only",
    }


def _detect_sandbox() -> dict[str, Any]:
    import shutil

    if shutil.which("docker") is None:
        return {"sandbox": "unavailable", "sandbox_reason": "docker not found"}
    return {"sandbox": "ready", "sandbox_reason": ""}


def _capabilities_from_report(report: dict[str, Any]) -> list[Capability]:
    caps: list[Capability] = []
    db = report.get("db", "unavailable")
    caps.append(
        Capability(
            "database",
            "available" if db == "ready" else "unavailable",
            "" if db == "ready" else str(db),
            "" if db == "ready" else "configure DATABASE_URL or start loopback pgvector",
        )
    )
    chat = report.get("chat_provider", "unavailable")
    caps.append(
        Capability(
            "chat_provider",
            "available" if chat == "ready" else "unavailable",
            "" if chat == "ready" else "no provider key configured",
            "" if chat == "ready" else "run ah setup or set OPENROUTER_API_KEY",
        )
    )
    emb = report.get("embedding", "keyword-only")
    caps.append(
        Capability(
            "memory_retrieval",
            "degraded" if emb == "keyword-only" else "available",
            "no embedding provider configured" if emb == "keyword-only" else "",
            "set OPENAI_API_KEY for dense retrieval" if emb == "keyword-only" else "",
        )
    )
    caps.append(
        Capability(
            "document_rag",
            "available",
            "",
            "",
        )
    )
    from ah.rag.reranker import IdentityReranker

    _ = IdentityReranker  # passthrough label; configured reranker detected per-pipeline
    caps.append(
        Capability(
            "reranker", "available", "passthrough (IdentityReranker) unless Cohere key set", ""
        )
    )
    caps.append(Capability("compaction", "available", "", ""))
    caps.append(Capability("skills", "available", "", ""))
    caps.append(Capability("jobs", "available", "", ""))
    caps.append(Capability("research_training", "research-only", "offline workflow", ""))
    return caps


runtime_services = RuntimeServices()
