// Settings commands: config, profile, status, model, provider.

import { userInfo } from "node:os";
import { keyValues, table } from "../format.ts";
import type { ConfigGetResult, ConfigSetResult, ProfileInfo, ProfileListResult, StatusResult, UsageResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { applyModel, CURATED_MODELS, filterCuratedModels, pickCuratedModel } from "./models.ts";
import { formatValue, options, type Command, type FeatureHost } from "./types.ts";

async function setConfig(host: FeatureHost, key: string, value: string, persist: boolean): Promise<void> {
	const result = await host.request<ConfigSetResult>("config.set", { key, value, persist });
	host.onConfig(result);
	host.print(`${key} = ${formatValue(result.value)}${persist ? " (saved)" : ""}`, "success");
}

const PERSONA_MAX_LENGTH = 2000;
const PERSONA_EXAMPLE = "Be concise, warm, and direct. Explain tradeoffs clearly.";

function personaError(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

async function personaAdd(host: FeatureHost, current: string): Promise<void> {
	let draft = current;
	for (;;) {
		const text = await host.askText({
			title: "Add persona",
			description: "Describe how you want your agent to communicate.\nSaved as your default across sessions.",
			initial: draft,
			example: PERSONA_EXAMPLE,
			maxLength: PERSONA_MAX_LENGTH,
		});
		if (text === undefined) return;
		if (!text.trim()) {
			host.print("Persona description cannot be blank.", "error");
			draft = text;
			continue;
		}
		if (Array.from(text).length > PERSONA_MAX_LENGTH) {
			host.print(`Persona description must be at most ${PERSONA_MAX_LENGTH} characters.`, "error");
			draft = text;
			continue;
		}
		try {
			const result = await host.request<ConfigSetResult>("config.set", { key: "persona_description", value: text, persist: true });
			host.onConfig(result);
			host.print("Default persona saved.", "success");
			return;
		} catch (error) {
			host.print(`Could not save persona: ${personaError(error)}`, "error");
			draft = text;
		}
	}
}

async function personaReset(host: FeatureHost, current: string): Promise<void> {
	if (!current) {
		host.print("Already using the agent default.", "plain");
		return;
	}
	const choice = await host.pick("Reset your default persona?", [
		{ value: "no", label: "No, keep it" },
		{ value: "yes", label: "Yes, reset" },
	]);
	if (choice?.value !== "yes") return;
	try {
		const result = await host.request<ConfigSetResult>("config.set", { key: "persona_description", value: "", persist: true });
		host.onConfig(result);
		host.print("Default persona reset.", "success");
	} catch (error) {
		host.print(`Could not reset persona: ${personaError(error)}`, "error");
	}
}

async function openPersonaMenu(host: FeatureHost): Promise<void> {
	const { config } = await host.request<ConfigGetResult>("config.get");
	const current = typeof config.persona_description === "string" ? config.persona_description : "";
	const choice = await host.pick("Persona", [
		{ value: "add", label: "Add", description: "Write or update your persona description" },
		{ value: "reset", label: "Reset", description: "Use the agent's default persona" },
	]);
	if (!choice) return;
	if (choice.value === "add") await personaAdd(host, current);
	else if (choice.value === "reset") await personaReset(host, current);
}

export const settingsCommands: Command[] = [
	{
		name: "persona",
		description: "Manage your default persona (menu when empty)",
		argumentHint: "[description|--clear]",
		async run(args, host) {
			const description = args.trim();
			if (description) {
				await setConfig(host, "persona_description", description === "--clear" ? "" : description, true);
				return;
			}
			await openPersonaMenu(host);
		},
	},
	{
		name: "default-model",
		description: "Pick the default model (menu when empty, saved across sessions)",
		argumentHint: "[search|model-id]",
		getArgumentCompletions: (prefix: string) => {
			const typed = prefix.trimStart().toLowerCase();
			if (/\s/.test(typed)) return null;
			const items = CURATED_MODELS.filter((e) => e.model.toLowerCase().startsWith(typed)).map((e) => ({
				value: e.model,
				label: `${e.model} (${e.provider})`,
				description: e.description,
			}));
			return items.length ? items : null;
		},
		async run(args, host) {
			const query = args.trim();
			if (!query) {
				const entry = await pickCuratedModel(host, CURATED_MODELS);
				if (!entry) return;
				await applyModel(host, entry, true);
				return;
			}
			const exact = CURATED_MODELS.find((e) => e.model.toLowerCase() === query.toLowerCase());
			if (exact) {
				await applyModel(host, exact, true);
				return;
			}
			const entries = filterCuratedModels(query);
			if (entries.length) {
				const entry = await pickCuratedModel(host, entries);
				if (!entry) return;
				await applyModel(host, entry, true);
				return;
			}
			// Off-list custom id: keep the current provider, save the model.
			await setConfig(host, "model", query, true);
			const session = host.session();
			if (session) {
				await host.request("session.setModel", { sessionId: session.id, model: query });
			}
			host.print("Provider unchanged — use /provider if this id belongs to another provider.", "plain");
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
				host.print(`Provider: ${config.provider}`, "plain");
			} else {
				const provider = args.toLowerCase();
				await setConfig(host, "provider", provider, false);
				const session = host.session();
				if (session) {
					await host.request("session.setModel", { sessionId: session.id, provider });
				}
			}
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
				host.print(`Reasoning effort: ${config.reasoning_effort || "off (provider default)"}`, "plain");
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
			if (!value) host.print(`${key} = ${secrets.includes(key) ? (config[key] ? "set (in .env)" : "not set") : formatValue(config[key])}`, "plain");
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
