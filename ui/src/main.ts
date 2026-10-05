// Entry point: `node ui/src/main.ts [--model M] [--provider P] [--session ID]`.
// Normally started by `ah` (ah/cli/launcher.py), which sets AH_PYTHON.

import { parseArgs } from "node:util";
import * as path from "node:path";
import * as fs from "node:fs";
import * as crypto from "node:crypto";
import { ProcessTerminal, TuiMainScreen } from "@earendil-works/pi-tui";
import { App } from "./app.ts";
import { GatewayClient } from "./gateway.ts";

const USAGE = `Usage: ah [--model MODEL] [--provider openrouter|ollama] [--session ID]

Interactive AgentHarness terminal UI. Set AH_PYTHON to the Python interpreter
that has AgentHarness installed (the \`ah\` command does this for you).`;

const { values } = parseArgs({
	args: process.argv.slice(2),
	options: {
		model: { type: "string" },
		provider: { type: "string" },
		session: { type: "string" },
		help: { type: "boolean", short: "h" },
	},
	strict: true,
});

if (values.help) {
	console.log(USAGE);
	process.exit(0);
}

function detectPython(): string {
	if (process.env.AH_PYTHON) return process.env.AH_PYTHON;
	if (process.platform === "win32") {
		// Prefer the project venv over system python (which lacks ah).
		const venv = path.resolve(import.meta.dirname, "..", "..", ".venv", "Scripts", "python.exe");
		try {
			if (fs.statSync(venv).isFile()) return venv;
		} catch { /* fall through */ }
		return "python";
	}
	return "python3";
}
const python = detectPython();
const gatewayToken = crypto.randomBytes(32).toString("hex");
const tui = new TuiMainScreen(new ProcessTerminal());
const client = new GatewayClient({ python, token: gatewayToken, env: { AH_GATEWAY_TOKEN: gatewayToken } });
const app = new App(tui, client, { model: values.model, provider: values.provider, sessionId: values.session });

// Never leave the terminal in raw mode, whatever happens.
const crash = (error: unknown) => {
	try {
		tui.stop();
	} catch {
		// terminal may already be restored
	}
	console.error(error instanceof Error ? (error.stack ?? error.message) : error);
	void client.stop().finally(() => process.exit(1));
};
process.on("uncaughtException", crash);
process.on("unhandledRejection", crash);

await app.start();
await app.exited;
process.exit(0);
