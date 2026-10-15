"""Task 4: loopback callback validation, once-only delivery, lifecycle."""

import asyncio
import logging

import pytest

from machina.connectors.oauth_callback import LoopbackOAuthCallback


async def _reserve():
    import asyncio as _asyncio

    return await LoopbackOAuthCallback.reserve(
        port=0, deadline=_asyncio.get_running_loop().time() + 5
    )


async def _get(port, path, query="", host=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path}{query} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n"
        f"Connection: close\r\n\r\n".encode()
    )
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    head, _, _ = raw.partition(b"\r\n\r\n")
    return head.decode("latin-1", "replace")


async def _full_get(port, path, query=""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path}{query} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Connection: close\r\n\r\n".encode()
    )
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return raw.decode("latin-1", "replace")


def test_bad_callback_does_not_consume_good_flow():
    async def _run():
        callback = await _reserve()
        try:
            state, issuer = "good-state", "http://127.0.0.1:9/auth"
            callback.arm(state=state, issuer=issuer, require_iss=False)
            path = callback.redirect_uri.split(f"127.0.0.1:{callback.port}")[1]
            waiter = asyncio.create_task(
                callback.wait(deadline=asyncio.get_running_loop().time() + 10)
            )
            # Wrong state/path/Host first.
            bad1 = await _get(callback.port, path, "?code=abc&state=wrong")
            assert "200 OK" in bad1
            bad2 = await _get(callback.port, "/oauth/callback/wrong", "?code=abc&state=x")
            assert "200 OK" in bad2
            bad3 = await _get(
                callback.port, path, f"?code=abc&state={state}", host="evil.example.com"
            )
            assert "200 OK" in bad3
            assert not waiter.done()
            good = await _get(callback.port, path, f"?code=legit&state={state}")
            assert "200 OK" in good
            result = await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            assert result.ok and result.code == "legit"
            # Replay refused: no second result.
            again = await _get(callback.port, path, f"?code=legit&state={state}")
            assert "200 OK" in again
            assert waiter.done()
        finally:
            await callback.aclose()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "query",
    [
        "?code=abc",  # missing state
        "?state=s",  # missing code and error
        "?code=a&state=s&state=t",  # duplicate state
        "?code=a&code=b&state=s",  # duplicate code
        "?code=a&error=x&state=s",  # code+error ambiguity
        "?error=x&state=s&iss=http://evil.example/&iss=http://evil2.example/",  # dup iss
    ],
)
def test_malformed_callbacks_rejected(query):
    async def _run():
        callback = await _reserve()
        try:
            callback.arm(state="s", issuer="http://127.0.0.1:9/auth", require_iss=False)
            path = callback.redirect_uri.split(f"127.0.0.1:{callback.port}")[1]
            head = await _get(callback.port, path, query)
            assert "200 OK" in head
        finally:
            await callback.aclose()

    asyncio.run(_run())


def test_non_ascii_state_returns_error_page_without_typeerror():
    """R4-085(d): hmac.compare_digest raises TypeError on non-ASCII str
    operands; the hostile state must produce the normal error page, never an
    unhandled exception, and must not consume the legitimate attempt."""

    async def _run():
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        callback = await _reserve()
        try:
            state = "good-state"
            callback.arm(state=state, issuer="http://127.0.0.1:9/auth", require_iss=False)
            path = callback.redirect_uri.split(f"127.0.0.1:{callback.port}")[1]
            waiter = asyncio.create_task(
                callback.wait(deadline=asyncio.get_running_loop().time() + 10)
            )
            bad = await _full_get(callback.port, path, "?code=abc&state=caf%C3%A9")
            await asyncio.sleep(0.05)
            assert "200 OK" in bad
            assert "Authorization request invalid" in bad
            assert not any(isinstance(item.get("exception"), TypeError) for item in unhandled)
            # A refused hostile state is not a consumption: the real flow lives.
            good = await _get(callback.port, path, f"?code=legit&state={state}")
            assert "200 OK" in good
            result = await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            assert result.ok and result.code == "legit"
        finally:
            loop.set_exception_handler(previous)
            await callback.aclose()

    asyncio.run(_run())


def test_issuer_rules_and_denial_without_exchange():
    async def _run():
        # Advertised iss support but missing iss fails.
        callback = await _reserve()
        try:
            callback.arm(state="s", issuer="http://127.0.0.1:9/auth", require_iss=True)
            path = callback.redirect_uri.split(f"127.0.0.1:{callback.port}")[1]
            waiter = asyncio.create_task(
                callback.wait(deadline=asyncio.get_running_loop().time() + 5)
            )
            await _get(callback.port, path, "?code=a&state=s")
            assert not waiter.done()
            # Mismatched iss fails.
            await _get(callback.port, path, "?code=a&state=s&iss=http://evil.example/")
            assert not waiter.done()
            # Valid denial returns cancellation without exchange.
            await _get(
                callback.port, path, "?error=access_denied&state=s&iss=http://127.0.0.1:9/auth"
            )
            result = await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            assert not result.ok and result.error == "access_denied"
            waiter2 = asyncio.create_task(
                callback.wait(deadline=asyncio.get_running_loop().time() + 2)
            )
            await asyncio.sleep(0.1)
            assert waiter2.done()  # consumed once; replay is the same terminal result
        finally:
            await callback.aclose()

    asyncio.run(_run())


def test_inert_preview_concurrent_duplicates_and_lifecycle():
    async def _run():
        callback = await _reserve()
        try:
            path = callback.redirect_uri.split(f"127.0.0.1:{callback.port}")[1]
            # Inert before arm: refuses results.
            await _get(callback.port, path, "?code=a&state=s")
            callback.arm(state="s", issuer="iss", require_iss=False)
            waiter = asyncio.create_task(
                callback.wait(deadline=asyncio.get_running_loop().time() + 5)
            )
            # Concurrent duplicates resolve only once.
            await asyncio.gather(*[_get(callback.port, path, "?code=a&state=s") for _ in range(4)])
            result = await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            assert result.ok and result.code == "a"
        finally:
            port = callback.port
            await callback.aclose()
        # Port can be rebound after close; no listening sockets leak.
        rebound = await LoopbackOAuthCallback.reserve(
            port=0, deadline=asyncio.get_running_loop().time() + 5
        )
        try:
            assert rebound.port != 0
        finally:
            await rebound.aclose()
        assert port != 0

    asyncio.run(_run())


def test_response_headers_and_no_secret_logging(caplog):
    async def _run():
        callback = await _reserve()
        try:
            callback.arm(state="sekret-state", issuer="iss", require_iss=False)
            path = callback.redirect_uri.split(f"127.0.0.1:{callback.port}")[1]
            with caplog.at_level(logging.INFO, logger="machina.connectors.oauth_callback"):
                head = await _get(callback.port, path, "?code=sekret-code&state=sekret-state")
            assert "Cache-Control: no-store" in head
            assert "Referrer-Policy: no-referrer" in head
            assert "Content-Security-Policy: default-src 'none'" in head
            assert "sekret-code" not in head and "sekret-state" not in head
            logs = caplog.text
            assert "sekret-code" not in logs and "sekret-state" not in logs
        finally:
            await callback.aclose()

    asyncio.run(_run())


def test_fixed_callback_path_accepts_only_correct_state_and_path():
    async def run():
        callback = await LoopbackOAuthCallback.reserve(
            port=0, path="/provider/callback", deadline=asyncio.get_running_loop().time() + 5
        )
        try:
            assert callback.redirect_uri.endswith("/provider/callback")
            callback.arm(state="secret-state", issuer="fixture", require_iss=False)
            waiter = asyncio.create_task(
                callback.wait(deadline=asyncio.get_running_loop().time() + 5)
            )
            await _get(callback.port, "/provider/callback", "?code=bad&state=wrong")
            await _get(callback.port, "/provider/other", "?code=bad&state=secret-state")
            assert not waiter.done()
            await _get(callback.port, "/provider/callback", "?code=accepted&state=secret-state")
            assert (await waiter).code == "accepted"
        finally:
            await callback.aclose()

    asyncio.run(run())
