import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock
os.environ['HOME'] = tempfile.mkdtemp(prefix='machina-provider-review-')
os.environ['MACHINA_ENV_FILE'] = '/dev/null'
os.environ['MACHINA_SECRET_BACKEND'] = ''
sys.path.insert(0, '/Users/shadow/machina')
import httpx
from machina.core.provider.anthropic import AnthropicProvider
from machina.core.provider.ollama import OllamaProvider
from machina.core.prompt import append_tool_messages

async def truncated_stream(cls, body):
    provider = cls.__new__(cls)
    provider.model = 'fixture-model'
    provider._default_effort = ''
    provider._rate_limiter = AsyncMock()
    provider.client = httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body)))
    try:
        events = [event async for event in provider.stream_complete([{'role':'user','content':'hello'}])]
        print(cls.__name__, 'unterminated stream events:', [event.type for event in events], 'final content:', events[-1].response.content)
    finally:
        await provider.close()

async def thinking_roundtrip():
    raw_blocks = [
      {'type':'thinking','thinking':'fixture reasoning','signature':'fixture-signature'},
      {'type':'tool_use','id':'fixture-call','name':'clock','input':{}},
    ]
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = 'claude-sonnet-4-6'
    provider._default_effort = 'medium'
    provider._rate_limiter = AsyncMock()
    provider.client = httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(lambda req: httpx.Response(200,json={'content':raw_blocks,'model':provider.model,'usage':{'input_tokens':1,'output_tokens':1}})))
    try:
        messages=[{'role':'user','content':'read clock'}]
        response=await provider.complete(messages)
        append_tool_messages(messages,response,response.tool_calls[0],'12:00')
        payload=provider._payload(messages,provider.model,0.7,4096,None,None,False)
        print('Anthropic inbound block types:',[b['type'] for b in raw_blocks])
        print('Anthropic follow-up assistant block types:',[b['type'] for m in payload['messages'] if m['role']=='assistant' for b in m['content']])
        print('Anthropic follow-up thinking enabled:',payload.get('thinking',{}).get('type'))
    finally:
        await provider.close()

async def main():
    await truncated_stream(AnthropicProvider,'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial answer"}}\n\n')
    await truncated_stream(OllamaProvider,'{"message":{"content":"partial answer"},"done":false}\n')
    await thinking_roundtrip()
asyncio.run(main())
