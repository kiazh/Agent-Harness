"""Code-review #4 probe: _read_page reads to EOF even for a tiny page."""
import tempfile, os
from pathlib import Path
import machina.tools.file as fmod

root = Path(tempfile.mkdtemp(prefix="rp-"))
target = root / "big.log"
N = 100_000
with open(target, "w", encoding="utf-8") as fh:
    for i in range(N):
        fh.write(f"line-{i:06d}\n")


class CountingStream:
    def __init__(self, path):
        self._f = open(path, encoding="utf-8", errors="replace")
        self.calls = 0

    def readline(self, n=-1):
        self.calls += 1
        return self._f.readline(n)


s = CountingStream(target)
page, shown, total, exact = fmod._read_page(s, offset=1, limit=10, max_bytes=1_048_576, path="big.log")
print(
    f"file lines={N} page_limit=10 -> readline calls={s.calls} "
    f"total_reported={total} total_exact={exact}"
)
print("FULL-SCAN" if s.calls > 2000 else "BOUNDED")
