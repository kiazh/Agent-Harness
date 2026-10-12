"""The optional graph benchmark uses the checkpointer's public API."""

import uuid
from types import SimpleNamespace

import pytest

from machina.research.workflow_trial import GraphApprovalTrial


@pytest.mark.asyncio
async def test_checkpoint_count_reads_all_requested_threads():
    run_ids = [uuid.uuid4(), uuid.uuid4()]
    seen = []

    async def history(config):
        seen.append(config["configurable"]["thread_id"])
        for index in range(3):
            yield {"checkpoint": index}

    trial = GraphApprovalTrial(None, "")
    trial._saver = SimpleNamespace(alist=history)
    assert await trial.checkpoint_count(run_ids) == 6
    assert seen == [str(run_id) for run_id in run_ids]
