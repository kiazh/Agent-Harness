// Settings commands: config, settings, profile, status, model, provider.

import type { SelectItem } from "@earendil-works/pi-tui";
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
import { getSkin } from "../theme.ts";
import type { ChatSide, WorkspaceMode } from "../workspace/state.ts";
import { applyModel, CURATED_MODELS, filterCuratedModels, pickCuratedModel } from "./models.ts";
import { redactSecret } from "./keys.ts";
import { applySkin } from "./theme.ts";
import { printLspRows } from "./workspace.ts";
import {
	confirm,
	formatValue,
	options,
	PROFILE_ID_RE,
	type Command,
	type FeatureHost,
	type UiPreferenceKey,
	type UiSettingsHost,
	type WorkspaceHost,
} from "./types.ts";

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
	const key = await host.askText({
		title: `API key for ${name} (optional)`,
		description: "Leave empty for keyless local endpoints. Stored in .env like /keys — masked, never shown again.",
		placeholder: "leave empty for no key",
		secret: true,
		allowEmpty: true,
	});
	// Escape cancels the whole flow; blank submits "" as the keyless path.
	if (key === undefined) return;
	const trimmed = key.trim();
	const confirmed = await confirm(host, `Add provider "${name}" at ${baseUrl}?`, "Add");
	if (!confirmed) return;
	let result: ProviderAddResult;
	try {
		result = await host.request<ProviderAddResult>("providers.add", { name, baseUrl, apiKey: trimmed || undefined });
	} catch (error) {
		// The submitted key must never echo in an error string (redactSecret
		// is empty-safe: a keyless endpoint submits "" and the message stays intact).
		const message = redactSecret(trimmed, error instanceof Error ? error.message : String(error));
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
	if (cleanup.apiKeyEnv) {
		bits.push(cleanup.envLineRemoved ? `${cleanup.apiKeyEnv} line in .env` : `${cleanup.apiKeyEnv} (no .env line found)`);
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

/** The historical text dump (bare `/config` before the menu; `/config list`). */
function printConfigDump(host: FeatureHost, config: Record<string, unknown>, secrets: string[]): void {
	const keys = Object.keys(config).sort();
	host.print(
		keyValues(
			keys.map((key) => [
				key,
				secrets.includes(key) ? (config[key] ? "set (in .env)" : "not set") : formatValue(config[key]),
			]),
		),
		"plain",
	);
}

// ─── /settings menu (closing pass) ──────────────────────────────────────────
//
// Bare `/config` and `/settings` open the same menu. Categories are fixed (the
// closing-pass spec): Workspace & Layout, Editor, Appearance, Language servers,
// Chat & Agent. Every item's description shows the CURRENT value; each
// category loops back to itself until "← Back"; Cancelling any picker exits the
// whole menu. Without a live workspace the Workspace & Layout and Language
// servers categories are hidden behind one notice, and the Editor category
// needs a live editor view (the uiSettings snapshot says so).

/** Menu flow: keep looping categories, or leave the menu entirely. */
type SettingsFlow = "back" | "exit";

function onOff(value: boolean): string {
	return value ? "On" : "Off";
}

async function pickBack(
	host: FeatureHost,
	title: string,
	items: SelectItem[],
): Promise<SelectItem | "back" | "exit"> {
	const choice = await host.pick(title, [...items, { value: "back", label: "← Back" }]);
	if (!choice) return "exit";
	if (choice.value === "back") return "back";
	return choice;
}

async function setUiPreference(
	host: FeatureHost,
	ui: UiSettingsHost,
	key: UiPreferenceKey,
	value: boolean | number,
): Promise<void> {
	try {
		await ui.setPreference(key, value);
	} catch {
		// The app keeps the value applied for this session; persistence failed.
		host.print(`Preference applied for this session; could not save (${key}).`, "warning");
	}
}

async function settingsWorkspaceCategory(host: FeatureHost, surface: WorkspaceHost): Promise<SettingsFlow> {
	for (;;) {
		const input = surface.controller.getLayoutInput();
		const choice = await pickBack(host, "Workspace & Layout", [
			{ value: "mode", label: "Mode", description: `current: ${input.requestedMode}` },
			{ value: "side", label: "Chat side", description: `current: ${input.chatSide ?? "left"}` },
			{ value: "ratio", label: "Split ratio", description: `current: ${input.splitRatio.toFixed(2)}` },
			{
				value: "explorer",
				label: "File explorer",
				description: `current: ${input.explorerRequested ? "open" : "closed"}`,
			},
			{ value: "open", label: "Open folder…", description: "Open the workspace at another folder" },
			{ value: "close", label: "Close workspace", description: "Local buffers stay open; unsaved edits are kept" },
		]);
		if (choice === "exit") return "exit";
		if (choice === "back") return "back";
		if (choice.value === "mode") {
			const mode = await host.pick("Mode", [
				{ value: "split", label: `Split${input.requestedMode === "split" ? " (current)" : ""}` },
				{ value: "chat", label: `Chat${input.requestedMode === "chat" ? " (current)" : ""}` },
				{ value: "editor", label: `Editor${input.requestedMode === "editor" ? " (current)" : ""}` },
				{ value: "back", label: "← Back" },
			]);
			if (!mode) return "exit";
			if (mode.value !== "back") surface.setMode(mode.value as WorkspaceMode);
			continue;
		}
		if (choice.value === "side") {
			const current = input.chatSide ?? "left";
			const side = await host.pick("Chat side", [
				{ value: "left", label: `Left${current === "left" ? " (current)" : ""}` },
				{ value: "right", label: `Right${current === "right" ? " (current)" : ""}` },
				{ value: "back", label: "← Back" },
			]);
			if (!side) return "exit";
			if (side.value !== "back") surface.setChatSide(side.value as ChatSide);
			continue;
		}
		if (choice.value === "ratio") {
			const ratio = await host.pick("Split ratio", [
				{ value: "reset", label: "50/50 reset", description: "The default split" },
				{ value: "60/40", label: "60/40", description: "Chat gets the larger pane" },
				{ value: "40/60", label: "40/60", description: "Editor gets the larger pane" },
				{ value: "custom", label: "Custom…", description: "Type a ratio between 0.25 and 0.75" },
				{ value: "back", label: "← Back" },
			]);
			if (!ratio) return "exit";
			if (ratio.value === "back") continue;
			if (ratio.value === "custom") {
				const typed = await host.askText({
					title: "Split ratio",
					description: "Chat pane share between 0.25 and 0.75 (e.g. 0.55).",
					initial: input.splitRatio.toFixed(2),
					placeholder: "0.50",
				});
				if (typed === undefined) return "exit";
				const parsed = Number(typed.trim());
				if (!Number.isFinite(parsed) || parsed < 0.25 || parsed > 0.75) {
					host.print("Ratio must be a number between 0.25 and 0.75.", "warning");
					continue;
				}
				surface.controller.setRatio(parsed);
				surface.layoutChanged();
				continue;
			}
			const value = ratio.value === "reset" ? 0.5 : ratio.value === "60/40" ? 0.6 : 0.4;
			surface.controller.setRatio(value);
			surface.layoutChanged();
			continue;
		}
		if (choice.value === "explorer") {
			const explorer = await host.pick("File explorer", [
				{ value: "open", label: `Open${input.explorerRequested ? " (current)" : ""}` },
				{ value: "closed", label: `Closed${input.explorerRequested ? "" : " (current)"}` },
				{ value: "back", label: "← Back" },
			]);
			if (!explorer) return "exit";
			if (explorer.value !== "back") surface.setExplorer(explorer.value === "open");
			continue;
		}
		if (choice.value === "open") {
			const ui = host.uiSettings?.();
			if (!ui) return "exit";
			const path = await host.askText({
				title: "Open folder",
				description: "Absolute path of the workspace root to open.",
				placeholder: "/path/to/project",
			});
			if (path === undefined) return "exit";
			const trimmed = path.trim();
			if (!trimmed) continue;
			const result = await ui.openWorkspaceFolder(trimmed);
			host.print(result.message, result.ok ? "info" : "error");
			continue;
		}
		// close
		const ui = host.uiSettings?.();
		if (!ui) return "exit";
		const dirty = surface.dirtyBuffers();
		if (dirty.count > 0) {
			const listed = dirty.names.slice(0, 2).join(", ");
			const more = dirty.count > 2 ? ` +${dirty.count - 2}` : "";
			const confirmed = await confirm(
				host,
				`Close the workspace with ${dirty.count} unsaved file(s) (${listed}${more})?`,
				"Close workspace",
			);
			if (!confirmed) continue;
		}
		await ui.closeWorkspace();
		host.print("workspace closed; local buffers stay open", "info");
		continue;
	}
}

async function settingsEditorCategory(host: FeatureHost, ui: UiSettingsHost): Promise<SettingsFlow> {
	for (;;) {
		const prefs = ui.snapshot().preferences;
		const choice = await pickBack(host, "Editor", [
			{ value: "syntax", label: "Syntax highlighting", description: `current: ${onOff(prefs.syntax)}` },
			{ value: "wrap", label: "Soft wrap", description: `current: ${onOff(prefs.softWrap)}` },
			{ value: "tab", label: "Tab width", description: `current: ${prefs.tabWidth}` },
		]);
		if (choice === "exit") return "exit";
		if (choice === "back") return "back";
		if (choice.value === "syntax" || choice.value === "wrap") {
			const current = choice.value === "syntax" ? prefs.syntax : prefs.softWrap;
			const next = await host.pick(
				choice.value === "syntax" ? "Syntax highlighting" : "Soft wrap",
				[
					{ value: "on", label: `On${current ? " (current)" : ""}` },
					{ value: "off", label: `Off${current ? "" : " (current)"}` },
					{ value: "back", label: "← Back" },
				],
			);
			if (!next) return "exit";
			if (next.value !== "back") {
				await setUiPreference(host, ui, choice.value === "syntax" ? "syntax" : "softWrap", next.value === "on");
			}
			continue;
		}
		const width = await host.pick("Tab width", [
			{ value: "2", label: `2${prefs.tabWidth === 2 ? " (current)" : ""}` },
			{ value: "4", label: `4${prefs.tabWidth === 4 ? " (current)" : ""}` },
			{ value: "8", label: `8${prefs.tabWidth === 8 ? " (current)" : ""}` },
			{ value: "back", label: "← Back" },
		]);
		if (!width) return "exit";
		if (width.value !== "back") await setUiPreference(host, ui, "tabWidth", Number(width.value));
	}
}

async function settingsAppearanceCategory(host: FeatureHost): Promise<SettingsFlow> {
	for (;;) {
		const reduced = /^(1|true)$/i.test(process.env.MACHINA_UI_REDUCED_MOTION ?? "");
		const choice = await pickBack(host, "Appearance", [
			{ value: "theme", label: "Theme", description: `current: ${getSkin()} (live preview)` },
			{
				value: "motion",
				label: "Reduced motion (env)",
				description: `current: ${onOff(reduced)} · MACHINA_UI_REDUCED_MOTION`,
			},
		]);
		if (choice === "exit") return "exit";
		if (choice === "back") return "back";
		if (choice.value === "theme") {
			const name = await host.pickTheme();
			if (!name) continue;
			await applySkin(host, name);
			continue;
		}
		// Read-only row: the setting is an environment variable, not config.
		host.print(
			"Reduced motion is an environment setting: restart with MACHINA_UI_REDUCED_MOTION=1 to keep motion static.",
			"info",
		);
	}
}

async function settingsLspCategory(host: FeatureHost, surface: WorkspaceHost, ui: UiSettingsHost): Promise<SettingsFlow> {
	const controller = surface.controller;
	if (controller.phase !== "ready") {
		host.print("Open a workspace first (/workspace).", "warning");
		return "back";
	}
	for (;;) {
		await ui.refreshLsp();
		const rows = ui.lspRows();
		if (rows.length === 0) {
			host.print("lsp: no language servers reported", "plain");
			return "back";
		}
		const choice = await pickBack(
			host,
			"Language servers",
			rows.map((row) => ({
				value: row.profile,
				label: row.profile,
				description: row.detail ? `${row.state} — ${row.detail}` : `current: ${row.state}`,
			})),
		);
		if (choice === "exit") return "exit";
		if (choice === "back") return "back";
		const action = await host.pick(`Language server: ${choice.value}`, [
			{ value: "start", label: "Start" },
			{ value: "stop", label: "Stop" },
			{ value: "restart", label: "Restart" },
			{ value: "status", label: "Show status", description: "Print per-server rows" },
			{ value: "back", label: "← Back" },
		]);
		if (!action) return "exit";
		if (action.value === "back") continue;
		if (action.value === "status") {
			await controller.lspStatus();
			printLspRows(host, controller.lspStatusSnapshot());
			continue;
		}
		const outcome = await controller.lspControl(choice.value, action.value as "start" | "stop" | "restart");
		if (outcome.status === "ok") {
			const detail = outcome.detail ? ` — ${outcome.detail}` : "";
			host.print(`lsp ${action.value}: ${outcome.profile} ${outcome.state}${detail}`, "info");
		}
		// refused/failed outcomes are notified by the controller.
	}
}

async function settingsChatCategory(host: FeatureHost): Promise<SettingsFlow> {
	for (;;) {
		const model = host.session()?.model ?? "default";
		const choice = await pickBack(host, "Chat & Agent", [
			{ value: "model", label: "Model", description: `current: ${model}` },
			{ value: "persona", label: "Persona", description: "Default persona across sessions" },
			{ value: "providers", label: "Providers", description: "Built-in and user-added providers" },
			{ value: "profiles", label: "Profiles", description: `current: ${host.profileId()}` },
		]);
		if (choice === "exit") return "exit";
		if (choice === "back") return "back";
		if (choice.value === "model") {
			const entry = await pickCuratedModel(host, CURATED_MODELS);
			if (entry) await applyModel(host, entry, true);
			continue;
		}
		if (choice.value === "persona") {
			await openPersonaMenu(host);
			continue;
		}
		if (choice.value === "providers") {
			await printProviders(host);
			continue;
		}
		await openProfileMenu(host);
	}
}

/**
 * The `/settings` menu (also bare `/config`). One category picker loops until
 * cancelled or closed from within; each category loops until "← Back".
 */
export async function openSettingsMenu(host: FeatureHost): Promise<void> {
	const ui = host.uiSettings?.();
	if (!ui) {
		// Hosts without the surface keep the historical text dump.
		const { config, secrets } = await host.request<ConfigGetResult>("config.get");
		printConfigDump(host, config, secrets);
		return;
	}
	const surface = host.workspace?.();
	const workspaceAvailable = surface !== undefined && surface.controllerAvailable();
	if (!workspaceAvailable) host.print("Workspace is not available.", "warning");
	for (;;) {
		const snapshot = ui.snapshot();
		const prefs = snapshot.preferences;
		const items: SelectItem[] = [];
		if (workspaceAvailable && surface) {
			const input = surface.controller.getLayoutInput();
			items.push({
				value: "cat-workspace",
				label: "Workspace & Layout",
				description: `${input.requestedMode} · chat ${input.chatSide ?? "left"} · explorer ${input.explorerRequested ? "open" : "closed"}`,
			});
		}
		if (snapshot.editorAvailable) {
			items.push({
				value: "cat-editor",
				label: "Editor",
				description: `syntax ${onOff(prefs.syntax)} · wrap ${onOff(prefs.softWrap)} · tab ${prefs.tabWidth}`,
			});
		}
		items.push({ value: "cat-appearance", label: "Appearance", description: `theme ${getSkin()}` });
		if (workspaceAvailable && surface) {
			const rows = ui.lspRows();
			items.push({
				value: "cat-lsp",
				label: "Language servers",
				description:
					rows.length === 0
						? "none reported"
						: rows.map((row) => `${row.profile} ${row.state}`).join(" · "),
			});
		}
		items.push({
			value: "cat-chat",
			label: "Chat & Agent",
			description: "model · persona · providers · profiles",
		});
		const choice = await host.pick("Settings", items);
		if (!choice) return;
		let flow: SettingsFlow = "back";
		switch (choice.value) {
			case "cat-workspace":
				if (surface) flow = await settingsWorkspaceCategory(host, surface);
				break;
			case "cat-editor":
				flow = await settingsEditorCategory(host, ui);
				break;
			case "cat-appearance":
				flow = await settingsAppearanceCategory(host);
				break;
			case "cat-lsp":
				if (surface) flow = await settingsLspCategory(host, surface, ui);
				break;
			case "cat-chat":
				flow = await settingsChatCategory(host);
				break;
		}
		if (flow === "exit") return;
	}
}

export const settingsCommands: Command[] = [
	{
		name: "settings",
		description: "Open the settings menu (same as bare /config)",
		async run(_args, host) {
			await openSettingsMenu(host);
		},
	},
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
		argumentHint: "[list|add [name url]|remove [name]|openrouter|openai|anthropic|chatgpt|copilot|google|mistral|groq|together|deepseek|xai|opencode|kimi|custom|ollama|<name>]",
		getArgumentCompletions: options([
			["list", "List built-in and user-added providers"],
			["add", "Add a provider (name + base URL + optional key)"],
			["remove", "Remove a user-added provider"],
			["openrouter", "OpenRouter (multi-model)"],
			["openai", "OpenAI direct"],
			["anthropic", "Anthropic direct"],
			["chatgpt", "OpenAI account (sign in with /auth)"],
			["copilot", "GitHub Copilot seat (machina auth copilot)"],
			["google", "Google direct (Gemini)"],
			["mistral", "Mistral direct"],
			["groq", "Groq direct"],
			["together", "Together AI direct"],
			["deepseek", "DeepSeek direct"],
			["xai", "xAI direct (Grok)"],
			["opencode", "OpenCode Zen gateway (OPENCODE_API_KEY)"],
			["kimi", "Kimi Code (KIMI_API_KEY; membership quota)"],
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
					"OpenAI account models need no API key — sign in with /auth first. " +
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
		description: "Show or set reasoning effort (provider-specific tiers)",
		argumentHint: "[low|medium|high|xhigh|max|off]",
		getArgumentCompletions: options([
			["low", "Shallow reasoning, fast and cheap"],
			["medium", "Balanced reasoning depth (not Kimi)"],
			["high", "Deep reasoning for hard problems"],
			["xhigh", "Extra-high (OpenAI catalog tier)"],
			["max", "Maximum depth (Kimi or supported OpenAI account models)"],
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
		description: "Settings menu, or get/set one key (--save to keep it)",
		argumentHint: "[list|<key> [value] [--save]]",
		async run(args, host) {
			const persist = /(^|\s)--save(\s|$)/.test(args);
			const [key, value] = splitSub(args.replace(/(^|\s)--save(?=\s|$)/, " "));
			if (!key) {
				// Closing pass: bare /config opens the settings menu; the old
				// text dump stays reachable as `/config list`.
				await openSettingsMenu(host);
				return;
			}
			const { config, secrets } = await host.request<ConfigGetResult>("config.get");
			if (key === "list") {
				printConfigDump(host, config, secrets);
				return;
			}
			if (!(key in config)) throw new Error(`Unknown setting "${key}". Use /config list to list them.`);
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
