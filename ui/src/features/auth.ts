// /auth — ChatGPT-account sign-in for the chatgpt provider.
//
// The gateway owns the browser PKCE flow: auth.chatgpt.login returns the
// authorization URL immediately and the flow settles on the
// auth.chatgpt.completed / auth.chatgpt.failed events. No token value ever
// reaches the UI; the status card is credentials-free by contract.

import { keyValues } from "../format.ts";
import type {
	ChatGPTAuthStatus,
	ChatGPTImportResult,
	ChatGPTLoginResult,
	ChatGPTLogoutResult,
	ChatGPTStatusResult,
} from "../protocol.ts";
import { splitSub } from "../commands.ts";
import type { Command, FeatureHost } from "./types.ts";

const SUBCOMMANDS: Array<[string, string]> = [
	["status", "Show the ChatGPT sign-in status"],
	["sign-in", "Sign in with your ChatGPT account (browser flow)"],
	["import-codex", "Import the Codex CLI's ChatGPT credentials"],
	["sign-out", "Remove Machina's ChatGPT tokens only"],
];

/** UI bound on the settle wait; the gateway bounds its own flow at 180s. */
const LOGIN_SETTLE_MS = 200_000;
const POLL_INTERVAL_MS = 1_000;

type AuthOutcome =
	| { kind: "completed"; accountId: string | null; expiry: number | null }
	| { kind: "failed"; reason: string }
	| { kind: "timeout" };

function sourceLabel(status: ChatGPTAuthStatus): string {
	if (status.source === "codex") return "Codex CLI credentials";
	if (status.source === "machina") return "Machina token store";
	return "unknown source";
}

/** Unix seconds -> ISO 8601 UTC (short form when the time is whole seconds). */
export function formatAuthExpiry(value: number | null): string {
	if (typeof value !== "number") return "unknown";
	return new Date(value * 1000).toISOString().replace(".000Z", "Z");
}

/** The credentials-free status card (or the honest signed-out message). */
export function renderAuthStatus(status: ChatGPTAuthStatus): string {
	if (!status.signedIn) {
		return [
			"ChatGPT account: not signed in.",
			"Sign in with /auth sign-in, or pick up the Codex CLI's credentials with /auth import-codex.",
		].join("\n");
	}
	const state = status.expired
		? `signed in via ${sourceLabel(status)} (expired — refreshes on next use)`
		: `signed in via ${sourceLabel(status)}`;
	return keyValues([
		["Status", state],
		["Account", status.accountId ?? "unknown"],
		["Expires", formatAuthExpiry(status.expiresAt)],
		["Last refresh", status.lastRefresh ?? "—"],
	]);
}

async function showStatus(host: FeatureHost): Promise<void> {
	const { status } = await host.request<ChatGPTStatusResult>("auth.chatgpt.status");
	host.print(
		`${renderAuthStatus(status)}\n\nActions: /auth sign-in · /auth import-codex · /auth sign-out`,
		"plain",
	);
}

function failureText(reason: string): string {
	if (reason === "timeout") {
		return "Sign-in timed out before the browser callback arrived. Run /auth sign-in to try again.";
	}
	if (reason === "state_mismatch") {
		return "The sign-in callback failed its state check and was rejected. Run /auth sign-in to try again.";
	}
	if (reason === "access_denied") return "The sign-in was denied in the browser.";
	return `Sign-in failed (${reason}). Run /auth sign-in to try again.`;
}

/** Settle on the flow's terminal event; poll status when the host has no events. */
async function waitForOutcome(host: FeatureHost): Promise<AuthOutcome> {
	if (host.onGatewayEvent) {
		const subscribe = host.onGatewayEvent.bind(host);
		return await new Promise<AuthOutcome>((resolve) => {
			let unsubscribe: () => void = () => {};
			const timer = setTimeout(() => {
				unsubscribe();
				resolve({ kind: "timeout" });
			}, LOGIN_SETTLE_MS);
			unsubscribe = subscribe((event) => {
				if (event.type !== "auth.chatgpt.completed" && event.type !== "auth.chatgpt.failed") return;
				clearTimeout(timer);
				unsubscribe();
				resolve(
					event.type === "auth.chatgpt.completed"
						? { kind: "completed", accountId: event.account_id, expiry: event.expiry }
						: { kind: "failed", reason: event.reason },
				);
			});
		});
	}
	// Bounded fallback for hosts without event streaming.
	const deadline = Date.now() + LOGIN_SETTLE_MS;
	while (Date.now() < deadline) {
		await new Promise<void>((resolve) => {
			setTimeout(resolve, POLL_INTERVAL_MS);
		});
		const { status } = await host.request<ChatGPTStatusResult>("auth.chatgpt.status");
		if (status.signedIn) {
			return { kind: "completed", accountId: status.accountId, expiry: status.expiresAt };
		}
	}
	return { kind: "timeout" };
}

async function signIn(host: FeatureHost): Promise<void> {
	const { url } = await host.request<ChatGPTLoginResult>("auth.chatgpt.login", {});
	host.print(
		`Open this in your browser to sign in with ChatGPT:\n${url}\nWaiting for the sign-in to complete…`,
		"plain",
	);
	const outcome = await waitForOutcome(host);
	if (outcome.kind === "timeout") {
		host.print(
			"The sign-in window closed without a callback. Run /auth sign-in to try again.",
			"warning",
		);
		return;
	}
	if (outcome.kind === "failed") {
		host.print(failureText(outcome.reason), "error");
		return;
	}
	const account = outcome.accountId ? ` (${outcome.accountId})` : "";
	const expiry =
		outcome.expiry === null ? "" : ` Access token expires ${formatAuthExpiry(outcome.expiry)}.`;
	host.print(
		`Signed in to ChatGPT${account}.${expiry} The chatgpt provider can now run turns.`,
		"success",
	);
}

async function importCodex(host: FeatureHost): Promise<void> {
	const result = await host.request<ChatGPTImportResult>("auth.chatgpt.importCodex", {});
	if (result.imported) {
		host.print(
			`Imported the Codex CLI's ChatGPT credentials.\n${renderAuthStatus(result.status)}`,
			"success",
		);
		return;
	}
	host.print(
		[
			"Nothing imported: no Codex CLI credentials at ~/.codex/auth.json, or Machina",
			"already has its own tokens (sign out first to replace them).",
			renderAuthStatus(result.status),
		].join("\n"),
		"warning",
	);
}

async function signOut(host: FeatureHost): Promise<void> {
	const result = await host.request<ChatGPTLogoutResult>("auth.chatgpt.logout", {});
	if (result.removed) {
		host.print(
			"Signed out; Machina's ChatGPT token store was removed. Codex CLI credentials (~/.codex/auth.json) were left untouched.",
			"success",
		);
	} else {
		host.print("Already signed out — Machina has no ChatGPT token store to remove.", "plain");
	}
	if (result.status.signedIn && result.status.source === "codex") {
		host.print(
			"Codex CLI credentials remain and are imported automatically on the next chatgpt call.",
			"plain",
		);
	}
}

export const authCommand: Command = {
	name: "auth",
	description: "ChatGPT-account sign-in for the chatgpt provider (no API key)",
	argumentHint: "[status | sign-in | import-codex | sign-out]",
	getArgumentCompletions: (prefix: string) => {
		const typed = prefix.trimStart().toLowerCase();
		if (/\s/.test(typed)) return null;
		const items = SUBCOMMANDS.filter(([value]) => value.startsWith(typed)).map(
			([value, description]) => ({ value, label: value, description }),
		);
		return items.length ? items : null;
	},
	async run(args, host) {
		const [sub] = splitSub(args);
		switch (sub) {
			case "":
			case "status":
				await showStatus(host);
				return;
			case "sign-in":
			case "login":
				await signIn(host);
				return;
			case "import-codex":
				await importCodex(host);
				return;
			case "sign-out":
			case "logout":
				await signOut(host);
				return;
			default:
				throw new Error(
					`Unknown /auth option "${sub}". Try: status, sign-in, import-codex, sign-out.`,
				);
		}
	},
};
