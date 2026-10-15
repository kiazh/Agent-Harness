"""R5-A A1 GREEN: tool-supplied mode above the server ceiling is refused
without spawning; at/under-ceiling modes still reach the child unchanged.

Cases (each with a spawn-forbidding or spawn-capturing _run_process):
1. operator default (ask) + mode="full" -> invalid_request /
   mode_ceiling_exceeded, child never started.
2. operator default (ask) + mode="workspace" -> refused (above ceiling).
3. operator full (MACHINA_EXECUTION_MODE=full) + mode="full" -> spawned with
   --mode full (no behavior change at/under ceiling).
4. operator full + MCP_MAX_MODE=ask + mode="workspace" -> refused
   (MCP_MAX_MODE tightens).
5. operator ask + MCP_MAX_MODE=full + mode="full" -> refused (MCP_MAX_MODE
   never escalates above the operator mode).
6. operator ask + MCP_MAX_MODE=bogus + mode="workspace" -> refused
   (invalid ceiling fails closed to ask).
7. no mode arg -> spawned without --mode (unchanged path).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/Users/shadow/machina")

from machina import mcp_server
from machina.mcp_server import ProcessResult

OK = ProcessResult(0, "Session ID: 123e4567-e89b-42d3-a456-426614174000\n\nhi\n", "", False, False)


def run_case(name, *, env, mode, expect_status, expect_spawn, expect_flag=None):
    for key in ("MACHINA_EXECUTION_MODE", "MCP_MAX_MODE"):
        os.environ.pop(key, None)
    os.environ.update(env)
    calls = []

    def fake(cmd, *, timeout, env_extra=None, cancel_event=None):
        calls.append(cmd)
        return OK

    real = mcp_server._run_process
    mcp_server._run_process = fake
    try:
        kwargs = {"prompt": "hello"}
        if mode is not None:
            kwargs["mode"] = mode
        payload = json.loads(asyncio.run(mcp_server.machina_chat(**kwargs)))
    finally:
        mcp_server._run_process = real
        for key in ("MACHINA_EXECUTION_MODE", "MCP_MAX_MODE"):
            os.environ.pop(key, None)
    status_ok = payload["status"] == expect_status
    spawn_ok = bool(calls) == expect_spawn
    flag_ok = True
    if expect_flag is not None and calls:
        flag_ok = "--mode" in calls[0] and calls[0][calls[0].index("--mode") + 1] == expect_flag
    if expect_flag is None and calls:
        flag_ok = "--mode" not in calls[0]
    verdict = "PASS" if (status_ok and spawn_ok and flag_ok) else "FAIL"
    print(f"{verdict} {name}: status={payload['status']} "
          f"code={payload.get('code')} ceiling={payload.get('ceiling')} spawned={bool(calls)}")
    assert status_ok and spawn_ok and flag_ok, payload


run_case("default-ask refuses full", env={}, mode="full",
         expect_status="invalid_request", expect_spawn=False)
run_case("default-ask refuses workspace", env={}, mode="workspace",
         expect_status="invalid_request", expect_spawn=False)
run_case("operator-full allows full", env={"MACHINA_EXECUTION_MODE": "full"},
         mode="full", expect_status="completed", expect_spawn=True, expect_flag="full")
run_case("operator-full allows workspace", env={"MACHINA_EXECUTION_MODE": "full"},
         mode="workspace", expect_status="completed", expect_spawn=True, expect_flag="workspace")
run_case("MCP_MAX_MODE tightens", env={"MACHINA_EXECUTION_MODE": "full", "MCP_MAX_MODE": "ask"},
         mode="workspace", expect_status="invalid_request", expect_spawn=False)
run_case("MCP_MAX_MODE never escalates",
         env={"MACHINA_EXECUTION_MODE": "ask", "MCP_MAX_MODE": "full"},
         mode="full", expect_status="invalid_request", expect_spawn=False)
run_case("invalid MCP_MAX_MODE fails closed",
         env={"MACHINA_EXECUTION_MODE": "ask", "MCP_MAX_MODE": "bogus"},
         mode="workspace", expect_status="invalid_request", expect_spawn=False)
run_case("no mode arg unchanged", env={}, mode=None,
         expect_status="completed", expect_spawn=True, expect_flag=None)

# Typed refusal shape on the headline case.
os.environ.pop("MACHINA_EXECUTION_MODE", None)
os.environ.pop("MCP_MAX_MODE", None)
real = mcp_server._run_process
mcp_server._run_process = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
try:
    payload = json.loads(asyncio.run(mcp_server.machina_chat("hello", mode="full")))
finally:
    mcp_server._run_process = real
assert payload["code"] == "mode_ceiling_exceeded", payload
assert payload["ceiling"] == "ask", payload
assert "no grant was minted" in payload["error"], payload
print("PASS refusal is typed (code/ceiling present, never a grant)")
print("GREEN A1: ceiling enforced, at/under-ceiling paths unchanged")
