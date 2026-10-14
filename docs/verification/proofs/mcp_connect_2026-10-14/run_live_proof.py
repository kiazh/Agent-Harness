"""Live proof: agent-callable MCP management through the real tool path.

Flow (machina's own MCP server as the target):
  mcp_connect(machina-self, python -m machina.mcp_server)
    -> approval gate pauses (NeedsApproval), a recorded human decision
       approves the exact digest, the retry claims it and executes
    -> tools/list discovers machina_chat / machina_session_create / machina_status
  mcp_list -> configured server + state + tool count
  mcp_call machina_status -> real remote result through our own server
  mcp_disconnect -> removed; store and live connection both empty

Isolation: connector inventory lives in a temp dir; the approval store is the
in-memory backend (no DB connected) because a headless proof has no durable
test database. The gate itself is real: every management call pauses first,
then executes exactly once after the decision is recorded -- the same
two-step transition the TUI / approvals API performs.

Run from the repo root:  .venv/bin/python docs/verification/proofs/mcp_connect_2026-10-14/run_live_proof.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# Keep the proof transcript readable: audit/console log lines are not part
# of the assertion surface (the approval audit trail is asserted separately).
logging.disable(logging.CRITICAL)

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))


def line(title: str = "") -> None:
    print(("\n== " + title + " ==") if title else "")


async def main() -> int:
    import machina.tools.agents as agents_module
    from machina.connectors.service import ConnectorService
    from machina.connectors.store import ConnectorStore
    from machina.core.runtime import runtime_services
    from machina.permissions import store
    from machina.permissions.broker import (
        clear_execution_context,
        permission_broker,
    )
    from machina.permissions.policy import build_request, decide
    from machina.permissions.tools import action_for_tool
    from machina.tools.base import registry

    agent = "proof-agent"
    session_id = uuid.uuid4()

    tmp = tempfile.TemporaryDirectory(prefix="machina-mcp-proof-")
    service = ConnectorService(store=ConnectorStore(Path(tmp.name) / "mcp.json"))
    runtime_services._connector_service = service

    async def fake_fetchval(_query, *_args):
        return agent

    agents_module.db.fetchval = fake_fetchval
    agent_token = agents_module.current_agent_id.set(agent)
    session_token = agents_module.current_session_id.set(session_id)

    line("Environment")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO
    ).stdout.strip()
    print(f"repo: {REPO}")
    print(f"git HEAD: {head} (working tree + this wave's changes)")
    print(f"python: {sys.executable} ({sys.version.split()[0]})")
    print("approval store backend: in-memory (db not connected)")
    print(f"connector inventory: {service.store.path}")

    line("Registered tool surface")
    for name in ("mcp_connect", "mcp_list", "mcp_disconnect"):
        tool = registry.get_tool(name)
        print(f"- {name}: effects={tool.effects} desc={tool.description[:140]}...")

    async def guarded_invoke(tool_name: str, args: dict) -> str:
        """Broker guard -> approval pause -> recorded decision -> retry -> execute."""
        request = build_request(
            tool=tool_name,
            **action_for_tool(tool_name, args),
            mode="ask",
            backend="host",
            agent_id=agent,
            session_id=str(session_id),
        )
        decision = decide(request)
        print(
            f"[{tool_name}] policy verdict: {decision.verdict} "
            f"({decision.reason}) capabilities={request.capabilities} "
            f"network={request.network} argv={request.argv}"
        )
        try:
            await permission_broker.guard(request)
        except Exception as pause:
            record = getattr(pause, "approval", {}) or {}
            print(
                f"[{tool_name}] PAUSED needs_approval: "
                f"request_id={record.get('request_id')} "
                f"operation={record.get('operation')} status={record.get('status')}"
            )
            resolved = await store.resolve_decision(
                record["request_id"], "approved", "proof-human"
            )
            print(
                f"[{tool_name}] human decision recorded: "
                f"{'approved' if resolved else 'FAILED'} (principal=proof-human)"
            )
            request = build_request(
                tool=tool_name,
                **action_for_tool(tool_name, args),
                mode="ask",
                backend="host",
                agent_id=agent,
                session_id=str(session_id),
            )
            await permission_broker.guard(request)
            print(f"[{tool_name}] approved digest claimed; executing exactly once")
        return await registry.execute(tool_name, **args)

    try:
        line("1. mcp_connect — add + launch + discover our own server")
        report = json.loads(
            await guarded_invoke(
                "mcp_connect",
                {
                    "name": "machina-self",
                    "command": sys.executable,
                    "argv": ["-m", "machina.mcp_server"],
                    "connect_timeout": 30,
                    "discovery_timeout": 30,
                    "call_timeout": 30,
                },
            )
        )
        print(json.dumps(report, indent=2))

        line("2. mcp_list — configured servers with enabled/trust/tool counts")
        listing = json.loads(await guarded_invoke("mcp_list", {}))
        print(json.dumps(listing, indent=2))

        line("3. mcp_call machina_status — real round trip through our own server")
        outcome = json.loads(
            await guarded_invoke(
                "mcp_call", {"name": "machina-self__machina_status", "arguments": {}}
            )
        )
        print(f"ok={outcome['ok']} outcome_unknown={outcome['outcome_unknown']}")
        print("remote text:")
        print(outcome["text"])

        line("4. mcp_disconnect — remove; store and live connection both empty")
        removal = json.loads(await guarded_invoke("mcp_disconnect", {"name": "machina-self"}))
        print(json.dumps(removal, indent=2))
        generation, configs = service.store.load()
        print(f"store after removal: generation={generation} configs={configs}")
        print(f"live connection: {service.manager.connection('machina-self')}")
        final = json.loads(await guarded_invoke("mcp_list", {}))
        print("final mcp_list:", json.dumps(final))
        return 0
    finally:
        clear_execution_context()
        agents_module.current_session_id.reset(session_token)
        agents_module.current_agent_id.reset(agent_token)
        await service.manager.shutdown_all()
        with_pending = service.store.load()[1]
        if with_pending:
            await service.remove("machina-self")
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
