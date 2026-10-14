"""Live proof: failed mcp_connect leaves nothing half-configured.

Targets a loopback endpoint that refuses instantly (127.0.0.1:1). The call
goes through the same approval gate as the success proof
(run_live_proof.py); after approval the launch fails, the store write is
compensated (config removed), and mcp_list shows no leftover server.

Run from the repo root:
  .venv/bin/python docs/verification/proofs/mcp_connect_2026-10-14/run_failed_connect_proof.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import uuid
from pathlib import Path

# Keep the proof transcript readable (see run_live_proof.py).
logging.disable(logging.CRITICAL)

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))


async def main() -> int:
    import machina.tools.agents as agents_module
    from machina.connectors.service import ConnectorService
    from machina.connectors.store import ConnectorStore
    from machina.core.runtime import runtime_services
    from machina.permissions import store
    from machina.permissions.broker import clear_execution_context, permission_broker
    from machina.permissions.policy import build_request
    from machina.permissions.tools import action_for_tool
    from machina.tools.base import registry

    agent = "proof-agent"
    session_id = uuid.uuid4()
    tmp = tempfile.TemporaryDirectory(prefix="machina-mcp-fail-proof-")
    service = ConnectorService(store=ConnectorStore(Path(tmp.name) / "mcp.json"))
    runtime_services._connector_service = service

    async def fake_fetchval(_query, *_args):
        return agent

    agents_module.db.fetchval = fake_fetchval
    agent_token = agents_module.current_agent_id.set(agent)
    session_token = agents_module.current_session_id.set(session_id)

    async def guarded_invoke(tool_name: str, args: dict) -> str:
        request = build_request(
            tool=tool_name,
            **action_for_tool(tool_name, args),
            mode="ask",
            backend="host",
            agent_id=agent,
            session_id=str(session_id),
        )
        try:
            await permission_broker.guard(request)
        except Exception as pause:
            record = getattr(pause, "approval", {}) or {}
            print(f"[{tool_name}] PAUSED needs_approval: request_id={record.get('request_id')}")
            await store.resolve_decision(record["request_id"], "approved", "proof-human")
            print(f"[{tool_name}] human decision recorded: approved (principal=proof-human)")
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
        print("== failed mcp_connect (endpoint refuses instantly) ==")
        report = json.loads(
            await guarded_invoke(
                "mcp_connect",
                {
                    "name": "broken",
                    "url": "http://127.0.0.1:1/mcp",
                    "local_access": True,
                    "connect_timeout": 5,
                    "discovery_timeout": 5,
                    "call_timeout": 5,
                },
            )
        )
        print(json.dumps(report, indent=2))
        generation, configs = service.store.load()
        print(f"store after compensation: generation={generation} configs={configs}")
        print(f"live connection: {service.manager.connection('broken')}")
        final = json.loads(await guarded_invoke("mcp_list", {}))
        print("final mcp_list:", json.dumps(final))
        return 0
    finally:
        clear_execution_context()
        agents_module.current_session_id.reset(session_token)
        agents_module.current_agent_id.reset(agent_token)
        await service.manager.shutdown_all()
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
