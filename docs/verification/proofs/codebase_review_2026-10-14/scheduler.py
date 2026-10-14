import asyncio
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch
os.environ['HOME']=tempfile.mkdtemp(prefix='machina-scheduler-review-')
os.environ['MACHINA_ENV_FILE']='/dev/null'
os.environ['MACHINA_SECRET_BACKEND']=''
sys.path.insert(0,'/Users/shadow/machina')
from machina.db.connection import db
from machina.core.session import session_manager
from machina.core import turns
from machina.core import scheduler

async def main():
    db.dsn='postgresql://postgres:postgres@localhost:5432/machina_test'
    await db.connect()
    session=None
    release=asyncio.Event()
    resisted=asyncio.Event()
    class Agent:
        async def run(self,*args,**kwargs):
            try:
                await release.wait()
            except asyncio.CancelledError:
                resisted.set()
                await release.wait()
            return SimpleNamespace(needs_approval=[],outcome='completed')
    class Store:
        def __init__(self,job): self.job=job
        async def claim_due(self): result,self.job=self.job,None; return result
        async def renew_lease(self,*args): return True
        async def finish(self,*args,**kwargs): return True
    original_begin=turns.try_begin_turn
    async def short_claim(sid,**kwargs): return await original_begin(sid,ttl_s=1)
    try:
        session=await session_manager.create(title='scheduler review scratch')
        job=SimpleNamespace(id=uuid.uuid4(),name='review',session_id=session.id,agent_name='harness',prompt='fixture',no_agent=False,model=None,provider=None,claim_token=uuid.uuid4())
        runner=scheduler.JobRunner(Store(job),agent_factory=lambda name:Agent())
        with patch.object(scheduler,'RUN_LEASE_SECONDS',0.15),patch.object(turns,'try_begin_turn',short_claim):
            await runner.run_due_once()
            print('job ignored cancellation:',resisted.is_set(),'quarantined effects:',len(runner._quarantined))
            await asyncio.sleep(1.05)
            second=await original_begin(session.id)
            print('another turn acquired while old job still active:',bool(second) and bool(runner._quarantined))
            if second: await turns.end_turn(session.id,second)
            release.set()
            if runner._quarantined: await asyncio.gather(*tuple(runner._quarantined))
            await runner.stop()
    finally:
        release.set()
        if session: await session_manager.delete(session.id)
        await db.close()
asyncio.run(main())
