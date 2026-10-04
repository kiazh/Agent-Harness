"""Load explicitly enabled package entry-point plugins."""

from __future__ import annotations

import logging
import os
from importlib.metadata import entry_points

from ah.plugins.registry import PluginRegistry, plugin_registry

logger = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "agent_harness.plugins"


def load_plugins(
    names: list[str] | None = None, registry: PluginRegistry | None = None
) -> list[str]:
    """Load named entry points; no third-party code runs without opt-in."""
    registry = registry or plugin_registry
    if names is None:
        names = [n.strip() for n in os.environ.get("AGENT_HARNESS_PLUGINS", "").split(",") if n.strip()]
    available = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
    loaded = []
    for name in names:
        if name in registry.list():
            continue
        entry = available.get(name)
        if entry is None:
            logger.warning("Plugin %s has no %s entry point", name, ENTRY_POINT_GROUP)
            continue
        try:
            factory = entry.load()
            plugin = factory() if isinstance(factory, type) else factory
            if getattr(plugin, "name", None) != name:
                raise ValueError(f"plugin entry point {name!r} must expose the same plugin name")
            registry.register(plugin)
            loaded.append(name)
        except Exception:
            logger.exception("Could not load plugin %s", name)
    return loaded
