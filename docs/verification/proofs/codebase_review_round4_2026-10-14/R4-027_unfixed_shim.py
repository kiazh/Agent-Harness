def _shim_points_at(shim_text: str, app_root: str) -> bool:
    """True when a ``~/.local/bin/machina`` shim execs an ``machina`` inside ``app_root``."""
    return app_root.rstrip("/\\").lower() in shim_text.lower()

