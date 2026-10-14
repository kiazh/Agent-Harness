import asyncio
import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from machina.core.agent import BaseReActAgent
from machina.core.models import ContextChunk
from machina.memory.consolidator import MemoryConsolidator
agent_module = importlib.import_module('machina.core.agent')
consolidator_module = importlib.import_module('machina.memory.consolidator')
usage_module = importlib.import_module('machina.core.usage')
async def main():
    sid = uuid.uuid4()
    chunk = ContextChunk(uuid.uuid4(), sid, 'harness', 'user_message', {'content':'Remember: I prefer concise answers.'})
    session = SimpleNamespace(state={})
    async def get_chunks_since(session_id, *, since_time=None, since_id=None, limit=100):
        return [] if since_id == chunk.id else [chunk]
    context = SimpleNamespace(get_chunks_since=get_chunks_since, get_chunks=AsyncMock(return_value=[chunk]))
    async def update_state_fields(session_id, fields):
        session.state.update(fields)
    sessions = SimpleNamespace(get=AsyncMock(return_value=session), update_state_fields=update_state_fields)
    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = 'harness'
    agent.memory_consolidator = MemoryConsolidator(llm_provider=object())
    completion = AsyncMock(return_value=SimpleNamespace(content='[{"category":"preference","importance":0.9}]'))
    with patch.object(consolidator_module, 'db', SimpleNamespace(connected=False)), patch.object(consolidator_module, 'context_manager', context), patch.object(agent_module, 'context_manager', context), patch.object(agent_module, 'session_manager', sessions), patch.object(usage_module.usage_store, 'complete_call', completion):
        await agent._consolidate_memories(sid)
        print('PERSISTED SESSION STATE:', session.state)
        await agent._consolidate_memories(sid)
        print('EXTRACTION CALLS AFTER TWO CONSOLIDATIONS:', completion.await_count)
asyncio.run(main())
