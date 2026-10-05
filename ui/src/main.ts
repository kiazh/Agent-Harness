// Entry point: `node ui/src/main.ts [--model M] [--provider P] [--session ID]`.
// Normally started by `ah` (ah/cli/launcher.py), which sets AH_PYTHON.

import { parseArgs } from "node:util";
import * as path from "node:path";
import * as fs from "node:fs";
import * as crypto from "node:crypto";
import { ProcessTerminal, TuiAltScreen } from "@earendil-works/pi-tui";
import { App } from "./app.ts";
import { GatewayClient } from "./gateway.ts";

const USAGE = `Usage: ah [--model MODEL] [--provider openrouter|ollama] [--session ID] [--no-clear]

Interactive AgentHarness terminal UI. Set AH_PYTHON to the Python interpreter
that has AgentHarness installed (the \`ah\` command does this for you).
The terminal is cleared on startup so the app owns the screen; pass
--no-clear to keep existing scrollback.`;

const { values } = parseArgs({
	args: process.argv.slice(2),
	options: {
		model: { type: "string" },
		provider: { type: "string" },
		session: { type: "string" },
		"no-clear": { type: "boolean" },
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
// Fullscreen alternate buffer, Codex style: the app owns the display and the
// composer docks to the bottom (see App's layout root). Restored on exit.
const tui = new TuiAltScreen(new ProcessTerminal());
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

if (!values["no-clear"] && process.stdout.isTTY) {
	// Wipe scrollback + visible screen and home the cursor, so the app is
	// the only thing on screen (opencode-style takeover). The alt screen
	// below then takes over fully and restores the terminal on exit.
	process.stdout.write("\x1b[3J\x1b[2J\x1b[H");
}

await app.start();
await app.exited;
process.exit(0);
