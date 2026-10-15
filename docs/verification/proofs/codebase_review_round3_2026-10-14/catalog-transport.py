import asyncio
import importlib
from unittest.mock import AsyncMock, patch
import httpx
from machina.core.provider import catalog
from machina.core.provider.chatgpt import ChatGPTProvider
from machina.core.provider.direct import DirectProvider
from machina.core.provider.specs import ProviderSpec
from machina.core.provider.caches import _cache_clear

async def main():
    provider=ChatGPTProvider.__new__(ChatGPTProvider)
    provider._default_effort=''
    messages=[{'role':'user','content':[{'type':'text','text':'What is in this image?'},{'type':'image_url','image_url':{'url':'data:image/png;base64,c3ludGhldGlj'}}]}]
    payload=provider._payload(messages, 'fixture-model', None, '')
    print('ChatGPT outgoing content types:',[part['type'] for msg in payload['input'] for part in msg.get('content',[])])
    print('Image serialized as text:', 'image_url' in payload['input'][0]['content'][0].get('text',''))
    catalog.clear_catalog_cache()
    def fetch(token,account,**kwargs):
        return catalog.parse_catalog({'models':[{'slug':f'only-{account}','context_window':100000,'supported_reasoning_levels':[{'effort':'low'}]}]})
    with patch.object(catalog,'fetch_live_catalog',fetch):
        catalog.refresh_live_catalog('fake-A','A',force=True)
        catalog.refresh_live_catalog('fake-B','B',force=True)
        print('Both account entries cached:',list(catalog._live_cache))
        print('Catalog used after B refresh:',[model.slug for model in catalog.cached_catalog()])
        try:
            catalog.validate_model_slug('only-B')
        except Exception as exc:
            print('Account B selection:',type(exc).__name__,str(exc))
    catalog.clear_catalog_cache()
    _cache_clear()
    calls=[]
    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(200,json={'model':'fixture-model','choices':[{'message':{'content':req.url.host}}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
    first=DirectProvider(ProviderSpec('fixture-named','https://before.invalid/v1','FIXTURE_KEY','fixture-model',requires_key=False))
    second=DirectProvider(ProviderSpec('fixture-named','https://after.invalid/v1','FIXTURE_KEY','fixture-model',requires_key=False))
    try:
        await first.client.aclose(); await second.client.aclose()
        first.client=httpx.AsyncClient(base_url='https://before.invalid/v1',transport=httpx.MockTransport(handler))
        second.client=httpx.AsyncClient(base_url='https://after.invalid/v1',transport=httpx.MockTransport(handler))
        first._rate_limiter=AsyncMock(); second._rate_limiter=AsyncMock()
        a=await first.complete([{'role':'user','content':'fixture-identical-request'}])
        b=await second.complete([{'role':'user','content':'fixture-identical-request'}])
        print('Endpoint rotation HTTP destinations:',calls,'responses:',a.content,b.content)
    finally:
        await first.close(); await second.close()
        _cache_clear()
asyncio.run(main())
