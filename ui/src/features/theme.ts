// Skin menu: live reversible preview plus header art, no restart.
//
// Picking previews immediately without config writes; Esc restores the
// original theme; Enter commits. Persistence happens exactly once through
// `config.theme`, and a failed save is reported as temporary — never claimed.

import { SKIN_NAMES, setOpaqueBackground, setSkin, skinDescription } from "../theme.ts";
import { options, type Command, type FeatureHost } from "./types.ts";

export const themeCommand: Command = {
	name: "theme",
	description: "Switch UI skin (menu when empty)",
	argumentHint: "[skin]",
	getArgumentCompletions: options(SKIN_NAMES.map((name) => [name, skinDescription(name)])),
	async run(args, host) {
		const name = args.trim().toLowerCase();
		if (!name) {
			const choice = await host.pickTheme();
			if (!choice) return;
			await applySkin(host, choice);
			return;
		}
		if (!SKIN_NAMES.includes(name)) throw new Error(`Unknown skin "${name}". Available: ${SKIN_NAMES.join(", ")}`);
		await applySkin(host, name);
	},
};

/**
 * Apply a skin and persist it exactly once. Exported for the `/settings`
 * menu's Appearance → Theme row (the same single persistence path `/theme`
 * uses); a failed save keeps the skin for the session and says so.
 */
export async function applySkin(host: FeatureHost, name: string): Promise<void> {
	setSkin(name);
	host.restyle();
	try {
		await host.request("config.set", { key: "theme", value: name, persist: true });
	} catch {
		// The theme stays applied for this session; persistence failed.
		host.print(`Skin ${name} applied for now (could not save).`, "warning");
		return;
	}
	host.print(`Skin: ${name} — ${skinDescription(name)}`, "success");
}

/**
 * Toggle the transparent-background setting and persist it exactly once. The
 * same single persistence path the theme row uses: the session keeps the new
 * behavior even if the save fails, and a failed save is reported honestly.
 */
export async function applyBackgroundTransparency(host: FeatureHost, transparent: boolean): Promise<void> {
	setOpaqueBackground(!transparent);
	host.restyle();
	try {
		await host.request("config.set", { key: "transparent_background", value: transparent, persist: true });
	} catch {
		// The setting stays applied for this session; persistence failed.
		host.print(
			transparent
				? "Transparent background applied for now (could not save)."
				: "Opaque background applied for now (could not save).",
			"warning",
		);
		return;
	}
	host.print(
		transparent ? "Transparent background on — using the terminal default." : "Transparent background off — theme canvas restored.",
		"success",
	);
}
