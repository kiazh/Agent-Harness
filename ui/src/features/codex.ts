// Codex-parity commands: project setup and agent-driven review.
//
// /init scaffolds an AGENTS.md with AgentHarness instructions (Codexparity:
// "create an AGENTS.md file with instructions"). /review sends the turn to
// the agent, exactly like Codex's "review my current changes".

import { basename } from "node:path";
import type { Command } from "./types.ts";

function agentsTemplate(project: string): string {
	return `# AGENTS.md — ${project}

Instructions for AI agents working in this repository.

## Tools

- Prefer the provided tools over guessing: read, write, and search files with
  the file tools; run commands with the sandboxed terminal tool.
- Keep changes minimal and consistent with surrounding code.

## Memory

- Durable facts, preferences, and decisions belong in long-term memory
  (\`remember\` them); recall them with \`recall\` before acting on them.
- Session context is finite — compress or fork long sessions.

## Workflow

- Restate the task, act step by step, and report what changed.
- Do not commit unless asked.
`;
}

export const codexCommands: Command[] = [
	{
		name: "init",
		description: "Create an AGENTS.md file with instructions for the agent",
		async run(_args, host) {
			const project = basename(process.cwd());
			const full = await host.writeFile("AGENTS.md", agentsTemplate(project));
			host.print(`Created ${full}. Edit it to teach the agent about this project.`, "success");
		},
	},
	{
		name: "review",
		description: "Review my current changes and find issues",
		argumentHint: "[focus]",
		async run(args, host) {
			const focus = args.trim();
			await host.submitTurn(
				`Review my current changes and find issues.${focus ? ` Focus on: ${focus}` : ""} Use the available tools to inspect the working tree, then report findings ordered by severity.`,
			);
		},
	},
];
