import asyncio, json, uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from typer.testing import CliRunner
import machina.cli as cli
from machina.core import turns
from machina.core.models import StreamEvent, AgentResponse

sid = uuid.uuid4()
session = SimpleNamespace(id=sid, provider='openrouter', model='mock', title='audit')
observed = []
class FakeAgent:
    provider = SimpleNamespace(model='mock')
    async def run_stream(self, session_id, message, verbose=False):
        observed.append({'action':'agent.run_stream', 'existing_owner':turns._local_owner(session_id)})
        yield StreamEvent(type='done', response=AgentResponse(content='PROVIDER FAILURE MUST BE VISIBLE', outcome='failed', tokens_used=0, iterations=1))

async def delete(session_id):
    observed.append({'action':'session.delete', 'existing_owner':turns._local_owner(session_id)})
    return True

def claimed_run(coro):
    async def wrapped():
        token = await turns.try_begin_turn(sid, turn_id='other-transport-turn')
        assert token
        try:
            return await coro
        finally:
            await turns.end_turn(sid, token)
    return asyncio.run(wrapped())

with patch.object(cli.db,'connect',AsyncMock()), patch.object(cli.db,'close',AsyncMock()), patch.object(cli.db,'initialize_runtime_schema',AsyncMock()), patch.object(cli.session_manager,'get',AsyncMock(return_value=session)), patch.object(cli.session_manager,'delete',delete), patch('machina.core.agent_factory.build_agent_for_session',AsyncMock(return_value=FakeAgent())), patch('machina.core.agent_factory.close_agent_provider',AsyncMock()), patch.object(cli,'drain_background_tasks',AsyncMock()), patch.object(cli,'_run',claimed_run):
    for args in [['chat','hello','--session',str(sid),'--quiet'], ['delete',str(sid),'--force']]:
        result = CliRunner().invoke(cli.app,args)
        print(json.dumps({'args':args, 'exit':result.exit_code, 'output':result.output, 'exception':repr(result.exception)}))
print(json.dumps(observed))
