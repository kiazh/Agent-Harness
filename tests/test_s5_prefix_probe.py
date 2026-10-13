"""S5.3 prefix-cache probe (measure-first, TESTS ONLY — no production changes).

Instruments the stable-prefix size (system prompt + tool schemas) and the
per-call resend bytes on a fixture ReAct turn, then reports the measured
resend totals and the projected saving IF prompt-cache breakpoints existed.

Nothing here touches production code paths: all measurement uses the live
registry/assembler read paths plus an explicit synthetic history fixture
whose size is stated below.
"""

from __future__ import annotations

import json

from machina.core.agent import SYSTEM_PROMPT
from machina.core.assembler import get_token_count
from machina.tools.base import registry

# Fixture turn shape: a 6-round ReAct turn. Each round appends one assistant
# tool-call message plus one tool-result message. The per-round growth figure
# below is the measured size of a representative pair (a `terminal` invocation
# with a 900-char result), stated explicitly so the projection is auditable.
_FIXTURE_ROUNDS = 6
_FIXTURE_CALL = {
    "id": "call-1",
    "function": {"name": "terminal", "arguments": {"command": "pytest -q"}},
}
_FIXTURE_RESULT = {"marker": "x" * 900}


def _tools_payload() -> list[dict]:
    return [
        {"name": d.name, "description": d.description, "parameters": d.parameters}
        for d in registry.get_tool_definitions()
    ]


def _measure() -> dict:
    system_bytes = len(SYSTEM_PROMPT.encode())
    tools_bytes = len(json.dumps(_tools_payload()).encode())
    prefix_bytes = system_bytes + tools_bytes
    growth_bytes = len(json.dumps(_FIXTURE_CALL).encode()) + len(
        json.dumps(_FIXTURE_RESULT).encode()
    )
    calls = []
    history = 0
    for _ in range(_FIXTURE_ROUNDS):
        total = prefix_bytes + history
        calls.append(total)
        history += growth_bytes
    turn_bytes = sum(calls)
    # Bytes 2..K that re-send an identical prefix (the first call must send it).
    resent_prefix_bytes = prefix_bytes * (_FIXTURE_ROUNDS - 1)
    return {
        "n_tools": len(_tools_payload()),
        "system_bytes": system_bytes,
        "tools_bytes": tools_bytes,
        "prefix_bytes": prefix_bytes,
        "prefix_tokens": get_token_count(SYSTEM_PROMPT)
        + get_token_count(json.dumps(_tools_payload())),
        "rounds": _FIXTURE_ROUNDS,
        "growth_bytes_per_round": growth_bytes,
        "per_call_bytes": calls,
        "turn_bytes": turn_bytes,
        "resent_prefix_bytes": resent_prefix_bytes,
        "resent_share": resent_prefix_bytes / turn_bytes,
    }


def test_s5_prefix_cache_probe_reports_numbers():
    m = _measure()
    print("\n[S5.3 prefix-cache probe]")
    print(f"  tools in schema payload : {m['n_tools']}")
    print(f"  system bytes            : {m['system_bytes']}")
    print(f"  tool-schema bytes       : {m['tools_bytes']}")
    print(f"  stable prefix bytes     : {m['prefix_bytes']}")
    print(f"  stable prefix tokens    : {m['prefix_tokens']}")
    print(f"  fixture rounds          : {m['rounds']}")
    print(f"  history growth/round    : {m['growth_bytes_per_round']} bytes")
    print(f"  per-call request bytes  : {m['per_call_bytes']}")
    print(f"  turn total bytes        : {m['turn_bytes']}")
    print(f"  resent prefix bytes     : {m['resent_prefix_bytes']}")
    print(f"  resent share of turn    : {m['resent_share']:.1%}")
    print(
        "  projection IF breakpoints existed: "
        f"{m['resent_prefix_bytes']} bytes (~{m['resent_prefix_bytes'] // 4} tok) "
        "of identical-prefix reprocessing avoidable per fixture turn; "
        "first call still sends the full prefix."
    )
    # Sanity bounds: the probe must measure a real, stable, dominant prefix.
    assert m["prefix_bytes"] > 0
    assert m["per_call_bytes"][0] == m["prefix_bytes"]
    assert all(
        later > earlier for earlier, later in zip(m["per_call_bytes"], m["per_call_bytes"][1:])
    )
    assert m["resent_prefix_bytes"] == m["prefix_bytes"] * (m["rounds"] - 1)
