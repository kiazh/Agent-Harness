"""R5-F2: chunk adjacency after recursive splitting.

Concern (R5 fix plan, "Additional concerns" #2): recursive emission clears
``current_text`` and can discard the separator that sat between the recursed
part and the following part. ``_apply_overlap`` then glues a suffix of the
previous chunk onto that next part, producing text that was never contiguous
in the source (a fabricated span).

These tests pin the invariant every consumer relies on:

* every emitted chunk is an exact substring of the source, and
* consecutive emitted chunks remain adjacent in the source (previous end ==
  next start), so any overlap the splitter prepends is itself a source span.

The deterministic case is the smallest reproduction: a long uninterrupted
word (forced through ``_force_split``) followed by a short word separated by
a single space.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from machina.rag.chunker import RecursiveCharacterTextSplitter


def test_recursive_split_never_fabricates_source_text():
    """Long forced-split word + short word must not fabricate ``word+short``.

    Pre-fix, ``_force_split`` emitted ``"W"*8`` and then the following part
    ``"end"`` was accumulated with an empty ``current_text`` (the separator
    before it had been dropped). ``_apply_overlap`` prepended the tail of the
    forced-split chunk, yielding ``"WWWWWWWWend"`` — absent from the source
    ``"W"*40 + " end"``.
    """
    source = "W" * 40 + " " + "end"
    splitter = RecursiveCharacterTextSplitter(chunk_size=4, chunk_overlap=2)
    chunks = splitter.split_text(source)

    assert len(chunks) > 1, "expected the long word to force multiple chunks"
    fabricated = [c.text for c in chunks if c.text not in source]
    assert not fabricated, f"chunks absent from source (fabricated spans): {fabricated!r}"


def test_recursive_boundary_chunks_stay_adjacent_in_source():
    """The overlap merge point must be a real source adjacency, not a bridge."""
    source = "W" * 40 + " " + "end"
    splitter = RecursiveCharacterTextSplitter(chunk_size=4, chunk_overlap=2)
    chunks = splitter.split_text(source)

    for chunk in chunks:
        assert chunk.text in source, f"fabricated chunk: {chunk.text!r}"
        # Every chunk carries a faithful source span when offsets are tracked.
        if chunk.start is not None and chunk.end is not None:
            assert source[chunk.start : chunk.end] == chunk.text


def test_recursive_split_only_drops_real_separator_runs():
    """A double separator must not be silently collapsed into one space.

    ``"x"*40 + "  " + "y"*40`` forces both words through ``_force_split``.
    Overlap must never glue an ``x`` fragment directly to a ``y`` fragment
    (the two spaces were real source characters, so ``x...y`` is fabricated).
    """
    source = "x" * 40 + "  " + "y" * 40
    splitter = RecursiveCharacterTextSplitter(chunk_size=4, chunk_overlap=2)
    chunks = splitter.split_text(source)

    for chunk in chunks:
        assert chunk.text in source, f"fabricated chunk: {chunk.text!r}"


def test_custom_separator_recursion_keeps_real_gap():
    """The recursion may drop a trailing separator *inside* the recursed part.

    Bridging back from ``part_end`` instead of the recursion's actual last
    emission re-attached the wrong gap and dropped the ``\\n`` here, yielding
    ``'cc.bicj '`` (absent from the source). The bridge must start at the
    last sub-chunk's real end.
    """
    source = "acbcd haaa \n\necfda.\nhc\ncc.bicj\n ihg\n.a.ca e\n fbc\nfcgea d.ej\n"
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=2, chunk_overlap=1, separators=[" ", "\n", ""]
    )
    chunks = splitter.split_text(source)
    for chunk in chunks:
        assert chunk.text in source, f"fabricated chunk: {chunk.text!r}"


def test_default_separators_do_not_splice_around_dropped_period_space():
    """``'aaaaaaaaaa.a a'`` at chunk_size=2 fabricated ``'aa.aa'`` pre-fix."""
    source = "aaaaaaaaaa.a a"
    splitter = RecursiveCharacterTextSplitter(chunk_size=2, chunk_overlap=1)
    chunks = splitter.split_text(source)
    assert "aa.aa" not in [c.text for c in chunks], "spliced across a dropped separator"
    for chunk in chunks:
        assert chunk.text in source, f"fabricated chunk: {chunk.text!r}"


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    text=st.text(alphabet=list("ab \n."), min_size=1, max_size=300),
    chunk_size=st.integers(min_value=2, max_value=16),
    overlap=st.integers(min_value=1, max_value=4),
)
def test_property_no_fabricated_chunks_after_overlap(text, chunk_size, overlap):
    """Every emitted chunk is a source substring; adjacent chunks abut."""
    if overlap >= chunk_size:
        overlap = max(1, chunk_size - 1)
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    chunks = splitter.split_text(text)

    for chunk in chunks:
        assert chunk.text in text, f"fabricated chunk {chunk.text!r} from {text!r}"

    tracked = [c for c in chunks if c.start is not None and c.end is not None]
    for chunk in tracked:
        assert 0 <= chunk.start <= chunk.end <= len(text)
        # The tracked span must describe the chunk exactly; overlap may only
        # repeat real source characters, never invent them.
        assert text[chunk.start : chunk.end] == chunk.text, (
            f"span does not describe text in {text!r}: {chunk.text!r}"
        )

    # Chunks advance monotonically. Overlap reaches backward into the
    # previous chunk (cur.start < prev.end) or a dropped separator leaves a
    # safe gap (cur.start >= prev.end); neither may rewind before prev.start.
    for prev, cur in zip(tracked, tracked[1:]):
        assert prev.end <= cur.end, f"non-monotonic ends in {text!r}"
        assert cur.start >= prev.start, f"non-monotonic starts in {text!r}"
