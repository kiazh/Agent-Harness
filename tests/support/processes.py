"""Bounded, credential-isolated subprocesses for real multi-worker tests."""

import asyncio
import json
import os
import sys
from pathlib import Path


class FencingWorker:
    def __init__(self, process):
        self.process = process

    @classmethod
    async def start(cls, schema):
        from ah.core.config import LEGACY_ENV_VARS, SECRET_KEYS

        env = dict(os.environ)
        for key in SECRET_KEYS:
            env[f"AGENT_HARNESS_{key.upper()}"] = ""
            env[f"AGENT_HARNESS_{key.upper()}_FILE"] = ""
            legacy = LEGACY_ENV_VARS.get(key)
            if legacy:
                env[legacy] = ""
                env[legacy + "_FILE"] = ""
        env["AGENT_HARNESS_SECRET_BACKEND"] = ""
        env["AH_GATEWAY_TOKEN"] = "fencing-test-token"
        env["AH_GATEWAY_NO_SCHEDULER"] = "1"
        env["AH_FENCING_TEST_SCHEMA"] = schema
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.fencing_worker",
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return cls(process)

    async def call(self, operation, **fields):
        self.process.stdin.write((json.dumps({"operation": operation, **fields}) + "\n").encode())
        await self.process.stdin.drain()
        line = await asyncio.wait_for(self.process.stdout.readline(), 15)
        assert line, "test worker exited without an outcome"
        result = json.loads(line)
        assert "error_type" not in result, result
        return result

    async def close(self):
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 8)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
