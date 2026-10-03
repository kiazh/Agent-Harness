"""AgentHarness tools — registry and built-in tools.

Importing this package registers every built-in tool with ``registry``.
"""

from ah.tools import (
    agents,  # noqa: F401 — delegate, list_agents
    builtins,  # noqa: F401 — web_search, web_extract, search_files
    file,  # noqa: F401 — read_file, write_file, list_files
    memory,  # noqa: F401 — remember, recall
    rag,  # noqa: F401 — index_document, search_documents
    terminal,  # noqa: F401 — terminal
)
from ah.tools.base import Tool, ToolRegistry, registry

__all__ = [
    "Tool",
    "ToolRegistry",
    "registry",
    "builtins",
    "file",
    "terminal",
    "memory",
    "rag",
    "agents",
]
