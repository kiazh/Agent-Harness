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
		`Callback: ${review.redirectUri}`,
	];
	return lines.map((line) => sanitizeDisplay(line)).join("\n");
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

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
	const cleanup = async (): Promise<void> => {
		disposed = true;
		if (pollTimer) clearTimeout(pollTimer);
	};
	const progress = host.showOAuthProgress({
		title: `Authorizing ${serverId}`,
		status: "Preparing authorization…",
		onOpen: () => {
			host.request("mcp.oauth.open", { id: serverId, sessionId, operationId }).catch(() => undefined);
		},
		onCancel: () => {
			void cleanup().then(() =>
				host.request("mcp.oauth.cancel", { id: serverId, sessionId, operationId }).catch(() => undefined));
		},
	});
	try {
		const deadline = Date.now() + OVERALL_MS;
		let awaitingBrowser = false;
		while (!disposed && Date.now() < deadline) {
			let status: { auth: MCPOAuthView; operation?: MCPOAuthOperationView };
			try {
				// Sequential awaits keep exactly one status request outstanding.
				status = await host.request("mcp.oauth.status", { id: serverId, sessionId, operationId });
			} catch {
				progress.update("Authorization service unavailable.");
				await sleep(POLL_MS);
				continue;
			}
			const state = status.operation?.state;
			if (state === "awaiting_browser" && !awaitingBrowser) {
				awaitingBrowser = true;
				try {
					const { authorizationUrl } = await host.request<{ authorizationUrl: string }>("mcp.oauth.url", {
						id: serverId, sessionId, operationId,
					});
					progress.update("Waiting for browser authorization…", authorizationUrl);
				} catch {
					progress.update("Waiting for browser authorization…");
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
			await sleep(POLL_MS);
		}
		if (!disposed) {
			await host.request("mcp.oauth.cancel", { id: serverId, sessionId, operationId }).catch(() => undefined);
			host.print(oauthErrorMessage("oauth_expired"), "error");
		}
	} finally {
		await cleanup();
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
