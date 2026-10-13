"""Dependency injection container — wires up all singletons for production or testing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from machina.core.context import ContextManager, context_manager
from machina.core.provider import LLMProvider, get_provider
from machina.core.session import SessionManager, session_manager
from machina.core.usage import UsageStore, usage_store
from machina.db.connection import Database, db
from machina.memory.consolidator import MemoryConsolidator
from machina.memory.forgetting import ForgettingModel
from machina.memory.retriever import MemoryRetriever
from machina.memory.scorer import ImportanceScorer
from machina.memory.store import MemoryStore
from machina.rag.pipeline import RAGPipeline
from machina.tools.base import ToolRegistry, registry

if TYPE_CHECKING:
    # Service-locator edge note: the skills registry is resolved lazily
    # (see _resolve_skill_registry) so that importing this module never
    # executes machina.skills itself at import time.
    from machina.skills.registry import SkillRegistry

__all__ = ["Container"]


def _resolve_skill_registry(*, shared: bool) -> SkillRegistry:
    """Resolve a skill registry without an import-time edge to ``machina.skills``.

    Imported inside the function (not at module level) so importing
    ``machina.core.container`` never executes ``machina.skills`` itself;
    initialization order is unchanged.

    Args:
        shared: when True return the global singleton (production wiring);
            when False construct a fresh registry (testing isolation).
    """
    from machina.skills.registry import SkillRegistry, skill_registry

    return skill_registry if shared else SkillRegistry()


@dataclass
class Container:
    """Holds references to all singletons and provides lifecycle management.

    Usage:
        container = Container.production()
        await container.start()
        # ... use container.db, container.session_manager, etc.
        await container.stop()

        # In tests:
        container = Container.testing()
        await container.start()
        # ... use mocks
        await container.stop()
    """

    # Core singletons
    _db: Database = field(default_factory=Database)
    _session_manager: SessionManager = field(default_factory=SessionManager)
    _context_manager: ContextManager = field(default_factory=ContextManager)
    _tool_registry: ToolRegistry = field(default_factory=ToolRegistry)
    _skill_registry: SkillRegistry = field(
        default_factory=lambda: _resolve_skill_registry(shared=False)
    )
    _usage_store: UsageStore = field(default_factory=UsageStore)

    # Memory singletons
    _memory_store: MemoryStore = field(default_factory=MemoryStore)
    _importance_scorer: ImportanceScorer = field(default_factory=ImportanceScorer)
    _forgetting_model: ForgettingModel = field(default_factory=ForgettingModel)
    _memory_retriever: MemoryRetriever = field(default_factory=MemoryRetriever)
    _memory_consolidator: MemoryConsolidator = field(default_factory=MemoryConsolidator)

    # RAG pipeline (lazy — created on start)
    _rag_pipeline: RAGPipeline | None = None

    # Provider (lazy — created on start)
    _provider: LLMProvider | None = None

    # State
    _started: bool = False

    @classmethod
    def production(cls) -> Container:
        """Create a container wired for production use.

        Uses real database, real registries, and the configured LLM provider.
        """
        return cls(
            _db=db,
            _session_manager=session_manager,
            _context_manager=context_manager,
            _tool_registry=registry,
            _skill_registry=_resolve_skill_registry(shared=True),
            _usage_store=usage_store,
        )

    @classmethod
    def testing(cls) -> Container:
        """Create a container wired for testing.

        Uses fresh singleton instances (not the global ones) so tests don't
        pollute global state. The provider is not created automatically.
        """
        return cls()

    @property
    def db(self) -> Database:
        return self._db

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def context_manager(self) -> ContextManager:
        return self._context_manager

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    @property
    def skill_registry(self) -> SkillRegistry:
        return self._skill_registry

    @property
    def usage_store(self) -> UsageStore:
        return self._usage_store

    @property
    def memory_store(self) -> MemoryStore:
        return self._memory_store

    @property
    def importance_scorer(self) -> ImportanceScorer:
        return self._importance_scorer

    @property
    def forgetting_model(self) -> ForgettingModel:
        return self._forgetting_model

    @property
    def memory_retriever(self) -> MemoryRetriever:
        return self._memory_retriever

    @property
    def memory_consolidator(self) -> MemoryConsolidator:
        return self._memory_consolidator

    @property
    def rag_pipeline(self) -> RAGPipeline | None:
        return self._rag_pipeline

    @property
    def provider(self) -> LLMProvider | None:
        return self._provider

    async def start(self) -> None:
        """Initialize all singletons (connect DB, load skills, create provider)."""
        if self._started:
            return

        # Connect database
        await self._db.connect()

        # Load skills
        self._skill_registry.load_all()

        # Create LLM provider (only in production)
        if self._provider is None:
            try:
                self._provider = get_provider()
            except ValueError:
                # No API key — provider stays None
                pass

        # Create RAG pipeline (only in production)
        if self._rag_pipeline is None:
            try:
                self._rag_pipeline = RAGPipeline()
            except ValueError:
                # No API key — RAG pipeline stays None
                pass

        self._started = True

    async def stop(self) -> None:
        """Clean up all singletons."""
        if not self._started:
            return

        # Close database
        await self._db.close()

        # Close provider if it has a close method
        if self._provider and hasattr(self._provider, "close"):
            await self._provider.close()

        # Close RAG pipeline's HTTP clients (embedder + reranker)
        if self._rag_pipeline is not None:
            for attr in ("_embedder", "_reranker"):
                component = getattr(self._rag_pipeline, attr, None)
                if component is not None and hasattr(component, "close"):
                    await component.close()

        self._started = False

    async def reset(self) -> None:
        """Reset all singletons to fresh instances and re-initialize.

        Useful for test teardown or when you want a clean slate.
        """
        await self.stop()

        # Reset global singletons using class methods
        Database.reset()
        SessionManager.reset()
        ContextManager.reset()
        ToolRegistry.reset()
        SkillRegistry.reset()

        # Replace our references with the fresh global singletons
        from machina.core.context import context_manager as fresh_context_manager
        from machina.core.session import session_manager as fresh_session_manager
        from machina.core.usage import usage_store as fresh_usage_store
        from machina.db.connection import db as fresh_db
        from machina.skills.registry import skill_registry as fresh_skill_registry
        from machina.tools.base import registry as fresh_tool_registry

        self._db = fresh_db
        self._session_manager = fresh_session_manager
        self._context_manager = fresh_context_manager
        self._tool_registry = fresh_tool_registry
        self._skill_registry = fresh_skill_registry
        self._usage_store = fresh_usage_store
        self._provider = None
        self._started = False

    async def __aenter__(self) -> Container:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()
