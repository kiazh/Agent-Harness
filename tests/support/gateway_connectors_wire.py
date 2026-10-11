"""Run the real gateway stdio entry point without reading operator state."""

from __future__ import annotations

import os
import runpy
from pathlib import Path


def main():
    state = Path(os.environ["MCP_INSPECTION_TEST_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    # Set home lookups before importing ah; do not change the process HOME.
    Path.home = classmethod(lambda cls: state)
    expanduser = os.path.expanduser
    os.path.expanduser = lambda path: (
        str(state) + path[1:]
        if isinstance(path, str) and (path == "~" or path.startswith("~/"))
        else expanduser(path)
    )
    env_file = state / "empty.env"
    env_file.touch()
    os.environ["AH_ENV_FILE"] = str(env_file)
    os.environ["AGENT_HARNESS_SECRET_BACKEND"] = ""
    os.environ["AGENT_HARNESS_TEST_DATABASE_URL"] = ""
    for key in tuple(os.environ):
        if key.endswith("API_KEY") or key in ("DATABASE_URL", "AGENT_HARNESS_DATABASE_URL"):
            os.environ[key] = ""
    runpy.run_module("ah.gateway", run_name="__main__")


if __name__ == "__main__":
    main()
