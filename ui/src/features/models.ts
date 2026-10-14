// Model menu: curated list, plus the REAL provider catalogs when available.
//
// `/models` (no args) asks the gateway for the selected provider's catalog
// (models.list). ChatGPT-account providers return the live Codex catalog
// with per-model reasoning efforts and context windows; user-added named
// providers return their pulled `/models` list. When a provider has no
// catalog (built-in direct families, older gateway, fetch failure) the
// curated list below remains the offline fallback.
//
// `/models catalog` prints the selected provider's catalog as a table
// instead of opening the picker.

import type { SelectItem } from "@earendil-works/pi-tui";
import { keyValues, table } from "../format.ts";
import type { ConfigSetResult, ModelsListResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import type { Command, FeatureHost } from "./types.ts";

interface ModelEntry {
	model: string;
	provider: "openrouter" | "openai" | "anthropic" | "chatgpt" | "google" | "mistral" | "groq" | "together" | "deepseek" | "xai" | "opencode" | "custom" | "ollama" | string;
	description: string;
}

// Offline fallback only: the real catalogs (chatgpt account + user-added
// providers) are fetched at runtime; this list keeps `/models` useful when
// no catalog is reachable. The chatgpt row is the catalog's priority-1 slug.
export const CURATED_MODELS: ModelEntry[] = [
	{ model: "openrouter/free", provider: "openrouter", description: "Default free tier (multi-model)" },
	{ model: "gpt-4o-mini", provider: "openai", description: "OpenAI direct, cheap and fast" },
	{ model: "claude-3-5-sonnet-20241022", provider: "anthropic", description: "Anthropic direct (native API)" },
	{ model: "gpt-6.1-sol", provider: "chatgpt", description: "ChatGPT account (Codex protocol) — sign in with /auth first" },
	{ model: "gemini-2.0-flash", provider: "google", description: "Google direct, long context" },
	{ model: "mistral-small-latest", provider: "mistral", description: "Mistral direct" },
	{ model: "llama-3.3-70b-versatile", provider: "groq", description: "Groq direct, fast inference" },
	{ model: "meta-llama/Llama-3.3-70B-Instruct-Turbo", provider: "together", description: "Together AI direct, open weights" },
	{ model: "deepseek-chat", provider: "deepseek", description: "DeepSeek direct (use deepseek-reasoner to think)" },
	{ model: "grok-4", provider: "xai", description: "xAI direct (Grok)" },
	{ model: "big-pickle", provider: "opencode", description: "OpenCode Zen gateway (set OPENCODE_API_KEY)" },
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
	// Sessions pin model/provider at creation: without this the switch would
	// only affect future sessions and the current chat would keep failing.
	const session = host.session();
	if (session) {
		await host.request("session.setModel", { sessionId: session.id, model: entry.model, provider: entry.provider });
	}
	host.print(`Model: ${entry.model} (${entry.provider})${persist ? " (saved as default)" : ""}`, "success");
}

/** Compact token count for table display ("500K", "1.05M"). */
export function formatContextWindow(tokens: number | null | undefined): string {
	if (!tokens || tokens <= 0) return "—";
	if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens % 1_000_000 ? 2 : 0)}M`;
	if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K`;
	return String(tokens);
}

/** Human description for a catalog row (efforts + context when known). */
export function catalogDescription(entry: ModelsListResult["models"][number]): string {
	const bits: string[] = [];
	if (entry.efforts?.length) bits.push(`effort: ${entry.efforts.join("/")}`);
	const window = formatContextWindow(entry.contextWindow);
	if (window !== "—") bits.push(`ctx ${window}`);
	if (entry.description) bits.push(entry.description);
	return bits.join(" · ") || "provider catalog";
}

/** Ask the gateway for the selected provider's models; undefined when the
 *  gateway cannot answer (older server or transport error) so callers can
 *  fall back to the curated list. */
export async function fetchCatalog(host: FeatureHost, refresh = false): Promise<ModelsListResult | undefined> {
	try {
		return await host.request<ModelsListResult>("models.list", refresh ? { refresh: true } : {});
	} catch {
		return undefined;
	}
}

export function catalogToEntries(result: ModelsListResult): ModelEntry[] {
	return result.models.map((entry) => ({
		model: entry.id,
		provider: result.provider,
		description: catalogDescription(entry),
	}));
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
	return entries.find((e) => `${e.provider}::${e.model}` === choice.value);
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

async function printCatalog(host: FeatureHost): Promise<void> {
	const result = await fetchCatalog(host, true);
	if (!result) {
		throw new Error("This gateway cannot list provider catalogs. Update Machina or use /models with a curated id.");
	}
	if (!result.models.length) {
		host.print(
			`No live catalog for provider "${result.provider}"${result.fetchError ? `: ${result.fetchError}` : ""}. Built-in direct providers do not expose a catalog here; use /models to pick a curated id.`,
			"warning",
		);
		return;
	}
	const rows = result.models.map((entry) => [
		entry.id,
		entry.efforts?.length ? entry.efforts.join("/") : "—",
		formatContextWindow(entry.contextWindow),
		entry.source ?? result.source,
	]);
	let body = table(["Model", "Efforts", "Context", "Source"], rows, 48);
	body += `\n\nProvider: ${result.provider} · source: ${result.source}`;
	if (result.fetchError) body += ` · live fetch failed: ${result.fetchError}`;
	host.print(body, "plain");
}

export const modelsCommand: Command = {
	name: "models",
	description: "Pick a model (catalog for chatgpt/added providers, curated menu otherwise)",
	argumentHint: "[search|catalog]",
	getArgumentCompletions: (prefix: string) => {
		const typed = prefix.trimStart().toLowerCase();
		if (/\s/.test(typed)) return null;
		const items = [
			{ value: "catalog", label: "catalog", description: "List the selected provider's models (efforts, context)" },
		].filter((item) => item.value.startsWith(typed));
		return items.length ? items : null;
	},
	async run(args, host) {
		const [sub] = splitSub(args);
		if (sub === "catalog" || sub === "list") {
			await printCatalog(host);
			return;
		}
		if (!sub) {
			const catalog = await fetchCatalog(host);
			if (catalog?.models.length) {
				const entries = catalogToEntries(catalog);
				const entry = await pickCuratedModel(host, entries);
				if (!entry) return;
				host.print(`Catalog: ${entries.length} model(s) from ${catalog.provider} (${catalog.source})`, "plain");
				await applyModel(host, entry);
				host.print(
					keyValues([
						["Efforts", "ChatGPT models accept low/medium/high/xhigh/max per model — see /models catalog"],
						["Off-list", "Use /default-model <id> for any other model id"],
						["Keys", "Missing provider key? See /keys"],
					]),
					"plain",
				);
				return;
			}
			const entries = CURATED_MODELS;
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
			return;
		}
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
