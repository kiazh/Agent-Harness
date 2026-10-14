"""Authority composition: one merge, presence/None semantics, no widening.

Regression pins for the consolidated merge_authority helper (adversarial
review H2): tools intersection is presence-aware (None = unrestricted,
[] = zero tools), max_mode only narrows, full_grant_sources union is
order-preserving, and a sandbox base stays sandbox. Also pins the call-site
truthiness fix: an empty definition.tools list must never come back as
"unrestricted".
"""

from __future__ import annotations

from machina.core.agent_factory import cap_child_authority, merge_authority


def test_tools_presence_matrix_never_widens():
    # absent/None = unrestricted on that side
    assert merge_authority({}, {})["tools"] is None
    assert merge_authority({"tools": None}, {})["tools"] is None
    assert merge_authority({}, {"tools": None})["tools"] is None
    # a restricted side always wins
    assert merge_authority({"tools": ["a", "b"]}, {})["tools"] == ["a", "b"]
    assert merge_authority({}, {"tools": ["a"]})["tools"] == ["a"]
    assert merge_authority({"tools": ["a", "b"]}, {"tools": ["b", "c"]})["tools"] == ["b"]
    # explicit empty list is ZERO tools, not unrestricted
    assert merge_authority({}, {"tools": []})["tools"] == []
    assert merge_authority({"tools": ["a"]}, {"tools": []})["tools"] == []


def test_definition_tool_cap_translates_conventions_explicitly():
    """AgentDef: empty tools = every tool (its default). Authority: None =
    unrestricted, [] = zero tools. The translation is a named helper so no
    call site can conflate the two by truthiness (review H2)."""
    from types import SimpleNamespace

    from machina.core.agent_factory import definition_tool_cap

    assert definition_tool_cap(SimpleNamespace(tools=[])) is None
    assert definition_tool_cap(SimpleNamespace(tools=None)) is None
    assert definition_tool_cap(SimpleNamespace(tools=["a", "b"])) == ["a", "b"]
    # The authority layer still treats an explicit [] as zero tools.
    assert cap_child_authority({}, []) == []
    assert cap_child_authority({"tools": None}, []) == []
    assert cap_child_authority({"tools": ["x"]}, []) == []


def test_max_mode_only_narrows_and_survives_one_sided_presence():
    assert merge_authority({"max_mode": "workspace"}, {})["max_mode"] == "workspace"
    assert merge_authority({}, {"max_mode": "sandbox"})["max_mode"] == "sandbox"
    # rank-min in both directions
    assert merge_authority({"max_mode": "sandbox"}, {"max_mode": "ask"})["max_mode"] == "ask"
    assert merge_authority({"max_mode": "ask"}, {"max_mode": "sandbox"})["max_mode"] == "ask"


def test_full_grant_sources_union_is_order_preserving_and_deduped():
    merged = merge_authority(
        {"full_grant_sources": ["s1", "s2"]}, {"full_grant_sources": ["s2", "s3"]}
    )
    assert merged["full_grant_sources"] == ["s1", "s2", "s3"]


def test_sandbox_base_pins_the_terminal_backend():
    merged = merge_authority({"terminal_backend": "sandbox"}, {"terminal_backend": "host"})
    assert merged["terminal_backend"] == "sandbox"
    # and a non-sandbox base lets the overlay choose
    assert merge_authority({}, {"terminal_backend": "host"})["terminal_backend"] == "host"


def test_other_keys_overlay_but_never_tool_shape():
    merged = merge_authority({"a": 1, "tools": ["x"]}, {"a": 2, "extra": 3, "tools": None})
    assert merged["a"] == 2 and merged["extra"] == 3
    # overlay tools=None (unrestricted side) cannot widen the restricted base
    assert merged["tools"] == ["x"]
