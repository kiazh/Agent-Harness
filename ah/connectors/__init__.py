"""MCP connector layer (host/client side).

Connectors let the harness use tools from user-configured MCP servers
through the existing execution, permission, accounting, and lifecycle
boundaries. Status of this release: configuration, runtime lifecycle,
discovery, and policy classification are implemented and tested; agent-loop
tool exposure, remote auth flows, and management surfaces are deferred —
see docs/plans/MCP_CONNECTORS_PROGRESS.md.
"""

from __future__ import annotations
