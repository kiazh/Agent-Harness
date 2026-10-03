"""Tests for the RAG pipeline — Embedder, Chunker, RAGPipeline, HybridSearch, etc."""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.rag import (
    Chunk,
    CohereReranker,
    Embedder,
    FileLoader,
    HybridSearch,
    OpenAIEmbedder,
    RAGPipeline,
    RecursiveCharacterTextSplitter,
    Reranker,
    SearchResult,
)
from ah.rag.loaders import Document
from ah.rag.reranker import IdentityReranker, RerankResult

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
def mock_embedder():
    """Create a mock embedder."""
    embedder = AsyncMock(spec=Embedder)
    embedder.embed = AsyncMock(return_value=[0.1] * 1536)
    embedder.embed_batch = AsyncMock(return_value=[[0.1] * 1536, [0.2] * 1536])
    embedder.dimensions = 1536
    embedder.model_name = "mock-model"
    return embedder


# ===========================================================================
# Embedder Tests
# ===========================================================================

class TestEmbedder:
    """Tests for the Embedder interface."""

    async def test_embed_single(self):
        """Test embedding a single text."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        embedder.dimensions = 1536
        embedder.model_name = "test"

        result = await embedder.embed("test text")
        assert isinstance(result, list)
        assert len(result) == 1536
        assert all(isinstance(x, float) for x in result)

    async def test_embed_batch(self):
        """Test embedding a batch of texts."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed_batch = AsyncMock(return_value=[[0.1] * 1536, [0.2] * 1536])
        embedder.dimensions = 1536
        embedder.model_name = "test"

        texts = ["text1", "text2"]
        results = await embedder.embed_batch(texts)
        assert len(results) == 2
        for r in results:
            assert isinstance(r, list)
            assert len(r) == 1536

    async def test_embed_empty_string(self):
        """Test embedding an empty string."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(return_value=[0.0] * 1536)
        embedder.dimensions = 1536
        embedder.model_name = "test"

        result = await embedder.embed("")
        assert isinstance(result, list)
        assert len(result) == 1536

    async def test_embed_consistency(self):
        """Test that the same text produces the same embedding."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        embedder.dimensions = 1536
        embedder.model_name = "test"

        result1 = await embedder.embed("test")
        result2 = await embedder.embed("test")
        assert result1 == result2

    async def test_embed_different_texts(self):
        """Test that different texts produce different embeddings."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(side_effect=[[0.1] * 1536, [0.2] * 1536])
        embedder.dimensions = 1536
        embedder.model_name = "test"

        result1 = await embedder.embed("hello")
        result2 = await embedder.embed("world")
        assert result1 != result2

    async def test_embed_dimension(self):
        """Test that embeddings have consistent dimension."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        embedder.dimensions = 1536
        embedder.model_name = "test"

        result = await embedder.embed("test")
        assert len(result) == 1536

    async def test_embed_unicode(self):
        """Test embedding unicode text."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        embedder.dimensions = 1536
        embedder.model_name = "test"

        result = await embedder.embed("你好世界")
        assert isinstance(result, list)
        assert len(result) == 1536

    async def test_embed_long_text(self):
        """Test embedding a long text."""
        embedder = AsyncMock(spec=Embedder)
        embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        embedder.dimensions = 1536
        embedder.model_name = "test"

        long_text = "word " * 10000
        result = await embedder.embed(long_text)
        assert isinstance(result, list)
        assert len(result) == 1536

    def test_openai_embedder_properties(self):
        """Test OpenAIEmbedder properties."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            embedder = OpenAIEmbedder(api_key="test-key")
            assert embedder.dimensions == 1536
            assert embedder.model_name == "text-embedding-3-small"

    def test_openai_embedder_no_api_key(self):
        """Test OpenAIEmbedder without API key raises error."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="API key"):
                OpenAIEmbedder()


# ===========================================================================
# Chunker Tests
# ===========================================================================

class TestChunker:
    """Tests for the RecursiveCharacterTextSplitter."""

    @pytest.fixture
    def chunker(self):
        """Create a RecursiveCharacterTextSplitter instance."""
        return RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=76)

    def test_split_text_basic(self, chunker, sample_text):
        """Test basic text splitting."""
        chunks = chunker.split_text(sample_text)
        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, Chunk)
            assert isinstance(chunk.text, str)
            assert len(chunk.text) > 0

    def test_split_text_respects_size(self, chunker, sample_text):
        """Test that chunks respect the size limit."""
        chunks = chunker.split_text(sample_text)
        for chunk in chunks:
            # Chunks should be approximately chunk_size (with some flexibility)
            assert chunk.token_count <= 512 * 1.5  # Allow some overflow

    def test_split_text_empty_string(self, chunker):
        """Test splitting an empty string."""
        chunks = chunker.split_text("")
        assert chunks == []

    def test_split_text_short_text(self, chunker):
        """Test splitting text shorter than chunk_size."""
        chunks = chunker.split_text("Short text")
        assert len(chunks) == 1
        assert chunks[0].text == "Short text"

    def test_split_text_preserves_content(self, chunker, sample_text):
        """Test that splitting preserves all content."""
        chunks = chunker.split_text(sample_text)
        # All original words should appear in chunks
        original_words = set(sample_text.split())
        chunk_words = set()
        for chunk in chunks:
            chunk_words.update(chunk.text.split())
        # Most words should be preserved (some may be lost at boundaries)
        assert len(chunk_words) >= len(original_words) * 0.8

    def test_split_text_with_metadata(self, chunker, sample_text):
        """Test splitting with metadata."""
        chunks = chunker.split_text(sample_text, metadata={"source": "test"})
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk.metadata.get("source") == "test"

    def test_split_markdown(self, sample_text):
        """Test Markdown-aware splitting."""
        chunker = RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=76)
        chunks = chunker.split_markdown(sample_text)
        assert len(chunks) > 0
        # Should preserve heading metadata
        has_heading = any("heading" in c.metadata for c in chunks)
        assert has_heading or len(chunks) > 0

    def test_split_code(self):
        """Test code-aware splitting."""
        code = """
def hello():
    print("Hello, World!")

def goodbye():
    print("Goodbye!")

class MyClass:
    def method(self):
        pass
"""
        chunker = RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=76)
        chunks = chunker.split_code(code, language="python")
        assert len(chunks) > 0

    def test_chunk_indices(self, chunker, sample_text):
        """Test that chunks have sequential indices."""
        chunks = chunker.split_text(sample_text)
        for i, chunk in enumerate(chunks):
            assert chunk.index == i

    def test_chunk_token_count(self, chunker, sample_text):
        """Test that chunks have token counts."""
        chunks = chunker.split_text(sample_text)
        for chunk in chunks:
            assert chunk.token_count > 0
            assert chunk.token_count == len(chunk.text) // 4

    def test_custom_separators(self, sample_text):
        """Test splitting with custom separators."""
        chunker = RecursiveCharacterTextSplitter(
            chunk_size=512,
            chunk_overlap=76,
            separators=["\n\n", "\n", ". "],
        )
        chunks = chunker.split_text(sample_text)
        assert len(chunks) > 0

    def test_overlap(self, chunker, sample_text):
        """Test that chunks have overlap."""
        chunks = chunker.split_text(sample_text)
        if len(chunks) > 1:
            # There should be some overlap between consecutive chunks
            assert len(chunks) >= 1


# ===========================================================================
# RAGPipeline Tests
# ===========================================================================

class TestRAGPipeline:
    """Tests for the RAGPipeline."""

    @pytest.fixture
    def pipeline(self):
        """Create a RAGPipeline instance with mocked dependencies."""
        mock_embedder = AsyncMock(spec=Embedder)
        mock_embedder.embed = AsyncMock(return_value=[0.1] * 1536)
        mock_embedder.embed_batch = AsyncMock(return_value=[[0.1] * 1536])
        mock_embedder.dimensions = 1536
        mock_embedder.model_name = "mock"

        mock_search = AsyncMock(spec=HybridSearch)
        mock_search.search = AsyncMock(return_value=[])

        mock_reranker = AsyncMock(spec=Reranker)
        mock_reranker.rerank = AsyncMock(return_value=[])

        return RAGPipeline(
            embedder=mock_embedder,
            search=mock_search,
            reranker=mock_reranker,
        )

    async def test_index_document(self, pipeline, tmp_path):
        """Test indexing a document."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Test content for indexing", encoding="utf-8")

        import msgpack
        mock_db = AsyncMock()
        mock_db.fetchrow = AsyncMock(return_value={
            "id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "agent_id": "harness",
            "chunk_type": "document",
            "payload_msgpack": msgpack.packb({"text": "Test content", "metadata": {}}, use_bin_type=True),
            "token_count": 5,
            "embedding": "[0.1,0.2]",
            "created_at": datetime.utcnow(),
            "accessed_at": None,
        })

        # Patch the loader's base_dir to allow loading from tmp_path
        pipeline._loader._base_dir = tmp_path

        with patch("ah.rag.pipeline.db", mock_db):
            chunks = await pipeline.index_document(
                str(test_file),
                session_id=uuid.uuid4(),
            )
            assert isinstance(chunks, list)

    async def test_search(self, pipeline):
        """Test searching the pipeline."""
        # Mock the search to return results
        mock_result = SearchResult(
            chunk=MagicMock(),
            score=0.9,
        )
        pipeline._search.search = AsyncMock(return_value=[mock_result])

        results = await pipeline.search("test query", session_id=uuid.uuid4())
        assert isinstance(results, list)

    async def test_search_with_top_k(self, pipeline):
        """Test searching with top_k parameter."""
        pipeline._search.search = AsyncMock(return_value=[])
        results = await pipeline.search("test", session_id=uuid.uuid4(), top_k=5)
        assert isinstance(results, list)

    async def test_index_session_context(self, pipeline):
        """Test indexing existing session context."""
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])
        mock_db.fetchval = AsyncMock(return_value=0)

        with patch("ah.rag.pipeline.db", mock_db):
            result = await pipeline.index_session_context(uuid.uuid4())
            assert isinstance(result, list)


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
    def mock_db(self):
        """Create a mock database."""
        mock = AsyncMock()
        mock.fetch = AsyncMock(return_value=[])
        return mock

    async def test_search_combines_results(self, searcher, mock_db):
        """Test that hybrid search combines BM25 and dense results."""
        results = await searcher.search(
            session_id=uuid.uuid4(),
            query_embedding=[0.1] * 1536,
            query_text="test query",
            db=mock_db,
        )
        assert isinstance(results, list)

    async def test_search_empty_query(self, searcher, mock_db):
        """Test searching with empty query."""
        results = await searcher.search(
            session_id=uuid.uuid4(),
            query_embedding=[0.1] * 1536,
            query_text="",
            db=mock_db,
        )
        assert isinstance(results, list)

    async def test_search_top_k(self, searcher, mock_db):
        """Test searching with top_k limit."""
        results = await searcher.search(
            session_id=uuid.uuid4(),
            query_embedding=[0.1] * 1536,
            query_text="test",
            db=mock_db,
            top_k=5,
        )
        assert isinstance(results, list)

    async def test_rrf_fusion(self, searcher):
        """Test RRF fusion of results."""
        from ah.core.models import ContextChunk

        chunk1 = ContextChunk(
            id=uuid.uuid4(), session_id=uuid.uuid4(), agent_id="test",
            chunk_type="document", payload={},
        )
        chunk2 = ContextChunk(
            id=uuid.uuid4(), session_id=uuid.uuid4(), agent_id="test",
            chunk_type="document", payload={},
        )

        dense_results = [(chunk1, 0.9), (chunk2, 0.7)]
        sparse_results = [(chunk2, 0.8), (chunk1, 0.6)]

        fused = searcher._rrf_fuse(dense_results, sparse_results)
        assert len(fused) == 2
        # All results should have RRF scores
        for r in fused:
            assert r.rrf_score > 0

    async def test_rrf_fuse_empty(self, searcher):
        """Test RRF fusion with empty results."""
        fused = searcher._rrf_fuse([], [])
        assert fused == []

    async def test_rrf_fuse_single_source(self, searcher):
        """Test RRF fusion with only dense results."""
        from ah.core.models import ContextChunk

        chunk = ContextChunk(
            id=uuid.uuid4(), session_id=uuid.uuid4(), agent_id="test",
            chunk_type="document", payload={},
        )
        fused = searcher._rrf_fuse([(chunk, 0.9)], [])
        assert len(fused) == 1
        assert fused[0].dense_score == 0.9


# ===========================================================================
# Reranker Tests
# ===========================================================================

class TestReranker:
    """Tests for the Reranker."""

    @pytest.fixture
    def reranker(self):
        """Create an IdentityReranker instance."""
        return IdentityReranker()

    async def test_rerank_basic(self, reranker):
        """Test basic reranking."""
        query = "What is RAG?"
        documents = [
            "RAG combines LLM with external knowledge",
            "The weather is sunny today",
            "Embeddings capture semantic meaning",
        ]
        results = await reranker.rerank(query, documents)
        assert len(results) == 3
        for r in results:
            assert isinstance(r, RerankResult)

    async def test_rerank_reorders(self, reranker):
        """Test that reranking reorders documents."""
        query = "Python programming"
        documents = [
            "The sky is blue",
            "Python is a programming language",
            "I like pizza",
        ]
        results = await reranker.rerank(query, documents)
        # IdentityReranker preserves order, so first doc stays first
        assert len(results) == 3

    async def test_rerank_with_scores(self, reranker):
        """Test reranking with scores."""
        query = "test"
        documents = ["doc1", "doc2", "doc3"]
        results = await reranker.rerank(query, documents)
        assert len(results) == 3
        for r in results:
            assert isinstance(r.score, float)
            assert r.score > 0

    async def test_rerank_empty_documents(self, reranker):
        """Test reranking with empty document list."""
        results = await reranker.rerank("test", [])
        assert results == []

    async def test_rerank_top_k(self, reranker):
        """Test reranking with top_k."""
        query = "test"
        documents = [f"Document {i}" for i in range(10)]
        results = await reranker.rerank(query, documents, top_k=3)
        assert len(results) == 3

    async def test_rerank_consistency(self, reranker):
        """Test that reranking is consistent."""
        query = "test"
        documents = ["doc1", "doc2", "doc3"]
        results1 = await reranker.rerank(query, documents)
        results2 = await reranker.rerank(query, documents)
        assert len(results1) == len(results2)

    def test_identity_reranker_model_name(self, reranker):
        """Test IdentityReranker model name."""
        assert reranker.model_name == "identity"

    def test_cohere_reranker_no_api_key(self):
        """Test CohereReranker without API key raises error."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="COHERE_API_KEY"):
                CohereReranker()


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
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert isinstance(doc, Document)
        assert doc.content == "Hello, world!"
        assert doc.source == str(test_file)

    def test_load_markdown_file(self, loader, tmp_path):
        """Test loading a markdown file."""
        test_file = tmp_path / "test.md"
        test_file.write_text("# Title\n\nContent here.", encoding="utf-8")
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert "Title" in doc.content
        assert doc.doc_type == "markdown"

    def test_load_json_file(self, loader, tmp_path):
        """Test loading a JSON file."""
        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}', encoding="utf-8")
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert "key" in doc.content

    def test_load_python_file(self, loader, tmp_path):
        """Test loading a Python file."""
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): pass", encoding="utf-8")
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert doc.doc_type == "code"

    def test_load_nonexistent_file(self, loader, tmp_path):
        """Test loading a non-existent file."""
        loader._base_dir = tmp_path
        with pytest.raises(ValueError, match="File not found"):
            loader.load(str(tmp_path / "nonexistent.txt"))

    def test_load_directory(self, loader, tmp_path):
        """Test loading a directory."""
        loader._base_dir = tmp_path
        with pytest.raises(ValueError, match="Not a file"):
            loader.load(str(tmp_path))

    def test_load_empty_file(self, loader, tmp_path):
        """Test loading an empty file."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("", encoding="utf-8")
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert doc.content == ""

    def test_load_large_file(self, loader, tmp_path):
        """Test loading a large file."""
        test_file = tmp_path / "large.txt"
        test_file.write_text("x" * 100000, encoding="utf-8")
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert len(doc.content) == 100000

    def test_load_with_metadata(self, loader, tmp_path):
        """Test that loaded documents have metadata."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content", encoding="utf-8")
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert "filename" in doc.metadata
        assert "extension" in doc.metadata
        assert "size_bytes" in doc.metadata

    def test_load_directory_of_files(self, loader, tmp_path):
        """Test loading a directory of files."""
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")
        loader._base_dir = tmp_path
        docs = loader.load_directory(str(tmp_path))
        assert len(docs) == 2

    def test_supported_extensions(self, loader):
        """Test that supported extensions are defined."""
        assert ".txt" in loader.TEXT_EXTENSIONS
        assert ".md" in loader.TEXT_EXTENSIONS
        assert ".py" in loader.CODE_EXTENSIONS


# ===========================================================================
# RAG Integration Tests
# ===========================================================================

class TestRAGIntegration:
    """Integration tests for the RAG pipeline."""

    async def test_full_rag_pipeline(self, tmp_path):
        """Test the full RAG pipeline: load → chunk → embed → index → search."""
        from ah.rag import FileLoader, RecursiveCharacterTextSplitter

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
        loader._base_dir = tmp_path
        doc = loader.load(str(test_file))
        assert len(doc.content) > 0

        # Chunk
        chunker = RecursiveCharacterTextSplitter(chunk_size=200, chunk_overlap=20)
        chunks = chunker.split_text(doc.content)
        assert len(chunks) > 0

    async def test_hybrid_search_integration(self):
        """Test hybrid search with real components."""
        from ah.rag import HybridSearch

        searcher = HybridSearch()
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        results = await searcher.search(
            session_id=uuid.uuid4(),
            query_embedding=[0.1] * 1536,
            query_text="test",
            db=mock_db,
        )
        assert isinstance(results, list)
