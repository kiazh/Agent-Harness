"""Task 3: bounded discovery, challenge parsing, metadata validation, SSRF."""

import asyncio
import json

import pytest

from machina.connectors.models import ConnectorConfig
from machina.connectors.oauth_network import (
    OAuthNetworkClient,
    OAuthNetworkError,
    issuer_metadata_candidates,
    normalize_resource,
    parse_bearer_challenges,
    protected_resource_candidates,
)


def _config(**overrides):
    base = {
        "id": "web",
        "transport": "streamable_http",
        "endpoint": "http://127.0.0.1:9/mcp",
        "local_access": True,
        "oauth": {"client_mode": "auto"},
    }
    base.update(overrides)
    return ConnectorConfig(**base)


class StubTransport:
    def __init__(self):
        self.fetches: list[str] = []
        self.posts: list[str] = []
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.probe_result: tuple = (None, {})

    def route(self, url, status=200, payload=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        self.routes[url] = (status, dict(headers or {}), body)
        return self

    async def fetch(self, url, *, headers=None, timeout=0, max_bytes=0):
        self.fetches.append(url)
        if url not in self.routes:
            return 404, {}, b"{}"
        status, response_headers, body = self.routes[url]
        if len(body) > max_bytes:
            raise OAuthNetworkError("oauth response exceeds size limit")
        encoding = (response_headers.get("content-encoding", "identity") or "identity").lower()
        if encoding not in ("identity", ""):
            raise OAuthNetworkError("compressed oauth responses are not supported")
        return status, response_headers, body

    async def probe(self, endpoint, *, timeout=0):
        return self.probe_result

    async def register(self, url, body, *, timeout=0):
        self.posts.append(url)
        return 200, {"client_id": "dyn-id"}

    async def post(self, url, *, data=None, headers=None, auth=None, timeout=0, max_bytes=0):
        self.posts.append(url)
        return 200, {}, json.dumps({"access_token": "tok", "token_type": "Bearer"}).encode()


def _discovery_routes(
    transport,
    *,
    resource="http://127.0.0.1:9/mcp",
    issuer="http://127.0.0.1:9/auth",
    scopes=("read",),
    registration=True,
):
    prm_url = "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp"
    if prm_url not in transport.routes:
        transport.route(
            prm_url,
            payload={
                "resource": resource,
                "authorization_servers": [issuer],
                "scopes_supported": list(scopes),
            },
        )
    meta = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }
    if registration:
        meta["registration_endpoint"] = f"{issuer}/register"
    from machina.connectors.oauth_network import issuer_metadata_candidates as _candidates

    for candidate in _candidates(issuer):
        transport.route(candidate, payload=meta)
    return meta


def test_challenge_parsing_handles_quoted_commas_and_multiples():
    header = (
        'Bearer resource_metadata="https://auth.example.com/.well-known/oauth-protected-resource", '
        'scope="read write", error="invalid_token", '
        'Bearer realm="other", scope="admin"'
    )
    challenges = parse_bearer_challenges(header)
    assert challenges[0]["resource_metadata"] == (
        "https://auth.example.com/.well-known/oauth-protected-resource"
    )
    assert challenges[0]["scope"] == "read write"
    assert challenges[1]["scope"] == "admin"
    assert parse_bearer_challenges("Basic realm=x") == []
    assert parse_bearer_challenges('Bearer error="a, b", scope="x"')[0]["error"] == "a, b"


def test_header_first_then_path_then_root_discovery_order():
    transport = StubTransport()
    transport.probe_result = (
        401,
        {"www-authenticate": 'Bearer resource_metadata="http://127.0.0.1:9/prm"'},
    )
    transport.route(
        "http://127.0.0.1:9/prm",
        payload={
            "resource": "http://127.0.0.1:9/mcp",
            "authorization_servers": ["http://127.0.0.1:9/auth"],
        },
    )
    transport.route(
        "http://127.0.0.1:9/.well-known/oauth-authorization-server/auth",
        payload={
            "issuer": "http://127.0.0.1:9/auth",
            "authorization_endpoint": "http://127.0.0.1:9/auth/authorize",
            "token_endpoint": "http://127.0.0.1:9/auth/token",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        },
    )
    client = OAuthNetworkClient(local_access=True, transport=transport)
    result = asyncio.run(_discover(client))
    assert result.selected_issuer == "http://127.0.0.1:9/auth"
    assert transport.fetches[0] == "http://127.0.0.1:9/prm"

    # Path/root fallback order with no challenge URL.
    transport2 = StubTransport()
    transport2.probe_result = (200, {})
    transport2.route(
        "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp", status=404, payload={}
    )
    transport2.route(
        "http://127.0.0.1:9/.well-known/oauth-protected-resource",
        payload={
            "resource": "http://127.0.0.1:9/mcp",
            "authorization_servers": ["http://127.0.0.1:9/auth"],
        },
    )
    _discovery_routes(transport2)
    client2 = OAuthNetworkClient(local_access=True, transport=transport2)
    result2 = asyncio.run(_discover(client2))
    assert result2.selected_issuer == "http://127.0.0.1:9/auth"
    path_index = transport2.fetches.index(
        "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp"
    )
    root_index = transport2.fetches.index("http://127.0.0.1:9/.well-known/oauth-protected-resource")
    assert path_index < root_index


async def _discover(client, config=None):
    import asyncio as _asyncio

    return await client.discover(
        config or _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
    )


def test_issuer_oidc_candidate_order():
    candidates = issuer_metadata_candidates("https://auth.example.com/tenant")
    assert candidates == [
        "https://auth.example.com/.well-known/oauth-authorization-server/tenant",
        "https://auth.example.com/.well-known/openid-configuration/tenant",
        "https://auth.example.com/tenant/.well-known/openid-configuration",
    ]
    assert protected_resource_candidates("https://host.example.com/team/mcp") == [
        "https://host.example.com/.well-known/oauth-protected-resource/team/mcp",
        "https://host.example.com/.well-known/oauth-protected-resource",
    ]


def test_ipv6_literal_authority_round_trips_bracketed():
    """IPv6 hosts keep their brackets in normalized and discovery URLs."""
    assert normalize_resource("http://[::1]:8080/mcp") == "http://[::1]:8080/mcp"
    assert normalize_resource("https://[2001:db8::1]/resource") == "https://[2001:db8::1]/resource"
    # Default port dropped, brackets kept.
    assert normalize_resource("https://[::1]:443/mcp") == "https://[::1]/mcp"
    assert protected_resource_candidates("http://[::1]:8080/mcp") == [
        "http://[::1]:8080/.well-known/oauth-protected-resource/mcp",
        "http://[::1]:8080/.well-known/oauth-protected-resource",
    ]
    assert issuer_metadata_candidates("https://[2001:db8::1]/tenant") == [
        "https://[2001:db8::1]/.well-known/oauth-authorization-server/tenant",
        "https://[2001:db8::1]/.well-known/openid-configuration/tenant",
        "https://[2001:db8::1]/tenant/.well-known/openid-configuration",
    ]


async def test_ipv6_discovery_requests_bracketed_authorities():
    """Discovery derives its well-known URLs from the same bracketed base."""
    transport = StubTransport()
    endpoint = "http://[::1]:8080/mcp"
    issuer = "http://[::1]:8080/auth"
    transport.route(
        "http://[::1]:8080/.well-known/oauth-protected-resource/mcp",
        payload={
            "resource": endpoint,
            "authorization_servers": [issuer],
            "scopes_supported": ["read"],
        },
    )
    meta = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }
    for candidate in issuer_metadata_candidates(issuer):
        transport.route(candidate, payload=meta)
    client = OAuthNetworkClient(local_access=True, transport=transport)
    result = await _discover(client, _config(endpoint=endpoint))
    assert result.resource == endpoint
    assert transport.fetches[0] == "http://[::1]:8080/.well-known/oauth-protected-resource/mcp"


def test_strict_mismatch_and_unsafe_urls_fail_closed():
    async def _run():
        import asyncio as _asyncio

        # Resource mismatch.
        transport = StubTransport()
        _discovery_routes(transport, resource="http://127.0.0.1:9/other")
        client = OAuthNetworkClient(local_access=True, transport=transport)
        with pytest.raises(OAuthNetworkError):
            await client.discover(
                _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )
        # Issuer mismatch in server metadata.
        transport2 = StubTransport()
        transport2.route(
            "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp",
            payload={
                "resource": "http://127.0.0.1:9/mcp",
                "authorization_servers": ["http://127.0.0.1:9/auth"],
            },
        )
        transport2.route(
            "http://127.0.0.1:9/.well-known/oauth-authorization-server/auth",
            payload={
                "issuer": "http://127.0.0.1:9/evil",
                "authorization_endpoint": "http://127.0.0.1:9/auth/authorize",
                "token_endpoint": "http://127.0.0.1:9/auth/token",
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
            },
        )
        client2 = OAuthNetworkClient(local_access=True, transport=transport2)
        with pytest.raises(OAuthNetworkError):
            await client2.discover(
                _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )
        # Missing S256 support.
        transport3 = StubTransport()
        meta = _discovery_routes(transport3)
        meta["code_challenge_methods_supported"] = ["plain"]
        transport3.routes["http://127.0.0.1:9/.well-known/oauth-authorization-server/auth"] = (
            200,
            {},
            json.dumps(meta).encode(),
        )
        client3 = OAuthNetworkClient(local_access=True, transport=transport3)
        with pytest.raises(OAuthNetworkError) as excinfo:
            await client3.discover(
                _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )
        assert excinfo.value.reason == "oauth_pkce_unsupported"

    asyncio.run(_run())


def test_fatal_discovery_stops_without_registration_or_token():
    async def _run():
        import asyncio as _asyncio

        for status in (429, 500):
            transport = StubTransport()
            transport.route(
                "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp",
                status=status,
                payload={},
            )
            client = OAuthNetworkClient(local_access=True, transport=transport)
            with pytest.raises(OAuthNetworkError):
                await client.discover(
                    _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
                )
            assert transport.posts == []
        # Malformed JSON stops.
        transport = StubTransport()
        transport.route(
            "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp", raw=b"not json"
        )
        client = OAuthNetworkClient(local_access=True, transport=transport)
        with pytest.raises(OAuthNetworkError):
            await client.discover(
                _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )
        assert transport.posts == []

    asyncio.run(_run())


def test_size_cap_and_compressed_metadata_rejected():
    async def _run():
        import asyncio as _asyncio

        transport = StubTransport()
        transport.route(
            "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp", raw=b"x" * (129 * 1024)
        )
        client = OAuthNetworkClient(local_access=True, transport=transport)
        with pytest.raises(OAuthNetworkError):
            await client.discover(
                _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )
        transport2 = StubTransport()
        transport2.route(
            "http://127.0.0.1:9/.well-known/oauth-protected-resource/mcp",
            payload={
                "resource": "http://127.0.0.1:9/mcp",
                "authorization_servers": ["http://127.0.0.1:9/auth"],
            },
            headers={"content-encoding": "gzip"},
        )
        client2 = OAuthNetworkClient(local_access=True, transport=transport2)
        with pytest.raises(OAuthNetworkError):
            await client2.discover(
                _config(), issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )

    asyncio.run(_run())


def test_ssrf_and_loopback_policy():
    from machina.connectors.oauth_network import validate_outbound_url

    # Literal-loopback http allowed only with local access.
    validate_outbound_url("http://127.0.0.1:9/token", local_access=True)
    with pytest.raises(OAuthNetworkError):
        validate_outbound_url("http://127.0.0.1:9/token", local_access=False)
    # Remote https literal fine; userinfo refused (parse-level, no DNS needed).
    validate_outbound_url("https://93.184.216.1/token", local_access=False)
    with pytest.raises(OAuthNetworkError):
        validate_outbound_url("https://user:pass@93.184.216.1/token", local_access=False)
    with pytest.raises(OAuthNetworkError):
        validate_outbound_url("http://169.254.169.254/token", local_access=True)

    # Public-to-private pivot rejected (literal IPs keep this hermetic).
    async def _run():
        import asyncio as _asyncio

        transport = StubTransport()
        transport.probe_result = (200, {})
        transport.route(
            "https://93.184.216.0/.well-known/oauth-protected-resource/mcp",
            payload={
                "resource": "https://93.184.216.0/mcp",
                "authorization_servers": ["http://127.0.0.1:9/auth"],
            },
        )
        client = OAuthNetworkClient(local_access=False, transport=transport)
        config = _config(endpoint="https://93.184.216.0/mcp", local_access=False)
        with pytest.raises(OAuthNetworkError):
            await client.discover(
                config, issuer=None, deadline=_asyncio.get_running_loop().time() + 20
            )

    asyncio.run(_run())


@pytest.mark.parametrize(
    "capability",
    ["authorization_response_iss_parameter_supported", "client_id_metadata_document_supported"],
)
def test_metadata_capabilities_are_preserved_in_discovery_fingerprint(capability):
    async def run():
        transport = StubTransport()
        meta = _discovery_routes(transport)
        client = OAuthNetworkClient(local_access=True, transport=transport)
        baseline = await _discover(client)
        meta[capability] = True
        for url in issuer_metadata_candidates("http://127.0.0.1:9/auth"):
            transport.route(url, payload=meta)
        advertised = await _discover(client)
        assert advertised.require_iss is (
            capability == "authorization_response_iss_parameter_supported"
        )
        assert advertised.supports_client_metadata_document is (
            capability == "client_id_metadata_document_supported"
        )
        assert advertised.fingerprint != baseline.fingerprint

    asyncio.run(run())
