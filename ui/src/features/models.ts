// Model menu: pick from a curated list instead of typing an id.
//
// `/models` (no args) opens a picker grouped by provider. `/model <id>` still
// works for anything off-list. Selecting an entry sets both model and provider.

import { keyValues } from "../format.ts";
import type { ConfigSetResult } from "../protocol.ts";
import type { Command, FeatureHost } from "./types.ts";

interface ModelEntry {
	model: string;
	provider: "openrouter" | "openai" | "anthropic" | "google" | "mistral" | "groq" | "together" | "deepseek" | "xai" | "ollama";
	description: string;
}

export const CURATED_MODELS: ModelEntry[] = [
	{ model: "openrouter/free", provider: "openrouter", description: "Default free tier (multi-model)" },
	{ model: "gpt-4o-mini", provider: "openai", description: "OpenAI direct, cheap and fast" },
	{ model: "claude-3-5-sonnet-20241022", provider: "anthropic", description: "Anthropic direct (native API)" },
	{ model: "gemini-2.0-flash", provider: "google", description: "Google direct, long context" },
	{ model: "mistral-small-latest", provider: "mistral", description: "Mistral direct" },
	{ model: "llama-3.3-70b-versatile", provider: "groq", description: "Groq direct, fast inference" },
	{ model: "meta-llama/Llama-3.3-70B-Instruct-Turbo", provider: "together", description: "Together AI direct, open weights" },
	{ model: "deepseek-chat", provider: "deepseek", description: "DeepSeek direct (use deepseek-reasoner to think)" },
	{ model: "grok-4", provider: "xai", description: "xAI direct (Grok)" },
	{ model: "llama3.1", provider: "ollama", description: "Local via Ollama" },
	{ model: "mistral", provider: "ollama", description: "Local via Ollama" },
	{ model: "codellama", provider: "ollama", description: "Local code model" },
	{ model: "phi3", provider: "ollama", description: "Local, small and fast" },
];

async function applyModel(host: FeatureHost, entry: ModelEntry): Promise<void> {
	const providerResult = await host.request<ConfigSetResult>("config.set", { key: "provider", value: entry.provider });
	host.onConfig(providerResult);
	const modelResult = await host.request<ConfigSetResult>("config.set", { key: "model", value: entry.model });
	host.onConfig(modelResult);
	host.print(`Model: ${entry.model} (${entry.provider})`, "success");
}

export const modelsCommand: Command = {
	name: "models",
	description: "Pick a model from the curated menu",
	argumentHint: "[search]",
	async run(args, host) {
		const query = args.trim().toLowerCase();
		const entries = query
			? CURATED_MODELS.filter(
					(e) =>
						e.model.toLowerCase().includes(query) ||
						e.provider.includes(query) ||
						e.description.toLowerCase().includes(query),
				)
			: CURATED_MODELS;
		if (!entries.length) {
			host.print(`No curated model matches "${args.trim()}". Use /model <id> for anything off-list.`, "warning");
			return;
		}
		const choice = await host.pick(
			"Models (Enter to apply)",
			entries.map((e) => ({
				value: `${e.provider}::${e.model}`,
				label: `${e.model} (${e.provider})`,
				description: e.description,
			})),
		);
		if (!choice) return;
		const entry = CURATED_MODELS.find((e) => `${e.provider}::${e.model}` === choice.value)!;
		await applyModel(host, entry);
		host.print(
			keyValues([
				["Off-list", "Use /model <id> for any other model id"],
				["Keys", "Missing provider key? See /keys"],
			]),
			"plain",
		);
	},
};
