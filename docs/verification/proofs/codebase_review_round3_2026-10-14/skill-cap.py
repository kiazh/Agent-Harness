import asyncio
import importlib
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from machina.db.connection import Database
from machina.skills.learning import LearningReviewer
from machina.skills.registry import SkillRegistry
learning = importlib.import_module('machina.skills.learning')
usage = importlib.import_module('machina.core.usage')
class FakeConfig:
    def get(self, key):
        return {'learning_review_max_per_session':3,'learning_review_auto_apply':True,'learning_review_auto_apply_per_day':1}.get(key)
class CoordinatedReviewer(LearningReviewer):
    def __init__(self, registry):
        super().__init__(registry)
        self.waiting = 0
        self.both_checked = asyncio.Event()
    async def _auto_apply_allowed(self, agent_id):
        decision = await super()._auto_apply_allowed(agent_id)
        self.waiting += 1
        if self.waiting == 2:
            self.both_checked.set()
        await self.both_checked.wait()
        return decision
async def main():
    database = Database(dsn='postgresql://postgres:postgres@localhost:5432/machina_test')
    await database.connect()
    agent_id = 'review-cap-' + uuid.uuid4().hex
    sessions = []
    call_index = 0
    async def fake_completion(*args, **kwargs):
        nonlocal call_index
        call_index += 1
        return SimpleNamespace(content=json.dumps({'name':f'concurrent-cap-{call_index}','description':'A deployment verification workflow','triggers':['deployment'],'content':'Inspect the project. Run focused checks. Record the verified result.'}))
    try:
        for _ in range(2):
            sid = await database.fetchval('INSERT INTO sessions(title, agent_id) VALUES($1,$2) RETURNING id', 'temporary cap review', agent_id)
            sessions.append(sid)
        with tempfile.TemporaryDirectory(prefix='machina-cap-review-') as tmp:
            reviewer = CoordinatedReviewer(SkillRegistry(Path(tmp)))
            with patch.object(learning,'db',database), patch.object(learning,'config',FakeConfig()), patch.object(learning,'_audit',lambda *a,**k:None), patch.object(usage.usage_store,'complete_call',fake_completion):
                results = await asyncio.wait_for(asyncio.gather(*(reviewer.review_turn(sid,agent_id,'Document this repeatable workflow','First inspect inputs then run checks and record the verified output.',object()) for sid in sessions)), timeout=5)
            count = await database.fetchval('SELECT COUNT(*) FROM learning_reviews WHERE agent_id=$1 AND auto_applied=TRUE',agent_id)
            fresh = SkillRegistry(Path(tmp)); fresh.load_all()
            print('configured per-agent/day cap: 1')
            print('concurrent production review statuses:', [r['status'] for r in results])
            print('durable auto-applied count:', count)
            print('live learned skills:', sorted(s.name for s in fresh.list_skills()))
    finally:
        await database.execute('DELETE FROM learning_reviews WHERE agent_id=$1',agent_id)
        for sid in sessions:
            await database.execute('DELETE FROM sessions WHERE id=$1',sid)
        await database.close()
asyncio.run(main())
