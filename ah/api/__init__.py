"""HTTP API server for AgentHarness.

A thin FastAPI layer over the same core services the terminal UI drives through
the JSON-RPC gateway: sessions, prompts (streamed as Server-Sent Events),
context, memory, skills, agents and scheduled jobs. Authentication is a shared
API key supplied via the ``Authorization: Bearer`` header (or ``X-API-Key``).

Run it with ``python -m ah.api`` or ``ah serve``.
"""

from __future__ import annotations

from ah.api.app import create_app

__all__ = ["create_app"]
