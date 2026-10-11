"""Connector configuration validation, registry, and naming (no SDK needed)."""

import pytest

from ah.connectors.models import ConfigError, ConnectorConfig, Trust
from ah.connectors.registry import ConnectorRegistry, namespaced_tool_name


def _stdio(**fields):
    base = {"id": "alpha", "command": "/usr/bin/echo", "argv": ["hi"]}
    base.update(fields)
    return ConnectorConfig(**base)


def _http(**fields):
    base = {"id": "web", "transport": "streamable_http", "endpoint": "https://mcp.example.com/mcp"}
    base.update(fields)
    return ConnectorConfig(**base)


def test_valid_configs_pass():
    _stdio().validate()
    _http().validate()


def test_id_rules():
    for bad in ("", "Alpha", "has space", "a" * 65, "-lead", "semi;colon"):
        with pytest.raises(ConfigError):
            _stdio(id=bad).validate()
    _stdio(id="a1-_ ok".replace(" ", "")).validate()


def test_transport_cross_rules():
    with pytest.raises(ConfigError):
        _stdio(endpoint="https://x.example/mcp").validate()
    with pytest.raises(ConfigError):
        _http(command="/bin/true").validate()
    with pytest.raises(ConfigError):
        _stdio(transport="carrier-pigeon").validate()
    with pytest.raises(ConfigError):
        _http(endpoint="notaurl").validate()
    with pytest.raises(ConfigError):
        _http(endpoint="ftp://x.example/mcp").validate()


def test_stdio_command_rejects_shell_syntax():
    for bad in ("", "echo hi", "a;b", "a|b", "$(x)", "`x`", "a\nb", "a$b"):
        with pytest.raises(ConfigError):
            _stdio(command=bad).validate()


def test_bounds_rejected():
    with pytest.raises(ConfigError):
        _stdio(argv=["x"] * 65).validate()
    with pytest.raises(ConfigError):
        _stdio(argv=["y" * 1025]).validate()
    with pytest.raises(ConfigError):
        _stdio(cwd="relative/path").validate()
    with pytest.raises(ConfigError):
        _stdio(env_allowlist=["lowercase"]).validate()
    with pytest.raises(ConfigError):
        _http(credential_profile="k" * 81).validate()
    with pytest.raises(ConfigError):
        _stdio(tool_allowlist=["has space"]).validate()
    with pytest.raises(ConfigError):
        _stdio(call_timeout=0).validate()
    with pytest.raises(ConfigError):
        _stdio(call_timeout=601).validate()
    with pytest.raises(ConfigError):
        _stdio(result_limit=0).validate()
    with pytest.raises(ConfigError):
        _stdio(concurrency=17).validate()
    with pytest.raises(ConfigError):
        _stdio(max_retries=6).validate()
    with pytest.raises(ConfigError):
        _stdio(trust="mostly").validate()
    with pytest.raises(ConfigError):
        _stdio(revision=-1).validate()
    with pytest.raises(ConfigError):
        _stdio(kind="native").validate()


def test_public_view_carries_no_secrets():
    view = _stdio(secret_refs=["MCP_TOKEN"], display_name="D").public_view()
    assert view["id"] == "alpha"
    assert "MCP_TOKEN" not in str(view.values())
    assert set(view) == {
        "id",
        "display_name",
        "kind",
        "enabled",
        "owner",
        "transport",
        "command",
        "endpoint",
        "credential_profile",
        "local_access",
        "trust",
        "revision",
        "tool_allowlist",
    }


def test_namespaced_names_are_stable_and_collision_safe():
    assert namespaced_tool_name("alpha", "echo") == "alpha__echo"
    assert namespaced_tool_name("alpha", "weird name!") == "alpha__weird_name_"
    assert namespaced_tool_name("alpha", "echo") != namespaced_tool_name("beta", "echo")


def test_registry_crud_and_revisions():
    registry = ConnectorRegistry()
    first = registry.add(_stdio())
    assert first.revision == 0
    with pytest.raises(ConfigError):
        registry.add(_stdio())
    updated = registry.update("alpha", display_name="A2", trust=Trust.TRUSTED_READ.value)
    assert updated.revision == 1
    assert updated.display_name == "A2"
    with pytest.raises(ConfigError):
        registry.update("alpha", no_such_field=1)
    with pytest.raises(ConfigError):
        registry.update("alpha", id="beta")
    with pytest.raises(ConfigError):
        registry.update("missing", display_name="x")
    assert registry.get("alpha") is not None
    assert [c.id for c in registry.list()] == ["alpha"]
    assert registry.remove("alpha") is True
    assert registry.remove("alpha") is False
    assert registry.get("alpha") is None


def test_registry_tool_catalog_bounds_and_isolation():
    from ah.connectors.models import DiscoveredTool

    registry = ConnectorRegistry()
    registry.add(_stdio(id="a"))
    registry.add(_stdio(id="b", command="/bin/true"))
    registry.set_tools("a", [DiscoveredTool(connector_id="a", name="", remote_name="echo")])
    assert registry.lookup("a__echo") is not None
    assert registry.lookup("b__echo") is None
    assert [t.name for t in registry.tools_for("a")] == ["a__echo"]
    with pytest.raises(ConfigError):
        registry.set_tools(
            "a",
            [DiscoveredTool(connector_id="a", name="", remote_name=f"t{i}") for i in range(201)],
        )
    with pytest.raises(ConfigError):
        registry.set_tools("missing", [])
    registry.update("a", display_name="changed")
    assert registry.lookup("a__echo") is None, "config change invalidates catalog"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:synthetic-secret@example.com/mcp",
        "https://example.com/mcp?token=synthetic-secret",
        "https://example.com/mcp#synthetic-secret",
    ],
)
def test_http_endpoints_cannot_embed_credentials(endpoint):
    with pytest.raises(ConfigError):
        _http(endpoint=endpoint).validate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "false"),
        ("local_access", "false"),
        ("call_timeout", True),
        ("concurrency", 1.5),
        ("max_retries", 1.5),
        ("result_limit", 1.5),
    ],
)
def test_configuration_refuses_invalid_runtime_types(field, value):
    with pytest.raises(ConfigError):
        _stdio(**{field: value}).validate()


def test_catalog_name_collision_is_rejected_without_losing_previous_catalog():
    from ah.connectors.models import DiscoveredTool

    registry = ConnectorRegistry()
    registry.add(_stdio())
    old = DiscoveredTool(connector_id="alpha", name="", remote_name="original")
    registry.set_tools("alpha", [old])
    colliding = [
        DiscoveredTool(connector_id="alpha", name="", remote_name=name) for name in ("a.b", "a-b")
    ]
    with pytest.raises(ConfigError, match="collision"):
        registry.set_tools("alpha", colliding)
    assert registry.lookup("alpha__original") is not None
