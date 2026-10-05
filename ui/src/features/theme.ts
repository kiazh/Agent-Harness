// Skin menu: recolor the whole UI plus header art, live, no restart.
//
// The choice persists via `config.theme` (config.yaml). Use /theme with no
// args for the picker, or /theme <name> to switch directly.

import { SKIN_NAMES, getSkin, setSkin, skinDescription } from "../theme.ts";
import { options, type Command, type FeatureHost } from "./types.ts";

export const themeCommand: Command = {
	name: "theme",
	description: "Switch UI skin (colors + header art)",
	argumentHint: "[skin]",
	getArgumentCompletions: options(SKIN_NAMES.map((name) => [name, skinDescription(name)])),
	async run(args, host) {
		const name = args.trim().toLowerCase();
		if (!name) {
			const choice = await host.pick(
				`Skins (current: ${getSkin()})`,
				SKIN_NAMES.map((skin) => ({
					value: skin,
					label: `${skin}${skin === getSkin() ? " (current)" : ""}`,
					description: skinDescription(skin),
				})),
			);
			if (!choice) return;
			await applySkin(host, choice.value);
			return;
		}
		if (!SKIN_NAMES.includes(name)) throw new Error(`Unknown skin "${name}". Available: ${SKIN_NAMES.join(", ")}`);
		await applySkin(host, name);
	},
};

async function applySkin(host: FeatureHost, name: string): Promise<void> {
	setSkin(name);
	host.banner();
	try {
		await host.request("config.set", { key: "theme", value: name, persist: true });
	} catch {
		// Theme still applies for this session; persistence is best-effort
		// (e.g. gateway started without a database).
	}
	host.print(`Skin: ${name} — ${skinDescription(name)}`, "success");
}
