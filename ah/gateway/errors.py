"""JSON-RPC error type and codes for the gateway."""

from __future__ import annotations

# JSON-RPC 2.0 standard codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application codes
DATABASE_UNAVAILABLE = 1001
NOT_FOUND = 1002
SESSION_NOT_FOUND = NOT_FOUND
TURN_IN_PROGRESS = 1003
OPERATION_FAILED = 1004
UNAUTHORIZED = 1005


class RpcError(Exception):
    """An error returned to the client as a JSON-RPC error object."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
