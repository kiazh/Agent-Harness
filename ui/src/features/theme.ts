// Skin menu: live reversible preview plus header art, no restart.
//
// Picking previews immediately without config writes; Esc restores the
// original theme; Enter commits. Persistence happens exactly once through
// `config.theme`, and a failed save is reported as temporary — never claimed.

import { SKIN_NAMES, setSkin, skinDescription } from "../theme.ts";
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

async function applySkin(host: FeatureHost, name: string): Promise<void> {
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
