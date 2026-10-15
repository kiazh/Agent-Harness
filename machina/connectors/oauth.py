"""OAuth coordinator: immutable previews, owner-bound operations, refresh.

Human authorization is separated from MCP transport and tool execution.
The bearer adapter (oauth_auth) yields the resource request exactly once
and never replays after 401/refresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from machina.connectors.oauth_callback import LoopbackOAuthCallback
from machina.connectors.oauth_models import (
    OAUTH_EXCHANGE_SECONDS,
    OAUTH_LOCK_SECONDS,
    OAUTH_MAX_OPERATIONS,
    OAUTH_MAX_PREVIEWS,
    OAUTH_OPERATION_TTL_SECONDS,
    OAUTH_PREVIEW_DISCOVERY_SECONDS,
    OAUTH_PREVIEW_TTL_SECONDS,
    OAUTH_REFRESH_SKEW_SECONDS,
    OAUTH_TERMINAL_RETENTION_SECONDS,
    OAuthBinding,
    OAuthCredentialRecord,
    OAuthOwner,
    OAuthReviewSnapshot,
    OAuthSettings,
    RegisteredOAuthClient,
)
from machina.connectors.oauth_network import (
    OAuthNetworkClient,
    OAuthNetworkError,
    normalize_resource,
)
from machina.connectors.oauth_store import OAuthCredentialStore, credential_key_for
from machina.observability.diagnostics import record_failure


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_expiry(value: str) -> datetime | None:
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


class OAuthError(Exception):
    def __init__(self, message: str, *, reason: str = "oauth_exchange_failed") -> None:
        super().__init__(message)
        self.reason = reason


class OAuthCoordinator:
    def __init__(
        self,
        *,
        config_resolver=None,
        service=None,
        credential_store: OAuthCredentialStore | None = None,
        network_factory=None,
        callback_factory=None,
        browser_opener=None,
        now=None,
    ) -> None:
        self._config_resolver = config_resolver
        self._service = service
        self._creds = credential_store or OAuthCredentialStore()
        self._network_factory = network_factory or (
            lambda *, local_access: OAuthNetworkClient(local_access=local_access)
        )
        self._callback_factory = callback_factory or LoopbackOAuthCallback
        self._browser = browser_opener or (lambda url: webbrowser.open(url, new=1))
        self._now = now or _utcnow
        self._previews: dict[str, dict[str, Any]] = {}
        self._operations: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._auth_cache: dict[str, dict[str, Any]] = {}
        # Current refresh-lock owner per credential key (task identity): an
        # indirect preflight from inside that task must not wait on itself.
        self._refresh_owners: dict[str, asyncio.Task] = {}
        self._retention_tasks: set[asyncio.Task] = set()
        self._shutting_down = False

    # ── config resolution ───────────────────────────────────────
    async def _resolve_config(self, connector_id: str):
        if self._config_resolver is not None:
            result = self._config_resolver(connector_id)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        if self._service is not None:
            await self._service.refresh()
            config = self._service.registry.get(connector_id)
            if config is None:
                raise OAuthError(
                    f"unknown connector: {connector_id}", reason="oauth_operation_not_found"
                )
            return config
        raise OAuthError("no connector resolver", reason="oauth_operation_not_found")

    def _binding_for(self, config, *, issuer: str, client_id: str) -> OAuthBinding:
        resource = normalize_resource(config.endpoint)
        key = credential_key_for(
            connector_id=config.id, resource=resource, issuer=issuer, client_id=client_id
        )
        return OAuthBinding(
            connector_id=config.id,
            credential_key=key,
            resource=resource,
            issuer=issuer,
            client_id=client_id,
        )

    @staticmethod
    def _digest_of(snapshot: OAuthReviewSnapshot, *, config_revision: int) -> str:
        canonical = json.dumps(
            {
                "connector": snapshot.connector_id,
                "revision": config_revision,
                "resource": snapshot.resource,
                "issuer": snapshot.issuer,
                "auth": snapshot.authorization_endpoint,
                "token": snapshot.token_endpoint,
                "reg": snapshot.registration_endpoint,
                "rev": snapshot.revocation_endpoint,
                "redirect": snapshot.redirect_uri,
                "scopes": list(snapshot.requested_scopes),
                "mode": snapshot.registration_mode,
                "client": snapshot.client_id,
                "fingerprint": snapshot.fingerprint,
                "requireIss": snapshot.require_iss,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    # ── preview ─────────────────────────────────────────────────
    async def preview(
        self, connector_id: str, owner: OAuthOwner, *, issuer: str | None = None
    ) -> dict[str, Any]:
        config = await self._resolve_config(connector_id)
        settings = OAuthSettings.from_dict(getattr(config, "oauth", None))
        if settings is None:
            raise OAuthError("oauth is not configured", reason="oauth_not_configured")
        if issuer is not None and (not isinstance(issuer, str) or len(issuer) > 2048):
            raise OAuthError("invalid issuer", reason="oauth_issuer_mismatch")
        async with self._lock:
            # FM3-RF6-F2: reap expired previews (stale loopback listeners/FDs)
            # before creating another, then one pending preview per
            # connector/session: replace older.
            await self._sweep_previews_locked()
            for pid, prev in list(self._previews.items()):
                if prev["connector_id"] == connector_id and prev["owner"] == owner:
                    await self._close_preview_locked(pid)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + OAUTH_PREVIEW_DISCOVERY_SECONDS
        network = self._network_factory(local_access=bool(config.local_access))
        try:
            try:
                discovered = await network.discover(config, issuer=issuer, deadline=deadline)
            except OAuthNetworkError as exc:
                if getattr(exc, "candidates", None) and issuer is None and not settings.issuer:
                    return {"issuers": list(exc.candidates)}  # type: ignore[attr-defined]
                raise OAuthError(
                    str(exc), reason=getattr(exc, "reason", "oauth_discovery_failed")
                ) from exc
            except ValueError as exc:
                # Defence in depth: an endpoint that bypassed
                # ConnectorConfig.validate (legacy in-memory config) must
                # still refuse as a typed OAuth error, never a raw ValueError.
                raise OAuthError(
                    "connector endpoint is invalid", reason="oauth_discovery_failed"
                ) from exc
            # Scope allowlist authority: empty allowlist requires explicit review
            # of discovered scopes (caller reviews them); non-empty must contain.
            requested = tuple(discovered.scopes)
            if settings.scope_allowlist:
                missing = set(requested) - set(settings.scope_allowlist)
                if missing:
                    raise OAuthError(
                        f"requested scopes not in allowlist: {sorted(missing)}",
                        reason="oauth_scope_not_allowed",
                    )
            mode = self._registration_mode(settings, discovered)
            # Pre-registered providers require their exact registered path.
            # Bound the callback reservation (5s): long enough for reserve()
            # to bind, short enough not to squat the port if discovery stalls.
            reservation = {"port": settings.callback_port, "deadline": loop.time() + 5.0}
            if mode == "preregistered":
                reservation["path"] = settings.callback_path
            try:
                callback = await self._callback_factory.reserve(**reservation)
            except Exception as exc:
                # FM3-RF6-F2: a busy fixed port / dead listener is a client
                # configuration problem (INVALID_PARAMS at the gateway), never
                # an unhandled INTERNAL_ERROR.
                raise OAuthError(
                    f"oauth callback listener unavailable: {exc}",
                    reason="oauth_callback_invalid",
                ) from exc
            redirect_uri = callback.redirect_uri
            snapshot = OAuthReviewSnapshot(
                connector_id=config.id,
                config_revision=config.revision,
                resource=discovered.resource,
                issuer=discovered.selected_issuer,
                authorization_endpoint=discovered.authorization_endpoint,
                token_endpoint=discovered.token_endpoint,
                registration_endpoint=discovered.registration_endpoint,
                revocation_endpoint=discovered.revocation_endpoint,
                redirect_uri=redirect_uri,
                requested_scopes=requested,
                registration_mode=mode,
                client_id=settings.client_id,
                fingerprint=discovered.fingerprint,
                require_iss=discovered.require_iss,
            )
            digest = self._digest_of(snapshot, config_revision=config.revision)
            preview_id = secrets.token_hex(8)
            expires_at = self._now() + timedelta(seconds=OAUTH_PREVIEW_TTL_SECONDS)
            async with self._lock:
                await self._sweep_previews_locked()
                # FM3-RF6-F2 cap: evict the oldest preview (closing its
                # listener) instead of growing unboundedly.
                while len(self._previews) >= OAUTH_MAX_PREVIEWS:
                    oldest = next(iter(self._previews), None)
                    if oldest is None:
                        break
                    await self._close_preview_locked(oldest)
                self._previews[preview_id] = {
                    "connector_id": connector_id,
                    "owner": owner,
                    "snapshot": snapshot,
                    "digest": digest,
                    "callback": callback,
                    "expires_at": expires_at,
                    "local_access": bool(config.local_access),
                }
            return {
                "previewId": preview_id,
                "digest": digest,
                "expiresAt": expires_at.isoformat(),
                "review": {
                    "connectorId": config.id,
                    "revision": config.revision,
                    "endpoint": config.endpoint,
                    "resource": snapshot.resource,
                    "issuer": snapshot.issuer,
                    "requestedScopes": list(requested),
                    "registrationMode": mode,
                    "endpoints": {
                        "authorization": snapshot.authorization_endpoint,
                        "token": snapshot.token_endpoint,
                        "registration": snapshot.registration_endpoint,
                        "revocation": snapshot.revocation_endpoint,
                    },
                    "redirectUri": redirect_uri,
                },
            }
        finally:
            # aclose is idempotent: one close site covers success and failure.
            try:
                await network.aclose()
            except Exception:
                pass

    @staticmethod
    def _registration_mode(settings: OAuthSettings, discovered) -> str:
        if settings.client_mode == "preregistered":
            return "preregistered"
        if settings.client_mode == "metadata_document":
            if not discovered.supports_client_metadata_document:
                raise OAuthError(
                    "server does not support client metadata documents",
                    reason="oauth_client_configuration_required",
                )
            return "metadata_document"
        if discovered.registration_endpoint:
            return "dcr"
        return "preregistered-required"

    async def _close_preview_locked(self, preview_id: str) -> None:
        prev = self._previews.pop(preview_id, None)
        if prev is not None:
            try:
                await prev["callback"].aclose()
            except Exception:
                pass

    async def _sweep_previews_locked(self, *, protect: frozenset[str] | None = None) -> None:
        """Close expired previews (stale loopback listeners).

        ``protect`` keeps requested preview ids so the caller can raise its
        specific expired-handle error instead of a generic not-found.
        """
        now = self._now()
        for preview_id, prev in list(self._previews.items()):
            if protect and preview_id in protect:
                continue
            expires_at = prev.get("expires_at")
            if expires_at is not None and now > expires_at:
                await self._close_preview_locked(preview_id)

    # ── start ───────────────────────────────────────────────────
    async def start(
        self, connector_id: str, owner: OAuthOwner, *, preview_id: str, digest: str
    ) -> dict[str, Any]:
        async with self._lock:
            # FM3-RF6-F2: reap every expired preview except the requested one
            # (which keeps its specific "preview expired" error below).
            await self._sweep_previews_locked(protect=frozenset({preview_id}))
            prev = self._previews.get(preview_id)
            if prev is None or prev["connector_id"] != connector_id or prev["owner"] != owner:
                raise OAuthError("preview not found", reason="oauth_operation_not_found")
            if prev["digest"] != digest:
                raise OAuthError("preview changed; review again", reason="oauth_config_changed")
            if self._now() > prev["expires_at"]:
                await self._close_preview_locked(preview_id)
                raise OAuthError("preview expired", reason="oauth_expired")
            active = [
                o
                for o in self._operations.values()
                if o["state"] not in ("complete", "cancelled", "expired", "failed")
            ]
            if len(active) >= OAUTH_MAX_OPERATIONS:
                raise OAuthError("too many active authorizations", reason="oauth_expired")
            if any(
                o["connector_id"] == connector_id
                and o["state"] in ("preparing", "awaiting_browser", "exchanging")
                for o in active
            ):
                raise OAuthError("authorization already in progress", reason="oauth_expired")
            snapshot = prev["snapshot"]
            callback = prev["callback"]
            del self._previews[preview_id]
        operation_id = secrets.token_hex(8)
        expires_at = self._now() + timedelta(seconds=OAUTH_OPERATION_TTL_SECONDS)
        operation = {
            "operation_id": operation_id,
            "connector_id": connector_id,
            "owner": owner,
            "state": "preparing",
            "snapshot": snapshot,
            "callback": callback,
            "expires_at": expires_at,
            "reason": "",
            "authorization_url": "",
            "client": None,
            "verifier": "",
            "task": None,
        }
        async with self._lock:
            self._operations[operation_id] = operation
        task = asyncio.create_task(self._run_operation(operation_id))
        operation["task"] = task
        return {"operation": self._operation_view(operation)}

    def _operation_view(self, operation: dict[str, Any]) -> dict[str, Any]:
        return {
            "operationId": operation["operation_id"],
            "connectorId": operation["connector_id"],
            "state": operation["state"],
            "expiresAt": operation["expires_at"].isoformat(),
            **({"reasonCode": operation["reason"]} if operation.get("reason") else {}),
        }

    async def _run_operation(self, operation_id: str) -> None:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return
            snapshot = operation["snapshot"]
            owner = operation["owner"]
            callback = operation["callback"]
            connector_id = operation["connector_id"]
        from machina.permissions import broker
        from machina.permissions import store as perm_store
        from machina.permissions.policy import build_request

        loop = asyncio.get_running_loop()
        op_deadline = loop.time() + OAUTH_OPERATION_TTL_SECONDS
        network = None
        try:
            config = await self._resolve_config(connector_id)
            # Revalidate metadata/configuration: rediscover and compare fingerprint.
            local_access = bool(config.local_access)
            network = self._network_factory(local_access=local_access)
            try:
                rediscovered = await network.discover(
                    config,
                    issuer=snapshot.issuer,
                    deadline=min(loop.time() + OAUTH_PREVIEW_DISCOVERY_SECONDS, op_deadline),
                )
            except OAuthNetworkError as exc:
                raise OAuthError(
                    str(exc), reason=getattr(exc, "reason", "oauth_discovery_failed")
                ) from exc
            if rediscovered.fingerprint != snapshot.fingerprint:
                raise OAuthError(
                    "configuration changed during authorization", reason="oauth_config_changed"
                )
            settings = OAuthSettings.from_dict(getattr(config, "oauth", None))
            if settings is None:
                raise OAuthError("oauth is not configured", reason="oauth_not_configured")
            content = json.dumps(
                {
                    "connector": snapshot.connector_id,
                    "revision": snapshot.config_revision,
                    "resource": snapshot.resource,
                    "issuer": snapshot.issuer,
                    "scopes": list(snapshot.requested_scopes),
                    "registrationMode": snapshot.registration_mode,
                    "redirectUri": snapshot.redirect_uri,
                },
                sort_keys=True,
            )
            request = build_request(
                operation="tool.mcp_oauth_authorize",
                tool="mcp_oauth_authorize",
                content=content,
                network=[snapshot.issuer, snapshot.token_endpoint],
                capabilities=["mcp_external_review"],
                mode="ask",
                backend="host",
                session_id=owner.session_id,
                agent_id=owner.agent_id,
                timeout=30,
            )

            async def reviewed(card):
                record = await perm_store.resolve_decision(
                    card["request_id"], "approved", "mcp-oauth-management"
                )
                return "approved" if record is not None else "denied"

            tokens = [
                (broker._approval_handler, broker._approval_handler.set(reviewed)),
                (broker._principal, broker._principal.set("mcp-oauth-management")),
                (broker._execution_context, broker._execution_context.set(None)),
            ]
            outcome = "failed"
            try:
                await broker.permission_broker.guard(request)
                # Config may have changed while awaiting approval.
                config = await self._resolve_config(connector_id)
                if config.revision != snapshot.config_revision:
                    raise OAuthError(
                        "configuration changed during approval", reason="oauth_config_changed"
                    )
                # Registration (only after reviewed start).
                from mcp.client.auth import PKCEParameters as _PKCE

                if snapshot.registration_mode == "preregistered":
                    if not snapshot.client_id:
                        raise OAuthError(
                            "pre-registration required",
                            reason="oauth_client_configuration_required",
                        )
                    client = RegisteredOAuthClient(
                        client_id=snapshot.client_id,
                        token_endpoint_auth_method=settings.token_endpoint_auth_method,
                        redirect_uri=snapshot.redirect_uri,
                    )
                    client_secret_ref = settings.client_secret_ref
                elif snapshot.registration_mode == "metadata_document":
                    client = RegisteredOAuthClient(
                        client_id=settings.client_metadata_url or snapshot.client_id,
                        token_endpoint_auth_method="none",
                        redirect_uri=snapshot.redirect_uri,
                    )
                    client_secret_ref = ""
                else:
                    try:
                        reg_deadline = min(loop.time() + OAUTH_EXCHANGE_SECONDS, op_deadline)
                        client = await network.register(snapshot, deadline=reg_deadline)
                    except OAuthNetworkError as exc:
                        raise OAuthError(
                            str(exc), reason=getattr(exc, "reason", "oauth_registration_failed")
                        ) from exc
                    client_secret_ref = ""
                pkce = _PKCE.generate()
                state = secrets.token_urlsafe(32)
                async with self._lock:
                    operation["client"] = client
                    operation["verifier"] = pkce.code_verifier
                    operation["oauth_state"] = state
                    operation["client_secret_ref"] = client_secret_ref
                auth_url = self._authorization_url(snapshot, client, pkce.code_challenge, state)
                async with self._lock:
                    operation["authorization_url"] = auth_url
                    operation["state"] = "awaiting_browser"
                callback.arm(
                    state=state, issuer=snapshot.issuer, require_iss=self._require_iss(snapshot)
                )
                try:
                    result = await callback.wait(deadline=op_deadline)
                except TimeoutError as exc:
                    raise OAuthError("authorization expired", reason="oauth_expired") from exc
                if not result.ok:
                    raise OAuthError("authorization cancelled", reason="oauth_cancelled")
                async with self._lock:
                    if operation["state"] == "cancelled":
                        raise OAuthError("authorization cancelled", reason="oauth_cancelled")
                    operation["state"] = "exchanging"
                # Generation guard: logout/remove before exchange completes.
                binding = self._binding_for(
                    config, issuer=snapshot.issuer, client_id=client.client_id
                )
                expected_gen = await self._creds.current_generation(binding)
                # Single bounded exchange.
                exchange_client = client
                if client_secret_ref:
                    exchange_client = _BasicClient(
                        client_id=client.client_id,
                        token_endpoint_auth_method="client_secret_basic",
                        redirect_uri=client.redirect_uri,
                        client_secret_ref=client_secret_ref,
                    )
                try:
                    exch_deadline = min(loop.time() + OAUTH_EXCHANGE_SECONDS, op_deadline)
                    token = await network.exchange_code(
                        snapshot,
                        exchange_client,
                        result.code,
                        pkce.code_verifier,
                        deadline=exch_deadline,
                    )
                except OAuthNetworkError as exc:
                    raise OAuthError(str(exc), reason="oauth_exchange_failed") from exc
                granted = (
                    tuple(str(token.scope).split())
                    if getattr(token, "scope", None) is not None
                    else tuple(snapshot.requested_scopes)
                )
                if set(granted) - set(snapshot.requested_scopes):
                    raise OAuthError(
                        "granted scopes exceed approved scopes", reason="oauth_scope_not_allowed"
                    )
                # Re-check config/auth generation before persisting.
                current = await self._resolve_config(connector_id)
                if current.revision != snapshot.config_revision:
                    raise OAuthError("configuration changed", reason="oauth_config_changed")
                now = self._now()
                expires_at = ""
                if getattr(token, "expires_in", None):
                    expires_at = (now + timedelta(seconds=int(token.expires_in))).isoformat()
                record = OAuthCredentialRecord(
                    version=1,
                    credential_key=binding.credential_key,
                    connector_id=connector_id,
                    config_revision=current.revision,
                    resource=snapshot.resource,
                    issuer=snapshot.issuer,
                    client_id=client.client_id,
                    client_secret_ref=client_secret_ref,
                    authorization_endpoint=snapshot.authorization_endpoint,
                    token_endpoint=snapshot.token_endpoint,
                    revocation_endpoint=snapshot.revocation_endpoint,
                    redirect_uri_used=snapshot.redirect_uri,
                    token_endpoint_auth_method=getattr(
                        exchange_client, "token_endpoint_auth_method", "none"
                    ),
                    grant_id=secrets.token_hex(8),
                    generation=expected_gen + 1,
                    approved_scopes=tuple(snapshot.requested_scopes),
                    granted_scopes=tuple(granted),
                    access_token=token.access_token,
                    refresh_token=getattr(token, "refresh_token", "") or "",
                    acquired_at_utc=now.isoformat(),
                    expires_at_utc=expires_at,
                    refresh_state="idle",
                )
                try:
                    await self._creds.save(record, expected_generation=expected_gen)
                except ValueError as exc:
                    raise OAuthError(
                        "credential generation changed", reason="oauth_config_changed"
                    ) from exc
                except Exception as exc:
                    raise OAuthError(
                        "credential storage failed", reason="oauth_storage_unavailable"
                    ) from exc
                async with self._lock:
                    operation["state"] = "complete"
                    operation["grant_id"] = record.grant_id
                outcome = "completed"
                self._auth_cache.pop(connector_id, None)
            finally:
                if request.approval_id:
                    try:
                        await broker.permission_broker.complete(request.approval_id, outcome)
                    except Exception:
                        pass
                for variable, token_var in reversed(tokens):
                    try:
                        variable.reset(token_var)
                    except Exception:
                        pass
        except OAuthError as exc:
            async with self._lock:
                if operation_id in self._operations:
                    op = self._operations[operation_id]
                    if op["state"] not in ("cancelled",):
                        op["state"] = "failed" if exc.reason != "oauth_cancelled" else "cancelled"
                        op["reason"] = exc.reason
            try:
                await callback.aclose()
            except Exception:
                pass
        except asyncio.CancelledError:
            async with self._lock:
                if operation_id in self._operations:
                    op = self._operations[operation_id]
                    op["state"] = "cancelled"
                    op["reason"] = "oauth_cancelled"
            try:
                await callback.aclose()
            except Exception:
                pass
            raise
        except Exception:
            async with self._lock:
                if operation_id in self._operations:
                    op = self._operations[operation_id]
                    op["state"] = "failed"
                    op["reason"] = "oauth_exchange_failed"
            try:
                await callback.aclose()
            except Exception:
                pass
        finally:
            if network is not None:
                try:
                    await network.aclose()
                except Exception:
                    pass
            # Scrub transient secrets from memory; close listener.
            async with self._lock:
                op = self._operations.get(operation_id)
                if op is not None:
                    op.pop("verifier", None)
                    op.pop("oauth_state", None)
                    op.pop("authorization_url", None)
                    op.pop("client_secret_ref", None)
            try:
                await callback.aclose()
            except Exception:
                pass

            # Schedule terminal retention eviction.
            async def _evict():
                await asyncio.sleep(OAUTH_TERMINAL_RETENTION_SECONDS)
                async with self._lock:
                    op = self._operations.get(operation_id)
                    if op is not None and op.get("state") in (
                        "complete",
                        "cancelled",
                        "expired",
                        "failed",
                    ):
                        self._operations.pop(operation_id, None)

            try:
                # Skip retention scheduling while shutting down or when this
                # task is itself being cancelled (create_task would raise).
                if not self._shutting_down and not asyncio.current_task().cancelling():
                    task = asyncio.create_task(_evict())
                    self._retention_tasks.add(task)
                    task.add_done_callback(self._retention_tasks.discard)
            except RuntimeError:
                pass  # loop closing; entries expire with the worker

    def _require_iss(self, snapshot: OAuthReviewSnapshot) -> bool:
        # Determined at discovery; coordinator stashes it on the snapshot path.
        # Preview snapshots from real discovery carry require_iss via closure;
        # default False for prebuilt snapshots in tests.
        return bool(getattr(snapshot, "require_iss", False))

    def _authorization_url(
        self,
        snapshot: OAuthReviewSnapshot,
        client: RegisteredOAuthClient,
        challenge: str,
        state: str,
    ) -> str:
        from urllib.parse import urlencode

        params = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": snapshot.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": snapshot.resource,
        }
        if snapshot.requested_scopes:
            params["scope"] = " ".join(snapshot.requested_scopes)
        base = snapshot.authorization_endpoint
        separator = "&" if "?" in base else "?"
        # Reject duplicate reserved params in provider endpoint query.
        from urllib.parse import parse_qsl as _pqs
        from urllib.parse import urlparse as _up

        existing = _pqs(_up(base).query, keep_blank_values=True)
        reserved = set(params)
        for key, _ in existing:
            if key in reserved:
                raise OAuthError(
                    "authorization endpoint has conflicting parameters",
                    reason="oauth_discovery_failed",
                )
        return base + separator + urlencode(params)

    # ── status / url / open / cancel ────────────────────────────
    async def status(
        self, connector_id: str, owner: OAuthOwner, *, operation_id: str | None = None
    ) -> dict[str, Any]:
        config = await self._resolve_config(connector_id)
        auth = await self._public_auth(config)
        result: dict[str, Any] = {"auth": auth}
        if operation_id is not None:
            async with self._lock:
                operation = self._operations.get(operation_id)
                if (
                    operation is None
                    or operation["connector_id"] != connector_id
                    or operation["owner"] != owner
                ):
                    raise OAuthError("operation not found", reason="oauth_operation_not_found")
                result["operation"] = self._operation_view(operation)
        return result

    async def _public_auth(self, config) -> dict[str, Any]:
        settings = OAuthSettings.from_dict(getattr(config, "oauth", None))
        if settings is None:
            return {"state": "not_configured", "generation": 0}
        # Derive candidate bindings: we don't know issuer/client until discovery,
        # so scan by loading via resolver attempts. Prefer configured issuer.
        # Fallback: report signed_out when no grant found.
        try:
            if settings.issuer and (settings.client_id or settings.client_mode != "preregistered"):
                binding = self._binding_for(
                    config,
                    issuer=settings.issuer,
                    client_id=settings.client_id or settings.client_metadata_url,
                )
                identity = await self._creds.public_identity(binding)
                if identity is not None:
                    return await self._auth_view(config, binding)
        except Exception:
            pass
        # Scan index for any grant for this connector (bounded, local only).
        stale_binding = None
        try:
            index = await asyncio.to_thread(self._creds._read_index)
            for key, entry in index.get("grants", {}).items():
                if entry.get("connector_id") == config.id:
                    record = await self._creds.load(
                        OAuthBinding(
                            connector_id=config.id,
                            credential_key=key,
                            resource="",
                            issuer="",
                            client_id="",
                        )
                    )
                    if record is not None and record.connector_id == config.id:
                        binding = OAuthBinding(
                            connector_id=config.id,
                            credential_key=key,
                            resource=record.resource,
                            issuer=record.issuer,
                            client_id=record.client_id,
                        )
                        if self._record_matches_config(record, config, binding):
                            return await self._auth_view(config, binding)
                        if stale_binding is None:
                            stale_binding = binding
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("oauth.public_auth.storage", _boundary_error)
            return {"state": "storage_unavailable", "generation": 0}
        if stale_binding is not None:
            return await self._auth_view(config, stale_binding)
        return {"state": "signed_out", "generation": 0}

    async def _auth_view(self, config, binding: OAuthBinding) -> dict[str, Any]:
        try:
            record = await self._creds.load(binding)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("oauth.public_auth.storage", _boundary_error)
            return {"state": "storage_unavailable", "generation": 0}
        if record is None:
            return {"state": "signed_out", "generation": 0}
        if not self._record_matches_config(record, config, binding):
            return {
                "state": "reauthorization_required",
                "generation": record.generation,
                "reasonCode": "oauth_config_changed",
                "grantId": record.grant_id,
                "resource": record.resource,
                "issuer": record.issuer,
            }
        expires = _parse_expiry(record.expires_at_utc)
        view: dict[str, Any] = {
            "state": "authorized",
            "issuer": record.issuer,
            "resource": record.resource,
            "grantedScopes": list(record.granted_scopes),
            "grantId": record.grant_id,
            "generation": record.generation,
        }
        if record.expires_at_utc:
            view["expiresAt"] = record.expires_at_utc
        if record.refresh_state != "idle" or (
            expires is not None and expires <= self._now() and not record.refresh_token
        ):
            view["state"] = "reauthorization_required"
            view["reasonCode"] = "oauth_reauthorization_required"
        return view

    async def _validate_handle_config(
        self, handle: dict[str, Any], owner: OAuthOwner, *, connector_id: str | None = None
    ) -> None:
        actual_id = handle["connector_id"]
        if handle["owner"] != owner or (connector_id is not None and connector_id != actual_id):
            raise OAuthError("operation not found", reason="oauth_operation_not_found")
        config = await self._resolve_config(actual_id)
        if config.id != actual_id or (config.owner and config.owner != owner.agent_id):
            raise OAuthError(
                "operation owner no longer authorized", reason="oauth_operation_not_found"
            )
        if config.revision != handle["snapshot"].config_revision:
            raise OAuthError("configuration changed", reason="oauth_config_changed")

    async def authorization_url(
        self, operation_id: str, owner: OAuthOwner, *, connector_id: str | None = None
    ) -> str:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation["owner"] != owner:
                raise OAuthError("operation not found", reason="oauth_operation_not_found")
        # Resolving may refresh service state and invalidate handles; never
        # hold the coordinator lock across that call.
        await self._validate_handle_config(operation, owner, connector_id=connector_id)
        async with self._lock:
            if self._operations.get(operation_id) is not operation:
                raise OAuthError("operation not found", reason="oauth_operation_not_found")
            if operation["state"] != "awaiting_browser" or not operation.get("authorization_url"):
                raise OAuthError(
                    "authorization url is not available", reason="oauth_operation_not_found"
                )
            return operation["authorization_url"]

    async def open_browser(
        self, operation_id: str, owner: OAuthOwner, *, connector_id: str | None = None
    ) -> bool:
        url = await self.authorization_url(operation_id, owner, connector_id=connector_id)
        try:
            opened = await asyncio.to_thread(self._browser, url)
            return bool(opened)
        except Exception:
            return False

    async def cancel(
        self,
        owner: OAuthOwner,
        *,
        preview_id: str | None = None,
        operation_id: str | None = None,
        connector_id: str | None = None,
    ) -> bool:
        if (preview_id is None) == (operation_id is None):
            raise OAuthError("exactly one handle is required", reason="oauth_operation_not_found")
        if preview_id is not None:
            async with self._lock:
                prev = self._previews.get(preview_id)
                if prev is None:
                    return False
                if prev["owner"] != owner:
                    raise OAuthError("not authorized", reason="oauth_operation_not_found")
            await self._validate_handle_config(prev, owner, connector_id=connector_id)
            async with self._lock:
                if self._previews.get(preview_id) is not prev:
                    return False
                await self._close_preview_locked(preview_id)
                return True
        assert operation_id is not None
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return False
            if operation["owner"] != owner:
                raise OAuthError("not authorized", reason="oauth_operation_not_found")
        await self._validate_handle_config(operation, owner, connector_id=connector_id)
        async with self._lock:
            if self._operations.get(operation_id) is not operation:
                return False
            if operation["state"] in ("complete", "failed", "cancelled", "expired"):
                return True
            operation["state"] = "cancelled"
            operation["reason"] = "oauth_cancelled"
            task = operation.get("task")
            callback = operation.get("callback")
        if task is not None and not task.done():
            task.cancel()
        if callback is not None:
            try:
                await callback.aclose()
            except Exception:
                pass
        return True

    async def forget(
        self, connector_id: str, owner: OAuthOwner, *, expected_generation: int, revoke: bool
    ) -> dict[str, Any]:
        config = await self._resolve_config(connector_id)
        # Cancel this connector's flows first.
        async with self._lock:
            for op in list(self._operations.values()):
                if op["connector_id"] == connector_id:
                    op["state"] = "cancelled"
                    op["reason"] = "oauth_cancelled"
                    task = op.get("task")
                    if task is not None and not task.done():
                        task.cancel()
            for pid, prev in list(self._previews.items()):
                if prev["connector_id"] == connector_id:
                    await self._close_preview_locked(pid)
        # Local invalidation first (tombstone), then one bounded revoke attempt.
        binding = await self._primary_binding(config)
        remote = "not_requested"
        snapshot_record = None
        if binding is not None:
            try:
                snapshot_record = await self._creds.load(binding)
            except Exception as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("oauth.forget.snapshot", _boundary_error)
                snapshot_record = None
        await self._retire_connector_credentials(
            connector_id, preferred_binding=binding, expected_generation=expected_generation
        )
        if binding is not None:
            if revoke and snapshot_record is not None and snapshot_record.revocation_endpoint:
                network = self._network_factory(local_access=bool(config.local_access))
                try:
                    loop = asyncio.get_running_loop()
                    remote = await network.revoke_token(
                        snapshot_record, deadline=loop.time() + 10.0
                    )
                except Exception:
                    remote = "failed"
                finally:
                    try:
                        await network.aclose()
                    except Exception:
                        pass
            elif revoke:
                remote = "unsupported"
        self._auth_cache.pop(connector_id, None)
        # Retire live connection/catalog.
        if self._service is not None:
            try:
                await self._service.manager.disconnect(connector_id)
            except Exception:
                pass
            try:
                self._service.registry._drop_tools(connector_id)
            except Exception:
                pass
        auth = await self._public_auth(config)
        return {"auth": auth, "remoteRevocation": remote}

    async def _primary_binding(self, config) -> OAuthBinding | None:
        try:
            index = await asyncio.to_thread(self._creds._read_index)
        except Exception:
            return None
        for key, entry in index.get("grants", {}).items():
            if entry.get("connector_id") == config.id:
                try:
                    record = await self._creds.load(
                        OAuthBinding(
                            connector_id=config.id,
                            credential_key=key,
                            resource="",
                            issuer="",
                            client_id="",
                        )
                    )
                except Exception:
                    continue
                if record is not None:
                    binding = OAuthBinding(
                        connector_id=config.id,
                        credential_key=key,
                        resource=record.resource,
                        issuer=record.issuer,
                        client_id=record.client_id,
                    )
                    if self._record_matches_config(record, config, binding):
                        return binding
        return None

    async def _retire_connector_credentials(
        self,
        connector_id: str,
        *,
        config_revision: int | None = None,
        preferred_binding: OAuthBinding | None = None,
        expected_generation: int | None = None,
    ) -> None:
        index = await asyncio.to_thread(self._creds._read_index)
        bindings: dict[str, tuple[OAuthBinding, int]] = {}
        protected_keys: set[str] = set()
        for key, entry in index.get("grants", {}).items():
            if entry.get("connector_id") != connector_id:
                continue
            binding = OAuthBinding(
                connector_id=connector_id, credential_key=key, resource="", issuer="", client_id=""
            )
            try:
                record = await self._creds.load(binding)
            except Exception as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("oauth.retire.load", _boundary_error)
                record = None
            if (
                record is not None
                and config_revision is not None
                and record.config_revision > config_revision
            ):
                protected_keys.add(key)
                continue
            bindings[key] = (binding, int(entry.get("generation", 0)))
        # An exchanging operation may not have committed its first envelope.
        # Tombstone its already known binding as well, so late commits lose CAS.
        async with self._lock:
            operations = list(self._operations.values())
        for operation in operations:
            client = operation.get("client")
            snapshot = operation.get("snapshot")
            if operation.get("connector_id") != connector_id or client is None or snapshot is None:
                continue
            if config_revision is not None and snapshot.config_revision > config_revision:
                continue
            key = credential_key_for(
                connector_id=connector_id,
                resource=snapshot.resource,
                issuer=snapshot.issuer,
                client_id=client.client_id,
            )
            if key not in bindings and key not in protected_keys:
                binding = OAuthBinding(
                    connector_id=connector_id,
                    credential_key=key,
                    resource=snapshot.resource,
                    issuer=snapshot.issuer,
                    client_id=client.client_id,
                )
                bindings[key] = (binding, await self._creds.current_generation(binding))
        if preferred_binding is not None and preferred_binding.credential_key not in bindings:
            bindings[preferred_binding.credential_key] = (
                preferred_binding,
                await self._creds.current_generation(preferred_binding),
            )
        if expected_generation is not None:
            key = (
                preferred_binding.credential_key
                if preferred_binding is not None
                else next(iter(bindings), None)
            )
            if key is None:
                if expected_generation != 0:
                    raise OAuthError("authority changed", reason="oauth_config_changed")
            elif bindings[key][1] != expected_generation:
                raise OAuthError("authority changed", reason="oauth_config_changed")
            else:
                # Validate the review-bound generation before retiring siblings.
                selected = bindings.pop(key)
                bindings = {key: selected, **bindings}
        for binding, generation in bindings.values():
            try:
                await self._creds.forget(
                    binding,
                    expected_generation=generation,
                    max_config_revision=config_revision,
                )
            except ValueError as exc:
                raise OAuthError(
                    "authority changed during retirement", reason="oauth_config_changed"
                ) from exc

    async def invalidate_connector(
        self, connector_id: str, *, config_revision: int | None = None
    ) -> None:
        async with self._lock:
            for op in list(self._operations.values()):
                if op["connector_id"] == connector_id and (
                    config_revision is None or op["snapshot"].config_revision <= config_revision
                ):
                    op["state"] = "cancelled"
                    op["reason"] = "oauth_config_changed"
                    task = op.get("task")
                    if task is not None and not task.done():
                        task.cancel()
            for pid, prev in list(self._previews.items()):
                if prev["connector_id"] == connector_id and (
                    config_revision is None or prev["snapshot"].config_revision <= config_revision
                ):
                    await self._close_preview_locked(pid)
        self._auth_cache.pop(connector_id, None)
        await self._retire_connector_credentials(connector_id, config_revision=config_revision)

    # ── tokens for transport ────────────────────────────────────
    def auth_identity(self, connector_id: str) -> dict[str, Any] | None:
        return self._auth_cache.get(connector_id)

    @staticmethod
    def _record_matches_config(record, config, binding: OAuthBinding) -> bool:
        try:
            settings = OAuthSettings.from_dict(getattr(config, "oauth", None))
            if settings is None:
                return False
            expected_client = (
                settings.client_id
                if settings.client_mode == "preregistered"
                else settings.client_metadata_url
                if settings.client_mode == "metadata_document"
                else ""
            )
            return (
                record.connector_id == config.id == binding.connector_id
                and record.config_revision == config.revision
                and record.credential_key == binding.credential_key
                and record.resource == binding.resource == normalize_resource(config.endpoint)
                and record.issuer == binding.issuer
                and record.client_id == binding.client_id
                and (not settings.issuer or record.issuer == settings.issuer)
                and (not expected_client or record.client_id == expected_client)
            )
        except (ValueError, TypeError):
            return False

    async def refresh_auth_cache(self, connector_id: str, *, config=None) -> None:
        try:
            if config is None:
                config = await self._resolve_config(connector_id)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("oauth.refresh_auth_cache", _boundary_error)
            self._auth_cache.pop(connector_id, None)
            return
        settings = None
        try:
            settings = OAuthSettings.from_dict(getattr(config, "oauth", None))
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("oauth.refresh_auth_cache", _boundary_error)
        if settings is None:
            self._auth_cache.pop(connector_id, None)
            return
        binding = await self._primary_binding(config)
        if binding is None:
            self._auth_cache.pop(connector_id, None)
            return
        try:
            record = await self._creds.load(binding)
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("oauth.refresh_auth_cache", _boundary_error)
            self._auth_cache.pop(connector_id, None)
            return
        if record is not None and not self._record_matches_config(record, config, binding):
            record = None
        if (
            record is not None
            and record.refresh_state != "idle"
            and self._refresh_owners.get(binding.credential_key) is not asyncio.current_task()
        ):
            # A live refresh journals "in_flight" before its token POST and
            # drops the auth cache; coalesce on that refresh (bounded) so a
            # concurrent preflight re-reads the settled record instead of
            # reporting the connector signed-out before reaching the lock.
            # The task holding the lock skips this wait (no self-deadlock).
            record = await self._await_in_flight_refresh(binding)
            if record is not None and not self._record_matches_config(record, config, binding):
                record = None
        if record is not None and record.refresh_state != "idle":
            # Crash journal (or a wait that timed out): fail closed.
            record = None
        if record is None:
            self._auth_cache.pop(connector_id, None)
            return
        self._auth_cache[connector_id] = {
            "credentialKey": record.credential_key,
            "grantId": record.grant_id,
            "generation": record.generation,
            "resource": record.resource,
            "issuer": record.issuer,
            "clientId": record.client_id,
            "grantedScopes": list(record.granted_scopes),
        }

    @asynccontextmanager
    async def _owned_refresh_lock(self, binding: OAuthBinding, *, deadline: float):
        """Store refresh lock that records its owning task for coalescing."""
        async with self._creds.refresh_lock(binding, deadline=deadline):
            self._refresh_owners[binding.credential_key] = asyncio.current_task()
            try:
                yield
            finally:
                self._refresh_owners.pop(binding.credential_key, None)

    async def _await_in_flight_refresh(self, binding: OAuthBinding) -> OAuthCredentialRecord | None:
        """Bounded coalesce on a concurrent refresh, then one re-read.

        ``refresh_state != "idle"`` is journaled inside the store's refresh
        lock, so waiting on that lock reaches either the winner's published
        record or, for a crash journal, the unchanged fail-closed state.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + OAUTH_LOCK_SECONDS
        try:
            async with self._creds.refresh_lock(binding, deadline=deadline):
                return await self._creds.load(binding)
        except (TimeoutError, ValueError):
            # Bounded wait exhausted or unreadable store: the caller keeps the
            # fail-closed signed-out outcome.
            return None

    async def ensure_token(
        self, binding: OAuthBinding, *, expected_grant: dict[str, Any], deadline: float
    ) -> str:
        try:
            record = await self._creds.load(binding)
        except Exception as exc:
            raise OAuthError(
                "credential storage unavailable", reason="oauth_storage_unavailable"
            ) from exc
        if record is None:
            raise OAuthError("authorization required", reason="oauth_reauthorization_required")
        config = await self._resolve_config(binding.connector_id)
        if not self._record_matches_config(record, config, binding):
            self._auth_cache.pop(binding.connector_id, None)
            raise OAuthError("configuration changed", reason="oauth_config_changed")
        if (
            str(expected_grant.get("grantId", "")) != record.grant_id
            or int(expected_grant.get("generation", -1)) != record.generation
        ):
            raise OAuthError(
                "authority changed; fresh review required", reason="oauth_config_changed"
            )
        now = self._now()
        expires = _parse_expiry(record.expires_at_utc)
        needs_refresh = (
            expires is not None and (expires - now).total_seconds() <= OAUTH_REFRESH_SKEW_SECONDS
        )
        if record.refresh_state != "idle" and not needs_refresh:
            self._auth_cache.pop(binding.connector_id, None)
            raise OAuthError("authorization required", reason="oauth_reauthorization_required")
        if expires is None:
            return record.access_token  # unknown expiry: use until challenged
        if not needs_refresh:
            return record.access_token
        if not record.refresh_token:
            raise OAuthError("authorization required", reason="oauth_reauthorization_required")
        async with self._owned_refresh_lock(binding, deadline=deadline):
            # Re-read: another worker may have refreshed already.
            fresh = await self._creds.load(binding)
            if fresh is None:
                raise OAuthError("authorization required", reason="oauth_reauthorization_required")
            config = await self._resolve_config(binding.connector_id)
            if not self._record_matches_config(fresh, config, binding):
                self._auth_cache.pop(binding.connector_id, None)
                raise OAuthError("configuration changed", reason="oauth_config_changed")
            if fresh.refresh_state != "idle":
                self._auth_cache.pop(binding.connector_id, None)
                raise OAuthError("authorization required", reason="oauth_reauthorization_required")
            if (
                fresh.access_token != record.access_token
                or fresh.refresh_token != record.refresh_token
                or fresh.generation != record.generation
            ):
                # Someone rotated already; re-validate authority.
                if (
                    fresh.grant_id != record.grant_id
                    or fresh.generation != record.generation
                    or set(fresh.granted_scopes) != set(record.granted_scopes)
                ):
                    raise OAuthError("authority changed", reason="oauth_config_changed")
                return fresh.access_token
            # Journal in_flight before POST (crash safety).
            fresh.refresh_state = "in_flight"
            try:
                await self._creds.save(fresh, expected_generation=fresh.generation)
            except ValueError as exc:
                self._auth_cache.pop(binding.connector_id, None)
                raise OAuthError(
                    "authority changed before refresh", reason="oauth_config_changed"
                ) from exc
            except Exception as exc:
                self._auth_cache.pop(binding.connector_id, None)
                raise OAuthError(
                    "refresh journal unavailable", reason="oauth_storage_unavailable"
                ) from exc
            self._auth_cache.pop(binding.connector_id, None)
            network = self._network_factory(local_access=bool(config.local_access))
            try:
                token = await network.refresh_token(fresh, deadline=deadline)
            except OAuthNetworkError as exc:
                reason = getattr(exc, "reason", "oauth_refresh_failed")
                # Ambiguous transport failure → conservative reauth (journal stays).
                raise OAuthError(str(exc), reason=reason) from exc
            finally:
                try:
                    await network.aclose()
                except Exception:
                    pass
            granted = (
                tuple(str(token.scope).split())
                if getattr(token, "scope", None) is not None
                else tuple(fresh.granted_scopes)
            )
            if set(granted) - set(fresh.approved_scopes or fresh.granted_scopes):
                raise OAuthError("refresh widened scopes", reason="oauth_scope_not_allowed")
            now2 = self._now()
            updated = OAuthCredentialRecord(
                **{
                    **asdict(fresh),
                    "access_token": token.access_token,
                    "refresh_token": getattr(token, "refresh_token", "") or fresh.refresh_token,
                    "granted_scopes": tuple(granted),
                    "acquired_at_utc": now2.isoformat(),
                    "expires_at_utc": (now2 + timedelta(seconds=int(token.expires_in))).isoformat()
                    if getattr(token, "expires_in", None)
                    else fresh.expires_at_utc,
                    "refresh_state": "idle",
                    "grant_id": secrets.token_hex(8)
                    if set(granted) != set(fresh.granted_scopes)
                    else fresh.grant_id,
                    "generation": fresh.generation + 1
                    if set(granted) != set(fresh.granted_scopes)
                    else fresh.generation,
                }
            )
            # Generation check at commit defeats late refresh after logout.
            current = await self._resolve_config(binding.connector_id)
            if not self._record_matches_config(fresh, current, binding):
                raise OAuthError(
                    "configuration changed during refresh", reason="oauth_config_changed"
                )
            try:
                await self._creds.save(updated, expected_generation=fresh.generation)
            except ValueError as exc:
                raise OAuthError(
                    "authority changed during refresh", reason="oauth_config_changed"
                ) from exc
            await self.refresh_auth_cache(binding.connector_id, config=current)
            if updated.grant_id != fresh.grant_id or updated.generation != fresh.generation:
                raise OAuthError(
                    "scope authority changed; fresh review required", reason="oauth_config_changed"
                )
            # Same-scope rotation preserves grant_id/generation.
            return updated.access_token

    async def shutdown(self, *, deadline: float = 5.0) -> None:
        self._shutting_down = True
        async with self._lock:
            callbacks = [preview["callback"] for preview in self._previews.values()]
            self._previews.clear()
            tasks = set(self._retention_tasks)
            for op in self._operations.values():
                task = op.get("task")
                if task is not None and not task.done():
                    tasks.add(task)
                callback = op.get("callback")
                if callback is not None:
                    callbacks.append(callback)
        for task in tasks:
            task.cancel()

        async def close_callback(callback):
            try:
                await callback.aclose()
            except Exception:
                pass

        tasks.update(asyncio.create_task(close_callback(callback)) for callback in set(callbacks))
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline))
            for task in pending:
                task.cancel()
            if pending:
                cancelled, _ = await asyncio.wait(pending, timeout=0.1)
                done.update(cancelled)
            for task in done:
                if not task.cancelled():
                    task.exception()


class _BasicClient:
    def __init__(
        self,
        *,
        client_id: str,
        token_endpoint_auth_method: str,
        redirect_uri: str,
        client_secret_ref: str,
    ) -> None:
        self.client_id = client_id
        self.token_endpoint_auth_method = token_endpoint_auth_method
        self.redirect_uri = redirect_uri
        self.client_secret_ref = client_secret_ref
