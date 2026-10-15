import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from machina.rag import embedder as e
cfg = importlib.import_module('machina.core.config')
class FakeConfig:
    def get(self,key):
        return {'embedding_backend':'auto','embedding_model':'openai/text-embedding-3-small','embedding_base_url':'','openai_api_key':'','openrouter_api_key':'synthetic-review-key'}.get(key,'')
async def main():
    captured = {}
    def handler(request):
        captured.update(endpoint=str(request.url),model=json.loads(request.content)['model'])
        return httpx.Response(200,json={'data':[{'index':0,'embedding':[0.1,0.2]}]})
    with patch.object(cfg,'config',FakeConfig()),patch.object(e,'_core_symbol',lambda module,name: (lambda *a,**k:None) if name=='audit_log' else FakeConfig()):
        settings = e.resolve_embedding_settings()
        embedder = e.build_embedder(settings)
        await embedder._client.aclose()
        embedder._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),base_url=embedder._base_url)
        try:
            await embedder.embed('review text')
            print('configured model: openai/text-embedding-3-small')
            print('resolved backend:',settings.kind)
            print('captured request:',captured)
        finally:
            await embedder.close()
asyncio.run(main())
