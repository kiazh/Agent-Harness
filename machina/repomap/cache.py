"""Content-hash cache: repeat inventory runs skip parsing, never serve stale bytes."""

from __future__ import annotations

import hashlib
import json
import os

_PARSED = tuple  # (loc: int, imports: list[str], is_entry: bool)


class ContentHashCache:
    """Maps absolute path -> (content sha256, parsed tuple).

    ``lookup`` returns the parsed tuple only after re-hashing the file's
    current bytes and matching the stored sha256, so a rewrite that preserves
    mtime and size (a same-stat write) is detected instead of serving stale
    inventory data (R4-088). Only parsing is skipped — every lookup performs
    one bounded read for hashing; a stat mismatch short-circuits before the
    read. ``store`` re-stats and re-hashes the file it records.
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
        expected = ent.get("sha256")
        if not expected:
            return None
        try:
            st = os.stat(abs_path)
            if ent.get("mtime") != st.st_mtime or ent.get("size") != st.st_size:
                return None  # stat changed -> caller re-reads + store() re-hashes
            with open(abs_path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None
        if digest != expected:
            return None  # content changed under an unchanged stat
        return (ent["loc"], list(ent["imports"]), bool(ent["entry"]))

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
