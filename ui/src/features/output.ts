// Retained tool-output inspection: UI-local, no re-execution, no backend call.

import type { Command } from "./types.ts";

export const outputCommand: Command = {
	name: "output",
	description: "Inspect retained tool output (menu when empty)",
	argumentHint: "[tool-id]",
	async run(args, host) {
		await host.inspectOutput(args.trim() || undefined);
	},
};
