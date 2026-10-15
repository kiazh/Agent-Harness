import asyncio,json,socket,tempfile
from pathlib import Path
from unittest.mock import patch
from machina.gateway.server import Gateway
from machina.gateway.features import auth_chatgpt as feature
from machina.security import chatgpt_auth
from machina.security.chatgpt_auth import LoginRequest

async def main():
    frames=[]
    gw=Gateway(frames.append,owns_db=False,inherit_env_token=False)
    gw.protocol_version=2
    root=Path(tempfile.mkdtemp(prefix='machina-auth-lifecycle-data-',dir='/tmp'))
    target=root/'fake-auth.json'
    target.write_text('{"fixture":"old credential"}')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    request=LoginRequest(url='https://auth.invalid/fixture',state='fake-state',code_verifier='fake-verifier',redirect_uri=f'http://127.0.0.1:{port}/auth/callback')
    def fake_exchange(*args,**kwargs):
        target.write_text('{"fixture":"new credential"}')
        return {'account_id':'fake-account','expires_at':1800000000}
    with patch.object(chatgpt_auth,'login_url',lambda:request),patch.object(chatgpt_auth,'auth_path',lambda:target),patch.object(chatgpt_auth,'exchange_code',fake_exchange),patch.object(feature,'_status_payload',lambda:{'signedIn':target.exists(),'source':'fixture'}):
        await feature.auth_chatgpt_login(gw,{'no_browser':True})
        pending=feature._PENDING
        await asyncio.sleep(.01)
        print('logout_result',json.dumps(await feature.auth_chatgpt_logout(gw,{})))
        print('pending_after_logout',bool(feature._PENDING and not feature._PENDING.task.done()))
        await gw.close()
        print('pending_after_gateway_close',bool(feature._PENDING and not feature._PENDING.task.done()))
        reader,writer=await asyncio.open_connection('127.0.0.1',port)
        writer.write(b'GET /auth/callback?code=fake-code&state=fake-state HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        await writer.drain();await reader.read();writer.close();await writer.wait_closed()
        await asyncio.wait_for(pending.task,1)
        print('credential_recreated_after_logout',target.exists())
        print('events',json.dumps([frame['params']['type'] for frame in frames if frame.get('method')=='event']))
asyncio.run(main())
