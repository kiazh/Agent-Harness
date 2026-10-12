"""Exercise the real HTTPX OAuth boundary with adversarial response streams."""

import asyncio
import base64

import httpx
import pytest

from machina.connectors.oauth_models import OAuthCredentialRecord, OAuthReviewSnapshot
from machina.connectors.oauth_network import OAuthNetworkClient, OAuthNetworkError


class ResponseChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def snapshot():
    return OAuthReviewSnapshot(
        connector_id="remote",
        config_revision=1,
        resource="http://127.0.0.1:9/mcp",
        issuer="http://127.0.0.1:9/auth",
        authorization_endpoint="http://127.0.0.1:9/auth/authorize",
        token_endpoint="http://127.0.0.1:9/auth/token",
        registration_endpoint="http://127.0.0.1:9/auth/register",
        revocation_endpoint="http://127.0.0.1:9/auth/revoke",
        redirect_uri="http://127.0.0.1:1234/oauth/callback",
        requested_scopes=(),
        registration_mode="auto",
        client_id="",
        fingerprint="fixture",
    )


@pytest.mark.parametrize("kind", ["metadata", "token", "registration"])
def test_http_response_limit_stops_before_draining_stream(kind):
    async def run():
        stream = ResponseChunks([b"x" * 40000] * 20)
        client = OAuthNetworkClient(local_access=True)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        )
        deadline = asyncio.get_running_loop().time() + 5
        try:
            with pytest.raises(OAuthNetworkError, match="size limit|too large"):
                if kind == "metadata":
                    await client._fetch_json(
                        "http://127.0.0.1:9/meta", max_bytes=65536, deadline=deadline
                    )
                elif kind == "token":
                    await client._post_form(
                        "http://127.0.0.1:9/token", {}, max_bytes=65536, deadline=deadline
                    )
                else:
                    await client.register(snapshot(), deadline=deadline)
            assert stream.read <= 2, "untrusted bytes were buffered beyond the configured cap"
            assert stream.closed
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["metadata", "token", "registration"])
def test_compressed_response_rejected_before_body_is_read(kind):
    async def run():
        # Invalid compressed bytes deliberately fail if HTTPX decodes them eagerly.
        stream = ResponseChunks([b"not a gzip body"])
        client = OAuthNetworkClient(local_access=True)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-encoding": "gzip"}, stream=stream
                )
            )
        )
        deadline = asyncio.get_running_loop().time() + 5
        try:
            with pytest.raises(OAuthNetworkError, match="compressed"):
                if kind == "metadata":
                    await client._fetch_json(
                        "http://127.0.0.1:9/meta", max_bytes=65536, deadline=deadline
                    )
                elif kind == "token":
                    await client._post_form(
                        "http://127.0.0.1:9/token", {}, max_bytes=65536, deadline=deadline
                    )
                else:
                    await client.register(snapshot(), deadline=deadline)
            assert stream.read == 0
            assert stream.closed
        finally:
            await client.aclose()

    asyncio.run(run())


def test_challenge_probe_reads_headers_without_buffering_body():
    async def run():
        stream = ResponseChunks([b"x" * 40000] * 20)
        client = OAuthNetworkClient(local_access=True)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, headers={"www-authenticate": "Bearer scope=read"}, stream=stream
                )
            )
        )
        try:
            status, headers = await client._probe_resource(
                "http://127.0.0.1:9/mcp", deadline=asyncio.get_running_loop().time() + 5
            )
            assert status == 401 and headers["www-authenticate"] == "Bearer scope=read"
            assert stream.read == 0
            assert stream.closed
        finally:
            await client.aclose()

    asyncio.run(run())


def test_revocation_accepts_success_with_empty_non_json_body():
    async def run():
        client = OAuthNetworkClient(local_access=True)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b""))
        )
        try:
            record = OAuthCredentialRecord(
                access_token="fake-token", revocation_endpoint="http://127.0.0.1:9/revoke"
            )
            assert (
                await client.revoke_token(record, deadline=asyncio.get_running_loop().time() + 5)
                == "succeeded"
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_basic_client_auth_uses_form_encoded_components():
    async def run():
        captured = []

        def handle(request):
            captured.append(request.headers["authorization"])
            return httpx.Response(200, json={"access_token": "fake", "token_type": "Bearer"})

        client = OAuthNetworkClient(local_access=True)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            await client._post_form(
                "http://127.0.0.1:9/token",
                {},
                auth=("client: a+é", "s:/ +&é"),
                max_bytes=65536,
                deadline=asyncio.get_running_loop().time() + 5,
            )
            assert (
                base64.b64decode(captured[0].split(" ", 1)[1]).decode()
                == "client%3A+a%2B%C3%A9:s%3A%2F+%2B%26%C3%A9"
            )
        finally:
            await client.aclose()

    asyncio.run(run())


def test_token_request_retains_separate_timeout_from_metadata_fetches():
    class SlowTokenResponse(ResponseChunks):
        async def __aiter__(self):
            await asyncio.sleep(0.03)
            async for chunk in super().__aiter__():
                yield chunk

    async def run():
        client = OAuthNetworkClient(local_access=True, timeout=0.01)
        stream = SlowTokenResponse([b'{"access_token":"fake","token_type":"Bearer"}'])
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        )
        try:
            payload = await client._post_form(
                "http://127.0.0.1:9/token",
                {},
                max_bytes=65536,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            assert payload["access_token"] == "fake"
        finally:
            await client.aclose()

    asyncio.run(run())
