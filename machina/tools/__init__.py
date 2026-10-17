"""Machina tools — registry and built-in tools.

Importing this package registers every built-in tool with ``registry``.
"""

from machina.tools import (
    agents,  # delegate, list_agents
    artifacts,  # artifact_read, artifact_search, artifact_describe
    builtins,  # web_search, web_extract, search_files
    code_intelligence,  # read-only workspace code intelligence
    file,  # read_file, write_file, list_files
    harness,  # read-only feature/configuration/learning inspection
    mcp,  # noqa: F401 — lazy connector catalogs and exact remote calls
    memory,  # remember, recall
    rag,  # index_document, search_documents
    repo,  # repo_stage, repo_activate
    repomap,  # learn_directory
    session_recall,  # transcript recall
    skills,  # skill discovery and reading
    terminal,  # terminal
    web_crawl,  # bounded multi-page crawl into session RAG
    web_research,  # session-scoped public web research
)
from machina.tools.base import Tool, ToolRegistry, registry

__all__ = [
    "Tool",
    "ToolRegistry",
    "registry",
    "builtins",
    "artifacts",
    "code_intelligence",
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
