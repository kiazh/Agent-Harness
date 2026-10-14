import asyncio
import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import patch
import msgpack
from machina.rag.pipeline import RAGPipeline, RAGConfig
from machina.rag.loaders import Document
from machina.memory.redaction import redact_secrets
pipeline_module = importlib.import_module('machina.rag.pipeline')

async def main():
    line = 'MIIReviewSyntheticPrivateKeyBodyABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg'
    text = 'Notes\n\n-----BEGIN PRIVATE KEY-----\n' + '\n'.join([line] * 140) + '\n-----END PRIVATE KEY-----\n'
    print('WHOLE DOCUMENT REDACTION:', {'pattern':redact_secrets(text).redactions, 'body_present':line in redact_secrets(text).text})
    received = []
    stored = []
    class Embeddings:
        model_name = 'spy'
        async def embed_batch(self, texts):
            received.extend(texts)
            return [None] * len(texts)
    async def fetch(query, *args):
        stored.extend(msgpack.unpackb(raw, raw=False)['text'] for raw in args[3])
        return []
    pipeline = RAGPipeline(embedder=Embeddings(), config=RAGConfig(chunk_size=512))
    with patch.object(pipeline_module, 'db', SimpleNamespace(fetch=fetch)), patch.object(pipeline_module, 'audit_log', lambda *a, **k: None):
        await pipeline.index_document(Document(text, 'review.pem.txt'), uuid.uuid4())
    print('PIPELINE INDEX:', {'chunks':len(received), 'embedding_chunks_with_private_key_body':sum(line in chunk for chunk in received), 'stored_chunks_with_private_key_body':sum(line in chunk for chunk in stored)})
    print('FIRST EMBEDDING INPUT PREVIEW:', received[0][:120])
asyncio.run(main())
