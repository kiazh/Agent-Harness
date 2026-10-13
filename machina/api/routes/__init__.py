"""Per-resource APIRouter modules for the Machina HTTP API.

Split from ``machina.api.app::create_app`` (S4.1) for depth/locality: each
module owns one resource's routes and nothing else. ``create_app()`` only
assembles the routers onto one ``FastAPI`` instance (lifespan + middleware
preserved there).

Legacy aliases
--------------
Routes under the bare ``/*`` prefix (``/sessions``, ``/agents``, ``/rpc``)
are legacy aliases of the versioned ``/api/v1/*`` routes. They are
intentionally preserved byte-for-byte (same paths, auth dependencies,
shapes, status codes).

REMOVAL PLAN (deprecation note, not yet enforced): the legacy aliases are
documented for removal in a future major API version. When that happens:
1. announce the removal window in the release notes,
2. keep serving the aliases with a ``Deprecation`` response header for one
   full minor-version cycle,
3. then delete the ``/*`` decorators, leaving ``/api/v1/*`` canonical.
Until then, every alias below carries a ``# legacy alias`` comment and
must keep passing ``tests/test_api_route_table.py``.
"""

from __future__ import annotations
