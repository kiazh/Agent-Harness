#!/usr/bin/env python
"""Audit finding: an agent returning AgentResponse(outcome='failed') without
raising finishes the job with error=None (recorded as an idle success)."""
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.core import turns
from machina.core.models import AgentResponse
from machina.core.scheduler import Job, JobRunner


async def fake_begin(session_id):
    return "turn-token"


async def fake_end(session_id, token):
    return None


turns.try_begin_turn = fake_begin
turns.end_turn = fake_end

job = Job(
    id=uuid.uuid4(),
    name="audit failed outcome",
    kind="interval",
    session_id=uuid.uuid4(),
    agent_name="harness",
    prompt="work",
    interval_seconds=60,
    enabled=True,
    status="running",
    last_run_at=None,
    next_run_at=datetime.now(UTC),
    last_error=None,
    run_count=0,
    no_agent=False,
    script_path=None,
    claim_token=uuid.uuid4(),
)

finished: dict = {}


class Store:
    async def claim_due(self):
        return job

    async def renew_lease(self, job_id, claim_token):
        return True

    async def finish(self, job_id, *, error, claim_token, paused_for):
        finished["error"] = error
        finished["paused_for"] = paused_for
        return True


class Agent:
    async def run(self, session_id, prompt, verbose):
        return AgentResponse(content="task could not be completed", outcome="failed")


async def main() -> None:
    runner = JobRunner(store=Store(), agent_factory=lambda _name: Agent())
    ran = await runner.run_due_once()
    print("run_due_once returned:", ran)
    print("recorded finish error:", repr(finished.get("error")))
    print("recorded paused_for:", repr(finished.get("paused_for")))
    confirmed = ran and finished.get("error") is None
    print("CLAIM CONFIRMED (failed outcome recorded as success/idle)"
          if confirmed else "CLAIM NOT REPRODUCED")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
