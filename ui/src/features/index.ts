// Re-exports all feature commands and maintains the same public API.

import type { SlashCommand } from "@earendil-works/pi-tui";
import { helpText, type ParsedCommand } from "../commands.ts";
import type { SessionResult } from "../protocol.ts";
import { codexCommands } from "./codex.ts";
import { sessionCommands } from "./sessions.ts";
import { memoryCommand } from "./memory.ts";
import { skillsCommand } from "./skills.ts";
import { agentsCommand, delegateCommand } from "./agents.ts";
import { jobsCommand } from "./jobs.ts";
import { keysCommand } from "./keys.ts";
import { modelsCommand } from "./models.ts";
import { settingsCommands } from "./settings.ts";
import { themeCommand } from "./theme.ts";
import { contextCommands } from "./context.ts";
import type { Command, FeatureHost, NoticeKind } from "./types.ts";

export type { Command, FeatureHost, NoticeKind };

const appCommands: Command[] = [
	{
		name: "clear",
		description: "Clear the terminal and start a new chat",
		async run(_args, host) {
			host.clear();
			const { session } = await host.request<SessionResult>("session.create", {});
			host.switchTo(session, []);
		},
	},
	{
		name: "exit",
		description: "Quit AgentHarness",
		async run(_args, host) {
			await host.exit();
		},
	},
];

const helpCommand: Command = {
	name: "help",
	description: "Show available commands",
	async run(_args, host) {
		host.print(
			`${helpText(COMMANDS)}\n\nKeys: Enter send · Shift+Enter newline · Esc stop reply · Tab complete · Ctrl+C exit`,
			"plain",
		);
	},
};

const COMMANDS: Command[] = [
	helpCommand,
	...sessionCommands,
	...codexCommands,
	...contextCommands,
	memoryCommand,
	skillsCommand,
	agentsCommand,
	delegateCommand,
	jobsCommand,
	keysCommand,
	modelsCommand,
	themeCommand,
	...settingsCommands,
	...appCommands,
];

/** Commands offered by editor autocomplete. */
export const SLASH_COMMANDS: SlashCommand[] = COMMANDS;

/** Run a parsed slash command. Errors are reported through the host. */
export async function runCommand({ name, args }: ParsedCommand, host: FeatureHost): Promise<void> {
	const command = COMMANDS.find((c) => c.name === name);
	if (!command) {
		host.print(`Unknown command /${name}. Type /help for the list.`, "warning");
		return;
	}
	try {
		await command.run(args, host);
	} catch (error) {
		host.print(error instanceof Error ? error.message : String(error), "error");
	}
}
