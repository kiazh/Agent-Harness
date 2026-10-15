"""``machina auth`` — sign in to subscription providers for their LLM providers.

Two providers are covered, each with ``login|status|logout``:

* ``chatgpt`` — the Codex CLI's public OAuth client (top-level ``login`` /
  ``status`` / ``logout`` remain as aliases for this one).
* ``copilot`` — GitHub's device flow (or a read-only ``gh auth token`` import)
  for a Copilot seat.

Every login listener binds loopback only and never logs a request line (the
one-time code and state travel in the query string); the state check uses
``hmac.compare_digest``. No token value is printed on any path. ``logout``
only ever removes Machina's own token store — the reference CLIs' credential
files (``~/.codex/auth.json``, the gh keyring) are never touched.

The Claude Code (Claude Pro/Max) and gemini-cli (Gemini Code Assist) OAuth
flows were removed 2026-10-14: both vendors' terms declare third-party reuse
of their subscription tokens not permitted. Their API-key providers
(``anthropic``, ``google``) are unaffected.
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
from machina.security import chatgpt_auth, copilot_auth

app = typer.Typer(
    name="auth",
    help="Sign in to subscription LLM providers (chatgpt, copilot).",
    no_args_is_help=True,
)

EXIT_OK = 0
EXIT_FAILED = 1
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_DEVICE_TIMEOUT_SECONDS = 300.0


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
    output.success(f"Signed in to ChatGPT ({account}).")
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
    # Capture the store's authentication generation: a logout in this or
    # another process bumps it, and the exchange then refuses to publish
    # (R3-2) instead of resurrecting revoked credentials.
    generation = chatgpt_auth.credential_generation()
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
            output.error(f"Sign-in was not completed ({server.error}).")
        raise typer.Exit(EXIT_FAILED)
    assert server.code  # a settled flow without error always carries a code
    try:
        tokens = chatgpt_auth.exchange_code(
            server.code,
            code_verifier=request.code_verifier,
            redirect_uri=request.redirect_uri,
            generation=generation,
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
    """Remove Machina's ChatGPT token store (never ~/.codex/auth.json).

    Removal bumps the store's authentication generation (durably, next to the
    store) before the unlink, so a login exchange or refresh already in
    flight in this or another process can never recreate the store.
    """
    path = chatgpt_auth.auth_path()
    try:
        removed = chatgpt_auth.logout(path=path)
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


# ─── chatgpt sub-app (aliases for the top-level commands above) ─────────────

chatgpt_app = typer.Typer(
    name="chatgpt",
    help="Sign in to your ChatGPT account for the chatgpt provider.",
    no_args_is_help=True,
)
chatgpt_app.command("login")(login)
chatgpt_app.command("status")(status)
chatgpt_app.command("logout")(logout)
app.add_typer(chatgpt_app, name="chatgpt")


# ─── copilot ─────────────────────────────────────────────────────────────────


def _print_copilot_signed_in(tokens: dict[str, Any]) -> None:
    account = tokens.get("account") or "unknown account"
    family = tokens.get("token_family") or "unknown"
    output.success(f"Signed in to GitHub Copilot ({account}, {family} token).")
    output.muted(f"Tokens live in {copilot_auth.auth_path()}; the copilot provider uses them.")


def _print_copilot_status(report: dict[str, Any]) -> None:
    console.print("[bold]GitHub Copilot[/bold]")
    if not report.get("signed_in"):
        output.status_line("Status", "not signed in", "warning")
        console.print("  Sign in with [bold]machina auth copilot login[/bold] (device flow), or")
        console.print(
            "  import the gh CLI token with [bold]machina auth copilot login --import-gh[/bold]."
        )
        return
    source = report.get("source")
    if source == "gh":
        detail = "signed in via the gh CLI credential (imported on first use)"
    else:
        detail = "signed in (Machina token store)"
    output.status_line("Status", detail, "success")
    output.status_line("Account", str(report.get("account") or "unknown"))
    output.status_line("Token family", str(report.get("token_family") or "unknown"))
    output.status_line("Last refresh", str(report.get("last_refresh") or "—"))


def _import_gh() -> int:
    """Non-interactive ``--import-gh`` path; returns an exit code."""
    try:
        tokens = copilot_auth.import_from_gh_cli(fetch_login=True)
    except copilot_auth.CopilotAuthError as error:
        output.error(str(error))
        return EXIT_FAILED
    except OSError as error:  # never expected; keep the command honest
        output.error(f"Import failed: {error}")
        return EXIT_FAILED
    if tokens is None:
        current = copilot_auth.status(probe_gh=False)
        if current.get("signed_in") and current.get("source") == "machina":
            output.error(
                "Machina already has its own Copilot token; the import was refused.",
                "Run `machina auth copilot logout` first to replace it from gh.",
            )
            return EXIT_FAILED
        output.error(
            "No gh CLI credential found (or `gh auth token` returned nothing).",
            "Run `gh auth login` first, or `machina auth copilot login` for the device flow.",
        )
        return EXIT_FAILED
    _print_copilot_signed_in(tokens)
    return EXIT_OK


copilot_app = typer.Typer(
    name="copilot",
    help="Sign in to your GitHub Copilot seat for the copilot provider.",
    no_args_is_help=True,
)
app.add_typer(copilot_app, name="copilot")


@copilot_app.command("login")
def copilot_login(
    import_gh: bool = typer.Option(
        False, "--import-gh", help="Copy the token `gh auth token` holds (non-interactive)"
    ),
    timeout: float = typer.Option(
        DEFAULT_DEVICE_TIMEOUT_SECONDS,
        "--timeout",
        help="Seconds to wait for the device authorization",
    ),
) -> None:
    """Sign in with GitHub's device flow (no browser listener needed)."""
    if import_gh:
        raise typer.Exit(_import_gh())
    if timeout <= 0:
        raise typer.BadParameter("--timeout must be a positive number of seconds")

    # Capture the store's authentication generation BEFORE the flow starts: a
    # logout while the user authorizes bumps it, and the completion below then
    # refuses to recreate the store.
    generation = copilot_auth.credential_generation()
    try:
        payload = copilot_auth.device_flow_start()
    except copilot_auth.CopilotAuthError as error:
        output.error(str(error))
        raise typer.Exit(EXIT_FAILED) from None
    verification_uri = str(payload.get("verification_uri") or "https://github.com/login/device")
    user_code = str(payload.get("user_code") or "")
    console.print("[bold]Authorize Machina for GitHub Copilot:[/bold]")
    console.print(f"  1. Open {verification_uri}", markup=False, highlight=False)
    console.print(f"  2. Enter the code: [bold]{escape(user_code)}[/bold]")
    output.muted(f"Waiting for authorization (up to {timeout:g}s)...")
    try:
        token = copilot_auth.device_flow_poll(
            str(payload.get("device_code") or ""),
            interval=float(payload.get("interval") or copilot_auth.DEVICE_POLL_INTERVAL),
            timeout=timeout,
        )
        account = copilot_auth.fetch_account(token)
        tokens = copilot_auth.publish_device_token(token, account=account, generation=generation)
    except copilot_auth.CopilotAuthError as error:
        output.error(str(error))
        raise typer.Exit(EXIT_FAILED) from None
    _print_copilot_signed_in(tokens)


@copilot_app.command("status")
def copilot_status(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
) -> None:
    """Show Copilot sign-in status; exit 0 when signed in, 1 otherwise."""
    report = copilot_auth.status()
    if json_output:
        typer.echo(json.dumps(report, sort_keys=True))
    else:
        _print_copilot_status(report)
    raise typer.Exit(EXIT_OK if report.get("signed_in") else EXIT_FAILED)


@copilot_app.command("logout")
def copilot_logout() -> None:
    """Remove Machina's Copilot token store (never gh's credential store).

    Removal bumps the store's authentication generation before the unlink, so
    a device flow completing afterwards writes nothing. The gh CLI credential
    itself is untouched and is re-imported on the next call.
    """
    path = copilot_auth.auth_path()
    try:
        removed = copilot_auth.logout(path=path)
    except OSError as error:
        output.error(f"Could not remove the Machina Copilot token store ({error}).")
        raise typer.Exit(EXIT_FAILED) from None
    if removed:
        output.success(f"Signed out of GitHub Copilot (removed {path}).")
    else:
        console.print("[yellow]Already signed out (no Machina Copilot token store).[/yellow]")
    if copilot_auth.status().get("source") == "gh":
        output.muted(
            "The gh CLI credential was left untouched and will be imported on the next call."
        )
