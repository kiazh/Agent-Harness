"""R5-A A1 RED: tool-supplied mode="full" is forwarded to the child CLI.

Proves finding 1 hop 1: machina_chat accepts model-supplied mode="full" and
forwards it as CLI --mode, with no server-side ceiling. Hop 2 (child mints a
full grant because its in-process config reads "full") is pinned by code
reference (cli/__init__.py:240 set -> :293 grant_full_default) plus the live
policy proof in red_a1_policy_bypass.py.
"""
import sys

sys.path.insert(0, "/Users/shadow/machina")

from machina import mcp_server

cmd = mcp_server._chat_command("hello", None, "full")
print("argv:", cmd)
assert "--mode" in cmd, "expected --mode flag for mode='full'"
assert cmd[cmd.index("--mode") + 1] == "full", "expected full forwarded verbatim"

# No ceiling seam exists pre-fix:
assert not hasattr(mcp_server, "_mcp_mode_ceiling"), "ceiling helper must not exist pre-fix"
assert "MCP_MAX_MODE" not in open("/Users/shadow/machina/machina/mcp_server/__init__.py").read()

print("RED A1-hop1 REPRODUCED: tool mode='full' forwarded as CLI --mode full, no ceiling")
