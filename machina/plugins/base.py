"""Plugin lifecycle hooks.

Plugins are duck-typed: :meth:`PluginRegistry.register <machina.plugins.registry.PluginRegistry.register>`
accepts any object with a non-empty ``name``, and
:meth:`PluginRegistry.dispatch <machina.plugins.registry.PluginRegistry.dispatch>`
looks the hook up by name (``pre_agent_run``, ``post_agent_run``,
``on_tool_call``, ``on_tool_result``, ``on_memory_extract``). There is no
required base class; override only the hooks your plugin needs. Each hook
failure is logged and isolates the plugin — it never aborts the agent run.
"""
