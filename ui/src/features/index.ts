// Re-exports all feature commands and maintains the same public API.

import type { SlashCommand } from "@earendil-works/pi-tui";
import { helpText, type ParsedCommand } from "../commands.ts";
import type { SessionResult } from "../protocol.ts";
import { codexCommands } from "./codex.ts";
import { sessionCommands } from "./sessions.ts";
import { memoryCommand } from "./memory.ts";
import { growthCommand } from "./growth.ts";
import { skillsCommand } from "./skills.ts";
import { agentsCommand, delegateCommand } from "./agents.ts";
import { authCommand } from "./auth.ts";
import { jobsCommand } from "./jobs.ts";
import { keysCommand } from "./keys.ts";
import { approvalsCommand, modeCommand } from "./mode.ts";
import { modelsCommand } from "./models.ts";
import { mcpCommand } from "./mcp.ts";
import { interopCommand } from "./interop.ts";
import { harnessesCommand } from "./harnesses.ts";
import { outputCommand } from "./output.ts";
import { settingsCommands } from "./settings.ts";
import { themeCommand } from "./theme.ts";
import { contextCommands } from "./context.ts";
import { workspaceCommands } from "./workspace.ts";
import type { AskTextOptions, AskTextResult, Command, FamilySession, FeatureHost, NoticeKind } from "./types.ts";

export type { AskTextOptions, AskTextResult, Command, FamilySession, FeatureHost, NoticeKind };
export { formatLspStatusLine, workspaceCommands } from "./workspace.ts";
export type { WorkspaceHost } from "./types.ts";

const appCommands: Command[] = [
	{
		name: "clear",
		description: "Clear the terminal and start a new chat",
		async run(_args, host) {
			host.clear();
			const agent = host.sessionAgent();
			const { session } = await host.request<SessionResult>(
				"session.create",
				agent ? { agent } : {},
			);
			host.switchTo(session, []);
		},
	},
	{
		name: "exit",
		description: "Quit Machina",
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
			`${helpText(COMMANDS)}\n\nKeys: Enter send · Shift+Enter newline · Esc stop reply · Tab complete · ↓ on empty input picks a subagent · Ctrl+C exit · Ctrl+Shift+←/→ switch agents\nWorkspace: Ctrl+P quick open · Ctrl+S save · Ctrl+Space completion · F12 definition · Shift+F12 references · F2 rename · F6/Shift+F6 cycle focus · Ctrl+→ maximize chat · Ctrl+← back to the 50/50 split (editor word motion: alt+arrows) · Ctrl+T explorer · Ctrl+W close tab (terminal may intercept)`,
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
	growthCommand,
	skillsCommand,
	agentsCommand,
	delegateCommand,
	authCommand,
	jobsCommand,
	keysCommand,
	modeCommand,
	approvalsCommand,
	modelsCommand,
	themeCommand,
	outputCommand,
	mcpCommand,
	interopCommand,
	harnessesCommand,
	...settingsCommands,
	...workspaceCommands,
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
