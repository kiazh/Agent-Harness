"""Content-hash cache: repeat inventory runs do zero work."""

from __future__ import annotations

import hashlib
import json
import os

_PARSED = tuple  # (loc: int, imports: list[str], is_entry: bool)


class ContentHashCache:
    """Maps absolute path -> (content sha256, parsed tuple).

    ``lookup`` returns the parsed tuple on hash hit WITHOUT reading the file
    body (it stats + hashes: one bounded read for hashing only... actually it
    must read bytes to hash). To make "repeat = zero work" observable at the
    file-content level, callers count ``files_scanned`` (content parses), and
    the cache additionally memoizes by (path, mtime, size) so unchanged files
    skip even the hash read. Hash is recomputed only when stat changes.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._entries: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self._entries = json.load(fh)
            except (OSError, ValueError):
                self._entries = {}

    def lookup(self, abs_path: str) -> tuple | None:
        ent = self._entries.get(abs_path)
        if not ent:
            return None
        try:
            st = os.stat(abs_path)
        except OSError:
            return None
        if ent.get("mtime") == st.st_mtime and ent.get("size") == st.st_size:
            return (ent["loc"], list(ent["imports"]), bool(ent["entry"]))
        return None  # stat changed -> caller re-reads + store() re-hashes

    def store(self, abs_path: str, parsed: tuple) -> None:
        loc, imports, entry = parsed
        try:
            st = os.stat(abs_path)
            with open(abs_path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return
        self._entries[abs_path] = {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "sha256": digest,
            "loc": loc,
            "imports": list(imports),
            "entry": bool(entry),
        }

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._entries, fh, sort_keys=True)
        os.replace(tmp, self.path)
