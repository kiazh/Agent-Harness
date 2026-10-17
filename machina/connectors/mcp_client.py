"""Official-SDK adapter for MCP servers (stdio + Streamable HTTP).

All transport details stay here. Server-initiated capabilities (sampling,
elicitation, roots) are disabled by construction: no callbacks are ever
passed to ``ClientSession``, so a server that requests them fails closed
instead of reaching a model, a user, or the filesystem.

Imports of the ``mcp`` package are function-local so ``machina.connectors``
imports (and the rest of the suite) work without the optional dependency.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import os
import re
import socket
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from machina.connectors.http_transport import run_dns_check
from machina.connectors.models import (
    MAX_CATALOG_TOOLS,
    MAX_DESCRIPTION_CHARS,
    MAX_STDERR_BYTES,
    AuthRequired,
    CallOutcome,
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorConfig,
    ConnectorToolError,
    DiscoveredTool,
    Transport,
)
from machina.connectors.policy import validate_arguments
from machina.observability.instrumentation import instrument, instrumented, log_event

logger = logging.getLogger(__name__)

_METADATA_HOSTS = {"169.254.169.254", "100.100.100.100", "fd00:ec2::254"}


def _looks_like_auth_failure(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text


def _response_from_exception(error: Exception) -> Any:
    """Find an HTTP response on the exception or its wrapped causes/groups."""
    pending: list[Any] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", None) is not None:
            return response
        exceptions = getattr(current, "exceptions", None)
        if exceptions:
            pending.extend(exceptions)
        for attribute in ("__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)
    return None


def _safe_scope_tokens(value: str) -> tuple[str, ...]:
    """Bound and sanitize a server-provided OAuth scope challenge."""
    tokens: list[str] = []
    for token in (value or "").split():
        if not 1 <= len(token) <= 128:
            continue
        if any(ord(char) < 0x21 or ord(char) > 0x7E or char in '"\\' for char in token):
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= 64:
            break
    return tuple(tokens)


def _auth_challenge_from_error(error: Exception) -> tuple[int | None, str, tuple[str, ...]]:
    """Extract a sanitized 401/403/insufficient_scope challenge, if present.

    A 403 may carry ``WWW-Authenticate: Bearer error="insufficient_scope",
    scope="a b"``. Only the fixed error class and scope tokens are retained;
    the raw header never reaches a client.
    """
    response = _response_from_exception(error)
    text = f"{type(error).__name__}: {error}"
    status: int | None = None
    header = ""
    if response is not None:
        try:
            status = int(response.status_code)
        except (TypeError, ValueError):
            status = None
        headers = getattr(response, "headers", None) or {}
        try:
            header = headers.get("www-authenticate") or headers.get("WWW-Authenticate") or ""
        except Exception:
            header = ""
    if status is None:
        match = re.search(r"\b(401|403)\b", text)
        status = int(match.group(1)) if match else None
    if not header:
        if "insufficient_scope" in text.lower():
            return status, "insufficient_scope", ()
        return status, "", ()
    from machina.connectors.oauth_network import parse_bearer_challenges

    for challenge in parse_bearer_challenges(str(header)):
        error_name = str(challenge.get("error") or "").lower()
        scopes = _safe_scope_tokens(str(challenge.get("scope") or ""))
        # Only the documented step-up signal gets the fixed label; a plain
        # 403 with no error class may still carry a scope challenge.
        if error_name == "insufficient_scope" or (status == 403 and not error_name):
            return status, "insufficient_scope" if (error_name or scopes) else "", scopes
    return status, "", ()


def _default_oauth_coordinator():
    try:
        from machina.core.runtime import runtime_services

        service = runtime_services.get_connector_service()
        return getattr(service, "oauth", None)
    except Exception:
        return None


def _retryable_connect_error(error: Exception) -> bool:
    if isinstance(error, ExceptionGroup):
        return all(_retryable_connect_error(item) for item in error.exceptions)
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OSError) and error.errno in {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }:
        return True
    # Imported only after requiring the SDK; its HTTP transport uses httpx2.
    try:
        from httpx2 import NetworkError, TimeoutException
    except Exception:
        # Optional dependency absent: classify with stdlib types only so the
        # original connection error is never masked by an ImportError.
        return False
    return isinstance(error, (NetworkError, TimeoutException))


def _require_sdk():
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise ConfigError(
            "the 'mcp' package is not installed (pip install 'Machina[connectors]')"
        ) from exc


class _PipeStderr:
    """Bounded server-stderr sink with a real fd (the SDK passes it through).

    A pipe with a draining task: the buffer stops at the cap no matter how
    much a server spews. No disk involved; nothing here is ever logged raw.

    The drain waits for loop-level pipe readiness (``add_reader``), never for a
    default-executor worker: one blocked ``os.read`` per live connector would
    let enough quiet servers occupy every shared worker and stall unrelated
    ``to_thread`` work (H15).
    """

    def __init__(self, cap: int = MAX_STDERR_BYTES) -> None:
        self._cap = cap
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False
        self._read_fd, write_fd = os.pipe()
        self.file = os.fdopen(write_fd, "w", encoding="utf-8", errors="replace")
        self._drainer: asyncio.Task | None = None

    def _consume(self, chunk: bytes) -> None:
        text = chunk.decode("utf-8", "replace")
        if self._size >= self._cap:
            self.truncated = True
            return
        room = self._cap - self._size
        self._chunks.append(text[:room])
        self._size += min(len(text), room)
        if len(text) > room:
            self.truncated = True

    async def _drain_loop(self) -> None:
        loop = asyncio.get_running_loop()
        readable: asyncio.Event | None = None
        try:
            # Non-blocking reads under selector readiness (same discipline as
            # asyncio's own pipe transports): after the pipe drains, the
            # selector can deliver one stale activation and a blocking read
            # there would park the event loop; a spurious wakeup instead
            # surfaces as BlockingIOError and the reader re-arms.
            os.set_blocking(self._read_fd, False)
            ready = asyncio.Event()
            loop.add_reader(self._read_fd, ready.set)
            readable = ready
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # The loop cannot watch raw pipe fds (e.g. a proactor transport):
            # restore blocking mode and use the executor read below.
            try:
                os.set_blocking(self._read_fd, True)
            except (OSError, ValueError):
                pass
            readable = None
        try:
            if readable is None:
                while True:
                    chunk = await loop.run_in_executor(None, os.read, self._read_fd, 65536)
                    if not chunk:
                        return
                    self._consume(chunk)
            while True:
                await readable.wait()
                readable.clear()
                try:
                    chunk = os.read(self._read_fd, 65536)
                except BlockingIOError:
                    # Stale selector activation for an emptied pipe: no data
                    # and not EOF; the reader stays armed for the next write.
                    continue
                if not chunk:
                    return
                self._consume(chunk)
        except (OSError, ValueError):
            return
        finally:
            if readable is not None:
                try:
                    loop.remove_reader(self._read_fd)
                except (OSError, ValueError):
                    pass

    def start(self) -> None:
        if self._drainer is None:
            self._drainer = asyncio.ensure_future(self._drain_loop())

    async def close(self) -> None:
        try:
            self.file.close()
        except Exception:
            pass
        if self._drainer is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._drainer), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._drainer.cancel()
                # Let the reader task unwind (it removes its loop reader)
                # before the fd closes: a reader still registered on a closed
                # descriptor makes the selector unregister a dead fd.
                try:
                    await asyncio.wait_for(asyncio.shield(self._drainer), timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass
        try:
            os.close(self._read_fd)
        except OSError:
            pass

    def read_text(self) -> str:
        return "".join(self._chunks)


def _classify_host(host: str, *, local_access: bool) -> tuple[str, ...]:
    """Reject link-local/metadata/private destinations without scoped consent.

    Return the complete approved IP set for the pinned connection backend.
    Raises ConnectionFailed closed before any connection is opened.
    """
    lowered = (host or "").lower().rstrip(".")
    if lowered in ("localhost",) or lowered.endswith(".localhost"):
        if not local_access:
            raise ConnectionFailed("loopback connectors require explicit local_access")
    try:
        ips = [ipaddress.ip_address(lowered)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(lowered, None)
        except OSError as exc:
            raise ConnectionFailed(f"cannot resolve connector host: {lowered}") from exc
        ips = []
        for info in infos:
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not ips:
            raise ConnectionFailed(
                f"connector host resolves to nothing usable: {lowered}"
            ) from None
    if lowered in _METADATA_HOSTS:
        raise ConnectionFailed("cloud metadata endpoints are never connector targets")
    for ip in ips:
        if str(ip) in _METADATA_HOSTS:
            raise ConnectionFailed("cloud metadata endpoints are never connector targets")
        if ip.is_loopback:
            if not local_access:
                raise ConnectionFailed("loopback connectors require explicit local_access")
            continue
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ConnectionFailed(f"non-routable connector address rejected: {ip}")
        if not ip.is_global:
            if not local_access:
                raise ConnectionFailed(
                    f"private connector address requires explicit local_access: {ip}"
                )
            continue

    return tuple(dict.fromkeys(str(ip) for ip in ips))


class MCPConnection:
    """One worker-local connection to one configured MCP server."""

    def __init__(self, config: ConnectorConfig, oauth_coordinator=None) -> None:
        self.config = config
        self._oauth_coordinator = (
            oauth_coordinator if oauth_coordinator is not None else _default_oauth_coordinator()
        )
        self.state = (
            ConnectionState.DISABLED if not config.enabled else ConnectionState.DISCONNECTED
        )
        self.protocol_version = ""
        self.server_name = ""
        self.server_version = ""
        self.last_error = ""
        # F-26: sanitized scope challenge from the last auth rejection. A 401
        # leaves requiredScopes empty; only a 403 insufficient_scope populates
        # it, for human step-up review (never auto-widen or replay).
        self.auth_status: int | None = None
        self.auth_challenge_error = ""
        self.auth_challenge_scopes: tuple[str, ...] = ()
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._stderr: _PipeStderr | None = None
        self._schemas: dict[str, dict[str, Any]] = {}
        self._owner_task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._stop: asyncio.Event | None = None
        self._secret_values: tuple[str, ...] = ()

    # ── lifecycle ─────────────────────────────────────────────────────

    @instrumented("mcp", "connect")
    async def connect(self, secrets: Mapping[str, str] | None = None) -> None:
        """Spawn/handshake the server within bounded deadlines."""
        self.config.validate()
        log_event(
            "mcp.connect.start",
            level="debug",
            server_id=self.config.id,
            transport=str(getattr(self.config.transport, "value", self.config.transport)),
        )
        if not self.config.enabled:
            raise ConnectionFailed("connector is disabled; disabled servers are never launched")
        _require_sdk()
        if self._owner_task is not None and not self._owner_task.done():
            if self.state == ConnectionState.CONNECTED:
                return
            if self.state != ConnectionState.CONNECTING:
                raise ConnectionFailed("previous connector transport is still retiring")
        else:
            supplied = {
                value for value in (secrets or {}).values() if isinstance(value, str) and value
            }
            self._secret_values = tuple(
                sorted(
                    supplied | {json.dumps(value, ensure_ascii=False)[1:-1] for value in supplied},
                    key=len,
                    reverse=True,
                )
            )
            self._ready = asyncio.get_running_loop().create_future()
            self._stop = asyncio.Event()
            self._owner_task = asyncio.create_task(
                self._run_transport(dict(secrets or {})), name=f"mcp-owner:{self.config.id}"
            )
        self.state = ConnectionState.CONNECTING
        self.last_error = ""
        self.auth_status = None
        self.auth_challenge_error = ""
        self.auth_challenge_scopes = ()
        try:
            assert self._ready is not None
            await asyncio.shield(self._ready)
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except TimeoutError as exc:
            await self._fail(f"connect timed out after {self.config.connect_timeout}s")
            raise ConnectionFailed(self.last_error, retryable=True) from exc
        except (ConfigError, AuthRequired, ConnectionFailed) as exc:
            self.last_error = self._redact(str(exc))
            self.state = (
                ConnectionState.AUTH_REQUIRED
                if isinstance(exc, AuthRequired)
                else ConnectionState.ERROR
            )
            await self.disconnect()
            raise
        except Exception as exc:
            await self._fail(f"connect failed: {type(exc).__name__}")
            raise ConnectionFailed(
                self.last_error, retryable=_retryable_connect_error(exc)
            ) from exc
        self.state = ConnectionState.CONNECTED
        log_event(
            "mcp.connect",
            server_id=self.config.id,
            transport=str(getattr(self.config.transport, "value", self.config.transport)),
            outcome="connected",
            protocol_version=self.protocol_version,
            server_version=self.server_version,
        )

    async def _run_transport(self, secrets: Mapping[str, str]) -> None:
        """Own SDK cancel scopes for their entire lifetime in one task.

        Both entering a transport and closing its exit stack must stay here.
        ``wait_for`` on either coroutine would create another asyncio task;
        the timeout context preserves AnyIO's task ownership instead.
        """
        self._stack = AsyncExitStack()
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                if self.config.transport == Transport.STDIO.value:
                    await self._connect_stdio(secrets)
                else:
                    await self._connect_http(secrets)
            assert self._ready is not None and self._stop is not None
            if not self._ready.done():
                self._ready.set_result(None)
            await self._stop.wait()
        except asyncio.CancelledError:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(ConnectionFailed("connection cancelled"))
        except Exception as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
            else:
                self.last_error = f"transport failed: {type(exc).__name__}"
                self.state = ConnectionState.ERROR
        finally:
            stack, self._stack = self._stack, None
            try:
                if stack is not None:
                    await stack.aclose()
            except Exception as exc:
                self.last_error = f"shutdown failed: {type(exc).__name__}"
                self.state = ConnectionState.ERROR
                logger.warning(
                    "connector %s shutdown issue: %s",
                    self.config.id,
                    type(exc).__name__,
                    exc_info=True,
                )
            finally:
                self._session = None
                self._schemas.clear()
                pipe, self._stderr = self._stderr, None
                if pipe is not None:
                    await pipe.close()
                if self.state not in (
                    ConnectionState.DISABLED,
                    ConnectionState.ERROR,
                    ConnectionState.AUTH_REQUIRED,
                ):
                    self.state = ConnectionState.DISCONNECTED

    async def _fail(self, reason: str) -> None:
        self.last_error = reason
        self.state = ConnectionState.ERROR
        log_event(
            "mcp.connect",
            level="warning",
            server_id=self.config.id,
            outcome="error",
            error_class="ConnectionFailed",
        )
        await self.disconnect()

    async def _new_session(self, read: Any, write: Any) -> Any:
        from mcp import ClientSession

        # No sampling/elicitation/roots callbacks: server-initiated requests
        # fail closed inside the SDK instead of reaching models or users.
        stack = self._stack
        assert stack is not None
        session = await stack.enter_async_context(ClientSession(read, write))
        result = await session.initialize()
        self.protocol_version = str(getattr(result, "protocol_version", ""))
        info = getattr(result, "server_info", None)
        self.server_name = self._redact(str(getattr(info, "name", "")))
        self.server_version = self._redact(str(getattr(info, "version", "")))
        return session

    async def _connect_stdio(self, secrets: Mapping[str, str]) -> None:
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cwd = self.config.cwd.strip()
        if cwd:
            path = Path(cwd)
            if not path.is_absolute() or not path.is_dir():
                raise ConfigError("stdio cwd must be an existing absolute directory")
        env: dict[str, str] = {}
        for name in self.config.env_allowlist:
            if name in os.environ:
                env[name] = os.environ[name]
        for ref in self.config.secret_refs:
            if not secrets.get(ref):
                raise AuthRequired(f"secret '{ref}' was not provided for launch")
            env[ref] = secrets[ref]
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.argv),
            env=env or None,
            cwd=cwd or None,
        )
        pipe = _PipeStderr()
        pipe.start()
        self._stderr = pipe
        assert self._stack is not None
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=pipe.file))
        self._session = await self._new_session(read, write)

    async def _connect_http(self, secrets: Mapping[str, str]) -> None:
        from urllib.parse import urlparse

        from mcp.client.streamable_http import streamable_http_client

        parts = urlparse(self.config.endpoint)
        await run_dns_check(
            lambda: _classify_host(parts.hostname or "", local_access=self.config.local_access),
            timeout=self.config.call_timeout,
        )
        if self.config.credential_profile:
            raise AuthRequired(
                "HTTP credential profiles are not implemented; authentication is deferred"
            )
        import httpx2

        from machina.connectors.http_transport import pinned_http_transport

        oauth_auth = None
        oauth_token_value = ""
        coordinator = getattr(self, "_oauth_coordinator", None)
        if coordinator is None:
            coordinator = _default_oauth_coordinator()
        if getattr(self.config, "oauth", None):
            oauth_auth, oauth_token_value = await self._oauth_preflight(coordinator)
            if oauth_token_value:
                self.register_oauth_token(oauth_token_value)

        async def check_destination(request):
            destination = urlparse(str(request.url))
            if (destination.scheme, destination.hostname, destination.port) != (
                parts.scheme,
                parts.hostname,
                parts.port,
            ):
                raise ConnectionFailed("MCP transport destination changed")
            await run_dns_check(
                lambda: _classify_host(
                    destination.hostname or "", local_access=self.config.local_access
                ),
                timeout=self.config.call_timeout,
            )

        assert self._stack is not None
        client_kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "trust_env": False,
            "transport": pinned_http_transport(
                httpx2, lambda host: _classify_host(host, local_access=self.config.local_access)
            ),
            "timeout": httpx2.Timeout(self.config.call_timeout),
            "event_hooks": {"request": [check_destination]},
        }
        if oauth_auth is not None:
            client_kwargs["auth"] = oauth_auth
        client = await self._stack.enter_async_context(httpx2.AsyncClient(**client_kwargs))
        try:
            streams = await self._stack.enter_async_context(
                streamable_http_client(
                    self.config.endpoint, http_client=client, terminate_on_close=True
                )
            )
        except Exception as exc:
            self._raise_auth_required(exc)
            raise
        read, write = streams
        try:
            self._session = await self._new_session(read, write)
        except Exception as exc:
            self._raise_auth_required(exc)
            raise

    def _capture_auth_challenge(self, exc: Exception) -> None:
        """Record a sanitized 403 scope challenge without changing behavior."""
        status, error_name, scopes = _auth_challenge_from_error(exc)
        if status is not None:
            self.auth_status = status
        if status == 403 and (error_name or scopes):
            self.auth_challenge_error = "insufficient_scope"
            self.auth_challenge_scopes = tuple(self._redact(scope) for scope in scopes)

    def auth_challenge_view(self) -> dict[str, Any] | None:
        """Sanitized connection-local auth evidence for status/preview views."""
        if self.auth_status is None:
            return None
        view: dict[str, Any] = {"status": self.auth_status}
        if self.auth_challenge_error:
            view["error"] = self.auth_challenge_error
        if self.auth_challenge_scopes:
            view["requiredScopes"] = list(self.auth_challenge_scopes)
        return view

    def _raise_auth_required(self, exc: Exception) -> None:
        """Mark AUTH_REQUIRED for transport auth failures; no-op otherwise."""
        if _looks_like_auth_failure(exc):
            self._capture_auth_challenge(exc)
            self.state = ConnectionState.AUTH_REQUIRED
            self.last_error = self._redact("server requires authorization; authorize via /mcp")
            raise AuthRequired(self.last_error) from exc

    async def _oauth_preflight(self, coordinator):
        """Bounded token preflight before dispatch; never opens a browser.

        ``self.config`` is the connection's validated immutable snapshot. The
        coordinator validates the grant against it instead of resolving config
        through ``service.refresh()``: connect dispatch can hold the service
        configuration lock here, and that refresh would re-enter the same
        non-reentrant lock (H13).
        """
        from machina.connectors.oauth_auth import OAuthBearerAuth

        if coordinator is None:
            raise AuthRequired("oauth authorization is required; authorize via /mcp")
        await coordinator.refresh_auth_cache(self.config.id, config=self.config)
        identity = coordinator.auth_identity(self.config.id)
        if identity is None:
            raise AuthRequired("oauth authorization is required; authorize via /mcp")
        from machina.connectors.oauth_models import OAuthBinding

        binding = OAuthBinding(
            connector_id=self.config.id,
            credential_key=str(identity.get("credentialKey", "")),
            resource=str(identity.get("resource", "")),
            issuer=str(identity.get("issuer", "")),
            client_id=str(identity.get("clientId", "")),
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.config.connect_timeout, 15.0)
        try:
            token = await coordinator.ensure_token(
                binding,
                expected_grant=identity,
                deadline=deadline,
                config=self.config,
            )
        except Exception as exc:
            raise AuthRequired(str(exc) or "authorization required") from exc

        def _deadline() -> float:
            return asyncio.get_running_loop().time() + self.config.call_timeout

        return OAuthBearerAuth(
            coordinator,
            binding,
            identity,
            _deadline,
            register_token=self.register_oauth_token,
            config=self.config,
        ), token

    def register_oauth_token(self, token_value: str) -> None:
        if token_value:
            self._secret_values = tuple(
                sorted(
                    set(self._secret_values)
                    | {token_value, json.dumps(token_value, ensure_ascii=False)[1:-1]},
                    key=len,
                    reverse=True,
                )
            )

    @instrumented("mcp", "disconnect")
    async def disconnect(self) -> None:
        """Ask the owner to close; a caller timeout never cancels SDK cleanup."""
        task = self._owner_task
        self._session = None
        self._schemas.clear()
        if task is None:
            if self.state not in (
                ConnectionState.DISABLED,
                ConnectionState.ERROR,
                ConnectionState.AUTH_REQUIRED,
            ):
                self.state = ConnectionState.DISCONNECTED
            log_event(
                "mcp.disconnect",
                level="debug",
                server_id=self.config.id,
                outcome="no_transport",
                state=str(getattr(self.state, "value", self.state)),
            )
            return
        if self._stop is not None:
            self._stop.set()
        if self._ready is not None and not self._ready.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.config.shutdown_timeout)
        except TimeoutError as exc:
            # Keep the owner tracked. Cancelling it here could interrupt the
            # SDK's own bounded process termination and leak a server.
            self.last_error = "shutdown timed out; transport cleanup remains tracked"
            self.state = ConnectionState.DEGRADED
            raise ConnectionFailed(self.last_error) from exc
        if self._owner_task is task:
            self._owner_task = None
        if self._ready is not None and self._ready.done():
            self._ready.exception()  # consume a connect cancellation's exception
        if self.state not in (
            ConnectionState.DISABLED,
            ConnectionState.ERROR,
            ConnectionState.AUTH_REQUIRED,
        ):
            self.state = ConnectionState.DISCONNECTED
        log_event(
            "mcp.disconnect",
            level="debug",
            server_id=self.config.id,
            outcome="closed",
            state=str(getattr(self.state, "value", self.state)),
        )

    # Capped stderr diagnostics; connector tests assert redaction, no production caller. Kept (R4-106).
    def read_stderr(self) -> str:
        """Capped server-stderr text (diagnostics only; never logged raw)."""
        return self._redact(self._stderr.read_text()) if self._stderr is not None else ""

    def _redact(self, text: str) -> str:
        for value in self._secret_values:
            text = text.replace(value, "[REDACTED]")
        return text

    # ── discovery ─────────────────────────────────────────────────────

    async def list_tools(self) -> tuple[list[DiscoveredTool], bool]:
        """Paginated catalog fetch; returns (tools, truncated)."""
        _require_sdk()
        if self._session is None:
            raise ConnectionFailed("not connected")
        from mcp.types import PaginatedRequestParams

        tools: list[DiscoveredTool] = []
        cursor: Any = None
        truncated = False
        deadline = asyncio.get_running_loop().time() + self.config.discovery_timeout
        seen_cursors: set[str] = set()
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                page = await asyncio.wait_for(
                    self._session.list_tools(
                        params=PaginatedRequestParams(cursor=cursor) if cursor else None
                    ),
                    timeout=remaining,
                )
                for entry in page.tools or []:
                    schema = getattr(entry, "input_schema", None) or {}
                    schema_json = json.dumps(schema, default=str, ensure_ascii=False)
                    if (
                        self._redact(entry.name) != entry.name
                        or self._redact(schema_json) != schema_json
                    ):
                        # Rewriting schema values could weaken argument validation
                        # or change an approved dispatch contract. Refuse discovery.
                        raise ConnectionFailed("tool catalog contains credential material")
                    tools.append(
                        DiscoveredTool(
                            connector_id=self.config.id,
                            name="",
                            remote_name=entry.name,
                            description=self._redact(entry.description or "")[
                                :MAX_DESCRIPTION_CHARS
                            ],
                            schema=json.loads(json.dumps(schema, default=str)),
                        )
                    )
                    if len(tools) >= MAX_CATALOG_TOOLS:
                        truncated = True
                        break
                cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
                if truncated or not cursor:
                    break
                if cursor in seen_cursors:
                    raise ConnectionFailed("tool discovery returned a repeated cursor")
                seen_cursors.add(cursor)
        except TimeoutError as exc:
            raise ConnectionFailed("tool discovery timed out") from exc
        except ConnectionFailed:
            raise
        except Exception as exc:
            raise ConnectionFailed(f"tool discovery failed: {type(exc).__name__}") from exc
        self._schemas = {
            tool.remote_name: tool.schema for tool in tools if isinstance(tool.schema, dict)
        }
        return tools, truncated

    # ── calls ─────────────────────────────────────────────────────────

    async def call_tool(self, remote_name: str, args: Mapping[str, Any]) -> CallOutcome:
        """Validate, dispatch with a timeout, and normalize the result."""
        with instrument(
            "mcp",
            "call_tool",
            server_id=self.config.id,
            tool=remote_name,
            arg_count=len(args or {}),
        ) as span:
            _require_sdk()
            if self._session is None:
                raise ConnectionFailed("not connected")
            from mcp import types as mcp_types

            schema = self._schemas.get(remote_name)
            if schema is None:
                # Discovery-first: never construct calls for unseen tools.
                raise ConnectorToolError(f"unknown tool (discover first): {remote_name}")
            validate_arguments(remote_name, dict(args or {}), schema)
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(remote_name, dict(args or {})),
                    timeout=self.config.call_timeout,
                )
            except TimeoutError:
                # Never auto-retry: completion after a timeout is unknowable.
                outcome = CallOutcome(
                    ok=False,
                    outcome_unknown=True,
                    error=f"call timed out after {self.config.call_timeout}s",
                )
                span.note(ok=False, outcome_unknown=True, error_class="TimeoutError")
                return outcome
            except AuthRequired as exc:
                self.state = ConnectionState.AUTH_REQUIRED
                self.last_error = self._redact(str(exc))
                span.note(ok=False, outcome_unknown=True, error_class="AuthRequired")
                return CallOutcome(ok=False, outcome_unknown=True, error=self.last_error)
            except Exception as exc:
                if _looks_like_auth_failure(exc):
                    # Post-dispatch auth failure: conservatively unknown, no replay.
                    # 403 insufficient_scope keeps its challenge for human step-up;
                    # 401 keeps the existing generic auth-required behavior.
                    self._capture_auth_challenge(exc)
                    self.state = ConnectionState.AUTH_REQUIRED
                    self.last_error = self._redact(
                        "server requires authorization; authorize via /mcp"
                    )
                    span.note(ok=False, outcome_unknown=True, error_class="AuthRequired")
                    return CallOutcome(ok=False, outcome_unknown=True, error=self.last_error)
                # Without a terminal result, a transport/protocol failure cannot
                # establish whether the dispatched operation had an effect.
                span.note(ok=False, outcome_unknown=True, error_class=type(exc).__name__)
                return CallOutcome(
                    ok=False, outcome_unknown=True, error=f"call failed: {type(exc).__name__}"
                )
            outcome = self._normalize(remote_name, result, mcp_types)
            span.note(
                ok=outcome.ok,
                outcome_unknown=outcome.outcome_unknown,
                truncated=outcome.truncated,
            )
            return outcome

    def _normalize(self, remote_name: str, result: Any, mcp_types: Any) -> CallOutcome:
        texts: list[str] = []
        structured: Any = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        for block in getattr(result, "content", []) or []:
            if isinstance(block, mcp_types.TextContent):
                texts.append(block.text or "")
            elif isinstance(block, mcp_types.ImageContent):
                texts.append(f"[image omitted: {getattr(block, 'mime_type', '?')}]")
            elif isinstance(block, mcp_types.AudioContent):
                texts.append(f"[audio omitted: {getattr(block, 'mime_type', '?')}]")
            elif isinstance(block, mcp_types.EmbeddedResource):
                texts.append("[embedded resource omitted: fetch explicitly, never auto-fetched]")
            elif isinstance(block, mcp_types.ResourceLink):
                texts.append(f"[resource link omitted: {getattr(block, 'uri', '?')}]")
            else:
                texts.append(f"[unsupported content omitted: {type(block).__name__}]")
        text = self._redact("\n".join(texts))
        if structured is not None:
            structured = json.loads(self._redact(json.dumps(structured, ensure_ascii=False)))
        truncated = False
        encoded = text.encode("utf-8", "replace")
        structured_bytes = (
            len(json.dumps(structured, ensure_ascii=False).encode("utf-8"))
            if structured is not None
            else 0
        )
        if len(encoded) + structured_bytes > self.config.result_limit:
            # The marker shares the byte budget. When the text portion leaves
            # room, serialize structuredContent into that room instead of
            # dropping the tool's only output (combined #41 / original #54).
            marker = b"\n...[truncated]..."[: self.config.result_limit]
            if structured is not None:
                structured_text = json.dumps(structured, ensure_ascii=False).encode("utf-8")
                room = self.config.result_limit - len(encoded) - len(marker)
                if room > 0:
                    prefix = text + ("\n" if text else "")
                    text = (
                        prefix
                        + structured_text[:room].decode("utf-8", "ignore")
                        + marker.decode()
                    )
                else:
                    text = (
                        encoded[: self.config.result_limit - len(marker)].decode(
                            "utf-8", "ignore"
                        )
                        + marker.decode()
                    )
            else:
                text = (
                    encoded[: self.config.result_limit - len(marker)].decode("utf-8", "ignore")
                    + marker.decode()
                )
            structured = None
            truncated = True
        if getattr(result, "is_error", getattr(result, "isError", False)):
            return CallOutcome(
                ok=False, text=text, structured=structured, truncated=truncated, error=text[:500]
            )
        return CallOutcome(ok=True, text=text, structured=structured, truncated=truncated)
