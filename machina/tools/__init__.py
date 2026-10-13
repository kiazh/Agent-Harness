"""Machina tools — registry and built-in tools.

Importing this package registers every built-in tool with ``registry``.
"""

from machina.tools import (
    agents,  # noqa: F401 — delegate, list_agents
    builtins,  # noqa: F401 — web_search, web_extract, search_files
    file,  # noqa: F401 — read_file, write_file, list_files
    harness,  # noqa: F401 — read-only feature/configuration/learning inspection
    mcp,  # noqa: F401 — lazy connector catalogs and exact remote calls
    memory,  # noqa: F401 — remember, recall
    rag,  # noqa: F401 — index_document, search_documents
    repo,  # noqa: F401 — repo_stage, repo_activate
    repomap,  # noqa: F401 — learn_directory
    session_recall,  # noqa: F401 — transcript recall
    skills,  # noqa: F401 — skill discovery and reading
    terminal,  # noqa: F401 — terminal
    web_crawl,  # noqa: F401 — bounded multi-page crawl into session RAG
    web_research,  # noqa: F401 — session-scoped public web research
)
from machina.tools.base import Tool, ToolRegistry, registry

__all__ = [
    "Tool",
    "ToolRegistry",
    "registry",
    "builtins",
    "file",
    "harness",
    "terminal",
    "memory",
    "rag",
    "repo",
    "repomap",
    "session_recall",
    "skills",
    "agents",
    "web_crawl",
    "web_research",
]
