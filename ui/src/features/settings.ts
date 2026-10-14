// Settings commands: config, profile, status, model, provider.

import { keyValues, table } from "../format.ts";
import type {
	ConfigGetResult,
	ConfigSetResult,
	EffortSetResult,
	ProfileInfo,
	ProfileListResult,
	ProviderAddResult,
	ProviderInfo,
	ProviderRemoveResult,
	ProvidersListResult,
	StatusResult,
	UsageResult,
} from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { applyModel, CURATED_MODELS, filterCuratedModels, pickCuratedModel } from "./models.ts";
import { confirm, formatValue, options, PROFILE_ID_RE, type Command, type FeatureHost } from "./types.ts";

function printProfile(host: FeatureHost, profile: ProfileInfo): void {
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
}

async function switchProfile(host: FeatureHost, userId: string): Promise<void> {
	host.setProfile(userId);
	host.print(
		`Switched to profile ${userId}. New chats learn under it; existing chats keep theirs.`,
		"success",
	);
}

async function createProfile(host: FeatureHost): Promise<void> {
	for (;;) {
		const name = await host.askText({
			title: "New profile",
			description: "Lowercase letters, numbers, and dashes (this becomes the profile id).\nEach profile keeps its own learned notes, RL, and memories.",
			maxLength: 40,
		});
		if (name === undefined) return;
		const userId = name.trim().toLowerCase();
		if (!PROFILE_ID_RE.test(userId)) {
			host.print("Use lowercase letters, numbers, and dashes (up to 40).", "error");
			continue;
		}
		await host.request("profile.get", { userId });
		await switchProfile(host, userId);
		return;
	}
}

async function openProfileMenu(host: FeatureHost): Promise<void> {
	const choice = await host.pick("Profile", [
		{ value: "view", label: "View", description: "Show the current profile" },
		{ value: "new", label: "New", description: "Create a profile and switch to it" },
		{ value: "switch", label: "Switch", description: "Change the active profile" },
	]);
	if (!choice) return;
	if (choice.value === "new") {
		await createProfile(host);
		return;
	}
	if (choice.value === "switch") {
		const { profiles } = await host.request<ProfileListResult>("profile.list", { limit: 50 });
		if (!profiles.length) {
			host.print("No profiles yet — create one first.", "plain");
			return;
		}
		const picked = await host.pick(
			"Switch profile",
			profiles.map((p) => ({
				value: p.userId,
				label: `${p.userId}${p.userId === host.profileId() ? " (current)" : ""}`,
				description: `${p.interactionCount} interactions`,
			})),
		);
		if (!picked || picked.value === host.profileId()) return;
		await switchProfile(host, picked.value);
		return;
	}
	const { profile } = await host.request<{ profile: ProfileInfo }>("profile.get", {
		userId: host.profileId(),
	});
	printProfile(host, profile);
}

async function setConfig(host: FeatureHost, key: string, value: string, persist: boolean): Promise<void> {
	const result = await host.request<ConfigSetResult>("config.set", { key, value, persist });
	host.onConfig(result);
	host.print(`${key} = ${formatValue(result.value)}${persist ? " (saved)" : ""}`, "success");
}

const PERSONA_MAX_LENGTH = 2000;
const PERSONA_EXAMPLE = "Be concise, warm, and direct. Explain tradeoffs clearly.";

// ─── /provider helpers: list, add, remove user-added providers ──────────────

function providerRow(p: ProviderInfo): string[] {
	return [
		p.selected ? `${p.name} (current)` : p.name,
		p.kind,
		p.baseUrl ?? "—",
		p.kind === "named" ? (p.keySet ? "set" : p.keylessLocal ? "keyless local" : "missing") : "—",
		p.kind === "named" ? String(p.modelsCount ?? 0) : "—",
	];
}

async function printProviders(host: FeatureHost): Promise<void> {
	const result = await host.request<ProvidersListResult>("providers.list");
	const rows = result.providers.map(providerRow);
	const body = table(["Provider", "Kind", "Base URL", "Key", "Models"], rows, 36);
	host.print(
		`${body}\n\nCurrent: ${result.selected}\nUser-added provider keys live in .env (never shown; manage with /provider add).`,
		"plain",
	);
}

async function addProvider(host: FeatureHost, rest: string): Promise<void> {
	const parts = rest.trim().split(/\s+/).filter(Boolean);
	let name = parts[0] ?? "";
	let baseUrl = parts[1] ?? "";
	if (!name) {
		name =
			(await host.askText({
				title: "Provider name",
				description: "Lowercase letters, digits and dashes (e.g. my-vllm). Used in /provider and /models.",
				placeholder: "my-provider",
				maxLength: 32,
			}))?.trim().toLowerCase() ?? "";
		if (!name) return;
	}
	if (!baseUrl) {
		baseUrl =
			(await host.askText({
				title: `Base URL for ${name}`,
				description: "OpenAI-compatible endpoint; /models is fetched from it and chat goes to /chat/completions.",
				placeholder: "http://127.0.0.1:8000/v1",
				maxLength: 500,
			}))?.trim() ?? "";
		if (!baseUrl) return;
	}
	const key =
		(await host.askText({
			title: `API key for ${name} (optional)`,
			description: "Leave empty for keyless local endpoints. Stored in .env like /keys — masked, never shown again.",
			placeholder: "leave empty for no key",
			secret: true,
		}))?.trim() ?? "";
	const confirmed = await confirm(host, `Add provider "${name}" at ${baseUrl}?`, "Add");
	if (!confirmed) return;
	let result: ProviderAddResult;
	try {
		result = await host.request<ProviderAddResult>("providers.add", { name, baseUrl, apiKey: key || undefined });
	} catch (error) {
		// The submitted key must never echo in an error string.
		const message = (error instanceof Error ? error.message : String(error)).split(key).join("[redacted]");
		host.print(`Could not add ${name}: ${message}`, "error");
		return;
	}
	if (result.fetchError) {
		host.print(
			`Provider "${result.provider}" saved, but model fetch failed: ${result.fetchError}\nFix the URL/key and re-run /provider add ${result.provider} ${result.baseUrl}.`,
			"warning",
		);
	} else {
		const preview = result.models.slice(0, 8).map((m) => m.id).join(", ");
		host.print(
			`Provider "${result.provider}" added — ${result.models.length} model(s) pulled${preview ? `: ${preview}${result.models.length > 8 ? ", …" : ""}` : ""}.\nSwitch with /provider ${result.provider}, then pick a model with /models (or /default-model).`,
			"success",
		);
	}
	if (result.keySaved) {
		host.print(`Key stored in .env as ${result.apiKeyEnv} (0600, never shown).`, "plain");
	} else if (result.keySet) {
		host.print("Key set for this session only (not persisted).", "plain");
	}
}

async function removeProvider(host: FeatureHost, rest: string): Promise<void> {
	let name = rest.trim().toLowerCase();
	let listed: ProvidersListResult | undefined;
	try {
		listed = await host.request<ProvidersListResult>("providers.list");
	} catch {
		listed = undefined;
	}
	const named = listed?.providers.filter((p) => p.kind === "named") ?? [];
	if (!name) {
		if (!named.length) {
			host.print("No user-added providers to remove (built-in providers cannot be removed).", "warning");
			return;
		}
		const choice = await host.pick(
			"Remove which provider?",
			named.map((p) => ({
				value: p.name,
				label: p.name,
				description: `${p.baseUrl ?? "?"} · key ${p.keySet ? "set" : "not set"} · ${p.modelsCount ?? 0} model(s)`,
			})),
		);
		if (!choice) return;
		name = choice.value;
	}
	const info = named.find((p) => p.name === name);
	const traces = describeProviderTraces(name, info);
	const question = `Remove provider "${name}"? Deletes ${traces}.${info?.selected ? " It is the active provider — falls back to openrouter." : ""}`;
	const confirmed = await confirm(host, question, "Remove");
	if (!confirmed) return;
	try {
		const result = await host.request<ProviderRemoveResult>("providers.remove", { name });
		// The current session pins model/provider at creation: when it was
		// pinned to the removed provider, re-pin it to the fallback so the
		// next turn cannot route to a provider that no longer exists.
		const session = host.session();
		if (result.switchedTo && session && session.provider?.toLowerCase() === name) {
			await host.request("session.setModel", {
				sessionId: session.id,
				provider: result.switchedTo,
				...(result.modelResetTo ? { model: result.modelResetTo } : {}),
			});
		}
		host.print(removalSummary(result), "success");
		if (result.switchedTo) {
			host.print(
				`Active provider fell back to ${result.switchedTo}${result.modelResetTo ? ` (model ${result.modelResetTo})` : ""}. Pick a new default with /provider and /models.`,
				"warning",
			);
		}
	} catch (error) {
		host.print(`Could not remove ${name}: ${error instanceof Error ? error.message : String(error)}`, "error");
	}
}

/** What a removal deletes, for the confirm copy (best-effort from providers.list). */
function describeProviderTraces(name: string, info?: ProviderInfo): string {
	const keyEnv = info?.apiKeyEnv ?? `MACHINA_PROVIDER_${name.toUpperCase().replace(/-/g, "_")}_API_KEY`;
	const models = info?.modelsCount ?? 0;
	return `the store entry, the ${keyEnv} .env line, ${models} cached model(s), and the registry entry`;
}

/** Exactly what the gateway cleaned, from the removal report. */
function removalSummary(result: ProviderRemoveResult): string {
	const cleanup = result.cleanup;
	if (!cleanup) {
		return `Provider "${result.provider}" removed${result.switchedTo ? `; switched to ${result.switchedTo}` : ""}.`;
	}
	const bits: string[] = ["store entry"];
	if (cleanup.envKey) {
		bits.push(cleanup.envLineRemoved ? `${cleanup.envKey} line in .env` : `${cleanup.envKey} (no .env line found)`);
	}
	if (cleanup.cachedModels) bits.push(`${cleanup.cachedModels} cached model(s)`);
	bits.push("registry entry");
	return `Provider "${result.provider}" removed — ${bits.join(", ")}.`;
}

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
		description: "Show or set a provider; add/remove user-added providers",
		argumentHint: "[list|add [name url]|remove [name]|openrouter|openai|anthropic|chatgpt|claude|copilot|gemini|google|mistral|groq|together|deepseek|xai|opencode|custom|ollama|<name>]",
		getArgumentCompletions: options([
			["list", "List built-in and user-added providers"],
			["add", "Add a provider (name + base URL + optional key)"],
			["remove", "Remove a user-added provider"],
			["openrouter", "OpenRouter (multi-model)"],
			["openai", "OpenAI direct"],
			["anthropic", "Anthropic direct"],
			["chatgpt", "ChatGPT account (sign in with /auth)"],
			["claude", "Claude Pro/Max subscription (machina auth claude)"],
			["copilot", "GitHub Copilot seat (machina auth copilot)"],
			["gemini", "Gemini Code Assist license (machina auth gemini)"],
			["google", "Google direct (Gemini)"],
			["mistral", "Mistral direct"],
			["groq", "Groq direct"],
			["together", "Together AI direct"],
			["deepseek", "DeepSeek direct"],
			["xai", "xAI direct (Grok)"],
			["opencode", "OpenCode Zen gateway (OPENCODE_API_KEY)"],
			["custom", "Custom OpenAI-compatible endpoint (CUSTOM_BASE_URL)"],
			["ollama", "Ollama (local models)"],
		]),
		async run(args, host) {
			const [sub, rest] = splitSub(args);
			if (sub === "list") {
				await printProviders(host);
				return;
			}
			if (sub === "add") {
				await addProvider(host, rest);
				return;
			}
			if (sub === "remove") {
				await removeProvider(host, rest);
				return;
			}
			if (!sub) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Provider: ${config.provider}`, "plain");
				host.print(
					"ChatGPT-account models need no API key — sign in with /auth first. " +
						"User-added providers: /provider list · /provider add · /provider remove.",
					"plain",
				);
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
		description: "Show or set reasoning effort (ChatGPT: per-model low..max)",
		argumentHint: "[low|medium|high|xhigh|max|off]",
		getArgumentCompletions: options([
			["low", "Shallow reasoning, fast and cheap"],
			["medium", "Balanced reasoning depth"],
			["high", "Deep reasoning for hard problems"],
			["xhigh", "Extra-high (ChatGPT catalog tier)"],
			["max", "Maximum depth (ChatGPT catalog tier)"],
			["off", "Provider default (no effort override)"],
		]),
		async run(args, host) {
			const value = args.trim().toLowerCase();
			if (!value) {
				const { config } = await host.request<ConfigGetResult>("config.get");
				host.print(`Reasoning effort: ${config.reasoning_effort || "off (provider default)"}`, "plain");
				return;
			}
			const session = host.session();
			const result = await host.request<EffortSetResult>("models.setEffort", session ? { effort: value, sessionId: session.id } : { effort: value });
			host.print(
				`Reasoning effort: ${result.effort ?? "off (provider default)"} · supported for ${result.model || result.provider}: ${result.supported.join("/")}`,
				"success",
			);
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
		description: "View, create, or switch your profile (menu when empty)",
		argumentHint: "[key value]",
		async run(args, host) {
			const [key, value] = splitSub(args);
			if (!key) {
				await openProfileMenu(host);
				return;
			}
			if (!value) throw new Error("Usage: /profile <key> <value>");
			const userId = host.profileId();
			const { profile } = await host.request<{ profile: ProfileInfo }>("profile.set", { userId, key, value });
			printProfile(host, profile);
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
			const current = host.profileId();
			host.print(
				table(
					["User", "Interactions", "Top topics"],
					profiles.map((p) => [
						`${p.userId}${p.userId === current ? " (current)" : ""}`,
						String(p.interactionCount),
						p.topTopics.map((t) => `${t.topic} (${t.count})`).join(", ") || "—",
					]),
					60,
				) + "\n\nUse /profile to view, create, or switch.",
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
