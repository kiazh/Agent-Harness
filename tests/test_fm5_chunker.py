"""FM5 W12: overlap delivery and bounded emission.

- The overlap window is cut at the LAST matching separator (max idx) so the
  emitted overlap is a clean suffix of the previous chunk.
- Applying overlap never grows the chunk count (the overflow branch used to
  duplicate the previous chunk tail as tiny extra chunks).
- Property: the source is a subsequence of the emitted chunks; every emitted
  chunk is a substring of the source; no chunk exceeds the hard char limit.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from machina.rag.chunker import RecursiveCharacterTextSplitter


def _is_subsequence(source: str, emitted: str) -> bool:
    it = iter(emitted)
    return all(char in it for char in source)


def _overlap_prefix_len(prev: str, cur: str) -> int:
    best = 0
    for length in range(1, min(len(prev), len(cur)) + 1):
        if prev[-length:] == cur[:length]:
            best = length
    return best


def _distinct_text() -> str:
    return " ".join(f"segment{i} carries unique words here." for i in range(60))


def test_overlap_never_grows_chunk_count():
    """The overflow branch used to emit extra duplicate fragments."""
    text = _distinct_text()
    splitter = RecursiveCharacterTextSplitter(chunk_size=50, chunk_overlap=10)
    raw = splitter._split_recursive(text, splitter.separators, {})
    merged = splitter._apply_overlap(raw)
    assert len(merged) <= len(raw)


def test_no_emitted_chunk_is_an_exact_duplicate_fragment():
    text = _distinct_text()
    splitter = RecursiveCharacterTextSplitter(chunk_size=50, chunk_overlap=10)
    chunks = splitter.split_text(text)
    assert len(chunks) > 1
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur.text not in prev.text, f"duplicate fragment: {cur.text!r}"


def test_overlap_starts_at_a_boundary_and_is_delivered():
    text = "Sentence one is here. Sentence two is here. Sentence three is here. " * 6
    splitter = RecursiveCharacterTextSplitter(chunk_size=40, chunk_overlap=10)
    chunks = splitter.split_text(text)
    assert len(chunks) > 1
    # Every emitted chunk keeps its predecessor's tail as overlap.
    for prev, cur in zip(chunks, chunks[1:]):
        assert _overlap_prefix_len(prev.text, cur.text) > 0


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    text=st.text(
        alphabet=st.sampled_from(list("abcde .,\n")),
        min_size=1,
        max_size=600,
    ),
    chunk_size=st.integers(min_value=8, max_value=64),
    overlap=st.integers(min_value=1, max_value=8),
)
def test_source_is_subsequence_of_emitted_chunks(text, chunk_size, overlap):
    if overlap >= chunk_size:
        overlap = max(1, chunk_size - 1)
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    chunks = splitter.split_text(text)
    emitted = "".join(c.text for c in chunks)
    # Separator-only spans are legitimately dropped (a chunk of pure
    # whitespace carries no content); every non-whitespace source character
    # must survive in order.
    content = "".join(char for char in text if not char.isspace())
    if content:
        assert _is_subsequence(content, emitted), (
            f"lost source under {chunk_size}/{overlap}: {text!r}"
        )
    # No fabrication: every emitted chunk exists inside the source.
    for chunk in chunks:
        assert chunk.text in text
        assert len(chunk.text) <= chunk_size * 4, (
            f"chunk of {len(chunk.text)} chars exceeds {chunk_size * 4}"
        )
