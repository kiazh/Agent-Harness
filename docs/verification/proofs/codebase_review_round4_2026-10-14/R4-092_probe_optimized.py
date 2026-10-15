"""R4-092c probe: run workflow_benchmark.compare under `python -O` with fakes.

Run from the repo root:
    uv run python -O docs/verification/proofs/.../R4-092_probe_optimized.py

Expected after the fix: starts=2 / resumes=2 (the measured awaits run).
Before the fix (awaits inside `assert`): starts=0 / resumes=0 — the calls are
stripped by -O and the benchmark reports fabricated empty timings.
"""

import asyncio

from machina.research import workflow_benchmark as wb

calls = []


class FakeDatabase:
    def __init__(self, dsn=None):
        pass

    async def connect(self):
        pass

    async def execute(self, sql, *args):
        pass

    async def close(self):
        pass


class FakeNative:
    def __init__(self, database):
        pass

    async def setup(self):
        pass

    async def start(self, *args):
        calls.append("native-start")
        return "pending"

    async def resume(self, *args):
        calls.append("native-resume")
        return "complete"


class FakeGraph:
    def __init__(self, database, dsn):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def start(self, *args):
        calls.append("graph-start")
        return "pending"

    async def resume(self, *args):
        calls.append("graph-resume")
        return "complete"

    async def checkpoint_count(self, run_ids):
        return 0

    async def delete_thread(self, run_id):
        pass


wb.Database = FakeDatabase
wb.NativeApprovalTrial = FakeNative
wb.GraphApprovalTrial = FakeGraph
wb.version = lambda name: "test"
asyncio.run(wb.compare("postgresql://unused", 1))
print(f"starts={sum(1 for c in calls if c.endswith('start'))}")
print(f"resumes={sum(1 for c in calls if c.endswith('resume'))}")
