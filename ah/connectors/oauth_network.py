"""Bounded OAuth network boundary: URL policy, discovery, registration, tokens.

All outbound OAuth traffic uses an explicit httpx.AsyncClient with
follow_redirects=False, trust_env=False, and normal TLS validation.
Every URL is validated before dispatch. Redirects are refused rather
than followed with credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

from ah.connectors.http_transport import run_dns_check
from ah.connectors.oauth_models import (
    OAUTH_MAX_ISSUERS,
    OAUTH_MAX_SCOPES,
    OAUTH_METADATA_FETCH_SECONDS,
    OAUTH_METADATA_MAX_BYTES,
    OAUTH_SCOPE_MAX_CHARS,
    OAUTH_TOKEN_MAX_BYTES,
    OAUTH_TOKEN_MAX_CHARS,
    DiscoveredOAuth,
    OAuthCredentialRecord,
    OAuthReviewSnapshot,
    RegisteredOAuthClient,
)

_METADATA_HOSTS = {"169.254.169.254", "100.100.100.100", "fd00:ec2::254"}
_LOOPBACK_LITERALS = {"127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1"}


class OAuthNetworkError(Exception):
    def __init__(self, message: str, *, reason: str = "oauth_discovery_failed") -> None:
        super().__init__(message)
        self.reason = reason


def normalize_resource(url: str) -> str:
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    out = f"{scheme}://{host}"
    if port:
        out += f":{port}"
    out += path
    return out


def _is_literal_loopback(host: str) -> bool:
    lowered = (host or "").lower().rstrip(".").strip("[]")
    return lowered in _LOOPBACK_LITERALS


def classify_url_host(host: str, *, local_access: bool) -> tuple[str, ...]:
    lowered = (host or "").lower().rstrip(".")
    if lowered in ("localhost",) or lowered.endswith(".localhost"):
        if not local_access:
            raise OAuthNetworkError(
                "loopback oauth destination requires local access",
                reason="oauth_unsafe_destination",
            )
    try:
        ips = [ipaddress.ip_address(lowered.strip("[]"))]
    except ValueError:
        try:
            infos = awaitable_dns(lowered)
        except OSError as exc:
            raise OAuthNetworkError(
                f"cannot resolve oauth host: {lowered}", reason="oauth_unsafe_destination"
            ) from exc
        ips = []
        for info in infos:
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not ips:
            raise OAuthNetworkError(
                f"oauth host resolves to nothing usable: {lowered}",
                reason="oauth_unsafe_destination",
            ) from None
    if lowered in _METADATA_HOSTS:
        raise OAuthNetworkError(
            "cloud metadata endpoints are never oauth targets",
            reason="oauth_unsafe_destination",
        )
    kinds = set()
    for ip in ips:
        if str(ip) in _METADATA_HOSTS:
            raise OAuthNetworkError(
                "cloud metadata endpoints are never oauth targets",
                reason="oauth_unsafe_destination",
            )
        if ip.is_loopback:
            kinds.add("loopback")
            if not local_access:
                raise OAuthNetworkError(
                    "loopback oauth destination requires local access",
                    reason="oauth_unsafe_destination",
                )
            continue
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise OAuthNetworkError(
                f"non-routable oauth address rejected: {ip}",
                reason="oauth_unsafe_destination",
            )
        if not ip.is_global:
            kinds.add("private")
            if not local_access:
                raise OAuthNetworkError(
                    f"private oauth address requires explicit local_access: {ip}",
                    reason="oauth_unsafe_destination",
                )
            continue
        kinds.add("public")
    if len(kinds) > 1:
        raise OAuthNetworkError(
            "oauth host resolves to mixed public/private addresses",
            reason="oauth_unsafe_destination",
        )

    return tuple(dict.fromkeys(str(ip) for ip in ips))


def awaitable_dns(host: str):
    return socket.getaddrinfo(host, None)


def validate_outbound_url(url: str, *, local_access: bool, allow_http_loopback: bool = True) -> str:
    parts = urlparse(url)
    if parts.username or parts.password:
        raise OAuthNetworkError(
            "oauth url must not embed userinfo", reason="oauth_unsafe_destination"
        )
    if parts.scheme == "https":
        pass
    elif (
        parts.scheme == "http"
        and allow_http_loopback
        and _is_literal_loopback(parts.hostname or "")
        and local_access
    ):
        pass
    else:
        raise OAuthNetworkError(
            "oauth url must be https (literal-loopback http only with local access)",
            reason="oauth_unsafe_destination",
        )
    if not parts.hostname:
        raise OAuthNetworkError("oauth url must have a host", reason="oauth_unsafe_destination")
    classify_url_host(parts.hostname, local_access=local_access)
    return url


def parse_bearer_challenges(header_value: str) -> list[dict[str, str]]:
    """Parse WWW-Authenticate challenges handling quoted commas and multiples."""
    challenges: list[dict[str, str]] = []
    current_scheme = ""
    current_params: dict[str, str] = {}
    token = ""
    in_quotes = False
    escaped = False
    items: list[str] = []
    for ch in header_value:
        if escaped:
            token += ch
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            escaped = True
            token += ch
            continue
        if ch == '"':
            in_quotes = not in_quotes
            token += ch
            continue
        if ch == "," and not in_quotes:
            items.append(token.strip())
            token = ""
            continue
        token += ch
    if token.strip():
        items.append(token.strip())
    # Reassemble: a new challenge starts with "<scheme> " (no '=' before space).
    pending: list[str] = []
    for item in items:
        if "=" not in item.split(" ")[0] and " " in item:
            if current_scheme or current_params or pending:
                challenges.append({"scheme": current_scheme, **current_params})
                current_params = {}
                pending = []
            scheme, _, rest = item.partition(" ")
            current_scheme = scheme.strip()
            item = rest.strip()
            if not item:
                continue
        if "=" in item:
            key, _, value = item.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                inner = value[1:-1]
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
                current_params[key] = inner
            else:
                current_params[key] = value
        elif item:
            pending.append(item)
    if current_scheme or current_params:
        challenges.append({"scheme": current_scheme, **current_params})
    return [c for c in challenges if c.get("scheme", "").lower() == "bearer"]


def _scope_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    scopes = tuple(value.split())
    if len(scopes) > OAUTH_MAX_SCOPES:
        raise OAuthNetworkError("scope list exceeds bounds", reason="oauth_scope_not_allowed")
    for scope in scopes:
        if len(scope) > OAUTH_SCOPE_MAX_CHARS:
            raise OAuthNetworkError("scope token too long", reason="oauth_scope_not_allowed")
    return scopes


def protected_resource_candidates(endpoint: str) -> list[str]:
    parts = urlparse(endpoint)
    base = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        base += f":{parts.port}"
    path = parts.path or ""
    candidates = []
    if path and path != "/":
        candidates.append(f"{base}/.well-known/oauth-protected-resource{path}")
    candidates.append(f"{base}/.well-known/oauth-protected-resource")
    return candidates


def issuer_metadata_candidates(issuer: str) -> list[str]:
    parts = urlparse(issuer)
    base = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        base += f":{parts.port}"
    path = (parts.path or "").rstrip("/")
    candidates = []
    if path:
        candidates.append(f"{base}/.well-known/oauth-authorization-server{path}")
        candidates.append(f"{base}/.well-known/openid-configuration{path}")
        candidates.append(f"{base}{path}/.well-known/openid-configuration")
    else:
        candidates.append(f"{base}/.well-known/oauth-authorization-server")
        candidates.append(f"{base}/.well-known/openid-configuration")
    return candidates


@dataclass
class _StubTransport:
    handler: Any = None


class OAuthNetworkClient:
    def __init__(
        self,
        *,
        local_access: bool = False,
        transport=None,
        timeout: float = OAUTH_METADATA_FETCH_SECONDS,
    ) -> None:
        self._local_access = local_access
        self._transport = transport
        self._timeout = timeout
        self._client = None

    async def _get_client(self):
        if self._transport is not None:
            return None
        if self._client is None:
            import httpx

            from ah.connectors.http_transport import pinned_http_transport

            self._client = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                transport=pinned_http_transport(
                    httpx, lambda host: classify_url_host(host, local_access=self._local_access)
                ),
                timeout=self._timeout,
                headers={"Accept-Encoding": "identity"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _validate_url(self, url: str, *, deadline: float, stage: str) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OAuthNetworkError(f"oauth destination check deadline expired during {stage}")
        local_access = self._local_access
        try:
            await run_dns_check(
                lambda: validate_outbound_url(url, local_access=local_access),
                timeout=min(remaining, self._timeout),
            )
        except (TimeoutError, OSError) as exc:
            # Host only: these URLs carry no secrets, but the full URL is
            # still left out of the message to keep logs terse.
            host = urlparse(url).hostname or "unknown host"
            raise OAuthNetworkError(
                f"oauth destination check for {host} timed out or unavailable during {stage}"
            ) from exc

    @staticmethod
    def _check_response(status: int, headers: dict, *, body: bytes, max_bytes: int) -> None:
        if status in (301, 302, 303, 307, 308):
            raise OAuthNetworkError(
                "oauth redirects are not followed", reason="oauth_redirect_unsupported"
            )
        encoding = str(headers.get("content-encoding", "identity") or "identity").lower()
        if encoding not in ("identity", ""):
            raise OAuthNetworkError("compressed oauth responses are not supported")
        if len(body) > max_bytes:
            raise OAuthNetworkError("oauth response exceeds size limit")

    async def _request_bounded(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        deadline: float,
        headers: dict,
        data=None,
        json_body=None,
        request_timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Read identity response chunks under one timeout and cumulative cap."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OAuthNetworkError("oauth deadline expired")
        client = await self._get_client()
        assert client is not None
        timeout = min(remaining, self._timeout if request_timeout is None else request_timeout)
        try:
            async with asyncio.timeout(timeout):
                async with client.stream(
                    method, url, headers=headers, data=data, json=json_body, timeout=timeout
                ) as response:
                    response_headers = dict(response.headers)
                    self._check_response(
                        response.status_code, response_headers, body=b"", max_bytes=max_bytes
                    )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise OAuthNetworkError("oauth response exceeds size limit")
                        chunks.append(chunk)
                    return response.status_code, response_headers, b"".join(chunks)
        except TimeoutError as exc:
            raise OAuthNetworkError("oauth request timed out") from exc

    async def _fetch_json(
        self, url: str, *, max_bytes: int, deadline: float, extra_headers: dict | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        await self._validate_url(url, deadline=deadline, stage="metadata fetch")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OAuthNetworkError("oauth discovery deadline expired")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if extra_headers:
            headers.update(extra_headers)
        if self._transport is not None:
            status, response_headers, body = await self._transport.fetch(
                url, headers=headers, timeout=min(remaining, self._timeout), max_bytes=max_bytes
            )
            self._check_response(status, response_headers, body=body, max_bytes=max_bytes)
            return status, response_headers, body
        return await self._request_bounded(
            "GET", url, max_bytes=max_bytes, deadline=deadline, headers=headers
        )

    async def _post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        max_bytes: int,
        deadline: float,
        auth: tuple[str, str] | None = None,
        extra_headers: dict | None = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        await self._validate_url(url, deadline=deadline, stage="token request")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OAuthNetworkError("oauth deadline expired")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if extra_headers:
            headers.update(extra_headers)
        if auth:
            import base64

            # RFC 6749 section 2.3.1: encode each component as form data
            # before constructing the HTTP Basic user/password pair.
            pair = f"{quote_plus(auth[0], safe='')}:{quote_plus(auth[1], safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("ascii")).decode(
                "ascii"
            )
        if self._transport is not None:
            status, response_headers, body = await self._transport.post(
                url,
                data=data,
                headers=headers,
                auth=None,
                timeout=min(remaining, 10.0),
                max_bytes=max_bytes,
            )
            self._check_response(status, response_headers, body=body, max_bytes=max_bytes)
        else:
            status, _, body = await self._request_bounded(
                "POST",
                url,
                max_bytes=max_bytes,
                deadline=deadline,
                headers=headers,
                data=data,
                request_timeout=10.0,
            )
        if not 200 <= status < 300:
            raise OAuthNetworkError(
                f"oauth request failed: {status}", reason="oauth_exchange_failed"
            )
        if not expect_json:
            return {}
        try:
            return json.loads(body.decode())
        except ValueError as exc:
            raise OAuthNetworkError("oauth response is not valid JSON") from exc

    async def _probe_resource(self, endpoint: str, *, deadline: float):
        await self._validate_url(endpoint, deadline=deadline, stage="resource probe")
        remaining = deadline - asyncio.get_running_loop().time()
        if self._transport is not None and hasattr(self._transport, "probe"):
            return await self._transport.probe(endpoint, timeout=min(remaining, 3.0))

        client = await self._get_client()
        assert client is not None
        try:
            async with asyncio.timeout(min(max(0.0, remaining), 3.0)):
                async with client.stream(
                    "GET", endpoint, headers={"Accept-Encoding": "identity"}
                ) as response:
                    return response.status_code, dict(response.headers)
        except TimeoutError:
            return None, {}

    # ── discovery ───────────────────────────────────────────────
    async def discover(self, config, *, issuer: str | None, deadline: float) -> DiscoveredOAuth:
        from ah.connectors.oauth_models import OAuthSettings

        settings = OAuthSettings.from_dict(getattr(config, "oauth", None) or {})
        if settings is None:
            raise OAuthNetworkError("oauth is not configured", reason="oauth_not_configured")
        endpoint = config.endpoint
        local_access = bool(config.local_access)
        self._local_access = local_access
        resource = normalize_resource(endpoint)
        # Optional challenge probe: bounded GET, inspect headers only.
        challenge_scopes: tuple[str, ...] = ()
        challenge_metadata_url: str | None = None
        try:
            status, headers = await self._probe_resource(endpoint, deadline=deadline)
        except OAuthNetworkError:
            raise
        except Exception:
            status, headers = None, {}
        if status == 401 and headers:
            www = headers.get("www-authenticate", "") or headers.get("WWW-Authenticate", "")
            if www:
                challenges = parse_bearer_challenges(www)
                for challenge in challenges:
                    if challenge.get("scope"):
                        challenge_scopes = _scope_list(challenge["scope"])
                    if challenge.get("resource_metadata"):
                        challenge_metadata_url = challenge["resource_metadata"]
                    if (
                        challenge.get("resource")
                        and normalize_resource(challenge["resource"]) != resource
                    ):
                        raise OAuthNetworkError(
                            "challenge resource does not match configured endpoint",
                            reason="oauth_resource_mismatch",
                        )
        # Protected resource metadata.
        prm_urls: list[str] = []
        if challenge_metadata_url:
            await self._validate_url(
                challenge_metadata_url, deadline=deadline, stage="challenge metadata"
            )
            prm_urls = [challenge_metadata_url]
        else:
            prm_urls = protected_resource_candidates(endpoint)
            for candidate in prm_urls:
                await self._validate_url(
                    candidate, deadline=deadline, stage="protected resource metadata"
                )
        prm: dict[str, Any] | None = None
        prm_fetched = False
        for candidate in prm_urls:
            status, _, body = await self._fetch_json(
                candidate, max_bytes=OAUTH_METADATA_MAX_BYTES, deadline=deadline
            )
            if status in (404, 410):
                continue
            if status == 401:
                raise OAuthNetworkError(
                    "protected metadata requires authentication", reason="oauth_discovery_failed"
                )
            if status == 429 or status >= 500:
                raise OAuthNetworkError(f"protected metadata unavailable: {status}")
            if status != 200:
                raise OAuthNetworkError(f"protected metadata failed: {status}")
            try:
                prm = json.loads(body.decode())
            except ValueError as exc:
                raise OAuthNetworkError("protected metadata is not valid JSON") from exc
            prm_fetched = True
            break
        advertised_scopes: tuple[str, ...] = ()
        issuers: list[str] = []
        fetched_resource = ""
        if prm is not None:
            if not isinstance(prm, dict):
                raise OAuthNetworkError("protected metadata is malformed")
            fetched_resource = str(prm.get("resource", ""))
            if not fetched_resource or normalize_resource(fetched_resource) != resource:
                # Strict policy: reject broader/different resources.
                raise OAuthNetworkError(
                    "protected resource mismatch", reason="oauth_resource_mismatch"
                )
            raw_servers = prm.get("authorization_servers", [])
            if not isinstance(raw_servers, list) or not raw_servers:
                raise OAuthNetworkError("protected metadata has no authorization servers")
            for entry in raw_servers[: OAUTH_MAX_ISSUERS + 1]:
                if not isinstance(entry, str):
                    raise OAuthNetworkError("protected metadata issuer must be a string")
                issuers.append(entry)
            if len(issuers) > OAUTH_MAX_ISSUERS:
                raise OAuthNetworkError("too many advertised issuers")
            raw_scopes = prm.get("scopes_supported", [])
            if isinstance(raw_scopes, list):
                advertised_scopes = tuple(str(s) for s in raw_scopes[: OAUTH_MAX_SCOPES + 1])
                if len(advertised_scopes) > OAUTH_MAX_SCOPES:
                    raise OAuthNetworkError("too many advertised scopes")
        # Issuer selection.
        selected = issuer or settings.issuer or None
        if prm is None and not prm_fetched and challenge_metadata_url is None:
            # Well-known fallback exhausted with all 404s and no challenge URL:
            # discovery failed closed (no guessed /token fallback).
            raise OAuthNetworkError("protected resource metadata not found")
        if selected is None:
            if len(issuers) == 1:
                selected = issuers[0]
            elif len(issuers) > 1:
                # Caller must choose explicitly; surface candidates via exception payload.
                err = OAuthNetworkError(
                    "multiple issuers require selection", reason="oauth_discovery_failed"
                )
                err.candidates = list(issuers)  # type: ignore[attr-defined]
                raise err
            else:
                raise OAuthNetworkError("no authorization server advertised")
        if issuers and selected not in issuers:
            raise OAuthNetworkError(
                "configured issuer is not advertised", reason="oauth_issuer_mismatch"
            )
        await self._validate_url(selected, deadline=deadline, stage="issuer selection")
        # Public-to-private pivot: public resource must not inherit private issuer
        # without local consent.
        try:
            endpoint_public, issuer_private = await run_dns_check(
                lambda: (_host_is_public(endpoint), not _host_is_public(selected)),
                timeout=max(0.0, min(deadline - asyncio.get_running_loop().time(), self._timeout)),
            )
        except (TimeoutError, OSError) as exc:
            raise OAuthNetworkError("oauth destination classification unavailable") from exc
        if endpoint_public and issuer_private and not local_access:
            raise OAuthNetworkError(
                "public resource advertises a private issuer", reason="oauth_unsafe_destination"
            )
        # Authorization server metadata (OAuth then OIDC candidates).
        server_meta: dict[str, Any] | None = None
        for candidate in issuer_metadata_candidates(selected):
            await self._validate_url(
                candidate, deadline=deadline, stage="authorization server metadata"
            )
            status, _, body = await self._fetch_json(
                candidate, max_bytes=OAUTH_METADATA_MAX_BYTES, deadline=deadline
            )
            if status in (404, 410):
                continue
            if status == 429 or status >= 500:
                raise OAuthNetworkError(f"authorization metadata unavailable: {status}")
            if status == 401:
                raise OAuthNetworkError("authorization metadata requires authentication")
            if status != 200:
                raise OAuthNetworkError(f"authorization metadata failed: {status}")
            try:
                server_meta = json.loads(body.decode())
            except ValueError as exc:
                raise OAuthNetworkError("authorization metadata is not valid JSON") from exc
            break
        if server_meta is None:
            raise OAuthNetworkError("authorization server metadata not found")
        advertised_issuer = str(server_meta.get("issuer", ""))
        if advertised_issuer != selected:
            raise OAuthNetworkError(
                "issuer mismatch in server metadata", reason="oauth_issuer_mismatch"
            )
        auth_endpoint = str(server_meta.get("authorization_endpoint", ""))
        token_endpoint = str(server_meta.get("token_endpoint", ""))
        registration_endpoint = str(server_meta.get("registration_endpoint", ""))
        revocation_endpoint = str(server_meta.get("revocation_endpoint", ""))
        if not auth_endpoint or not token_endpoint:
            raise OAuthNetworkError("authorization metadata lacks endpoints")
        for url in (
            [auth_endpoint, token_endpoint]
            + ([registration_endpoint] if registration_endpoint else [])
            + ([revocation_endpoint] if revocation_endpoint else [])
        ):
            await self._validate_url(url, deadline=deadline, stage="endpoint pinning")
        response_types = server_meta.get("response_types_supported", ["code"])
        if "code" not in (response_types or []):
            raise OAuthNetworkError(
                "server does not support authorization code flow", reason="oauth_pkce_unsupported"
            )
        challenge_methods = server_meta.get("code_challenge_methods_supported", [])
        if "S256" not in (challenge_methods or []):
            raise OAuthNetworkError(
                "server does not support S256 PKCE", reason="oauth_pkce_unsupported"
            )
        scopes = challenge_scopes or advertised_scopes
        # Allowlist enforcement happens at preview/start; record requested here.
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "resource": resource,
                    "issuer": selected,
                    "auth": auth_endpoint,
                    "token": token_endpoint,
                    "reg": registration_endpoint,
                    "rev": revocation_endpoint,
                    "scopes": list(scopes),
                    "require_iss": server_meta.get("authorization_response_iss_parameter_supported")
                    is True,
                    "supports_client_metadata_document": server_meta.get(
                        "client_id_metadata_document_supported"
                    )
                    is True,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        require_iss = server_meta.get("authorization_response_iss_parameter_supported") is True
        return DiscoveredOAuth(
            resource=resource,
            issuers=tuple(issuers),
            selected_issuer=selected,
            authorization_endpoint=auth_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=registration_endpoint,
            revocation_endpoint=revocation_endpoint,
            scopes=scopes,
            require_iss=require_iss,
            supports_s256=True,
            fingerprint=fingerprint,
            supports_client_metadata_document=server_meta.get(
                "client_id_metadata_document_supported"
            )
            is True,
        )

    # ── registration / tokens ───────────────────────────────────
    async def register(
        self, snapshot: OAuthReviewSnapshot, *, deadline: float
    ) -> RegisteredOAuthClient:
        from ah.connectors.oauth_models import OAuthSettings  # noqa (schema parity)

        if not snapshot.registration_endpoint:
            raise OAuthNetworkError(
                "server does not advertise dynamic registration", reason="oauth_registration_failed"
            )
        body = {
            "client_name": "AgentHarness",
            "redirect_uris": [snapshot.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

        await self._validate_url(
            snapshot.registration_endpoint, deadline=deadline, stage="client registration"
        )
        remaining = deadline - asyncio.get_running_loop().time()
        if self._transport is not None and hasattr(self._transport, "register"):
            response = await self._transport.register(
                snapshot.registration_endpoint, body, timeout=min(remaining, 10.0)
            )
        else:
            status, _, raw = await self._request_bounded(
                "POST",
                snapshot.registration_endpoint,
                max_bytes=OAUTH_TOKEN_MAX_BYTES,
                deadline=deadline,
                json_body=body,
                request_timeout=10.0,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            try:
                response = (status, json.loads(raw.decode()))
            except ValueError as exc:
                raise OAuthNetworkError("registration response is not JSON") from exc
        if isinstance(response, tuple):
            status, payload = response
        else:
            status, payload = 200, response
        if status and status >= 400:
            raise OAuthNetworkError(
                f"registration failed: {status}", reason="oauth_registration_failed"
            )
        if not isinstance(payload, dict) or not payload.get("client_id"):
            raise OAuthNetworkError(
                "registration response lacks client_id", reason="oauth_registration_failed"
            )
        method = str(payload.get("token_endpoint_auth_method", "none"))
        if method != "none":
            raise OAuthNetworkError(
                "registration returned unsupported auth method", reason="oauth_registration_failed"
            )
        return RegisteredOAuthClient(
            client_id=str(payload["client_id"]),
            token_endpoint_auth_method="none",
            redirect_uri=snapshot.redirect_uri,
        )

    def _validate_token_payload(
        self, payload: dict[str, Any], *, approved: tuple[str, ...]
    ) -> None:
        if not isinstance(payload, dict):
            raise OAuthNetworkError(
                "token response is not an object", reason="oauth_exchange_failed"
            )
        token_type = str(payload.get("token_type", ""))
        if token_type.lower() != "bearer":
            raise OAuthNetworkError("unsupported token type", reason="oauth_exchange_failed")
        access = payload.get("access_token", "")
        if not isinstance(access, str) or not access or len(access) > OAUTH_TOKEN_MAX_CHARS:
            raise OAuthNetworkError(
                "token response has invalid access token", reason="oauth_exchange_failed"
            )
        if any(c in access for c in ("\r", "\n", "\x00")):
            raise OAuthNetworkError(
                "token contains illegal characters", reason="oauth_exchange_failed"
            )
        refresh = payload.get("refresh_token", "")
        if refresh and (
            not isinstance(refresh, str)
            or len(refresh) > OAUTH_TOKEN_MAX_CHARS
            or any(c in refresh for c in ("\r", "\n", "\x00"))
        ):
            raise OAuthNetworkError(
                "token response has invalid refresh token", reason="oauth_exchange_failed"
            )
        expires_in = payload.get("expires_in")
        if expires_in is not None:
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
                raise OAuthNetworkError(
                    "token expires_in must be numeric", reason="oauth_exchange_failed"
                )
            if not (0 < float(expires_in) < 10 * 365 * 24 * 3600):
                raise OAuthNetworkError(
                    "token expires_in out of range", reason="oauth_exchange_failed"
                )
        granted = payload.get("scope")
        if granted:
            granted_scopes = tuple(str(granted).split())
            if set(granted_scopes) - set(approved or granted_scopes):
                raise OAuthNetworkError(
                    "granted scopes exceed approved scopes", reason="oauth_scope_not_allowed"
                )

    async def exchange_code(self, snapshot, client, code: str, verifier: str, *, deadline: float):
        from ah.security.secrets import get_secret as _get_secret

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": snapshot.redirect_uri,
            "client_id": client.client_id,
            "code_verifier": verifier,
            "resource": snapshot.resource,
        }
        auth = None
        extra_secret = ""
        # snapshot may carry token_endpoint_auth_method + secret ref via caller record;
        # Basic is resolved only here.
        method = getattr(client, "token_endpoint_auth_method", "none")
        secret_ref = getattr(
            client,
            "client_secret_ref",
            "",
        )
        if method == "client_secret_basic":
            if not secret_ref:
                raise OAuthNetworkError(
                    "basic auth requires a secret reference",
                    reason="oauth_client_configuration_required",
                )
            extra_secret = _get_secret(secret_ref) or ""
            if not extra_secret:
                raise OAuthNetworkError("client secret unavailable", reason="oauth_exchange_failed")
            auth = (client.client_id, extra_secret)
        payload = await self._post_form(
            snapshot.token_endpoint,
            data,
            max_bytes=OAUTH_TOKEN_MAX_BYTES,
            deadline=deadline,
            auth=auth,
        )
        self._validate_token_payload(payload, approved=snapshot.requested_scopes)
        try:
            from mcp.shared.auth import OAuthToken as _OAuthToken

            return _OAuthToken.model_validate(payload)
        except Exception as exc:
            raise OAuthNetworkError("token response failed validation") from exc

    async def refresh_token(self, record: OAuthCredentialRecord, *, deadline: float):
        if not record.refresh_token:
            raise OAuthNetworkError("no refresh token", reason="oauth_reauthorization_required")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": record.refresh_token,
            "client_id": record.client_id,
            "resource": record.resource,
        }
        auth = None
        if record.token_endpoint_auth_method == "client_secret_basic":
            from ah.security.secrets import get_secret as _get_secret

            secret = _get_secret(record.client_secret_ref) or ""
            if not secret:
                raise OAuthNetworkError("client secret unavailable", reason="oauth_refresh_failed")
            auth = (record.client_id, secret)
        try:
            payload = await self._post_form(
                record.token_endpoint,
                data,
                max_bytes=OAUTH_TOKEN_MAX_BYTES,
                deadline=deadline,
                auth=auth,
            )
        except OAuthNetworkError as exc:
            text = str(exc).lower()
            if "400" in text or "invalid_grant" in text or "invalid grant" in text:
                raise OAuthNetworkError(
                    "refresh rejected", reason="oauth_reauthorization_required"
                ) from exc
            exc.reason = "oauth_refresh_failed"
            raise
        self._validate_token_payload(
            payload, approved=tuple(record.approved_scopes) or tuple(record.granted_scopes)
        )
        try:
            from mcp.shared.auth import OAuthToken as _OAuthToken

            return _OAuthToken.model_validate(payload)
        except Exception as exc:
            raise OAuthNetworkError(
                "refresh response failed validation", reason="oauth_refresh_failed"
            ) from exc

    async def revoke_token(self, record: OAuthCredentialRecord, *, deadline: float) -> str:
        if not record.revocation_endpoint:
            return "unsupported"
        token = record.refresh_token or record.access_token
        if not token:
            return "not_requested"
        data = {"token": token}
        auth = None
        if record.token_endpoint_auth_method == "client_secret_basic":
            from ah.security.secrets import get_secret as _get_secret

            secret = _get_secret(record.client_secret_ref) or ""
            auth = (record.client_id, secret) if secret else None
        try:
            await self._post_form(
                record.revocation_endpoint,
                data,
                max_bytes=OAUTH_TOKEN_MAX_BYTES,
                deadline=deadline,
                auth=auth,
                expect_json=False,
            )
        except OAuthNetworkError:
            return "failed"
        return "succeeded"


def _host_is_public(url: str) -> bool:
    from urllib.parse import urlparse as _up

    host = (_up(url).hostname or "").lower().rstrip(".")
    if _is_literal_loopback(host) or host in ("localhost",) or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return bool(ip.is_global)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    ips = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not ips:
        return True
    return all(ip.is_global for ip in ips)
