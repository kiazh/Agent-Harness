"""JSON-RPC gateway between the TypeScript UI (``ui/``) and the agent core.

Run with ``python -m ah.gateway``; see ``ah.gateway.server`` for the protocol.
"""

from ah.gateway.server import Gateway, RpcError, history_from_chunks, session_to_dict

__all__ = ["Gateway", "RpcError", "history_from_chunks", "session_to_dict"]
