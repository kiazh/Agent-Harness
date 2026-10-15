"""R5-J (finding 14): Host authority must carry effective port + IPv6 brackets.

RED-first: against unfixed machina/security/fetch.py (``Host: {hostname}``),
the custom-port and IPv6 cases below FAIL with the wrong authority.
After the fix they pass.
"""

from __future__ import annotations


def _install_fake_transport(monkeypatch, routes):
    """Stub DNS + sockets for pinned_fetch; capture raw request bytes.

    routes: {hostname: (ips, handler)}. Returns (connected, sent_requests).
    """
    import machina.security.fetch as fetchmod

    connected: list[str] = []
    sent_requests: list[bytes] = []

    def _resolve(hostname, timeout=5.0):
        if hostname not in routes:
            raise fetchmod.FetchError(f"unknown host in test: {hostname!r}")
        return list(routes[hostname][0])

    def _ok(body: bytes):
        return (200, {"Content-Length": str(len(body))}, [body])

    class _FakeSock:
        def __init__(self, *a, **k):
            self.sent = b""
            self._recv = b""
            self._pos = 0
            self.host = None

        def settimeout(self, t):
            pass

        def connect(self, addr):
            connected.append(addr[0])
            for host, (ips, handler) in routes.items():
                if addr[0] in ips:
                    self.host = host
                    status, headers, chunks = handler()
                    head = [f"HTTP/1.1 {status} x"]
                    head += [f"{k}: {v}" for k, v in headers.items()]
                    head += ["", ""]
                    self._recv = "\r\n".join(head).encode() + b"".join(chunks)
                    return
            raise OSError("no route")

        def sendall(self, data):
            self.sent += data
            sent_requests.append(data)

        def recv(self, size):
            data = self._recv[self._pos : self._pos + size]
            self._pos += len(data)
            return data

        def close(self):
            pass

    class _FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            return sock

    monkeypatch.setattr(fetchmod, "_bounded_resolve", _resolve)
    # Focus on Host-authority formatting, not IP policy.
    monkeypatch.setattr(fetchmod, "_check_ip_public", lambda ip: None)
    monkeypatch.setattr(fetchmod.socket, "socket", _FakeSock)
    import ssl as _ssl

    monkeypatch.setattr(_ssl, "create_default_context", lambda: _FakeCtx())
    return connected, sent_requests, _ok


def _host_header(raw: bytes) -> str:
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    for line in head.split("\r\n"):
        if line.lower().startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no Host header in request:\n{head}")


def test_r5j_custom_port_in_host(monkeypatch):
    import machina.security.fetch as fetchmod

    _, sent, ok = _install_fake_transport(
        monkeypatch, {"example.com": (["93.184.216.34"], lambda: ok_body())}
    )

    def ok_body():
        return ok(b"hello")

    assert fetchmod.pinned_fetch("http://example.com:8080/s") == "hello"
    assert _host_header(sent[-1]) == "example.com:8080"


def test_r5j_ipv6_literal_bracketed(monkeypatch):
    import machina.security.fetch as fetchmod

    _, sent, ok = _install_fake_transport(
        monkeypatch, {"2001:db8::1": (["2001:db8::1"], lambda: ok(b"v6"))}
    )
    assert fetchmod.pinned_fetch("http://[2001:db8::1]/s") == "v6"
    assert _host_header(sent[-1]) == "[2001:db8::1]"


def test_r5j_ipv6_custom_port(monkeypatch):
    import machina.security.fetch as fetchmod

    _, sent, ok = _install_fake_transport(
        monkeypatch, {"2001:db8::1": (["2001:db8::1"], lambda: ok(b"v6p"))}
    )
    assert fetchmod.pinned_fetch("http://[2001:db8::1]:8080/s") == "v6p"
    assert _host_header(sent[-1]) == "[2001:db8::1]:8080"


def test_r5j_default_ports_elided(monkeypatch):
    import machina.security.fetch as fetchmod

    _, sent, ok = _install_fake_transport(
        monkeypatch, {"example.com": (["93.184.216.34"], lambda: ok(b"x"))}
    )
    assert fetchmod.pinned_fetch("http://example.com:80/s") == "x"
    assert _host_header(sent[-1]) == "example.com"
    assert fetchmod.pinned_fetch("https://example.com:443/s") == "x"
    assert _host_header(sent[-1]) == "example.com"


def test_r5j_https_custom_port_kept(monkeypatch):
    import machina.security.fetch as fetchmod

    _, sent, ok = _install_fake_transport(
        monkeypatch, {"example.com": (["93.184.216.34"], lambda: ok(b"x"))}
    )
    assert fetchmod.pinned_fetch("https://example.com:8443/s") == "x"
    assert _host_header(sent[-1]) == "example.com:8443"
