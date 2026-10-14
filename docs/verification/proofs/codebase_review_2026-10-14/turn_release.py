import asyncio
import os
import sys
import tempfile
import uuid
from unittest.mock import patch
os.environ['HOME']=tempfile.mkdtemp(prefix='machina-release-review-')
os.environ['MACHINA_ENV_FILE']='/dev/null'
sys.path.insert(0,'/Users/shadow/machina')
from machina.core.turn_engine import TurnEngine,TurnSpec
from machina.core import turns
async def scenario(cancel):
    entered=asyncio.Event()
    release=asyncio.Event()
    held=True
    async def blocked_release(*args):
        nonlocal held
        entered.set()
        await release.wait()
        held=False
        return True
    with patch.object(turns,'end_turn',blocked_release):
        engine=TurnEngine(TurnSpec(session_id=uuid.uuid4(),turn_id='review',token='fixture-token'),release_timeout_s=0.02)
        close=asyncio.create_task(engine.close())
        await entered.wait()
        if cancel:
            close.cancel()
            result=await close
            print('cancelled cleanup reports release_failed:',result.release_failed,'while claim still held:',held)
        else:
            done,_=await asyncio.wait({close},timeout=0.08)
            print('normal cleanup exceeded configured0.02s bound:',not bool(done))
        release.set()
        if not close.done(): await close
        await asyncio.sleep(0)
async def main():
    await scenario(False)
    await scenario(True)
asyncio.run(main())
