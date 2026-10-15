"""Scratch pytest plugin: inject the HEAD (unfixed) ``_shim_points_at``.

Usage: PYTHONPATH=<scratch dir> pytest tests/test_uninstall.py -p red_shim_plugin
"""

from pathlib import Path

import machina.cli as cli

_ns: dict = {}
exec(  # noqa: S102 - trusted local extraction of the HEAD implementation
    Path(__file__).with_name("unfixed_shim.py").read_text(encoding="utf-8"),
    _ns,
)
cli._shim_points_at = _ns["_shim_points_at"]
