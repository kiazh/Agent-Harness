"""``machina auth`` — sign in to a ChatGPT account for the chatgpt provider.

The ChatGPT-account provider reuses the Codex CLI's public OAuth client and
its fixed loopback redirect (``http://localhost:1455/auth/callback``). Three
commands cover the whole surface:

* ``machina auth login`` — browser PKCE flow. ``--no-browser`` prints the URL
  only, ``--timeout`` bounds the callback wait, and ``--import-codex`` is the
  non-interactive path that copies ``~/.codex/auth.json`` (read-only there).
* ``machina auth status`` — one screen (``--json`` for machine use); exit 0
  when signed in, 1 when not.
* ``machina auth logout`` — deletes ONLY ``~/.machina/auth/chatgpt.json``;
  ``~/.codex/auth.json`` is never touched.

The callback listener binds loopback only and never logs a request line (the
one-time code and state travel in the query string); the state check uses
``hmac.compare_digest``. No token value is printed on any path.
"""

from __future__ import annotations

import hmac
import json
import socketserver
import webbrowser
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer
from rich.markup import escape

from machina.cli import output
from machina.cli.output import console
from machina.security import chatgpt_auth

app = typer.Typer(
    name="auth",
    help="Sign in to your ChatGPT account for the chatgpt provider.",
    no_args_is_help=True,
)

EXIT_OK = 0
EXIT_FAILED = 1
DEFAULT_TIMEOUT_SECONDS = 180.0


class _LoopbackHTTPServer(HTTPServer):
    """HTTPServer without the startup reverse-DNS lookup.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)``, which can stall
    for a long time on networks with a hanging resolver — a sign-in must not
    wait on DNS. The server name is cosmetic here.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class _CallbackServer:
    """One-shot loopback listener for the OAuth callback.

    The authorization code and state travel only in the browser's request
    line, so the handler never logs requests. The first request carrying a
    code (or an OAuth error / state mismatch) settles the flow; every other
    request gets a 404.
    """

    def __init__(self, redirect_uri: str, state: str) -> None:
        parsed = urlparse(redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        # Loopback only: the callback carries the one-time authorization code.
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = parsed.port or chatgpt_auth.CALLBACK_PORT
        self.path = parsed.path or chatgpt_auth.CALLBACK_PATH
        self.done = Event()
        self.code: str | None = None
        self.error: str | None = None
        self._state = str(state)
        self._http: HTTPServer | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        """Bind and serve; raises OSError when the port is unavailable."""
        self._http = _LoopbackHTTPServer((self.host, self.port), self._handler())
        self._thread = Thread(
            target=self._http.serve_forever, name="machina-auth-callback", daemon=True
        )
        self._thread.start()

    def wait(self, timeout: float) -> bool:
        """True when the flow settled before *timeout* elapsed."""
        return self.done.wait(timeout)

    def stop(self) -> None:
        http, self._http = self._http, None
        if http is None:
            return
        with suppress(Exception):
            http.shutdown()
        with suppress(Exception):
            http.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        flow = self

        class _Handler(BaseHTTPRequestHandler):
            # Bound request reads so a stalled client cannot hang the server.
            timeout = 10

            def do_GET(self) -> None:  # noqa: N802 — http.server API
                flow._handle(self)

            def log_message(self, *args: Any) -> None:
                pass  # never log the request line: it carries code + state

        return _Handler

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        if parsed.path != self.path:
            self._respond(request, 404, "Not found.")
            return
        query = parse_qs(parsed.query)
        error = (query.get("error") or [""])[0]
        if error:
            self.error = error
            self._respond(request, 200, "Sign-in was not completed. You can close this tab.")
            self.done.set()
            return
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            self._respond(request, 400, "Missing authorization code.")
            return
        if not hmac.compare_digest(state.encode("utf-8"), self._state.encode("utf-8")):
            self.error = "state_mismatch"
            self._respond(
                request, 400, "Sign-in state did not match. Run `machina auth login` again."
            )
            self.done.set()
            return
        self.code = code
        self._respond(request, 200, "Signed in to ChatGPT. You can close this tab.")
        self.done.set()

    @staticmethod
    def _respond(request: BaseHTTPRequestHandler, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><body style='font-family: system-ui; margin: 4rem auto; "
            f"max-width: 32rem;'><p>{message}</p></body></html>"
        ).encode()
        with suppress(BrokenPipeError, ConnectionResetError):
            request.send_response(status)
            request.send_header("Content-Type", "text/html; charset=utf-8")
            request.send_header("Content-Length", str(len(body)))
            request.end_headers()
            request.wfile.write(body)


def _expiry_text(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%d %H:%M UTC")


def _print_signed_in(tokens: dict[str, Any]) -> None:
    account = tokens.get("account_id") or "unknown account"
    output.success(f"Signed in to ChatGPT ({escape(str(account))}).")
    output.status_line("Access token expires", _expiry_text(tokens.get("expires_at")))
    output.muted(f"Tokens live in {chatgpt_auth.auth_path()}; the chatgpt provider uses them.")


def _print_status(report: dict[str, Any]) -> None:
    console.print("[bold]ChatGPT account[/bold]")
    if not report.get("signed_in"):
        output.status_line("Status", "not signed in", "warning")
        console.print("  Sign in with [bold]machina auth login[/bold], or import the Codex CLI's")
        console.print("  credentials with [bold]machina auth login --import-codex[/bold].")
        return
    source = report.get("source")
    if source == "codex":
        detail = "signed in via Codex CLI credentials (imported on first use)"
    else:
        detail = "signed in (Machina token store)"
    if report.get("expired"):
        detail += " — access token expired, refreshes on the next call"
    output.status_line("Status", detail, "success")
    output.status_line("Account", str(report.get("account_id") or "unknown"))
    output.status_line("Expires", _expiry_text(report.get("expires_at")))
    output.status_line("Last refresh", str(report.get("last_refresh") or "—"))


def _import_codex() -> int:
    """Non-interactive ``--import-codex`` path; returns an exit code."""
    try:
        tokens = chatgpt_auth.import_from_codex()
    except OSError as error:  # never expected; keep the command honest
        output.error(f"Import failed: {error}")
        return EXIT_FAILED
    if tokens is None:
        current = chatgpt_auth.status()
        if current.get("signed_in") and current.get("source") == "machina":
            output.error(
                "Machina already has its own ChatGPT tokens; the import was refused.",
                "Run `machina auth logout` first to replace them from the Codex CLI.",
            )
            return EXIT_FAILED
        output.error(
            "No Codex CLI credentials found (~/.codex/auth.json).",
            "Run `codex login` first, or `machina auth login` to sign in here.",
        )
        return EXIT_FAILED
    _print_signed_in(tokens)
    return EXIT_OK


@app.command()
def login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening a browser"
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for the browser callback"
    ),
    import_codex: bool = typer.Option(
        False, "--import-codex", help="Copy ~/.codex/auth.json tokens (non-interactive)"
    ),
) -> None:
    """Sign in with your ChatGPT account (browser PKCE flow)."""
    if import_codex:
        raise typer.Exit(_import_codex())
    if timeout <= 0:
        raise typer.BadParameter("--timeout must be a positive number of seconds")

    request = chatgpt_auth.login_url()
    console.print("[bold]Open this URL in your browser to sign in with ChatGPT:[/bold]")
    console.print(request.url, markup=False, highlight=False)

    server = _CallbackServer(request.redirect_uri, request.state)
    try:
        server.start()
    except OSError as error:
        output.error(
            f"Could not listen on {server.host}:{server.port} for the sign-in callback ({error}).",
            "Close whatever holds that port (another login?), then retry.",
        )
        raise typer.Exit(EXIT_FAILED) from None

    if not no_browser:
        with suppress(Exception):
            if not webbrowser.open(request.url):
                output.muted("Could not open a browser automatically; use the URL above.")
    output.muted(
        f"Waiting for the callback on {escape(server.host)}:{server.port} (up to {timeout:g}s)..."
    )
    try:
        settled = server.wait(timeout)
    finally:
        server.stop()

    if not settled:
        output.error(
            f"Sign-in timed out after {timeout:g}s without a browser callback.",
            "Run `machina auth login` again.",
        )
        raise typer.Exit(EXIT_FAILED)
    if server.error:
        if server.error == "state_mismatch":
            output.error(
                "Sign-in state did not match; the callback was rejected.",
                "Run `machina auth login` again.",
            )
        else:
            output.error(f"Sign-in was not completed ({escape(server.error)}).")
        raise typer.Exit(EXIT_FAILED)
    assert server.code  # a settled flow without error always carries a code
    try:
        tokens = chatgpt_auth.exchange_code(
            server.code,
            code_verifier=request.code_verifier,
            redirect_uri=request.redirect_uri,
        )
    except chatgpt_auth.ChatGPTAuthError as error:
        output.error(str(error))
        raise typer.Exit(EXIT_FAILED) from None
    _print_signed_in(tokens)


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    """Show ChatGPT sign-in status; exit 0 when signed in, 1 otherwise."""
    report = chatgpt_auth.status()
    if json_output:
        typer.echo(json.dumps(report, sort_keys=True))
    else:
        _print_status(report)
    raise typer.Exit(EXIT_OK if report.get("signed_in") else EXIT_FAILED)


@app.command()
def logout() -> None:
    """Remove Machina's ChatGPT token store (never ~/.codex/auth.json)."""
    path = chatgpt_auth.auth_path()
    removed = False
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    except OSError as error:
        output.error(f"Could not remove the Machina ChatGPT token store ({error}).")
        raise typer.Exit(EXIT_FAILED) from None
    if removed:
        output.success(f"Signed out of ChatGPT (removed {path}).")
    else:
        console.print("[yellow]Already signed out (no Machina ChatGPT token store).[/yellow]")
    if chatgpt_auth.status().get("source") == "codex":
        output.muted(
            "Codex CLI credentials (~/.codex/auth.json) were left untouched and will be "
            "imported on the next call."
        )
