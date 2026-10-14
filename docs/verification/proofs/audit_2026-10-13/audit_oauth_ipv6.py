#!/usr/bin/env python
"""Audit finding: IPv6 OAuth resource URLs lose brackets on normalization."""
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.connectors.oauth_network import normalize_resource

cases = [
    "http://[::1]:8080/mcp",
    "https://[2001:db8::1]/resource",
]
for url in cases:
    print(f"{url!r} -> {normalize_resource(url)!r}")

out = normalize_resource("http://[::1]:8080/mcp")
broken = out == "http://::1:8080/mcp"
print("CLAIM CONFIRMED (invalid IPv6 authority)" if broken else "CLAIM NOT REPRODUCED")
sys.exit(0 if broken else 1)
