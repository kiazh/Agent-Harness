// API-key menu: list, set and clear provider keys without leaving the UI.
//
// Values are never echoed back. `secrets.set` applies immediately and saves
// to the git-ignored `.env` unless `--no-save` is given. Run `ah setup` once
// for first-launch configuration of the same file.

import { keyValues, table } from "../format.ts";
import type { SecretsListResult, SecretsSetResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { options, type Command, type FeatureHost } from "./types.ts";

// Mirrors PROVIDER_KEYS + MANAGED_ENV_KEYS in ah/security/env_file.py and
// ah/gateway/features/secrets.py (used for completions when offline).
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
	["COHERE_API_KEY", "Cohere (reranking)"],
	["DATABASE_URL", "PostgreSQL connection string"],
	["SEARXNG_URL", "Self-hosted SearXNG for web search"],
	["AGENT_HARNESS_MODEL", "Default model identifier"],
];

function statusTable(secrets: SecretsListResult["secrets"]): string {
	return table(
		["Key", "Status", "Description"],
		secrets.map((s) => [s.key, s.set ? "set" : "not set", s.description]),
		40,
	);
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
			const { secrets, envFile } = await host.request<SecretsListResult>("secrets.list");
			host.print(`${statusTable(secrets)}\n\nFile: ${envFile}\nValues are never shown. Use /keys set KEY VALUE to update one.`, "plain");
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
			const result = await host.request<SecretsSetResult>("secrets.set", { key, value, persist });
			host.print(
				`${result.key} set${result.persisted ? ` (saved to ${result.envFile})` : " (this session only)"}. Takes effect on the next turn.`,
				"success",
			);
			return;
		}
		if (sub === "clear") {
			const key = rest.trim();
			if (!key) throw new Error("Usage: /keys clear KEY");
			const picked = await resolveKey(host, key);
			await host.request("secrets.clear", { key: picked });
			host.print(`${picked.toUpperCase()} cleared (session and .env).`, "success");
			return;
		}
		// Unknown word: open the interactive menu instead of failing.
		await openMenu(host);
	},
};

async function resolveKey(host: FeatureHost, prefix: string): Promise<string> {
	const { secrets } = await host.request<SecretsListResult>("secrets.list");
	const keys = secrets.map((s) => s.key);
	const exact = keys.find((k) => k === prefix.trim().toUpperCase());
	if (exact) return exact;
	const matches = keys.filter((k) => k.startsWith(prefix.trim().toUpperCase()));
	if (matches.length === 1) return matches[0]!;
	if (!matches.length) throw new Error(`No key starts with "${prefix}". Use /keys list.`);
	throw new Error(`"${prefix}" matches ${matches.length} keys; be more specific.`);
}

async function openMenu(host: FeatureHost): Promise<void> {
	const { secrets, envFile } = await host.request<SecretsListResult>("secrets.list");
	const choice = await host.pick(
		"API keys (values never shown)",
		secrets.map((s) => ({
			value: s.key,
			label: `${s.key} — ${s.set ? "set" : "not set"}`,
			description: s.description,
		})),
	);
	if (!choice) return;
	const info = secrets.find((s) => s.key === choice.value)!;
	host.print(
		keyValues([
			["Key", info.key],
			["Status", info.set ? "set" : "not set"],
			["About", info.description],
			["File", envFile],
		]) + `\n\nSet: /keys set ${info.key} VALUE [--no-save]\nClear: /keys clear ${info.key}`,
		"plain",
	);
}
