"""RepoMap: bounded single-pass stdlib inventory (Stream D).

Uses only ``os.walk`` / ``ast`` / ``hashlib`` / ``json``. No LLM, no network,
no production DB. Safe to run on /Users/shadow/machina itself in seconds:
``.venv``, ``.git`` and other heavy dirs are excluded by default.
"""

from __future__ import annotations

from machina.repomap.cache import ContentHashCache
from machina.repomap.scan import RepoMap, build_repomap

__all__ = ["ContentHashCache", "RepoMap", "build_repomap"]
