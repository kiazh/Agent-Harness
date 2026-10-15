"""Synthetic-only security review proofs for HEAD 7d11d46 + dirty tree."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import parse_qs

scratch=Path(tempfile.mkdtemp(prefix='machina-auth-security-review-')).resolve()
os.environ['HOME']=str(scratch/'home')
os.environ['MACHINA_ENV_FILE']=str(scratch/'unused.env')
os.environ.pop('MACHINA_CHATGPT_AUTH_FILE',None)

import httpx
from machina.core.config import config
config.machina_home=str(scratch/'workspace')
config.workspace_root=str(scratch/'workspace')
config.execution_mode='ask'
from machina.security import chatgpt_auth as auth
from machina.permissions.policy import build_request,decide
from machina.permissions.tools import action_for_tool
from machina.permissions.broker import permission_broker,clear_execution_context
from machina.tools.file import read_file
from machina.memory.redaction import redact_secrets

def tokens(suffix='old'):
 return {'access_token':f'SYNTHETIC_ACCESS_{suffix}_ABCDEFGHIJKLMNOPQRSTUVWXYZ',
         'refresh_token':f'SYNTHETIC_REFRESH_{suffix}_ABCDEFGHIJKLMNOPQRSTUVWXYZ',
         'account_id':'synthetic-account','expires_at':time.time()-300}

async def workspace_read():
 path=auth.save_tokens(tokens())
 request=build_request(tool='read_file',**action_for_tool('read_file',{'path':'auth/chatgpt.json'}))
 print('STORE_PATH',path)
 print('READ_DECISION',decide(request).verdict)
 await permission_broker.guard(request)
 try: result=await read_file('auth/chatgpt.json')
 finally: clear_execution_context()
 print('READ_RETURNS_REFRESH',tokens()['refresh_token'] in result)
 print('REDACTION_STILL_CONTAINS_REFRESH',tokens()['refresh_token'] in redact_secrets(result).text)

def concurrent_refresh():
 target=scratch/'concurrent.json'
 auth.save_tokens(tokens(),target)
 barrier=threading.Barrier(2)
 lock=threading.Lock()
 requests=[]
 def handler(request):
  supplied=parse_qs(request.content.decode())['refresh_token'][0]
  with lock:
   requests.append(supplied)
   ordinal=len(requests)
  barrier.wait(timeout=5)
  if ordinal==1:
   return httpx.Response(200,json={'access_token':tokens('fresh')['access_token'],'refresh_token':tokens('fresh')['refresh_token']})
  return httpx.Response(400,json={'error':'invalid_grant','error_description':'synthetic single-use token was already consumed'})
 def call():
  with httpx.Client(transport=httpx.MockTransport(handler)) as client:
   try: return auth.refresh(tokens(),path=target,client=client)['refresh_token']
   except auth.ChatGPTAuthError as error:return f'ERROR:{error.code}'
 with ThreadPoolExecutor(max_workers=2) as pool:
  results=list(pool.map(lambda _:call(),range(2)))
 print('REFRESH_POST_COUNT',len(requests),'SAME_OLD_REFRESH',requests==[tokens()['refresh_token']]*2)
 print('REFRESH_RESULTS',results)

def resurrection_after_logout():
 target=scratch/'logout.json'
 auth.save_tokens(tokens(),target)
 dispatched=threading.Event();release=threading.Event()
 def handler(request):
  dispatched.set();assert release.wait(timeout=5)
  return httpx.Response(200,json={'access_token':tokens('fresh')['access_token'],'refresh_token':tokens('fresh')['refresh_token']})
 def call():
  with httpx.Client(transport=httpx.MockTransport(handler)) as client:
   return auth.refresh(tokens(),path=target,client=client)
 with ThreadPoolExecutor(max_workers=1) as pool:
  future=pool.submit(call)
  assert dispatched.wait(timeout=5)
  target.unlink()
  print('LOGOUT_STORE_REMOVED',not target.exists())
  release.set();future.result(timeout=5)
 print('STORE_RESURRECTED',target.exists())

asyncio.run(workspace_read())
concurrent_refresh()
resurrection_after_logout()
print('SCRATCH',scratch)
