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
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from machina.core.background_tasks import drain_background_tasks
from machina.core.cleanup import cancel_and_join as _bounded_join

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from machina.connectors.service import ConnectorService


async def _bounded_cleanup(cleanup: Coroutine[Any, Any, None], timeout: float, label: str) -> None:
    from machina.core.cleanup import bounded_cleanup

    await bounded_cleanup(cleanup, timeout, label)


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
    _shutting_down: bool = False
    capabilities: list[Capability] = field(default_factory=list)
    _connector_service: ConnectorService | None = field(default=None, repr=False)

    def get_connector_service(self) -> ConnectorService:
        """Return this worker's shared inventory without starting connections.

        Configuration reloads from installation-local non-secret durable JSON.
        Other applications' MCP connections are outside this runtime's scope.
        Neither runtime startup nor inspection requires the optional MCP SDK.
        """
        if self._connector_service is None:
            from machina.connectors.oauth import OAuthCoordinator
            from machina.connectors.service import ConnectorService
            from machina.connectors.store import ConnectorStore

            self._connector_service = ConnectorService(
                store=ConnectorStore(),
                oauth_coordinator=OAuthCoordinator(),
            )
            self._connector_service.oauth._service = self._connector_service
        return self._connector_service

    async def shutdown_connectors(self, timeout: float = 10.0) -> None:
        """Retire owned connections without creating an unused service."""
        if self._connector_service is not None:
            oauth = getattr(self._connector_service, "oauth", None)
            if oauth is not None:
                try:
                    await _bounded_cleanup(
                        oauth.shutdown(deadline=min(5.0, timeout)),
                        min(5.0, timeout),
                        "oauth shutdown",
                    )
                except Exception:
                    logger.exception("oauth shutdown failed")
            await _bounded_cleanup(
                self._connector_service.manager.shutdown_all(), timeout, "connector shutdown"
            )

    async def startup(self) -> dict[str, Any]:
        """Connect DB, start audit, load skills, detect providers/sandbox.

        Never raises: every failure becomes a degraded/unavailable capability
        with a reason and next action.
        """
        async with self._lock:
            if self._started:
                return self.status()
            report: dict[str, Any] = {"db": "unavailable", "skills": 0}
            try:
                from machina.db.connection import db

                await db.connect()
                report["db"] = "ready" if db.connected else "unavailable"
            except Exception as e:
                report["db"] = f"unavailable: {e}"
            if report["db"] == "ready":
                try:
                    await db.initialize_runtime_schema()
                    report["personality_growth"] = "ready"
                except Exception:
                    logger.exception("Runtime schema upgrade failed")
                    report["db"] = "unavailable: runtime schema upgrade failed"
                    report["personality_growth"] = "unavailable"
                    await db.close()
            try:
                from machina.observability.audit import audit_persistence

                audit_persistence.start()
                report["audit"] = "ready"
            except Exception as e:
                report["audit"] = f"unavailable: {e}"
            try:
                from machina.skills.registry import skill_registry

                skill_registry.load_all()
                report["skills"] = len(skill_registry.list_skills())
            except Exception as e:
                report["skills_error"] = str(e)
            # Blocking probes (docker subprocess, ollama socket) must not stall
            # the event loop: run detection in worker threads with same timeouts.
            report.update(await asyncio.to_thread(_detect_providers))
            report.update(await asyncio.to_thread(_detect_sandbox))
            report.update(await asyncio.to_thread(_detect_toggles))
            self.capabilities = _capabilities_from_report(report)
            self._started = True
            self._last_report = report
            return self.status()

    async def refresh(self) -> dict[str, Any]:
        """Recompute capabilities after configuration changes.

        m7: also the reconnect point. startup() may have latched "unavailable"
        while the database was down; a refresh retries the connection (and
        the runtime schema upgrade) instead of staying degraded forever.
        """
        async with self._lock:
            report = dict(getattr(self, "_last_report", {}))
            try:
                from machina.db.connection import db

                if not db.connected:
                    try:
                        await db.connect()
                    except Exception:
                        logger.exception("Runtime refresh could not connect to the database")
                if db.connected:
                    try:
                        await db.initialize_runtime_schema()
                        report["db"] = "ready"
                        report["personality_growth"] = "ready"
                    except Exception:
                        logger.exception("Runtime schema upgrade failed")
                        report["db"] = "unavailable: runtime schema upgrade failed"
                        report["personality_growth"] = "unavailable"
                        await db.close()
                else:
                    report["db"] = "unavailable"
            except Exception:
                report["db"] = "unavailable"
            report.update(await asyncio.to_thread(_detect_providers))
            report.update(await asyncio.to_thread(_detect_sandbox))
            report.update(await asyncio.to_thread(_detect_toggles))
            self.capabilities = _capabilities_from_report(report)
            self._last_report = report
            return self.status()

    def status(self) -> dict[str, Any]:
        report = dict(getattr(self, "_last_report", {}))
        report["capabilities"] = [
            {"name": c.name, "state": c.state, "reason": c.reason, "next": c.next_action}
            for c in self.capabilities
        ]
        return report

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Bounded shutdown (AH-AUDIT-024/025).

        Requested cancellation is distinguished from confirmed termination:
        background tasks get a bounded join, then leftovers are quarantined
        (logged + dropped from tracking) instead of awaited forever — a
        cancellation-resistant task cannot exceed the outer deadline. DB
        closure runs after the join with its own bound. New tasks created
        during shutdown are rejected. Caller cancellation still attempts
        remaining service cleanup (100ms per stage), then propagates, so
        restarting cannot acquire audit ownership a second time.
        """
        async with self._lock:
            self._shutting_down = True
            cancellation: asyncio.CancelledError | None = None
            try:
                try:
                    leftovers = await _bounded_join(
                        list(self._background), timeout=timeout, label="runtime background"
                    )
                    if leftovers:
                        logger.warning(
                            "runtime shutdown: %d task(s) quarantined after %ss",
                            len(leftovers),
                            timeout,
                        )
                except asyncio.CancelledError as e:
                    cancellation = e
                finally:
                    self._background.clear()
                try:
                    await self.shutdown_connectors(
                        timeout=min(0.1 if cancellation is not None else 5, timeout)
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("connector shutdown failed")
                try:
                    from machina.observability.audit import audit_persistence

                    await _bounded_cleanup(
                        audit_persistence.stop(),
                        timeout=min(0.1 if cancellation is not None else 5, timeout),
                        label="audit stop",
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("audit shutdown failed")
                try:
                    await drain_background_tasks(
                        timeout=min(0.1 if cancellation is not None else 5, timeout)
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("background drain failed")
                try:
                    from machina.db.connection import db

                    await _bounded_cleanup(
                        db.close(),
                        timeout=min(0.1 if cancellation is not None else 5, timeout),
                        label="database close",
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("database shutdown failed")
                if cancellation is not None:
                    raise cancellation
            finally:
                self._started = False
                self._shutting_down = False

    def track(self, task: asyncio.Task) -> asyncio.Task:
        """Register a maintenance task for bounded shutdown joining."""
        if getattr(self, "_shutting_down", False):
            task.cancel()
            return task
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task


def _detect_providers() -> dict[str, Any]:
    """Per-selected-provider readiness (LP-12): the SELECTED provider's key.

    A wrong-family key (e.g. only an OpenAI key while provider=openrouter)
    reports unavailable for the selected route instead of healthy-by-any-key.
    """
    from machina.core.config import config

    try:
        selected = (config.get("provider") or "openrouter").lower()
    except Exception:
        selected = "openrouter"
    key_ok = False
    reason = ""
    try:
        from machina.core.provider import PROVIDER_SPECS

        if selected == "openrouter":
            key_ok = bool(config.get("openrouter_api_key"))
            reason = "" if key_ok else "OPENROUTER_API_KEY not set"
        elif selected == "ollama":
            key_ok = _ollama_reachable()
            reason = "" if key_ok else "Ollama daemon unreachable"
        elif selected in PROVIDER_SPECS:
            env_name = PROVIDER_SPECS[selected].api_key_env
            key_ok = bool(config.get(PROVIDER_SPECS[selected].config_key))
            reason = "" if key_ok else f"{env_name} not set"
        else:
            reason = f"unknown provider {selected!r}"
    except Exception as e:
        reason = str(e)
    embedding_ready = bool(config.get("openai_api_key") or config.get("openrouter_api_key"))
    return {
        "chat_provider": "ready" if key_ok else "unavailable",
        "chat_provider_reason": reason,
        "chat_provider_selected": selected,
        "embedding": "ready" if embedding_ready else "keyword-only",
    }


def _ollama_reachable(timeout: float = 1.5) -> bool:
    """Cheap TCP probe of the Ollama daemon (never a model call)."""
    import os
    import socket
    from urllib.parse import urlparse

    raw = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host, port = parsed.hostname or "localhost", parsed.port or 11434
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _detect_sandbox() -> dict[str, Any]:
    """Binary presence is not readiness: probe the daemon (LP-12)."""
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return {"sandbox": "unavailable", "sandbox_reason": "docker not found"}
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0:
            return {"sandbox": "ready", "sandbox_reason": ""}
        return {"sandbox": "degraded", "sandbox_reason": "docker daemon unreachable"}
    except Exception:
        return {"sandbox": "degraded", "sandbox_reason": "docker daemon check failed"}


def _detect_toggles() -> dict[str, Any]:
    """Configured vs enforced states for flag-gated features."""
    from machina.core.config import config

    def _flag(name: str) -> bool:
        try:
            return bool(config.get(name))
        except Exception:
            return False

    return {
        "rag_config": "enabled" if _flag("rag_enabled") else "disabled",
        "compaction_config": "enabled"
        if (_flag("auto_compaction_enabled") and _flag("compression_enabled"))
        else "disabled",
        "memory_config": "enabled" if _flag("memory_enabled") else "disabled",
        "extraction_config": "enabled"
        if (_flag("memory_consolidation_enabled") and _flag("memory_enabled"))
        else "disabled",
    }


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
    chat_reason = report.get("chat_provider_reason") or (
        "" if chat == "ready" else "no provider key configured"
    )
    selected = report.get("chat_provider_selected", "")
    caps.append(
        Capability(
            "chat_provider",
            "available" if chat == "ready" else "unavailable",
            f"{chat_reason} (selected: {selected})".strip()
            if chat != "ready"
            else f"selected: {selected}",
            "" if chat == "ready" else "run machina setup or set the selected provider key",
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
            "enabled" if report.get("rag_config", "enabled") == "enabled" else "disabled",
            ""
            if report.get("rag_config", "enabled") == "enabled"
            else "rag_enabled is off (explicit tools still available)",
            "",
        )
    )
    # Reranker identity is chosen lazily per pipeline (AH-AUDIT-036); before
    # any pipeline exists, key presence is the only honest signal. The exact
    # identity is reported by services.status_summary once initialized.
    cohere_configured = False
    try:
        from machina.core.config import config as _cfg

        cohere_configured = bool(_cfg.get("cohere_api_key"))
    except Exception:
        cohere_configured = False
    caps.append(
        Capability(
            "reranker",
            "available",
            "cohere key configured; selected on first RAG use"
            if cohere_configured
            else "passthrough (IdentityReranker); set COHERE_API_KEY to enable Cohere rerank",
            "",
        )
    )
    caps.append(Capability("compaction", report.get("compaction_config", "enabled"), "", ""))
    caps.append(Capability("skills", "available", "", ""))
    caps.append(
        Capability(
            "jobs",
            "available" if db == "ready" else "unavailable",
            "" if db == "ready" else "scheduler needs the database",
            "",
        )
    )
    caps.append(Capability("research_training", "research-only", "offline workflow", ""))
    from machina.core.config import config

    growth_enabled = config.get("personality_growth_enabled") and config.get("memory_enabled")
    caps.append(
        Capability(
            "personality_growth",
            "disabled"
            if not growth_enabled
            else ("active" if report.get("personality_growth") == "ready" else "unavailable"),
            "persistent notes and feedback-driven retrieval policy",
            "",
        )
    )
    return caps


runtime_services = RuntimeServices()
