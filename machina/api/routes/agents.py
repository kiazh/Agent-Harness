"""Agent inventory endpoint (moved verbatim from app.py; assembled by create_app)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from machina.api.auth import require_api_key

router = APIRouter()


# legacy alias — preserved per the removal plan in routes/__init__.py; do not remove.
@router.get("/agents", dependencies=[Depends(require_api_key)])
async def list_agents() -> dict[str, Any]:
    """List all agents."""
    from machina.core.agent_def import agent_registry

    agents = await agent_registry.list()
    return {"agents": [a.to_dict() for a in agents]}
