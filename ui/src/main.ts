// Entry point: `node ui/src/main.ts [--model M] [--provider P] [--session ID] [--mode MODE]`.
// Normally started by `machina` (machina/cli/launcher.py), which sets MACHINA_PYTHON.

import { parseArgs } from "node:util";
import * as path from "node:path";
import * as fs from "node:fs";
import * as crypto from "node:crypto";
import { TuiAltScreen } from "@earendil-works/pi-tui";
import { ThemedTerminal } from "./design/terminal.ts";
import { tuiClipboardOptions } from "./clipboard.ts";
import { App } from "./app.ts";
import { GatewayClient } from "./gateway.ts";

const USAGE = `Usage: machina [--model MODEL] [--provider PROVIDER] [--session ID] [--mode MODE] [--no-clear]

Interactive Machina terminal UI. Set MACHINA_PYTHON to the Python interpreter
that has Machina installed (the \`machina\` command does this for you).
The terminal is cleared on startup so the app owns the screen; pass
--no-clear to keep existing scrollback.`;

const { values } = parseArgs({
	args: process.argv.slice(2),
	options: {
		model: { type: "string" },
		provider: { type: "string" },
		session: { type: "string" },
		mode: { type: "string" },
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
	if (process.env.MACHINA_PYTHON) return process.env.MACHINA_PYTHON;
	if (process.platform === "win32") {
		// Prefer the project venv over system python (which lacks machina).
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
// Selection copy must hit the real clipboard (pbcopy/wl-copy/Set-Clipboard);
// the framework's OSC 52 fallback lies "Copied!" on terminals that ignore it.
const tui = new TuiAltScreen(new ThemedTerminal(), undefined, undefined, tuiClipboardOptions());
const client = new GatewayClient({ python, token: gatewayToken, env: { MACHINA_GATEWAY_TOKEN: gatewayToken } });
const app = new App(tui, client, { model: values.model, provider: values.provider, sessionId: values.session, mode: values.mode });

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
