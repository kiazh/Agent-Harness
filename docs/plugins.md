# AgentHarness plugins

Plugins are installed Python packages with an `agent_harness.plugins` entry
point. They are loaded only when named in `AGENT_HARNESS_PLUGINS` (comma
separated). Each plugin must expose an object or class whose `name` equals its
entry point name. Hook failures and timeouts are logged and do not stop the
agent turn. The default hook timeout is five seconds; set
`AGENT_HARNESS_PLUGIN_TIMEOUT_SECONDS` to change it.

```toml
[project.entry-points."agent_harness.plugins"]
sample = "sample_plugin:SamplePlugin"
```

```python
from ah.plugins.base import Plugin

class SamplePlugin(Plugin):
    name = "sample"

    async def pre_agent_run(self, session, message):
        ...

    async def post_agent_run(self, session, response):
        ...

    async def on_tool_call(self, tool_name, args):
        ...

    async def on_tool_result(self, tool_name, result):
        ...

    async def on_memory_extract(self, memories):
        ...
```

Hooks receive live objects. Install and enable only plugins you trust.
