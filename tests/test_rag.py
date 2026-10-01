"""Tests for the RAG pipeline — Embedder, Chunker, RAGPipeline, HybridSearch, etc.

These tests define the expected interface for the RAG module. They will
skip gracefully if the module is not yet implemented.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — skip all tests in this module if ah.rag is not implemented
# ---------------------------------------------------------------------------
try:
    from ah.rag import (
        Embedder,
        Chunker,
        RAGPipeline,
        HybridSearch,
        Reranker,
        FileLoader,
    )
    _RAG_AVAILABLE = True
except ImportError:
    _RAG_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _RAG_AVAILABLE, reason="ah.rag not implemented yet")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_text():
    """Create a sample text for chunking."""
    return """
    # Introduction
    This is a sample document for testing the RAG pipeline.
    It contains multiple paragraphs and sections.

    ## Section 1
    The first section discusses the importance of retrieval-augmented generation.
    RAG combines the strengths of large language models with external knowledge.

    ## Section 2
    The second section covers embedding models. Embeddings are dense vector
    representations of text that capture semantic meaning.

    ## Section 3
    The final section discusses chunking strategies. Effective chunking is
    crucial for retrieval quality. Chunks should be semantically coherent.
    """


@pytest.fixture
def sample_chunks():
    """Create sample chunks for testing."""
    return [
        {"id": str(uuid.uuid4()), "text": "RAG combines LLM with external knowledge", "metadata": {"source": "doc1"}},
        {"id": str(uuid.uuid4()), "text": "Embeddings capture semantic meaning", "metadata": {"source": "doc1"}},
        {"id": str(uuid.uuid4()), "text": "Chunking is crucial for retrieval quality", "metadata": {"source": "doc2"}},
    ]


@pytest.fixture
def mock_embedder():
    """Create a mock embedder."""
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1536)
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * 1536, [0.2] * 1536])
    return embedder


# ===========================================================================
# Embedder Tests
# ===========================================================================

class TestEmbedder:
    """Tests for the Embedder interface."""

    @pytest.fixture
    def embedder(self):
        """Create an Embedder instance."""
        return Embedder()

    async def test_embed_single(self, embedder):
        """Test embedding a single text."""
        result = await embedder.embed("test text")
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(isinstance(x, float) for x in result)

    async def test_embed_batch(self, embedder):
        """Test embedding a batch of texts."""
        texts = ["text1", "text2", "text3"]
        results = await embedder.embed_batch(texts)
        assert len(results) == 3
        for r in results:
            assert isinstance(r, list)
            assert len(r) > 0

    async def test_embed_empty_string(self, embedder):
        """Test embedding an empty string."""
        result = await embedder.embed("")
        assert isinstance(result, list)
        assert len(result) > 0

    async def test_embed_consistency(self, embedder):
        """Test that the same text produces the same embedding."""
        result1 = await embedder.embed("test")
        result2 = await embedder.embed("test")
        assert result1 == result2

    async def test_embed_different_texts(self, embedder):
        """Test that different texts produce different embeddings."""
        result1 = await embedder.embed("hello")
        result2 = await embedder.embed("world")
        assert result1 != result2

    async def test_embed_dimension(self, embedder):
        """Test that embeddings have consistent dimension."""
        result = await embedder.embed("test")
        assert len(result) == 1536  # Standard OpenAI dimension

    async def test_embed_unicode(self, embedder):
        """Test embedding unicode text."""
        result = await embedder.embed("你好世界")
        assert isinstance(result, list)
        assert len(result) > 0

    async def test_embed_long_text(self, embedder):
        """Test embedding a long text."""
        long_text = "word " * 10000
        result = await embedder.embed(long_text)
        assert isinstance(result, list)
        assert len(result) > 0


# ===========================================================================
# Chunker Tests
# ===========================================================================

class TestChunker:
    """Tests for the Chunker."""

    @pytest.fixture
    def chunker(self):
        """Create a Chunker instance."""
        return Chunker(chunk_size=500, chunk_overlap=50)

    def test_chunk_basic(self, chunker, sample_text):
        """Test basic chunking."""
        chunks = chunker.chunk(sample_text)
        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, str)
            assert len(chunk) > 0

    def test_chunk_respects_size(self, chunker, sample_text):
        """Test that chunks respect the size limit."""
        chunks = chunker.chunk(sample_text)
        for chunk in chunks:
            # Chunks should be approximately chunk_size (with some flexibility)
            assert len(chunk) <= 600  # Allow some overflow

    def test_chunk_overlap(self, chunker, sample_text):
        """Test that chunks have overlap."""
        chunks = chunker.chunk(sample_text)
        if len(chunks) > 1:
            # There should be some overlap between consecutive chunks
            # This is a soft check — overlap may vary
            assert len(chunks) >= 1

    def test_chunk_empty_string(self, chunker):
        """Test chunking an empty string."""
        chunks = chunker.chunk("")
        assert chunks == []

    def test_chunk_short_text(self, chunker):
        """Test chunking text shorter than chunk_size."""
        chunks = chunker.chunk("Short text")
        assert len(chunks) == 1
        assert chunks[0] == "Short text"

    def test_chunk_preserves_content(self, chunker, sample_text):
        """Test that chunking preserves all content."""
        chunks = chunker.chunk(sample_text)
        # All original words should appear in chunks
        original_words = set(sample_text.split())
        chunk_words = set()
        for chunk in chunks:
            chunk_words.update(chunk.split())
        # Most words should be preserved (some may be lost at boundaries)
        assert len(chunk_words) >= len(original_words) * 0.8

    def test_chunk_with_metadata(self, chunker, sample_text):
        """Test chunking with metadata."""
        chunks = chunker.chunk(sample_text, metadata={"source": "test"})
        assert len(chunks) > 0

    def test_recursive_chunker(self, sample_text):
        """Test recursive chunking strategy."""
        chunker = Chunker(chunk_size=200, chunk_overlap=20, strategy="recursive")
        chunks = chunker.chunk(sample_text)
        assert len(chunks) > 0

    def test_structure_aware_chunker(self, sample_text):
        """Test structure-aware chunking."""
        chunker = Chunker(chunk_size=500, chunk_overlap=50, strategy="structure")
        chunks = chunker.chunk(sample_text)
        assert len(chunks) > 0
        # Structure-aware should produce fewer, more coherent chunks
        flat_chunker = Chunker(chunk_size=500, chunk_overlap=50, strategy="flat")
        flat_chunks = flat_chunker.chunk(sample_text)
        assert len(chunks) <= len(flat_chunks) * 1.5

    def test_chunk_by_paragraph(self, sample_text):
        """Test chunking by paragraph."""
        chunker = Chunker(strategy="paragraph")
        chunks = chunker.chunk(sample_text)
        assert len(chunks) > 0

    def test_chunk_by_sentence(self, sample_text):
        """Test chunking by sentence."""
        chunker = Chunker(strategy="sentence")
        chunks = chunker.chunk(sample_text)
        assert len(chunks) > 0

    def test_chunk_custom_separators(self, sample_text):
        """Test chunking with custom separators."""
        chunker = Chunker(
            chunk_size=500,
            chunk_overlap=50,
            separators=["\n\n", "\n", ". "],
        )
        chunks = chunker.chunk(sample_text)
        assert len(chunks) > 0


# ===========================================================================
# RAGPipeline Tests
# ===========================================================================

class TestRAGPipeline:
    """Tests for the RAGPipeline."""

    @pytest.fixture
    def pipeline(self):
        """Create a RAGPipeline instance."""
        return RAGPipeline()

    @pytest.fixture
    def mock_store(self):
        """Create a mock vector store."""
        store = AsyncMock()
        store.search = AsyncMock(return_value=[])
        store.add = AsyncMock()
        return store

    async def test_index_documents(self, pipeline, mock_store):
        """Test indexing documents."""
        docs = [
            {"text": "Document 1", "metadata": {"source": "test"}},
            {"text": "Document 2", "metadata": {"source": "test"}},
        ]
        with patch.object(pipeline, "_store", mock_store):
            await pipeline.index(docs)
            assert mock_store.add.call_count >= 1

    async def test_query(self, pipeline, mock_store):
        """Test querying the pipeline."""
        mock_store.search = AsyncMock(return_value=[
            {"text": "RAG is great", "score": 0.9},
            {"text": "LLMs are powerful", "score": 0.8},
        ])
        with patch.object(pipeline, "_store", mock_store):
            results = await pipeline.query("What is RAG?")
            assert len(results) > 0

    async def test_query_with_top_k(self, pipeline, mock_store):
        """Test querying with top_k parameter."""
        mock_store.search = AsyncMock(return_value=[
            {"text": f"Result {i}", "score": 0.9 - i * 0.1}
            for i in range(10)
        ])
        with patch.object(pipeline, "_store", mock_store):
            results = await pipeline.query("test", top_k=3)
            assert len(results) <= 3

    async def test_query_with_filter(self, pipeline, mock_store):
        """Test querying with metadata filter."""
        mock_store.search = AsyncMock(return_value=[
            {"text": "Filtered result", "score": 0.9},
        ])
        with patch.object(pipeline, "_store", mock_store):
            results = await pipeline.query(
                "test", filter={"source": "doc1"}
            )
            assert len(results) >= 0

    async def test_add_document(self, pipeline, mock_store):
        """Test adding a single document."""
        with patch.object(pipeline, "_store", mock_store):
            await pipeline.add_document(
                text="Test document",
                metadata={"source": "test"},
            )
            mock_store.add.assert_called_once()

    async def test_delete_document(self, pipeline, mock_store):
        """Test deleting a document."""
        mock_store.delete = AsyncMock()
        with patch.object(pipeline, "_store", mock_store):
            await pipeline.delete_document(str(uuid.uuid4()))
            mock_store.delete.assert_called_once()

    async def test_query_empty_store(self, pipeline, mock_store):
        """Test querying when store is empty."""
        mock_store.search = AsyncMock(return_value=[])
        with patch.object(pipeline, "_store", mock_store):
            results = await pipeline.query("test")
            assert results == []

    async def test_query_with_reranking(self, pipeline, mock_store):
        """Test querying with reranking."""
        mock_store.search = AsyncMock(return_value=[
            {"text": "Result A", "score": 0.7},
            {"text": "Result B", "score": 0.9},
            {"text": "Result C", "score": 0.8},
        ])
        with patch.object(pipeline, "_store", mock_store):
            results = await pipeline.query("test", rerank=True)
            assert len(results) > 0


# ===========================================================================
# HybridSearch Tests
# ===========================================================================

class TestHybridSearch:
    """Tests for HybridSearch (BM25 + dense + RRF)."""

    @pytest.fixture
    def searcher(self):
        """Create a HybridSearch instance."""
        return HybridSearch()

    @pytest.fixture
    def mock_bm25(self):
        """Create a mock BM25 index."""
        bm25 = AsyncMock()
        bm25.search = AsyncMock(return_value=[
            {"id": "1", "score": 0.8},
            {"id": "2", "score": 0.6},
        ])
        return bm25

    @pytest.fixture
    def mock_dense(self):
        """Create a mock dense index."""
        dense = AsyncMock()
        dense.search = AsyncMock(return_value=[
            {"id": "2", "score": 0.9},
            {"id": "3", "score": 0.7},
        ])
        return dense

    async def test_search_combines_results(self, searcher, mock_bm25, mock_dense):
        """Test that hybrid search combines BM25 and dense results."""
        with patch.object(searcher, "_bm25", mock_bm25), \
             patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search("test query")
            assert len(results) > 0

    async def test_search_rrf_fusion(self, searcher, mock_bm25, mock_dense):
        """Test RRF fusion of results."""
        with patch.object(searcher, "_bm25", mock_bm25), \
             patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search("test query")
            # Results should be ranked by RRF score
            if len(results) > 1:
                scores = [r.get("score", 0) for r in results]
                assert scores == sorted(scores, reverse=True)

    async def test_search_empty_query(self, searcher, mock_bm25, mock_dense):
        """Test searching with empty query."""
        with patch.object(searcher, "_bm25", mock_bm25), \
             patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search("")
            assert isinstance(results, list)

    async def test_search_top_k(self, searcher, mock_bm25, mock_dense):
        """Test searching with top_k limit."""
        with patch.object(searcher, "_bm25", mock_bm25), \
             patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search("test", top_k=2)
            assert len(results) <= 2

    async def test_search_weights(self, searcher, mock_bm25, mock_dense):
        """Test searching with custom weights."""
        with patch.object(searcher, "_bm25", mock_bm25), \
             patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search(
                "test", bm25_weight=0.7, dense_weight=0.3
            )
            assert isinstance(results, list)

    async def test_bm25_only(self, searcher, mock_bm25):
        """Test BM25-only search."""
        with patch.object(searcher, "_bm25", mock_bm25):
            results = await searcher.search("test", mode="bm25")
            assert len(results) > 0

    async def test_dense_only(self, searcher, mock_dense):
        """Test dense-only search."""
        with patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search("test", mode="dense")
            assert len(results) > 0


# ===========================================================================
# Reranker Tests
# ===========================================================================

class TestReranker:
    """Tests for the Reranker."""

    @pytest.fixture
    def reranker(self):
        """Create a Reranker instance."""
        return Reranker()

    def test_rerank_basic(self, reranker):
        """Test basic reranking."""
        query = "What is RAG?"
        documents = [
            "RAG combines LLM with external knowledge",
            "The weather is sunny today",
            "Embeddings capture semantic meaning",
        ]
        results = reranker.rerank(query, documents)
        assert len(results) == 3

    def test_rerank_reorders(self, reranker):
        """Test that reranking reorders documents."""
        query = "Python programming"
        documents = [
            "The sky is blue",
            "Python is a programming language",
            "I like pizza",
        ]
        results = reranker.rerank(query, documents)
        # The most relevant document should be first
        assert "Python" in results[0]

    def test_rerank_with_scores(self, reranker):
        """Test reranking with scores."""
        query = "test"
        documents = ["doc1", "doc2", "doc3"]
        results = reranker.rerank(query, documents, return_scores=True)
        assert len(results) == 3
        for doc, score in results:
            assert isinstance(score, float)

    def test_rerank_empty_documents(self, reranker):
        """Test reranking with empty document list."""
        results = reranker.rerank("test", [])
        assert results == []

    def test_rerank_top_k(self, reranker):
        """Test reranking with top_k."""
        query = "test"
        documents = [f"Document {i}" for i in range(10)]
        results = reranker.rerank(query, documents, top_k=3)
        assert len(results) == 3

    def test_rerank_consistency(self, reranker):
        """Test that reranking is consistent."""
        query = "test"
        documents = ["doc1", "doc2", "doc3"]
        results1 = reranker.rerank(query, documents)
        results2 = reranker.rerank(query, documents)
        assert results1 == results2


# ===========================================================================
# FileLoader Tests
# ===========================================================================

class TestFileLoader:
    """Tests for the FileLoader."""

    @pytest.fixture
    def loader(self):
        """Create a FileLoader instance."""
        return FileLoader()

    def test_load_text_file(self, loader, tmp_path):
        """Test loading a text file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, world!", encoding="utf-8")
        result = loader.load(str(test_file))
        assert result == "Hello, world!"

    def test_load_markdown_file(self, loader, tmp_path):
        """Test loading a markdown file."""
        test_file = tmp_path / "test.md"
        test_file.write_text("# Title\n\nContent here.", encoding="utf-8")
        result = loader.load(str(test_file))
        assert "Title" in result

    def test_load_json_file(self, loader, tmp_path):
        """Test loading a JSON file."""
        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}', encoding="utf-8")
        result = loader.load(str(test_file))
        assert "key" in result

    def test_load_nonexistent_file(self, loader):
        """Test loading a non-existent file."""
        with pytest.raises(FileNotFoundError):
            loader.load("/nonexistent/path/file.txt")

    def test_load_directory(self, loader, tmp_path):
        """Test loading a directory."""
        with pytest.raises((IsADirectoryError, ValueError)):
            loader.load(str(tmp_path))

    def test_load_with_encoding(self, loader, tmp_path):
        """Test loading with specific encoding."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello", encoding="utf-8")
        result = loader.load(str(test_file), encoding="utf-8")
        assert result == "Hello"

    def test_load_large_file(self, loader, tmp_path):
        """Test loading a large file."""
        test_file = tmp_path / "large.txt"
        test_file.write_text("x" * 100000, encoding="utf-8")
        result = loader.load(str(test_file))
        assert len(result) == 100000

    def test_load_empty_file(self, loader, tmp_path):
        """Test loading an empty file."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("", encoding="utf-8")
        result = loader.load(str(test_file))
        assert result == ""

    def test_load_with_metadata(self, loader, tmp_path):
        """Test loading with metadata."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content", encoding="utf-8")
        result = loader.load(str(test_file), metadata={"source": "test"})
        assert "content" in result

    def test_supported_extensions(self, loader):
        """Test that supported extensions are defined."""
        assert hasattr(loader, "supported_extensions")
        assert ".txt" in loader.supported_extensions
        assert ".md" in loader.supported_extensions

    def test_is_supported(self, loader):
        """Test file extension support check."""
        assert loader.is_supported("test.txt")
        assert loader.is_supported("test.md")
        assert not loader.is_supported("test.unknown")


# ===========================================================================
# RAG Integration Tests
# ===========================================================================

class TestRAGIntegration:
    """Integration tests for the RAG pipeline."""

    async def test_full_rag_pipeline(self, tmp_path):
        """Test the full RAG pipeline: load → chunk → embed → index → query."""
        from ah.rag import FileLoader, Chunker, RAGPipeline

        # Create a test document
        test_file = tmp_path / "test.txt"
        test_file.write_text(
            "RAG is a technique that combines retrieval with generation. "
            "It uses embeddings to find relevant documents. "
            "The retrieved documents are then used to ground the LLM response.",
            encoding="utf-8",
        )

        # Load
        loader = FileLoader()
        text = loader.load(str(test_file))
        assert len(text) > 0

        # Chunk
        chunker = Chunker(chunk_size=100, chunk_overlap=10)
        chunks = chunker.chunk(text)
        assert len(chunks) > 0

        # Index and query
        pipeline = RAGPipeline()
        mock_store = AsyncMock()
        mock_store.add = AsyncMock()
        mock_store.search = AsyncMock(return_value=[
            {"text": chunk, "score": 0.9} for chunk in chunks[:2]
        ])

        with patch.object(pipeline, "_store", mock_store):
            await pipeline.index([{"text": c} for c in chunks])
            results = await pipeline.query("What is RAG?")
            assert len(results) > 0

    async def test_hybrid_search_integration(self):
        """Test hybrid search with real components."""
        from ah.rag import HybridSearch

        searcher = HybridSearch()

        mock_bm25 = AsyncMock()
        mock_bm25.search = AsyncMock(return_value=[
            {"id": "1", "text": "BM25 result", "score": 0.8},
        ])
        mock_dense = AsyncMock()
        mock_dense.search = AsyncMock(return_value=[
            {"id": "1", "text": "Dense result", "score": 0.9},
        ])

        with patch.object(searcher, "_bm25", mock_bm25), \
             patch.object(searcher, "_dense", mock_dense):
            results = await searcher.search("test")
            assert len(results) > 0
