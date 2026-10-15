import asyncio,json,uuid
from unittest.mock import AsyncMock,patch
from machina.gateway.server import Gateway
from machina.core.models import Session
from machina.core.config import config
from machina.gateway.errors import RpcError

async def main():
    frames=[]
    gw=Gateway(frames.append,owns_db=False,inherit_env_token=False)
    gw._db_ready=True
    gw.model='openrouter/free'
    gw.provider='openrouter'
    session=Session(id=uuid.uuid4(),title='resumed account chat',model='gpt-6.1-sol',provider='chatgpt')
    with patch('machina.gateway.server.session_manager.get',AsyncMock(return_value=session)),patch('machina.gateway.server.context_manager.get_transcript_chunks',AsyncMock(return_value=[])):
        resumed=await gw._session_resume({'sessionId':str(session.id)})
        print('resume',json.dumps(resumed))
        print('catalog',json.dumps(await gw._methods['models.list']({})))
        for effort in ['max','off']:
            try:
                out=await gw._methods['models.setEffort']({'effort':effort})
                print('effort',effort,json.dumps(out))
            except RpcError as error:
                print('effort',effort,'REJECTED',str(error))
asyncio.run(main())
