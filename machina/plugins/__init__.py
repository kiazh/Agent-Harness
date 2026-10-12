"""Opt-in lifecycle plugins for Machina."""

from machina.plugins.loader import load_plugins
from machina.plugins.registry import PluginRegistry, plugin_registry

__all__ = ["PluginRegistry", "plugin_registry", "load_plugins"]
