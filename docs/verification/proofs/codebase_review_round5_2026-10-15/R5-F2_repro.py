"""R5-F2 minimal reproduction: chunk adjacency after recursive splitting.

Loads the chunker module from the path given as argv[1] and splits a source
consisting of a long uninterrupted word followed by a short word (separated
by a single space) at a small chunk size with overlap. Pre-fix, the forced
split of the long word emits chunks that do not retain the following
separator, so ``_apply_overlap`` glues a suffix of the last forced piece onto
the short word and produces text absent from the source.

Usage: python R5-F2_repro.py <path-to-chunker.py>
Exit code 0 = no fabricated span (fixed), 1 = fabricated span (bug present).
"""

from __future__ import annotations

import importlib.util
import sys


def _load(path: str):
    spec = importlib.util.spec_from_file_location("chunker_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec so @dataclass can resolve the module (py3.12+).
    sys.modules["chunker_under_test"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    path = sys.argv[1]
    splitter_cls = _load(path).RecursiveCharacterTextSplitter

    source = "W" * 40 + " " + "end"
    chunks = splitter_cls(chunk_size=4, chunk_overlap=2).split_text(source)
    print(f"chunker module: {path}")
    print(f"source: {source!r}")
    for chunk in chunks:
        verdict = "substring" if chunk.text in source else "FABRICATED (absent from source)"
        print(f"  chunk {chunk.index}: {chunk.text!r}  -> {verdict}")

    fabricated = [c.text for c in chunks if c.text not in source]
    if fabricated:
        print(f"RESULT: FAIL — fabricated spans {fabricated!r}")
        return 1
    print("RESULT: PASS — every chunk is a source substring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
