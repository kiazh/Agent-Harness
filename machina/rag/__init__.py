"""RAG pipeline — ingestion, retrieval, reranking, and generation."""

from machina.rag.chunker import Chunk, RecursiveCharacterTextSplitter
from machina.rag.embedder import (
    Embedder,
    LocalEmbedder,
    OpenAIEmbedder,
    build_embedder,
    get_shared_embedder,
)
from machina.rag.loaders import FileLoader
from machina.rag.pipeline import RAGPipeline
from machina.rag.reranker import CohereReranker, Reranker
from machina.rag.search import HybridSearch, SearchResult

__all__ = [
    "Chunk",
    "RecursiveCharacterTextSplitter",
    "Embedder",
    "LocalEmbedder",
    "OpenAIEmbedder",
    "build_embedder",
    "get_shared_embedder",
    "FileLoader",
    "RAGPipeline",
    "Reranker",
    "CohereReranker",
    "HybridSearch",
    "SearchResult",
]
