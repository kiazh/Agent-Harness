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
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from machina.core.background_tasks import drain_background_tasks
from machina.core.cleanup import bounded_cleanup
from machina.core.cleanup import cancel_and_join as _bounded_join

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from machina.connectors.service import ConnectorService


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
    _essential_background: set[asyncio.Task] = field(default_factory=set)
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
                    await bounded_cleanup(
                        oauth.shutdown(deadline=min(5.0, timeout)),
                        min(5.0, timeout),
                        "oauth shutdown",
                    )
                except Exception:
                    logger.exception("oauth shutdown failed")
            await bounded_cleanup(
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
        closure runs after the join with its own bound. New maintenance work
        created during shutdown is rejected; essential owner cleanup
        (``track(..., essential=True)``) is exempt and drained bounded so a
        deferred provider close + claim release still executes. Caller
        cancellation still attempts remaining service cleanup (100ms per
        stage), then propagates, so restarting cannot acquire audit ownership
        a second time.

        Ordering: background work is drained *before* the audit writer is
        stopped (mirroring the gateway), so consolidation/learning tasks that
        emit audit events still get persisted instead of being dropped by a
        writer that already stopped.
        """
        async with self._lock:
            self._shutting_down = True
            cancellation: asyncio.CancelledError | None = None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
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
                    # Deferred owner cleanup (provider close + claim release)
                    # registered via track(..., essential=True) must EXECUTE:
                    # it is never cancelled before its first step. Wait for it
                    # within the remaining shutdown budget instead.
                    await self._drain_essential(timeout=max(0.0, deadline - loop.time()))
                except asyncio.CancelledError as e:
                    cancellation = e
                try:
                    await self.shutdown_connectors(
                        timeout=min(0.1 if cancellation is not None else 5, timeout)
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("connector shutdown failed")
                try:
                    # Drain agent background work BEFORE stopping the audit
                    # writer: consolidation/learning tasks emit audit events,
                    # and a submit after the writer stops is dropped
                    # (observability/audit.py). Mirrors the gateway close order
                    # (gateway/server.py) so both shutdown paths preserve the
                    # rows (final-review gateway F5).
                    await drain_background_tasks(
                        timeout=min(0.1 if cancellation is not None else 5, timeout)
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("background drain failed")
                try:
                    from machina.observability.audit import audit_persistence

                    await bounded_cleanup(
                        audit_persistence.stop(),
                        timeout=min(0.1 if cancellation is not None else 5, timeout),
                        label="audit stop",
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("audit shutdown failed")
                try:
                    from machina.rag.embedder import close_shared_embedder

                    await bounded_cleanup(
                        close_shared_embedder(),
                        timeout=min(0.1 if cancellation is not None else 5, timeout),
                        label="embedder close",
                    )
                    # H19: release the active shared RAG generation through its
                    # one lifecycle helper. Closing only ``_rag_pipeline._embedder``
                    # inline left the active reranker client (HTTP/Cohere) open
                    # across shutdown and kept the released generation cached.
                    from machina.tools.rag import close_active_rag_pipeline

                    await bounded_cleanup(
                        close_active_rag_pipeline(),
                        timeout=min(0.1 if cancellation is not None else 5, timeout),
                        label="rag pipeline close",
                    )
                except asyncio.CancelledError as e:
                    cancellation = e
                except Exception:
                    logger.exception("embedder shutdown failed")
                try:
                    from machina.db.connection import db

                    await bounded_cleanup(
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

    def track(self, task: asyncio.Task, *, essential: bool = False) -> asyncio.Task:
        """Register a maintenance task for bounded shutdown joining.

        ``essential=True`` marks owner cleanup (a deferred provider close +
        claim release). Essential cleanup registered while shutdown is already
        in progress is NOT cancelled: killing it before its first step leaks
        the provider and strands the session claim until TTL expiry. It is
        drained bounded by :meth:`shutdown` instead. Non-essential work keeps
        the documented reject policy: new background work scheduled during
        shutdown is cancelled immediately.
        """
        if essential:
            self._essential_background.add(task)
            task.add_done_callback(self._essential_background.discard)
            return task
        if getattr(self, "_shutting_down", False):
            task.cancel()
            return task
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _drain_essential(self, *, timeout: float) -> None:
        """Wait (never cancel) for owner cleanup registered during shutdown.

        Deferred closes release the session claim and close the owned provider;
        cancelling one mid-flight leaks the provider and falls back to claim
        TTL expiry. A close that outlives the shutdown budget is left running
        (its claim still expires via TTL) and reported — never cancelled here.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pending = [task for task in self._essential_background if not task.done()]
            if not pending:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "runtime shutdown: %d essential cleanup task(s) still pending; "
                    "their claims fall back to TTL expiry",
                    len(pending),
                )
                return
            await asyncio.wait(pending, timeout=remaining)


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
        elif selected == "custom":
            # Any OpenAI-compatible endpoint: the base URL is what's required;
            # the key is optional (keyless local servers are a supported mode).
            if str(config.get("custom_base_url") or "").strip():
                key_ok = True
                if not config.get("custom_api_key"):
                    reason = "no key (local endpoint)"
            else:
                reason = "custom_base_url not set"
        elif selected == "chatgpt":
            # ChatGPT-account usage (Codex protocol): readiness comes from the
            # auth store (Machina's token file or an existing Codex login),
            # never from an env key.
            from machina.security.chatgpt_auth import status as _chatgpt_status

            st = _chatgpt_status()
            if st.get("signed_in"):
                key_ok = True
                if st.get("expired"):
                    reason = "session expired; refreshes on the next call"
                elif st.get("source") == "codex":
                    reason = "via Codex login (run `machina auth login --import-codex` to copy it)"
            else:
                reason = "not signed in - run `machina auth login` (or --import-codex)"
        elif selected == "copilot":
            # GitHub Copilot seat: readiness comes from the auth store
            # (Machina's token file or the `gh auth token` credential).
            from machina.security.copilot_auth import status as _copilot_status

            st = _copilot_status()
            if st.get("signed_in"):
                key_ok = True
                if st.get("source") == "gh":
                    reason = (
                        "via gh CLI login (run `machina auth copilot login --import-gh` to copy it)"
                    )
            else:
                reason = "not signed in - run `machina auth copilot login` (or --import-gh)"
        elif selected == "ollama":
            key_ok = _ollama_reachable()
            reason = "" if key_ok else "Ollama daemon unreachable"
        elif selected == "anthropic":
            # First-class API-key vendor: anthropic has no PROVIDER_SPECS
            # entry (it is a native transport, not an OpenAI-compatible
            # direct provider), so it needs an explicit arm or the readiness
            # ladder calls it an unknown provider (final-review P2-1).
            key_ok = bool(config.get("anthropic_api_key"))
            reason = "" if key_ok else "ANTHROPIC_API_KEY not set"
        elif selected in PROVIDER_SPECS:
            env_name = PROVIDER_SPECS[selected].api_key_env
            key_ok = bool(config.get(PROVIDER_SPECS[selected].config_key))
            reason = "" if key_ok else f"{env_name} not set"
        else:
            readiness = _named_provider_readiness(selected)
            if readiness is None:
                reason = f"unknown provider {selected!r}"
            else:
                key_ok, reason = readiness
    except Exception as e:
        reason = str(e)
    try:
        from machina.rag.embedder import resolve_embedding_settings

        embedding = resolve_embedding_settings()
        embedding_ready = embedding.kind != "none"
        embedding_detail = (
            f"{embedding.kind}:{embedding.model}" if embedding_ready else embedding.reason
        )
    except Exception as e:
        embedding_ready, embedding_detail = False, str(e)
    return {
        "chat_provider": "ready" if key_ok else "unavailable",
        "chat_provider_reason": reason,
        "chat_provider_selected": selected,
        "embedding": "ready" if embedding_ready else "keyword-only",
        "embedding_detail": embedding_detail,
    }


def _tcp_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    """Cheap TCP connect probe (never a protocol/API call)."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _endpoint_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """Host:port reachability for an http(s) endpoint URL."""
    try:
        parsed = urlparse(str(base_url or ""))
    except ValueError:
        return False
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _tcp_reachable(host, port, timeout)


def _named_provider_readiness(name: str) -> tuple[bool, str] | None:
    """Readiness for a user-added provider: (ready, reason), None when absent.

    Honest three-state surface: "ready" (reachable, key set or keyless local),
    "no key" (remote endpoint without a credential), "unreachable" (probe
    failed). A keyless loopback endpoint reports ready with a note. The probe
    is a bounded TCP connect, never a model call.
    """
    from machina.security.providers import (
        get_provider_entry,
        is_loopback_base_url,
        resolve_provider_key,
    )

    entry = get_provider_entry(name)
    if entry is None:
        return None
    base = str(entry.get("base_url") or "")
    if not base:
        return False, "base_url missing from the provider store"
    key = resolve_provider_key(entry)
    if not key and not is_loopback_base_url(base):
        return False, f"no key set ({entry.get('api_key_env')}); add it with /keys or /provider"
    if not _endpoint_reachable(base):
        return False, f"{base} unreachable"
    if not key:
        return True, "no key (local endpoint)"
    return True, ""


def _ollama_reachable(timeout: float = 1.5) -> bool:
    """Cheap TCP probe of the Ollama daemon (never a model call)."""
    import os

    raw = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host, port = parsed.hostname or "localhost", parsed.port or 11434
    except ValueError:
        return False
    return _tcp_reachable(host, port, timeout)


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
    emb_detail = str(report.get("embedding_detail") or "")
    if emb == "keyword-only":
        emb_reason = emb_detail or "no embedding provider configured"
        emb_next = (
            "set OPENAI_API_KEY, configure MACHINA_EMBEDDING_BASE_URL, or enable a "
            "local runtime (pip install 'Machina[embeddings-onnx]' or "
            "'Machina[embeddings-local]' and set MACHINA_EMBEDDING_BACKEND) for "
            "dense retrieval"
        )
    else:
        emb_reason = f"dense retrieval via {emb_detail}" if emb_detail else ""
        emb_next = ""
    caps.append(
        Capability(
            "memory_retrieval",
            "degraded" if emb == "keyword-only" else "available",
            emb_reason,
            emb_next,
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
    # any pipeline exists, the resolved selection is the only honest signal.
    # The exact identity is reported by services.status_summary once initialized.
    try:
        from machina.rag.reranker import resolve_reranker_selection

        reranker_selection = resolve_reranker_selection()
        if reranker_selection.kind == "none":
            reranker_reason = (
                "passthrough; install 'Machina[rerank]' for the local ONNX cross-encoder "
                "or set COHERE_API_KEY"
            )
        else:
            reranker_reason = (
                f"{reranker_selection.kind} reranker configured; selected on first RAG use"
            )
    except Exception as e:
        reranker_reason = f"reranker selection unavailable: {e}"
    caps.append(Capability("reranker", "available", reranker_reason, ""))
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
