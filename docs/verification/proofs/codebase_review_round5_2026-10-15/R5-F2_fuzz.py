"""R5-F2 randomized check: no emitted chunk is fabricated.

For a large number of random (text, chunk_size, overlap, separators) tuples,
split the text and assert that

* every emitted chunk is an exact substring of the source,
* every chunk's tracked ``[start, end)`` span describes its text exactly, and
* no chunk exceeds the hard char bound (``chunk_size * 4``).

Separator sets include the default list, custom literal lists (including ones
that drop '.' / '\\n') and regex separators, and chunk sizes/overlaps that
force heavy recursion and force-splitting.

Note: dropping content characters is legitimate when a caller passes a
*separator* (e.g. separators=["."] deletes periods); that is why this check
asserts no fabrication/oversize rather than subsequence preservation.
"""

from __future__ import annotations

import random

from machina.rag.chunker import RecursiveCharacterTextSplitter

ALPHABETS = ["ab ", "ab \n.", "abcde .,\n", "abcdefghij    \n\n.", "a "]
SEPARATOR_SPECS = [
    (["\n\n", "\n", ". ", " ", ""], False),
    ([" ", "\n", ""], False),
    (["\n", ""], False),
    ([r"\n+", r"[ ]+", ""], True),
    ([r"\s+", ""], True),
    ([""], False),
]


def main(trials: int = 15000, seed: int = 2026) -> int:
    rng = random.Random(seed)
    failures = 0
    for _ in range(trials):
        alphabet = rng.choice(ALPHABETS)
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 400)))
        chunk_size = rng.randint(1, 24)
        overlap = rng.randint(0, 10)
        if overlap >= chunk_size:
            overlap = chunk_size - 1
        separators, is_regex = rng.choice(SEPARATOR_SPECS)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separators=separators,
            is_separator_regex=is_regex,
        )
        chunks = splitter.split_text(text)
        for chunk in chunks:
            if chunk.text not in text:
                print(f"FABRICATED {chunk.text!r} from {text!r} ({chunk_size}/{overlap})")
                failures += 1
                break
            if len(chunk.text) > chunk_size * 4:
                print(f"OVERSIZE {len(chunk.text)} > {chunk_size * 4}: {chunk.text!r}")
                failures += 1
                break
            if (chunk.start is None) != (chunk.end is None):
                print(f"PARTIAL OFFSET {chunk.start}/{chunk.end}: {chunk.text!r}")
                failures += 1
                break
            if chunk.start is not None and text[chunk.start : chunk.end] != chunk.text:
                print(f"SPAN MISMATCH {chunk.start}:{chunk.end} {chunk.text!r} in {text!r}")
                failures += 1
                break
        if failures > 5:
            break

    print(f"trials={trials} seed={seed} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
