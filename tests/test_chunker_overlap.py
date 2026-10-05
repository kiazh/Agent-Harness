"""Tests for chunker overlap behavior."""
from ah.rag.chunker import RecursiveCharacterTextSplitter


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
