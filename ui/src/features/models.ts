// Model menu: provider-grouped, catalog-first, curated fallback.
//
// `/models` (no args) opens one searchable picker whose rows are grouped by
// provider. The session's selected provider comes first; every other provider
// (built-ins and user-added named entries) follows in its own section.
// Catalogs come from the gateway (models.list) — the live ChatGPT/Codex
// catalog with per-model reasoning efforts and context windows, and
// user-added providers' pulled `/models` lists. Providers without a discovery
// endpoint keep their curated offline rows, which stay fallback-only.
//
// The menu filter matches label, value and description, so typing a provider
// ("deepseek") or a model family ("gpt") narrows to those rows; section
// headers yield while a filter is active.
//
// Cross-provider picks are never silent: if the same model id exists under
// the current provider that row wins, otherwise a one-line confirm asks
// before the provider is switched. `/models <query>` searches all groups;
// `/models catalog` prints the selected provider's catalog as a table.

import type { SelectItem } from "@earendil-works/pi-tui";
import { keyValues, table } from "../format.ts";
import type { ConfigSetResult, ModelsListResult, ProvidersListResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, type Command, type FeatureHost } from "./types.ts";

export interface ModelEntry {
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
	{ model: "kimi-for-coding", provider: "kimi", description: "Kimi Code membership tier (set KIMI_API_KEY)" },
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

/** Ask the gateway for the request session's models; undefined when the
 *  gateway cannot answer (older server or transport error) so callers can
 *  fall back to the curated list. Session identity lets a resumed session
 *  list ITS pinned provider (round-3 review finding 10). */
export async function fetchCatalog(host: FeatureHost, refresh = false): Promise<ModelsListResult | undefined> {
	const session = host.session();
	const params: Record<string, unknown> = {};
	if (refresh) params.refresh = true;
	if (session) params.sessionId = session.id;
	try {
		return await host.request<ModelsListResult>("models.list", params);
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

// ─── provider-grouped listing ────────────────────────────────────────────────

/** One provider's rows in the grouped model picker. */
export interface ModelGroup {
	provider: string;
	/** "builtin" | "named" (from providers.list; named = user-added). */
	kind: string;
	/** True for the session's selected provider (rendered first). */
	current: boolean;
	entries: ModelEntry[];
	/** True when entries came from the provider catalog, false for curated fallback rows. */
	fromCatalog: boolean;
	/** Catalog source ("live" | "cache" | "fallback" | "none") when known. */
	source: string | null;
	/** Why live discovery did not answer, when the gateway said so. */
	fetchError: string | null;
}

export interface GroupedModels {
	current: string;
	groups: ModelGroup[];
}

/** A picker row: a model entry, or a non-selectable provider section header. */
export type ModelPickerRow = SelectItem & { section?: true };

function toSelectItem(e: ModelEntry): SelectItem {
	return {
		value: `${e.provider}::${e.model}`,
		label: `${e.model} (${e.provider})`,
		description: e.description,
	};
}

/**
 * Pull every provider's catalog and assemble the grouped listing.
 *
 * One `providers.list` decides the provider order (the session's selected
 * provider first, then the gateway's order — user-added named entries
 * included). Every provider is then asked for its catalog in parallel:
 * built-ins without a discovery endpoint answer instantly with an empty
 * list, user-added providers answer from their TTL cache (pulling only when
 * stale), and chatgpt answers from its TTL-cached Codex catalog. Providers
 * the gateway cannot answer for keep their curated fallback rows.
 *
 * Older gateways without `providers.list` keep the legacy single-provider
 * catalog call; the groups then come from the curated list order.
 */
export async function fetchModelGroups(host: FeatureHost): Promise<GroupedModels> {
	const sessionProvider = (host.session()?.provider ?? "").trim().toLowerCase();
	let listed: ProvidersListResult | undefined;
	try {
		listed = await host.request<ProvidersListResult>("providers.list");
	} catch {
		listed = undefined;
	}
	const current = sessionProvider || (listed?.selected ?? "").trim().toLowerCase() || "openrouter";
	const sessionId = host.session()?.id;

	const kinds = new Map<string, string>();
	const names: string[] = [];
	const push = (name: string) => {
		if (name && !names.includes(name)) names.push(name);
	};
	push(current);
	if (listed) {
		for (const provider of listed.providers) {
			kinds.set(provider.name, provider.kind);
			push(provider.name);
		}
	} else {
		for (const entry of CURATED_MODELS) push(entry.provider);
	}

	const results = new Map<string, ModelsListResult>();
	if (listed) {
		const settled = await Promise.allSettled(
			names.map(async (name) => {
				const params: Record<string, unknown> = { provider: name };
				if (sessionId) params.sessionId = sessionId;
				const result = await host.request<ModelsListResult>("models.list", params);
				return { name, result };
			}),
		);
		for (const outcome of settled) {
			if (outcome.status === "fulfilled") {
				results.set(outcome.value.name, outcome.value.result);
			}
		}
	} else {
		const catalog = await fetchCatalog(host);
		if (catalog) results.set(catalog.provider || current, catalog);
	}

	const groups = names.map((name): ModelGroup => {
		const curated = CURATED_MODELS.filter((entry) => entry.provider === name);
		const catalog = results.get(name);
		const fromCatalog = catalog !== undefined && catalog.models.length > 0;
		return {
			provider: name,
			kind: kinds.get(name) ?? (curated.length ? "builtin" : "named"),
			current: name === current,
			entries: fromCatalog ? catalogToEntries(catalog) : curated,
			fromCatalog,
			source: catalog?.source ?? null,
			fetchError: catalog?.fetchError ?? null,
		};
	});
	return { current, groups };
}

/** Picker rows: each non-empty group gets a dim section header, then its
 *  rows. Header values can never collide with a model value (provider names
 *  cannot start with "_"). */
export function buildModelPickerRows(groups: ModelGroup[]): ModelPickerRow[] {
	const rows: ModelPickerRow[] = [];
	for (const group of groups) {
		if (!group.entries.length) continue;
		rows.push({
			value: `__section__::${group.provider}`,
			label: `── ${group.provider}${group.current ? " (current)" : ""} · ${group.entries.length} model(s) ──`,
			section: true,
		});
		for (const entry of group.entries) rows.push(toSelectItem(entry));
	}
	return rows;
}

/** Narrow groups to entries matching *query* (provider name, model id or
 *  description), dropping empty groups. */
export function filterModelGroups(groups: ModelGroup[], query: string): ModelGroup[] {
	const q = query.trim().toLowerCase();
	if (!q) return groups;
	return groups
		.map((group) => ({
			...group,
			entries: group.entries.filter(
				(entry) =>
					entry.model.toLowerCase().includes(q) ||
					entry.provider.toLowerCase().includes(q) ||
					entry.description.toLowerCase().includes(q),
			),
		}))
		.filter((group) => group.entries.length > 0);
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

/**
 * Apply a picked row. Crossing providers is never silent: the same model id
 * under the current provider always wins, and any real switch asks a
 * one-line confirm first. Returns the applied entry (undefined on cancel).
 */
export async function applyGroupedPick(host: FeatureHost, grouped: GroupedModels, value: string): Promise<ModelEntry | undefined> {
	const separator = value.indexOf("::");
	if (separator <= 0) return undefined;
	const provider = value.slice(0, separator);
	const model = value.slice(separator + 2);
	const entries = grouped.groups.flatMap((group) => group.entries);
	const picked = entries.find((entry) => entry.provider === provider && entry.model === model);
	if (!picked) return undefined;
	let target = picked;
	if (picked.provider !== grouped.current) {
		const shadow = entries.find((entry) => entry.provider === grouped.current && entry.model === picked.model);
		if (shadow) {
			host.print(`"${picked.model}" also exists under ${grouped.current} — keeping the current provider.`, "info");
			target = shadow;
		} else {
			const ok = await confirm(
				host,
				`Switch provider ${grouped.current} → ${picked.provider} for "${picked.model}"?`,
				"Switch",
			);
			if (!ok) {
				host.print("Provider unchanged.", "plain");
				return undefined;
			}
			host.print(`Switching provider: ${grouped.current} → ${picked.provider}`, "info");
		}
	}
	await applyModel(host, target);
	const group = grouped.groups.find((candidate) => candidate.provider === target.provider);
	if (group?.fromCatalog) {
		host.print(`Catalog: ${group.entries.length} model(s) from ${group.provider} (${group.source})`, "plain");
	}
	if (group?.fetchError) {
		host.print(`Live catalog for ${group.provider} failed: ${group.fetchError}`, "warning");
	}
	return target;
}

/** Open the grouped picker; applies the selection through applyGroupedPick. */
export async function openGroupedModelPicker(host: FeatureHost, grouped: GroupedModels, warnMissingCurrent = true): Promise<void> {
	const currentGroup = grouped.groups.find((group) => group.provider === grouped.current);
	if (warnMissingCurrent && (!currentGroup || !currentGroup.entries.length)) {
		const reason = currentGroup?.fetchError ? `: ${currentGroup.fetchError}` : "";
		host.print(
			`No live catalog for provider "${grouped.current}"${reason}. Other providers are listed below; use /default-model <id> for anything off-list.`,
			"warning",
		);
	}
	const rows = buildModelPickerRows(grouped.groups);
	if (!rows.length) {
		host.print("No models available from any provider right now. Use /default-model <id> to set one directly.", "warning");
		return;
	}
	const choice = await host.pick("Models (Enter to apply)", rows);
	if (!choice) return;
	const applied = await applyGroupedPick(host, grouped, choice.value);
	if (!applied) return;
	const hints: Array<[string, unknown]> = [];
	if (applied.provider === "chatgpt") {
		hints.push(["Efforts", "ChatGPT models accept low/medium/high/xhigh/max per model — see /models catalog"]);
	}
	hints.push(["Off-list", "Use /default-model <id> for any other model id"]);
	hints.push(["Keys", "Missing provider key? See /keys"]);
	host.print(keyValues(hints), "plain");
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
	description: "Pick a model by provider group (catalog first, curated fallback)",
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
		const grouped = await fetchModelGroups(host);
		if (!sub) {
			await openGroupedModelPicker(host, grouped);
			return;
		}
		const matches = filterModelGroups(grouped.groups, args);
		if (!matches.length) {
			host.print(`No model matches "${args.trim()}" across providers. Use /default-model <id> for anything off-list.`, "warning");
			return;
		}
		await openGroupedModelPicker(host, { current: grouped.current, groups: matches }, false);
	},
};
