import assert from "node:assert/strict";
import { test } from "node:test";
import type { SelectItem } from "@earendil-works/pi-tui";
import { parseCommand } from "../src/commands.ts";
import { type FeatureHost, type NoticeKind, runCommand } from "../src/features/index.ts";
import type { AskTextOptions, OAuthProgressHandle, OAuthProgressOptions } from "../src/features/types.ts";
import type { SessionInfo } from "../src/protocol.ts";
import { OAuthProgress, sanitizeStatus } from "../src/components/OAuthProgress.ts";

const SESSION: SessionInfo = {
	id: "11111111-2222-3333-4444-555555555555",
	title: "current",
	model: "m",
	provider: "openrouter",
	status: "active",
	lastActivity: null,
};

class OAuthFakeHost implements FeatureHost {
	calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	printed: Array<{ text: string; kind: NoticeKind }> = [];
	picks: Array<string | undefined> = [];
	textQueue: Array<string | undefined> = [];
	current: SessionInfo | undefined = SESSION;
	responses: Record<string, unknown | ((params: Record<string, unknown>) => unknown)> = {};
	progressOptions: OAuthProgressOptions[] = [];
	progressUpdates: Array<{ status: string; loginUrl?: string }> = [];
	progressDisposals = 0;

	async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
		this.calls.push({ method, params });
		const response = this.responses[method];
		if (response === undefined) throw new Error(`unexpected request ${method}`);
		if (typeof response === "function") {
			const fn = response as (p: Record<string, unknown>) => unknown;
			const value = fn(params);
			if (value instanceof Promise) return (await value) as T;
			return value as T;
		}
		return response as T;
	}
	print(text: string, kind: NoticeKind = "info") {
		this.printed.push({ text, kind });
	}
	printMarkdown(_text: string) {}
	session() {
		return this.current;
	}
	async submitTurn(_text: string) {}
	switchTo(_session: SessionInfo, _history: never[]) {}
	updateSession(_session: SessionInfo) {}
	async pick(_title: string, items: SelectItem[]) {
		const value = this.picks.shift();
		return items.find((i) => i.value === value);
	}
	async pickMany(_title: string, _items: SelectItem[]) {
		return undefined;
	}
	async pickTheme() {
		return undefined;
	}
	async askText(_options: AskTextOptions): Promise<string | undefined> {
		return this.textQueue.shift();
	}
	async inspectOutput(_toolId?: string) {}
	profileId() {
		return "default";
	}
	setProfile(_userId: string) {}
	sessionAgent() {
		return undefined;
	}
	async writeFile(path: string, _text: string) {
		return `/abs/${path}`;
	}
	onConfig(_result: unknown) {}
	clear() {}
	banner() {}
	restyle() {}
	async exit() {}
	showOAuthProgress(options: OAuthProgressOptions): OAuthProgressHandle {
		this.progressOptions.push(options);
		return {
			update: (status: string, loginUrl?: string) => {
				this.progressUpdates.push({ status, loginUrl });
			},
			dispose: () => {
				this.progressDisposals++;
			},
		};
	}
	last() {
		return this.printed.at(-1)!;
	}
}

const run = (text: string, host: OAuthFakeHost) => runCommand(parseCommand(text)!, host);

const HTTP_SERVER = {
	id: "docs", display_name: "Docs", transport: "streamable_http",
	state: "disconnected", enabled: true, oauth: { client_mode: "auto" },
};

function authorizeHost(): OAuthFakeHost {
	const host = new OAuthFakeHost();
	host.responses["mcp.list"] = { servers: [HTTP_SERVER] };
	host.responses["mcp.status"] = { server: HTTP_SERVER };
	host.responses["mcp.oauth.status"] = {
		auth: { state: "signed_out", generation: 0 },
		operation: { operationId: "op-1", connectorId: "docs", state: "awaiting_browser", expiresAt: new Date().toISOString() },
	};
	host.responses["mcp.oauth.preview"] = {
		previewId: "preview-1", digest: "digest-1",
		review: {
			connectorId: "docs", revision: 1, endpoint: "https://mcp.example.com/mcp",
			resource: "https://mcp.example.com/mcp", issuer: "https://auth.example.com",
			requestedScopes: ["read"], registrationMode: "dcr",
			endpoints: { authorization: "https://auth.example.com/authorize", token: "https://auth.example.com/token" },
			redirectUri: "http://127.0.0.1:9999/oauth/callback/x",
		},
	};
	host.responses["mcp.oauth.start"] = {
		operation: { operationId: "op-1", connectorId: "docs", state: "preparing", expiresAt: new Date().toISOString() },
	};
	host.responses["mcp.oauth.url"] = { authorizationUrl: "https://auth.example.com/authorize?client_id=x" };
	host.responses["mcp.oauth.open"] = { opened: true };
	host.responses["mcp.oauth.cancel"] = { cancelled: true };
	return host;
}

test("/mcp HTTP add offers OAuth and saves non-secret settings", async () => {
	const host = authorizeHost();
	host.responses["mcp.list"] = { servers: [] };
	host.picks.push("__add__", "streamable_http", "oauth", "auto");
	host.textQueue.push("docs", "Docs", "https://mcp.example.com/mcp", "", "[]", "{}");
	host.responses["mcp.configure"] = { server: { ...HTTP_SERVER, id: "docs" } };
	await run("/mcp", host);
	const configure = host.calls.find((call) => call.method === "mcp.configure");
	assert.ok(configure);
	const config = (configure.params as { config: Record<string, unknown> }).config;
	assert.equal(config.transport, "streamable_http");
	assert.deepEqual(config.oauth, {
		client_mode: "auto", client_id: "", client_metadata_url: "", client_secret_ref: "",
		token_endpoint_auth_method: "none", issuer: "", scope_allowlist: [], callback_port: 0,
	});
	assert.equal((config.oauth as { client_secret_ref: string }).client_secret_ref, "");
});

test("/mcp stdio add shows no OAuth controls", async () => {
	const host = authorizeHost();
	host.responses["mcp.list"] = { servers: [] };
	host.picks.push("__add__", "stdio");
	host.textQueue.push("local", "Local", "node", "[]", "", "{}");
	host.responses["mcp.configure"] = { server: { id: "local" } };
	await run("/mcp", host);
	const configure = host.calls.find((call) => call.method === "mcp.configure");
	const config = (configure!.params as { config: Record<string, unknown> }).config;
	assert.equal(config.oauth, undefined);
});

test("authorize confirms, starts, polls, and reports without leaking the URL", async () => {
	const host = authorizeHost();
	let urlFetched = false;
	const awaiting = () => ({
		auth: { state: "signed_out", generation: 0 },
		operation: { operationId: "op-1", connectorId: "docs", state: "awaiting_browser", expiresAt: new Date().toISOString() },
	});
	const complete = () => ({
		auth: { state: "authorized", generation: 1, issuer: "https://auth.example.com" },
		operation: { operationId: "op-1", connectorId: "docs", state: "complete", expiresAt: new Date().toISOString() },
	});
	host.responses["mcp.oauth.status"] = () => (urlFetched ? complete() : awaiting());
	host.responses["mcp.oauth.url"] = () => {
		urlFetched = true;
		return { authorizationUrl: "https://auth.example.com/authorize?client_id=x" };
	};
	host.picks.push("docs", "authorize", "yes");
	await run("/mcp", host);
	const methods = host.calls.map((call) => call.method);
	assert.ok(methods.includes("mcp.oauth.preview"));
	assert.ok(methods.includes("mcp.oauth.start"));
	assert.ok(methods.includes("mcp.oauth.status"));
	assert.equal(host.last().kind, "success");
	assert.match(host.last().text, /authorized/);
	// The authorization URL appears only in the transient overlay, never printed.
	assert.ok(!host.printed.some((entry) => entry.text.includes("auth.example.com/authorize?")));
	assert.ok(host.progressUpdates.some((update) => (update.loginUrl ?? "").includes("auth.example.com/authorize?")));
	assert.ok(host.progressDisposals >= 1);
});

test("preview cancel sends no start/open/exchange", async () => {
	const host = authorizeHost();
	host.picks.push("docs", "authorize", "no");
	await run("/mcp", host);
	const methods = host.calls.map((call) => call.method);
	assert.ok(methods.includes("mcp.oauth.preview"));
	assert.ok(methods.includes("mcp.oauth.cancel"));
	assert.ok(!methods.includes("mcp.oauth.start"));
	assert.ok(!methods.includes("mcp.oauth.open"));
});

test("escape during authorization cancels and clears timers", async () => {
	const host = authorizeHost();
	host.responses["mcp.oauth.status"] = () => new Promise((_resolve, reject) => setTimeout(() => reject(new Error("gone")), 20));
	host.picks.push("docs", "authorize", "yes");
	const finished = run("/mcp", host);
	while (host.progressOptions.length === 0) await new Promise((resolve) => setTimeout(resolve, 5));
	host.progressOptions[0]!.onCancel();
	await finished;
	const methods = host.calls.map((call) => call.method);
	assert.ok(methods.includes("mcp.oauth.cancel"));
	assert.ok(host.progressDisposals >= 1);
});

test("forget confirmation reports the revocation outcome", async () => {
	const host = authorizeHost();
	host.responses["mcp.oauth.status"] = { auth: { state: "authorized", generation: 3 } };
	host.responses["mcp.oauth.forget"] = { auth: { state: "signed_out", generation: 4 }, remoteRevocation: "unsupported" };
	host.picks.push("docs", "forget", "yes", "no");
	await run("/mcp", host);
	const forget = host.calls.find((call) => call.method === "mcp.oauth.forget");
	assert.deepEqual(forget!.params, { id: "docs", sessionId: SESSION.id, expectedGeneration: 3, revoke: false });
	assert.match(host.last().text, /forgotten/);
});

test("OAuthProgress sanitizes provider text and stays usable when narrow", async () => {
	assert.equal(sanitizeStatus("Title\x1b[31m"), "Title");
	const progress = new OAuthProgress("Title\x1b[31m", "Status\x00with\x1bcontrols", {
		onOpen: () => {},
		onCancel: () => {},
	}, "https://auth.example.com/authorize?x=1");
	// eslint-disable-next-line no-control-regex
	const strip = (row: string): string => row.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
	const rows = progress.render(20).map(strip);
	assert.ok(rows.every((row) => !row.includes("[31m") && !row.includes("\x00")));
	assert.ok(progress.render(80).map(strip).some((row) => row.includes("cancel")));

	let cancelled = false;
	const cancellable = new OAuthProgress("t", "s", { onOpen: () => {}, onCancel: () => {
		cancelled = true;
	} });
	cancellable.handleInput("\x1b");
	assert.equal(cancelled, true);
});

test("real gateway wrapper exposes the oauth method boundary", { timeout: 60_000 }, async (t) => {
	const python = process.env.AH_TEST_PYTHON;
	if (!python) {
		t.skip("AH_TEST_PYTHON not set");
		return;
	}
	const { mkdtemp } = await import("node:fs/promises");
	const { tmpdir } = await import("node:os");
	const { join } = await import("node:path");
	const { GatewayClient } = await import("../src/gateway.ts");
	const home = await mkdtemp(join(tmpdir(), "ah-oauth-wrapper-"));
	const token = `oauth-wrapper-${Math.random().toString(36).slice(2)}`;
	const client = new GatewayClient({
		python,
		token,
		env: {
			HOME: home,
			AH_ENV_FILE: join(home, "empty.env"),
			AGENT_HARNESS_SECRET_BACKEND: "",
			AGENT_HARNESS_TEST_DATABASE_URL: "",
			AH_GATEWAY_NO_SCHEDULER: "1",
			AH_GATEWAY_TOKEN: token,
		},
	});
	client.start();
	try {
		const list = await client.request<{ servers: unknown[] }>("mcp.list", {}, 30_000);
		assert.deepEqual(list, { servers: [] });
		// Unknown params are rejected before any session, network, or browser work.
		await assert.rejects(
			client.request("mcp.oauth.preview", { id: "x", sessionId: "y", callback: "evil" }, 30_000),
			(error: unknown) => (error as { code?: number }).code === -32602,
		);
		await assert.rejects(
			client.request("mcp.oauth.status", { id: "x", sessionId: "y", operationId: 42 }, 30_000),
			(error: unknown) => (error as { code?: number }).code === -32602,
		);
		await assert.rejects(
			client.request("mcp.call", {}, 30_000),
			(error: unknown) => (error as { code?: number }).code === -32601,
		);
		await client.request("shutdown", {}, 5_000).catch(() => undefined);
	} finally {
		await client.stop();
	}
});
