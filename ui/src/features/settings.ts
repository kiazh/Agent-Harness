// Settings commands: config, profile, status, model, provider.

import { userInfo } from "node:os";
import { keyValues, table } from "../format.ts";
import type { ConfigGetResult, ConfigSetResult, ProfileInfo, ProfileListResult, StatusResult, UsageResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { formatValue, options, type Command, type FeatureHost } from "./types.ts";

async function setConfig(host: FeatureHost, key: string, value: string, persist: boolean): Promise<void> {
	const result = await host.request<ConfigSetResult>("config.set", { key, value, persist });
	host.onConfig(result);
	host.print(`${key} = ${formatValue(result.value)}${persist ? " (saved)" : ""}`, "success");
}

export const settingsCommands: Command[] = [
	{
		name: "model",
		description: "Show or set the model",
		argumentHint: "[model]",
		async run(args, host) {
			if (!args) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Model: ${config.model}`);
			} else await setConfig(host, "model", args, false);
		},
	},
	{
		name: "provider",
		description: "Show or set the provider",
		argumentHint: "[openrouter|openai|anthropic|google|mistral|groq|together|deepseek|xai|ollama]",
		getArgumentCompletions: options([
			["openrouter", "OpenRouter (multi-model)"],
			["openai", "OpenAI direct"],
			["anthropic", "Anthropic direct"],
			["google", "Google direct (Gemini)"],
			["mistral", "Mistral direct"],
			["groq", "Groq direct"],
			["together", "Together AI direct"],
			["deepseek", "DeepSeek direct"],
			["xai", "xAI direct (Grok)"],
			["ollama", "Ollama (local models)"],
		]),
		async run(args, host) {
			if (!args) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Provider: ${config.provider}`);
			} else await setConfig(host, "provider", args.toLowerCase(), false);
		},
	},
	{
		name: "effort",
		description: "Show or set reasoning effort (low|medium|high|off)",
		argumentHint: "[low|medium|high|off]",
		getArgumentCompletions: options([
			["low", "Shallow reasoning, fast and cheap"],
			["medium", "Balanced reasoning depth"],
			["high", "Deep reasoning for hard problems"],
			["off", "Provider default (no effort override)"],
		]),
		async run(args, host) {
			const value = args.trim().toLowerCase();
			if (!value) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Reasoning effort: ${config.reasoning_effort || "off (provider default)"}`);
				return;
			}
			await setConfig(host, "reasoning_effort", value, false);
		},
	},
	{
		name: "config",
		description: "Show settings, or set one (--save to keep it)",
		argumentHint: "[key [value] [--save]]",
		async run(args, host) {
			const persist = /(^|\s)--save(\s|$)/.test(args);
			const [key, value] = splitSub(args.replace(/(^|\s)--save(?=\s|$)/, " "));
			const { config, secrets } = await host.request<ConfigGetResult>("config.get");
			if (!key) {
				const keys = Object.keys(config).sort();
				host.print(
					keyValues(keys.map((key) => [key, secrets.includes(key) ? (config[key] ? "set (in .env)" : "not set") : formatValue(config[key])])),
					"plain",
				);
				return;
			}
			if (!(key in config)) throw new Error(`Unknown setting "${key}". Use /config to list them.`);
			if (!value) host.print(`${key} = ${secrets.includes(key) ? (config[key] ? "set (in .env)" : "not set") : formatValue(config[key])}`);
			else await setConfig(host, key, value, persist);
		},
	},
	{
		name: "profile",
		description: "Show your profile, or set a preference",
		argumentHint: "[key value]",
		async run(args, host) {
			const userId = userInfo().username || "default";
			const [key, value] = splitSub(args);
			if (key && !value) throw new Error("Usage: /profile <key> <value>");
			const { profile } = key
				? await host.request<{ profile: ProfileInfo }>("profile.set", { userId, key, value })
				: await host.request<{ profile: ProfileInfo }>("profile.get", { userId });
			const prefs = Object.entries(profile.preferences);
			host.print(
				keyValues([
					["User", profile.userId],
					["Interactions", profile.interactionCount],
					["Top topics", profile.topTopics.map((t) => `${t.topic} (${t.count})`).join(", ")],
					...prefs.map(([k, v]): [string, unknown] => [`pref.${k}`, v]),
				]),
				"plain",
			);
		},
	},
	{
		name: "status",
		description: "Database, counts and configuration health",
		async run(_args, host) {
			const s = await host.request<StatusResult>("status");
			host.print(
				keyValues([
					["Database", s.postgres],
					["Model", `${s.model} (${s.provider})`],
					["OpenRouter key", s.openrouterKeySet ? "set" : "not set"],
					["Sessions", s.sessions],
					["Context chunks", s.contextChunks],
					["Memories", `${s.memories} (${s.pendingMemories} pending)`],
					["Tools", s.tools.join(", ")],
				]),
				"plain",
			);
		},
	},
	{
		name: "profiles",
		description: "List all user profiles",
		async run(_args, host) {
			const { profiles } = await host.request<ProfileListResult>("profile.list", { limit: 50 });
			if (!profiles.length) {
				host.print("No profiles found.", "plain");
				return;
			}
			host.print(
				table(
					["User", "Interactions", "Top topics"],
					profiles.map((p) => [
						p.userId,
						String(p.interactionCount),
						p.topTopics.map((t) => `${t.topic} (${t.count})`).join(", ") || "—",
					]),
					60,
				),
				"plain",
				);
		},
	},
	{
		name: "usage",
		description: "Token and request usage for this session",
		async run(_args, host) {
			const session = host.session();
			if (!session) throw new Error("No active session. Use /new.");
			const u = await host.request<UsageResult>("usage.get", { sessionId: session.id });
			host.print(
				keyValues([
					["Session requests", u.session.requests],
					["Session tokens", u.session.accountedTokens],
					["Session charged requests", u.session.chargedRequests],
					["Session charged tokens", u.session.chargedTokens],
					["Agent requests", u.agent.requests],
					["Agent tokens", u.agent.accountedTokens],
					["Unknown calls", u.session.unknownCalls],
					["Request limit", u.session.requestLimit ?? "—"],
					["Token limit", u.session.tokenLimit ?? "—"],
					["Requests remaining", u.session.requestsRemaining ?? "—"],
					["Tokens remaining", u.session.tokensRemaining ?? "—"],
				]),
				"plain",
			);
		},
	},
];
