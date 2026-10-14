import asyncio
import importlib
import json
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from machina.core.models import ContextChunk
from machina.memory.consolidator import MemoryConsolidator
from machina.skills.learning import LearningReviewer
from machina.skills.registry import SkillRegistry

consolidator = importlib.import_module('machina.memory.consolidator')
context = importlib.import_module('machina.core.context')
usage = importlib.import_module('machina.core.usage')
learning = importlib.import_module('machina.skills.learning')

async def invalid_items():
    sid = uuid.uuid4()
    chunk = ContextChunk(uuid.uuid4(), sid, 'harness', 'user_message', {'content': 'Remember: I prefer concise answers.'})
    calls = AsyncMock(return_value=SimpleNamespace(content='[{"category":"preference","importance":0.9}]'))
    manager = SimpleNamespace(get_chunks_since=AsyncMock(return_value=[chunk]))
    with patch.object(consolidator, 'db', SimpleNamespace(connected=False)), patch.object(consolidator, 'context_manager', manager), patch.object(usage.usage_store, 'complete_call', calls):
        result = await MemoryConsolidator(llm_provider=object()).consolidate_session(sid, 'harness')
    print('INVALID ARRAY ITEM:', {'status': result.status, 'written': len(result.entries), 'checkpoint': result.checkpoint, 'input_chunk': str(chunk.id)})

class BoundedDB:
    connected = True
    def __init__(self, capacity):
        self.capacity = capacity
        self.sem = asyncio.Semaphore(capacity)
        self.locked = 0
        self.all_locked = asyncio.Event()
    @asynccontextmanager
    async def acquire(self):
        await asyncio.wait_for(self.sem.acquire(), timeout=0.06)
        try:
            yield self
        finally:
            self.sem.release()
    @asynccontextmanager
    async def transaction(self):
        yield
    async def execute(self, query, *args):
        if 'pg_advisory_xact_lock' in query:
            self.locked += 1
            if self.locked == self.capacity:
                self.all_locked.set()
            await self.all_locked.wait()
    async def fetch(self, query, *args):
        async with self.acquire():
            return []

async def pool_starvation(capacity):
    database = BoundedDB(capacity)
    with patch.object(consolidator, 'db', database), patch.object(context, 'db', database):
        results = await asyncio.gather(*(MemoryConsolidator().consolidate_session(uuid.uuid4(), 'harness') for _ in range(capacity)), return_exceptions=True)
    print('POOL CAPACITY:', capacity, 'RESULTS:', [type(r).__name__ for r in results])

class FailingApprovalDB:
    def __init__(self, row):
        self.row = row
    @asynccontextmanager
    async def acquire(self):
        yield self
    @asynccontextmanager
    async def transaction(self):
        yield
    async def fetchrow(self, query, *args):
        if query.startswith('UPDATE'):
            raise OSError('synthetic DB status update failure')
        return self.row

async def skill_approval():
    row = {'id': uuid.uuid4(), 'agent_id':'harness', 'session_id':uuid.uuid4(), 'status':'pending', 'name':'review-demo', 'description':'Review demonstration', 'content':'Check inputs. Run a focused verification. Record the result.', 'triggers':[], 'reason':''}
    with tempfile.TemporaryDirectory(prefix='machina-review-') as tmp:
        registry = SkillRegistry(Path(tmp))
        reviewer = LearningReviewer(registry)
        with patch.object(learning, 'db', FailingApprovalDB(row)):
            try:
                await reviewer.approve(row['id'], agent_id='harness')
            except OSError as exc:
                print('SKILL APPROVAL ERROR:', str(exc))
            fresh_registry = SkillRegistry(Path(tmp))
            fresh_registry.load_all()
            print('AFTER FAILED APPROVAL:', {'review_status':row['status'], 'live_skills':[s.name for s in fresh_registry.list_skills()]})
            try:
                await reviewer.approve(row['id'], agent_id='harness')
            except ValueError as exc:
                print('APPROVAL RETRY ERROR:', str(exc))

async def main():
    await invalid_items()
    await pool_starvation(1)
    await pool_starvation(10)
    await skill_approval()
asyncio.run(main())
