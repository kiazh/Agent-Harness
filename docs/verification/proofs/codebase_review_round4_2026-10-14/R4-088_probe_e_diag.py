"""R4-088(e) diagnosis: which file misses the digest-verified cache and why."""

import hashlib
import os

from machina.repomap.cache import ContentHashCache
from machina.repomap.scan import build_repomap


def explain(cache):
    for path, ent in cache._entries.items():
        try:
            st = os.stat(path)
        except OSError as e:
            print(f"STAT-FAIL {path}: {e}")
            continue
        if ent.get("mtime") != st.st_mtime or ent.get("size") != st.st_size:
            print(f"STAT-CHANGED {path}")
            continue
        try:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError as e:
            print(f"READ-FAIL {path}: {e}")
            continue
        if digest != ent.get("sha256"):
            print(f"DIGEST-CHANGED {path}")


cache = ContentHashCache()
first = build_repomap(".", cache=cache)
print(f"first files_scanned={first.files_scanned} entries={len(cache._entries)}")
explain(cache)
second = build_repomap(".", cache=cache)
print(f"second files_scanned={second.files_scanned}")
explain(cache)
third = build_repomap(".", cache=cache)
print(f"third files_scanned={third.files_scanned}")
