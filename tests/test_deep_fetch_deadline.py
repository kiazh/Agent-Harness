"""Pinned fetch deadlines hold while a peer keeps making small progress."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from ah.security import fetch


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        (b"HTTP/1.1 200 ", b"\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\nX-Slow: ", b"\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 24\r\n\r\n", b""),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n18\r\n", b"\r\n0\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\n\r\n", b""),
    ],
    ids=["status", "headers", "fixed-body", "chunked-body", "eof-body"],
)
def test_slow_progress_cannot_extend_overall_deadline(prefix, suffix):
    """Buffered reads must not reset the budget each time another byte arrives."""
    reader, writer = socket.socketpair()
    stop = threading.Event()

    def send_finite_response():
        try:
            writer.sendall(prefix)
            for _ in range(24):
                if stop.wait(0.025):
                    return
                writer.sendall(b"a")
            writer.sendall(suffix)
        except OSError:
            pass  # The reader closes promptly when the deadline expires.
        finally:
            writer.close()

    sender = threading.Thread(target=send_finite_response)
    reader.settimeout(0.12)
    start = time.monotonic()
    sender.start()
    error = None
    try:
        try:
            fetch._read_response(reader, 200_000, start + 0.12)
        except fetch.FetchError as caught:
            error = caught
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        reader.close()
        sender.join(timeout=1)

    assert not sender.is_alive()
    assert elapsed < 0.4, f"overall deadline was extended to {elapsed:.3f}s"
    assert isinstance(error, fetch.FetchError)
    assert "overall deadline" in str(error)


def test_connect_failure_closes_allocated_socket(monkeypatch):
    """A failing TCP connect must still release the real socket descriptor."""
    real_socket = socket.socket

    class RefusedSocket(real_socket):
        def connect(self, address):
            raise ConnectionRefusedError("deterministic local transport failure")

    raw = RefusedSocket(socket.AF_INET, socket.SOCK_STREAM)
    monkeypatch.setattr(fetch.socket, "socket", lambda *args, **kwargs: raw)
    monkeypatch.setattr(fetch, "_bounded_resolve", lambda *args, **kwargs: ["93.184.216.34"])
    try:
        with pytest.raises(fetch.FetchError, match="connection.*failed"):
            fetch.pinned_fetch("http://example.com/skill")
        assert raw.fileno() == -1
    finally:
        raw.close()


@pytest.mark.parametrize("encoding", ["fixed", "chunked", "eof"])
def test_read_ahead_preserves_body_and_chunk_boundaries(encoding):
    """Header buffering must preserve coalesced body bytes across recv calls."""
    body = b"x" * 5000 + b"world"
    if encoding == "fixed":
        header = b"Content-Length: 5005\r\n"
        wire_body = body
    elif encoding == "chunked":
        header = b"Transfer-Encoding: chunked\r\n"
        wire_body = b"1388;extension=yes\r\n" + b"x" * 5000 + b"\r\n5\r\nworld\r\n0\r\n\r\n"
    else:
        header = b""
        wire_body = body

    reader, writer = socket.socketpair()
    try:
        writer.sendall(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n" + wire_body)
        writer.close()
        status, _, result = fetch._read_response(reader, 200_000, time.monotonic() + 1)
    finally:
        reader.close()
        writer.close()

    assert status == 200
    assert result == body


def test_tls_and_send_use_budget_remaining_after_connect(monkeypatch):
    """TLS handshake must not inherit the larger budget set before connect."""
    clock = [10.0]
    handshake = []
    sends = []
    real_socket = socket.socket

    class ControlledSocket(real_socket):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

        def connect(self, address):
            clock[0] += 0.4

        def sendall(self, data):
            sends.append(self.gettimeout())

        def recv(self, size):
            data, self.response = self.response[:size], self.response[size:]
            return data

    class ControlledTLS:
        def wrap_socket(self, raw, server_hostname):
            handshake.append((raw.gettimeout(), server_hostname))
            clock[0] += 0.2
            return raw

    raw = ControlledSocket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: clock[0])
            patch.setattr(fetch.socket, "socket", lambda *args, **kwargs: raw)
            patch.setattr(fetch.ssl, "create_default_context", ControlledTLS)
            patch.setattr(fetch, "_bounded_resolve", lambda *args, **kwargs: ["93.184.216.34"])
            response = fetch.pinned_fetch("https://example.com/skill", timeout=1)

        assert response == "ok"
        assert handshake == [(pytest.approx(0.6), "example.com")]
        assert sends == [pytest.approx(0.4)]
        assert raw.fileno() == -1
    finally:
        raw.close()
