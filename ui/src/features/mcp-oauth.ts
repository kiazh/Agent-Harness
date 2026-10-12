// OAuth browser-authorization flow for HTTP MCP servers.
// Human-only sequencing over mcp.oauth.* control-plane methods:
// preview → review → start → progress/open/poll → authorized → connect.
// Authorization URLs live only in the transient overlay, never in the
// transcript, history, notices, or error serialization.

import type {
	MCPOAuthOperationView,
	MCPOAuthReview,
	MCPOAuthView,
} from "../protocol.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { confirm, requireSession, type FeatureHost } from "./types.ts";

const POLL_MS = 500;
const OVERALL_MS = 300_000;

const OAUTH_ERROR_HELP: Record<string, string> = {
	oauth_not_configured: "Configure OAuth for this server first.",
	oauth_discovery_failed: "The server's OAuth metadata could not be read. Check the endpoint.",
	oauth_unsafe_destination: "An OAuth endpoint failed safety checks.",
	oauth_issuer_mismatch: "The issuer changed; review a fresh authorization.",
	oauth_resource_mismatch: "The resource changed; review a fresh authorization.",
	oauth_pkce_unsupported: "The server does not support S256 PKCE.",
	oauth_client_configuration_required: "This server needs a pre-registered client ID.",
	oauth_registration_failed: "Client registration failed.",
	oauth_scope_not_allowed: "A requested scope is outside the allowlist.",
	oauth_callback_invalid: "The browser callback was invalid.",
	oauth_cancelled: "Authorization was cancelled.",
	oauth_expired: "Authorization expired; start again.",
	oauth_exchange_failed: "The code exchange failed; start a fresh login.",
	oauth_refresh_failed: "Token refresh failed.",
	oauth_reauthorization_required: "Reauthorization is required.",
	oauth_storage_unavailable: "Secure credential storage is unavailable. Set a master key or unlock the OS keyring.",
	oauth_config_changed: "The configuration changed; review again.",
	oauth_operation_not_found: "The authorization session ended; start again.",
	oauth_local_browser_unavailable: "A local browser is unavailable for this worker.",
	oauth_redirect_unsupported: "The server uses redirects the harness does not follow.",
};

export function oauthErrorMessage(reasonCode?: string): string {
	if (!reasonCode) return "Authorization failed.";
	return sanitizeDisplay(OAUTH_ERROR_HELP[reasonCode] ?? "Authorization failed.");
}

export function formatOAuthStatus(auth: MCPOAuthView): string {
	const lines = [`Authorization: ${sanitizeDisplay(auth.state)}`];
	if (auth.issuer) lines.push(`Issuer: ${sanitizeDisplay(auth.issuer)}`);
	if (auth.resource) lines.push(`Resource: ${sanitizeDisplay(auth.resource)}`);
	if (auth.grantedScopes?.length) {
		const scopes = auth.grantedScopes.slice(0, 10).map((scope) => sanitizeDisplay(scope)).join(", ");
		lines.push(`Scopes: ${scopes}${auth.grantedScopes.length > 10 ? ` (+${auth.grantedScopes.length - 10} more)` : ""}`);
	}
	if (auth.expiresAt) lines.push(`Expires: ${sanitizeDisplay(auth.expiresAt)}`);
	return lines.join("\n");
}

function formatReview(review: MCPOAuthReview): string {
	const lines = [
		`Issuer: ${review.issuer}`,
		`Resource: ${review.resource}`,
		`Scopes: ${review.requestedScopes.length ? review.requestedScopes.map((scope) => sanitizeDisplay(scope)).join(", ") : "(none requested)"}`,
		`Registration: ${review.registrationMode}`,
		`Authorization endpoint: ${review.endpoints.authorization || "(not advertised)"}`,
		`Token endpoint: ${review.endpoints.token || "(not advertised)"}`,
		`Registration endpoint: ${review.endpoints.registration || "(not advertised)"}`,
		`Revocation endpoint: ${review.endpoints.revocation || "(not advertised)"}`,
		`Callback: ${review.redirectUri}`,
	];
	return lines.map((line) => sanitizeDisplay(line)).join("\n");
}

interface PreviewResult {
	previewId?: string;
	digest?: string;
	review?: MCPOAuthReview;
	issuers?: string[];
}

/** Run Authorize/Reauthorize for one HTTP server; resolves when settled. */
export async function authorizeConnector(host: FeatureHost, serverId: string): Promise<void> {
	const session = requireSession(host);
	const sessionId = session.id;
	// 1. Bounded login metadata (no tokens; user-selected action).
	let preview = await host.request<PreviewResult>("mcp.oauth.preview", { id: serverId, sessionId });
	if (preview.issuers?.length) {
		const choice = await host.pick("Choose the authorization issuer", preview.issuers.map((issuer) => ({
			value: issuer,
			label: sanitizeDisplay(issuer),
			description: "Advertised by the resource",
		})));
		if (!choice) return;
		preview = await host.request<PreviewResult>("mcp.oauth.preview", { id: serverId, sessionId, issuer: choice.value });
	}
	if (!preview.previewId || !preview.digest || !preview.review) {
		host.print(oauthErrorMessage("oauth_discovery_failed"), "error");
		return;
	}
	host.print(`Authorize ${sanitizeDisplay(serverId)}?\n${formatReview(preview.review)}`, "plain");
	if (!await confirm(host, `Continue to browser authorization for ${sanitizeDisplay(serverId)}?`, "Continue")) {
		await host.request("mcp.oauth.cancel", { id: serverId, sessionId, previewId: preview.previewId }).catch(() => undefined);
		return;
	}
	// 2. Consume the reviewed preview; work continues in the polled operation.
	let operation: MCPOAuthOperationView;
	try {
		const started = await host.request<{ operation: MCPOAuthOperationView }>("mcp.oauth.start", {
			id: serverId, sessionId, previewId: preview.previewId, digest: preview.digest,
		});
		operation = started.operation;
	} catch (error) {
		host.print(oauthErrorMessage(operationReason(error)), "error");
		return;
	}
	const operationId = operation.operationId;
	let disposed = false;
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let wakePoll: (() => void) | undefined;
	let loginUrl: string | undefined;
	let browserOpening = false;
	const cleanup = (): void => {
		disposed = true;
		if (pollTimer) clearTimeout(pollTimer);
		wakePoll?.();
	};
	const waitForPoll = (): Promise<void> => new Promise((resolve) => {
		if (disposed) return resolve();
		wakePoll = resolve;
		pollTimer = setTimeout(() => { wakePoll = undefined; resolve(); }, POLL_MS);
	});
	const progress = host.showOAuthProgress({
		title: `Authorizing ${serverId}`,
		status: "Preparing authorization…",
		onOpen: () => {
			if (disposed || browserOpening || !loginUrl) return;
			browserOpening = true;
			void (async () => {
				try {
					const { opened } = await host.request<{ opened: boolean }>("mcp.oauth.open", { id: serverId, sessionId, operationId });
					if (disposed) return;
					progress.update(opened ? "Browser opened. Waiting for authorization…"
						: "Could not open the browser. Press y to copy the full link, or o to retry.", loginUrl);
				} catch {
					if (!disposed) progress.update("Could not open the browser. Press y to copy the full link, or o to retry.", loginUrl);
				} finally {
					browserOpening = false;
				}
			})();
		},
		onCopy: () => {
			if (disposed || !loginUrl) return;
			void (async () => {
				let copied = false;
				try { copied = await host.copyText?.(loginUrl!) ?? false; } catch { /* local clipboard unavailable */ }
				if (!disposed) progress.update(copied ? "Login link copied. Paste it into your browser to authorize."
					: "Clipboard unavailable. Select the full link below and copy it into your browser, or press o to retry.", loginUrl);
			})();
		},
		onCancel: () => {
			if (disposed) return;
			cleanup();
			void host.request("mcp.oauth.cancel", { id: serverId, sessionId, operationId }).catch(() => undefined);
		},
	});
	try {
		const deadline = Date.now() + OVERALL_MS;
		let awaitingBrowser = false;
		let unavailablePolls = 0;
		while (!disposed && Date.now() < deadline) {
			let status: { auth: MCPOAuthView; operation?: MCPOAuthOperationView };
			try {
				// Sequential awaits keep exactly one status request outstanding.
				status = await host.request("mcp.oauth.status", { id: serverId, sessionId, operationId });
			} catch (error) {
				if (disposed) return;
				const failure = permanentStatusError(error);
				if (failure || ++unavailablePolls >= 3) {
					host.print(failure ?? "Authorization service unavailable. Start authorization again.", "error");
					return;
				}
				progress.update("Authorization service unavailable. Retrying…");
				await waitForPoll();
				continue;
			}
			if (disposed) return;
			unavailablePolls = 0;
			const state = status.operation?.state;
			if (!state) {
				host.print(oauthErrorMessage("oauth_operation_not_found"), "error");
				return;
			}
			if (state === "awaiting_browser" && !awaitingBrowser) {
				try {
					const { authorizationUrl } = await host.request<{ authorizationUrl: string }>("mcp.oauth.url", {
						id: serverId, sessionId, operationId,
					});
					if (disposed) return;
					loginUrl = authorizationUrl;
					awaitingBrowser = true;
					progress.update("Waiting for browser authorization…", authorizationUrl);
				} catch (error) {
					if (disposed) return;
					host.print(permanentStatusError(error) ?? "The login link could not be loaded. Start authorization again.", "error");
					return;
				}
			} else if (state === "preparing" || state === "exchanging") {
				progress.update(state === "preparing" ? "Preparing authorization…" : "Completing authorization…");
			} else if (state === "complete") {
				progress.dispose();
				disposed = true;
				host.print(`${sanitizeDisplay(serverId)} authorized. Open /mcp and choose Connect to use it.`, "success");
				return;
			} else if (state === "cancelled" || state === "expired" || state === "failed") {
				progress.dispose();
				disposed = true;
				host.print(oauthErrorMessage(status.operation?.reasonCode ?? status.auth.reasonCode), state === "cancelled" ? "warning" : "error");
				return;
			}
			await waitForPoll();
		}
		if (!disposed) {
			await host.request("mcp.oauth.cancel", { id: serverId, sessionId, operationId }).catch(() => undefined);
			if (disposed) return;
			host.print(oauthErrorMessage("oauth_expired"), "error");
		}
	} finally {
		cleanup();
		progress.dispose();
	}
}

/** Forget saved access (Cancel default; optional provider revocation). */
export async function forgetConnectorAccess(host: FeatureHost, serverId: string): Promise<void> {
	const session = requireSession(host);
	const status = await host.request<{ auth: MCPOAuthView }>("mcp.oauth.status", { id: serverId, sessionId: session.id });
	const generation = status.auth.generation;
	if (!await confirm(host, `Forget saved access for ${sanitizeDisplay(serverId)}?`, "Forget access")) return;
	const revokeChoice = await host.pick("Also revoke at the provider?", [
		{ value: "no", label: "Keep provider grant" },
		{ value: "yes", label: "Also revoke at provider" },
	]);
	if (!revokeChoice) return;
	const result = await host.request<{ auth: MCPOAuthView; remoteRevocation: string }>("mcp.oauth.forget", {
		id: serverId, sessionId: session.id, expectedGeneration: generation, revoke: revokeChoice.value === "yes",
	});
	const remote = result.remoteRevocation === "succeeded" ? " Provider revocation succeeded."
		: result.remoteRevocation === "failed" ? " Provider revocation failed; local access was still forgotten."
		: result.remoteRevocation === "unsupported" ? " The provider has no revocation endpoint; local access was forgotten."
		: "";
	host.print(`${sanitizeDisplay(serverId)} access forgotten.${sanitizeDisplay(remote)}`, "success");
}

function operationReason(error: unknown): string | undefined {
	if (typeof error === "object" && error !== null && "message" in error) {
		const message = String((error as { message: unknown }).message);
		const match = /oauth_[a-z_]+/.exec(message);
		return match?.[0];
	}
	return undefined;
}

function permanentStatusError(error: unknown): string | undefined {
	const reason = operationReason(error);
	if (reason) return oauthErrorMessage(reason);
	if (typeof error !== "object" || error === null || !("code" in error)) return undefined;
	const code = (error as { code: unknown }).code;
	if (code === 1002) return oauthErrorMessage("oauth_operation_not_found");
	if (code === 1005) return "Permission to authorize this server changed. Review authorization again.";
	if (code === -32600 || code === -32601 || code === -32602) return "The authorization request is no longer valid. Start authorization again.";
	return undefined;
}
