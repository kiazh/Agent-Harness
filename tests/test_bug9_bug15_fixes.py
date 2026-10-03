"""Tests for Bug 9 (task reference) and Bug 15 (lazy config loading) fixes."""
import asyncio
import importlib
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _get_config_module():
    """Return the actual ah.core.config module object (not the Config instance).

    ``import ah.core.config as m`` binds to the Config instance because
    ``ah/core/__init__.py`` does ``from ah.core.config import config`` and
    the module's ``__getattr__`` returns it.  Use ``sys.modules`` instead.
    """
    import ah.core.config  # noqa: F401 — ensure it's imported
    return sys.modules["ah.core.config"]


# ---------------------------------------------------------------------------
# Bug 9: _schedule_memory_consolidation must keep a strong reference to the
# asyncio.Task so it is never garbage-collected mid-execution.
# ---------------------------------------------------------------------------


class TestMemoryConsolidationTaskReference:
    """Verify that scheduled consolidation tasks are tracked and not GC'd."""

    @pytest.mark.asyncio
    async def test_task_is_stored_in_consolidation_tasks(self):
        """The scheduled task must appear in _consolidation_tasks."""
        from ah.core.agent import BaseReActAgent

        consolidator = MagicMock()
        consolidator.consolidate_session = AsyncMock(return_value=[])

        agent = BaseReActAgent(memory_consolidator=consolidator)
        session_id = uuid.uuid4()

        agent._schedule_memory_consolidation(session_id)

        assert len(agent._consolidation_tasks) == 1
        task = next(iter(agent._consolidation_tasks))
        assert isinstance(task, asyncio.Task)

    @pytest.mark.asyncio
    async def test_task_is_removed_after_completion(self):
        """Once the task finishes it must be discarded from the set."""
        from ah.core.agent import BaseReActAgent

        consolidator = MagicMock()
        consolidator.consolidate_session = AsyncMock(return_value=[])

        agent = BaseReActAgent(memory_consolidator=consolidator)
        session_id = uuid.uuid4()

        agent._schedule_memory_consolidation(session_id)
        task = next(iter(agent._consolidation_tasks))

        # Let the task run to completion
        await asyncio.wait_for(task, timeout=5)

        # The done-callback should have removed it
        assert len(agent._consolidation_tasks) == 0

    @pytest.mark.asyncio
    async def test_task_actually_completes(self):
        """The consolidation coroutine must run to completion (not be GC'd)."""
        from ah.core.agent import BaseReActAgent

        consolidator = MagicMock()
        consolidator.consolidate_session = AsyncMock(return_value=["mem1", "mem2"])

        agent = BaseReActAgent(memory_consolidator=consolidator)
        session_id = uuid.uuid4()

        agent._schedule_memory_consolidation(session_id)

        # Give the event loop a chance to run the task
        await asyncio.sleep(0.1)

        consolidator.consolidate_session.assert_awaited_once_with(
            session_id=session_id,
            agent_id=agent.agent_id,
        )

    @pytest.mark.asyncio
    async def test_no_task_when_no_consolidator(self):
        """No task should be created when memory_consolidator is None."""
        from ah.core.agent import BaseReActAgent

        agent = BaseReActAgent(memory_consolidator=None)
        session_id = uuid.uuid4()

        agent._schedule_memory_consolidation(session_id)

        assert len(agent._consolidation_tasks) == 0

    @pytest.mark.asyncio
    async def test_multiple_tasks_tracked_independently(self):
        """Multiple scheduled tasks must all be tracked."""
        from ah.core.agent import BaseReActAgent

        consolidator = MagicMock()
        consolidator.consolidate_session = AsyncMock(return_value=[])

        agent = BaseReActAgent(memory_consolidator=consolidator)

        for _ in range(3):
            agent._schedule_memory_consolidation(uuid.uuid4())

        assert len(agent._consolidation_tasks) == 3

        # Let them all finish
        await asyncio.gather(*list(agent._consolidation_tasks))

        assert len(agent._consolidation_tasks) == 0


# ---------------------------------------------------------------------------
# Bug 15: config = Config.load() must not run at import time.
# ---------------------------------------------------------------------------


class TestLazyConfigLoading:
    """Verify that Config.load() is deferred until first access."""

    def setup_method(self):
        """Save and reset the config singleton before each test."""
        config_module = _get_config_module()
        self._config_module = config_module
        self._saved_config = config_module._config
        config_module._config = None

    def teardown_method(self):
        """Restore the config singleton after each test."""
        self._config_module._config = self._saved_config

    def test_config_not_loaded_at_import(self):
        """After reset, _config should be None (not yet loaded)."""
        assert self._config_module._config is None

    def test_get_config_loads_and_caches(self):
        """get_config() must load the config and return the same object."""
        cfg1 = self._config_module.get_config()
        assert cfg1 is not None
        assert isinstance(cfg1, self._config_module.Config)

        # Second call should return the cached instance
        cfg2 = self._config_module.get_config()
        assert cfg1 is cfg2

    def test_module_getattr_returns_config(self):
        """`from ah.core.config import config` must still work via __getattr__."""
        cfg = self._config_module.config
        assert isinstance(cfg, self._config_module.Config)

    def test_config_reset_clears_cache(self):
        """Config.reset() must clear the cached config."""
        # Force a load
        _ = self._config_module.get_config()
        assert self._config_module._config is not None

        self._config_module.Config.reset()
        assert self._config_module._config is None

    def test_getattr_raises_for_unknown_attribute(self):
        """Accessing a non-existent attribute must raise AttributeError."""
        with pytest.raises(AttributeError):
            _ = self._config_module.nonexistent_attribute

    def test_config_load_called_only_on_first_access(self):
        """Config.load() must be called exactly once, on first access."""
        with patch.object(self._config_module.Config, "load", wraps=self._config_module.Config.load) as mock_load:
            # Access config multiple times
            _ = self._config_module.config
            _ = self._config_module.config
            _ = self._config_module.get_config()

            # load() should have been called only once
            mock_load.assert_called_once()
