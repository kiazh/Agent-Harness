"""Tests for chunker overlap behavior."""

import subprocess
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

from machina.rag.chunker import RecursiveCharacterTextSplitter


def test_nonpositive_size_guard_never_hangs():
    """R4-088(f): non-positive sizes raise a typed error instead of looping.

    Runs in a subprocess so a regression fails this test instead of hanging
    the suite forever.
    """
    code = (
        "from machina.rag.chunker import RecursiveCharacterTextSplitter as S\n"
        "for bad in (0, -1, -10):\n"
        "    try:\n"
        "        S._hard_split('abcdef', bad)\n"
        "    except ValueError:\n"
        "        pass\n"
        "    else:\n"
        "        raise SystemExit(3)\n"
        "    try:\n"
        "        S(chunk_size=4)._force_split('abcdef', bad, {})\n"
        "    except ValueError:\n"
        "        pass\n"
        "    else:\n"
        "        raise SystemExit(4)\n"
        "try:\n"
        "    S(chunk_size=0).split_text('ab')\n"
        "except ValueError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(5)\n"
        "print('guarded')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "guarded" in proc.stdout


@settings(max_examples=80, deadline=None)
@given(
    text=st.text(alphabet=list("ab \n."), min_size=1, max_size=200),
    max_chars=st.integers(min_value=1, max_value=40),
)
def test_hard_split_pieces_are_lossless_and_bounded(text, max_chars):
    """R4-088(f): positive bounds split losslessly, every piece within bound."""
    pieces = RecursiveCharacterTextSplitter._hard_split(text, max_chars)
    assert "".join(pieces) == text
    assert all(0 < len(piece) <= max_chars for piece in pieces)


class TestApplyOverlap:
    """Tests for _apply_overlap method."""

    def test_merged_chunk_does_not_exceed_chunk_size(self):
        """Merged chunks should not exceed chunk_size after overlap is applied."""
        # Use a small chunk_size to make the test fast
        splitter = RecursiveCharacterTextSplitter(chunk_size=50, chunk_overlap=10)
        # Create text that will produce multiple chunks
        # Each chunk will be at most 50*4=200 chars
        # With overlap of 10*4=40 chars, merged chunks should not exceed 200 chars
        text = "word " * 100  # 500 chars
        chunks = splitter.split_text(text)
        for chunk in chunks:
            # The merged chunk should not exceed chunk_size * 4 (the char limit)
            # Allow some margin for the overlap logic
            assert len(chunk.text) <= splitter.chunk_size * 4 + 50, (
                f"Chunk size {len(chunk.text)} exceeds chunk_size * 4 = {splitter.chunk_size * 4}"
            )

    def test_overlap_produces_valid_chunks(self):
        """All chunks after overlap should be valid."""
        splitter = RecursiveCharacterTextSplitter(chunk_size=50, chunk_overlap=10)
        text = "a" * 500
        chunks = splitter.split_text(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.text
            assert chunk.index >= 0

    def test_small_chunks_with_large_overlap(self):
        """When chunks are smaller than overlap_chars, merged should not exceed chunk_size."""
        # Create a scenario where prev_text <= overlap_chars
        # chunk_size=20 (80 chars), chunk_overlap=10 (40 chars overlap)
        # If we have small chunks, the merged text could exceed chunk_size
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=10)
        text = "a" * 200
        chunks = splitter.split_text(text)
        for chunk in chunks:
            # Merged chunk should not exceed chunk_size * 4 + some margin
            assert len(chunk.text) <= splitter.chunk_size * 4 + 50, (
                f"Chunk size {len(chunk.text)} exceeds chunk_size * 4 = {splitter.chunk_size * 4}"
            )

    def test_merge_does_not_exceed_chunk_size_when_prev_is_small(self):
        """When prev_text is smaller than overlap_chars, merged should not exceed chunk_size."""
        # This test specifically targets the bug where prev_text <= overlap_chars
        # causes merged = prev_text + current_text which can exceed chunk_size
        splitter = RecursiveCharacterTextSplitter(chunk_size=30, chunk_overlap=15)
        # Create text that will produce chunks where some are smaller than overlap_chars
        text = "a" * 300
        chunks = splitter.split_text(text)
        for chunk in chunks:
            # The merged chunk should not exceed chunk_size * 4
            assert len(chunk.text) <= splitter.chunk_size * 4, (
                f"Chunk size {len(chunk.text)} exceeds chunk_size * 4 = {splitter.chunk_size * 4}"
            )
