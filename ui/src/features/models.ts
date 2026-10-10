// Model menu: pick from a curated list instead of typing an id.
//
// `/models` (no args) opens a picker grouped by provider for this session.
// `/default-model` opens the same menu to choose the saved default (or takes
// a custom id). Selecting an entry sets both model and provider.

import type { SelectItem } from "@earendil-works/pi-tui";
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

export async function applyModel(host: FeatureHost, entry: ModelEntry, persist = false): Promise<void> {
	// Only send `persist` when saving the default, so the session-scoped
	// `/models` request shape is unchanged.
	const extra = persist ? { persist: true } : {};
	const providerResult = await host.request<ConfigSetResult>("config.set", { key: "provider", value: entry.provider, ...extra });
	host.onConfig(providerResult);
	const modelResult = await host.request<ConfigSetResult>("config.set", { key: "model", value: entry.model, ...extra });
	host.onConfig(modelResult);
	host.print(`Model: ${entry.model} (${entry.provider})${persist ? " (saved as default)" : ""}`, "success");
}

function toSelectItem(e: ModelEntry): SelectItem {
	return {
		value: `${e.provider}::${e.model}`,
		label: `${e.model} (${e.provider})`,
		description: e.description,
	};
}

/** Open the curated-model picker; returns the chosen entry (or undefined). */
export async function pickCuratedModel(host: FeatureHost, entries: ModelEntry[]): Promise<ModelEntry | undefined> {
	const choice = await host.pick(
		"Models (Enter to apply)",
		entries.map(toSelectItem),
	);
	if (!choice) return undefined;
	return CURATED_MODELS.find((e) => `${e.provider}::${e.model}` === choice.value);
}

export function filterCuratedModels(query: string): ModelEntry[] {
	const q = query.trim().toLowerCase();
	if (!q) return CURATED_MODELS;
	return CURATED_MODELS.filter(
		(e) =>
			e.model.toLowerCase().includes(q) ||
			e.provider.includes(q) ||
			e.description.toLowerCase().includes(q),
	);
}

export const modelsCommand: Command = {
	name: "models",
	description: "Pick a model from the curated menu",
	argumentHint: "[search]",
	async run(args, host) {
		const entries = filterCuratedModels(args);
		if (!entries.length) {
			host.print(`No curated model matches "${args.trim()}". Use /default-model <id> for anything off-list.`, "warning");
			return;
		}
		const entry = await pickCuratedModel(host, entries);
		if (!entry) return;
		await applyModel(host, entry);
		host.print(
			keyValues([
				["Off-list", "Use /default-model <id> for any other model id"],
				["Keys", "Missing provider key? See /keys"],
			]),
			"plain",
		);
	},
};
