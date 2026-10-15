// API-key menu: list, set and clear provider keys without leaving the UI.
//
// Bare /keys runs a real flow: pick a key, then Set/Replace, Clear or Back.
// Values are collected through a masked editor and never echoed, printed,
// logged, or kept in history. `secrets.set` applies immediately and saves
// to the git-ignored `.env` (persist:true). Run `machina setup` once for
// first-launch configuration of the same file.
//
// The rows are the static catalog (secrets.list) plus one row per user-added
// provider from providers.list: a provider with a key contributes its
// derived `MACHINA_PROVIDER_<NAME>_API_KEY`; a keyless local endpoint gets a
// non-editable "no key (local endpoint)" row (nothing to set or clear).
// Ordering stays stable: catalog keys, then named providers in list order.

import { table } from "../format.ts";
import type { EffectiveCredentialState, ProvidersListResult, SecretsListResult, SecretsSetResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, type Command, type FeatureHost } from "./types.ts";

function printSet(host: FeatureHost, result: SecretsSetResult): void {
    host.print(result.message ?? `${result.key} set${result.persisted ? ` (saved to ${result.envFile})` : " (this session only)"}. Takes effect on the next turn.`,
        result.effectiveUpdated === false ? "info" : "success");
}

async function clearKey(host: FeatureHost, key: string): Promise<void> {
    const result = await host.request<EffectiveCredentialState>("secrets.clear", { key });
    host.print(result.message ?? `${key.toUpperCase()} cleared (session and .env).`,
        result.effectiveUpdated === false ? "info" : "success");
}

// Mirrors PROVIDER_KEYS + MANAGED_ENV_KEYS in machina/security/env_file.py and
// machina/gateway/features/secrets.py: `secrets.set` refuses anything outside
// that allowlist (plus derived MACHINA_PROVIDER_<NAME>_API_KEY rows, which
// arrive via providers.list), so completions must not suggest more.
const KNOWN_KEYS: Array<[string, string]> = [
	["OPENROUTER_API_KEY", "OpenRouter (cloud models, default provider)"],
	["OPENAI_API_KEY", "OpenAI (GPT models, embeddings)"],
	["ANTHROPIC_API_KEY", "Anthropic (Claude models)"],
	["GOOGLE_API_KEY", "Google (Gemini models)"],
	["MISTRAL_API_KEY", "Mistral models"],
	["GROQ_API_KEY", "Groq (fast inference)"],
	["TOGETHER_API_KEY", "Together AI (open models)"],
	["DEEPSEEK_API_KEY", "DeepSeek models"],
	["XAI_API_KEY", "xAI (Grok models)"],
	["OPENCODE_API_KEY", "OpenCode Zen (free + paid models gateway)"],
	["KIMI_API_KEY", "Kimi Code (K3 coding models)"],
	["CUSTOM_API_KEY", "Custom OpenAI-compatible endpoint"],
	["COHERE_API_KEY", "Cohere (reranking)"],
	["DATABASE_URL", "PostgreSQL connection string"],
	["SEARXNG_URL", "Self-hosted SearXNG for web search"],
	["MACHINA_MODEL", "Default model identifier"],
	["CUSTOM_BASE_URL", "Custom OpenAI-compatible base URL"],
];

/** One /keys row: a catalog key or a user-added provider's derived key. */
export interface KeyRow {
	key: string;
	status: string;
	set: boolean;
	/** False for keyless-local provider rows: nothing to set or clear. */
	editable: boolean;
	description: string;
	/** User-added provider name for provider rows (absent for catalog keys). */
	provider?: string;
}

function statusTable(rows: KeyRow[]): string {
	return table(
		["Key", "Status", "Description"],
		rows.map((row) => [row.key, row.status, row.description]),
		40,
	);
}

/**
 * Merge the gateway's static key catalog with the user-added providers'
 * derived env keys. A provider without a key on a loopback base URL is
 * keyless by design and gets a non-editable row instead of an empty set
 * flow. providers.list failures (older gateway) leave the static rows.
 */
export async function loadKeyRows(host: FeatureHost): Promise<{ rows: KeyRow[]; envFile: string }> {
	const { secrets, envFile } = await host.request<SecretsListResult>("secrets.list");
	const rows: KeyRow[] = secrets.map((secret) => ({
		key: secret.key,
		status: secret.effectiveSet === null ? "unresolved" : (secret.effectiveSet ?? secret.set) ? `set${secret.effectiveSource ? ` (${secret.effectiveSource})` : ""}` : "not set",
		set: secret.effectiveSet ?? secret.set,
		editable: true,
		description: secret.description,
	}));
	let listed: ProvidersListResult | undefined;
	try {
		listed = await host.request<ProvidersListResult>("providers.list");
	} catch {
		listed = undefined;
	}
	if (listed) {
		const seen = new Set(rows.map((row) => row.key));
		for (const provider of listed.providers) {
			if (provider.kind !== "named" || !provider.apiKeyEnv || seen.has(provider.apiKeyEnv)) continue;
			const keyless = provider.keylessLocal === true && provider.keySet !== true;
			seen.add(provider.apiKeyEnv);
			rows.push({
				key: provider.apiKeyEnv,
				status: keyless ? "no key (local endpoint)" : provider.keySet ? "set" : "not set",
				set: provider.keySet === true,
				editable: !keyless,
				description: `${provider.name} (user-added provider)${provider.baseUrl ? ` · ${provider.baseUrl}` : ""}`,
				provider: provider.name,
			});
		}
	}
	return { rows, envFile };
}

/** Strip a secret from error text so failures never leak the value. */
export function redactSecret(secret: string, text: string): string {
	if (!secret) return text;
	return text.split(secret).join("[redacted]");
}

export const keysCommand: Command = {
	name: "keys",
	description: "Manage API keys: list, set or clear (menu when empty)",
	argumentHint: "[list|set KEY VALUE [--no-save]|clear KEY]",
	getArgumentCompletions: (prefix: string) => {
		const typed = prefix.trimStart();
		const subcommands = options([["list", "Show all keys"], ["set", "Set a key"], ["clear", "Remove a key"]]);
		if (!/\s/.test(typed)) return subcommands ? subcommands(prefix) : null;
		const [sub, rest] = splitSub(typed);
		if (sub !== "set" && sub !== "clear") return null;
		const items = KNOWN_KEYS.filter(([key]) => key.toLowerCase().startsWith(rest.trimStart().toLowerCase())).map(
			([key, description]) => ({ value: key, label: key, description }),
		);
		return items.length ? items : null;
	},
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		if (!sub) {
			await openMenu(host);
			return;
		}
		if (sub === "list") {
			const { rows, envFile } = await loadKeyRows(host);
			host.print(`${statusTable(rows)}\n\nFile: ${envFile}\nValues are never shown. Use /keys set KEY VALUE to update one.\nOpenAI account models need no key — sign in with /auth instead.`, "plain");
			return;
		}
		if (sub === "set") {
			const persist = !/(^|\s)--no-save(\s|$)/.test(rest);
			const cleaned = rest.replace(/(^|\s)--no-save(?=\s|$)/, " ").trim();
			const space = cleaned.search(/\s/);
			if (space < 0) throw new Error("Usage: /keys set KEY VALUE [--no-save]");
			const key = cleaned.slice(0, space).toUpperCase();
			const value = cleaned.slice(space).trim();
			if (!value) throw new Error("Usage: /keys set KEY VALUE [--no-save]");
			try {
				const result = await host.request<SecretsSetResult>("secrets.set", { key, value, persist });
				printSet(host, result);
			} catch (error) {
				throw new Error(`Could not save ${key}: ${redactSecret(value, error instanceof Error ? error.message : String(error))}`);
			}
			return;
		}
		if (sub === "clear") {
			const key = rest.trim();
			if (!key) throw new Error("Usage: /keys clear KEY");
			const picked = await resolveKey(host, key);
			await clearKey(host, picked);
			return;
		}
		throw new Error(`Unknown /keys option "${sub}". Try: list, set, clear.`);
	},
};

async function resolveKey(host: FeatureHost, prefix: string): Promise<string> {
	const { rows } = await loadKeyRows(host);
	const keys = rows.map((row) => row.key);
	const exact = keys.find((k) => k === prefix.trim().toUpperCase());
	if (exact) return exact;
	const matches = keys.filter((k) => k.startsWith(prefix.trim().toUpperCase()));
	if (matches.length === 1) return matches[0]!;
	if (!matches.length) throw new Error(`No key starts with "${prefix}". Use /keys list.`);
	throw new Error(`"${prefix}" matches ${matches.length} keys; be more specific.`);
}

async function openMenu(host: FeatureHost): Promise<void> {
	for (;;) {
		const { rows } = await loadKeyRows(host);
		const choice = await host.pick(
			"API keys (values never shown)",
			rows.map((row) => ({
				value: row.key,
				label: `${row.key} — ${row.status}`,
				description: row.description,
			})),
		);
		if (!choice) return;
		const info = rows.find((row) => row.key === choice.value);
		if (!info) return;
		if (!info.editable) {
			host.print(
				`${info.key}: ${info.description} — no key is needed (or settable) here. Remove the provider with /provider remove ${info.provider}.`,
				"info",
			);
			continue;
		}
		const action = await host.pick(`${info.key} (${info.status})`, [
			{ value: "set", label: info.set ? "Replace…" : "Set…", description: "Enter a new value (masked, never shown)" },
			{ value: "clear", label: "Clear", description: "Remove from this session and .env" },
			{ value: "back", label: "Back", description: "Return to the key list" },
		]);
		if (!action || action.value === "back") continue;
		if (action.value === "clear") {
			const confirmed = await confirm(host, `Clear ${info.key}?`, "Clear");
			if (!confirmed) continue;
			try {
				await clearKey(host, info.key);
			} catch (error) {
				host.print(`Could not clear ${info.key}: ${error instanceof Error ? error.message : String(error)}`, "error");
			}
			return;
		}
		const value = await host.askText({
			title: `Set ${info.key}`,
			description: "Paste the value. It is masked and never displayed.",
			placeholder: "••••••••",
			secret: true,
		});
		if (value === undefined) continue;
		try {
			const result = await host.request<SecretsSetResult>("secrets.set", { key: info.key, value, persist: true });
			printSet(host, result);
		} catch (error) {
			host.print(
				`Could not save ${info.key}: ${redactSecret(value, error instanceof Error ? error.message : String(error))}`,
				"error",
			);
		}
		return;
	}
}
