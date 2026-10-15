// /harnesses — a picker over external agent harnesses (interop.list) with
// per-harness actions: Status, Register (enable), Unregister (disable) and
// Run a task.
//
// Running delegates one prompt into the external harness through interop.run;
// the gateway bounds and redacts the child output. The harness executes with
// access to this host, so the confirm line says so plainly before anything
// runs. A harness whose run surface is unavailable shows its hint and never
// offers the run action.

import { sanitizeDisplay } from "../design/text.ts";
import { keyValues } from "../format.ts";
import type {
	HarnessListResult,
	HarnessRow,
	HarnessRunResult,
	InteropActionResult,
} from "../protocol.ts";
import { confirm, type Command, type FeatureHost } from "./types.ts";

/** Positive finite run-deadline seconds, or undefined when not reported (H22). */
function runDeadlineSeconds(body: HarnessListResult): number | undefined {
	const value = body.runTimeoutSeconds;
	return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
}

/** Compact one-line state summary for the picker row. */
function summary(row: HarnessRow): string {
	const parts: string[] = [];
	parts.push(row.installed ? (row.version ? `✓ installed (${row.version})` : "✓ installed") : "✗ not installed");
	if (row.running) parts.push("● running");
	parts.push(row.registered ? (row.scope ? `registered (${row.scope})` : "registered") : "not registered");
	if (row.runSupported) parts.push("runnable");
	return parts.join(" · ");
}

/** Shared enable/disable rendering: the message plus steps and verification. */
function printResult(host: FeatureHost, result: InteropActionResult): void {
	host.print(result.message, result.ok ? "success" : "warning");
	const lines = [...(result.steps ?? [])];
	const detail = result.verify?.detail;
	if (detail) lines.push(`${result.verify?.ok ? "verified" : "not verified"}: ${detail}`);
	if (lines.length) host.print(lines.map((line) => `  ${line}`).join("\n"), "plain");
}

/** Status view: version, registration, scope and the shared state file. */
function statusText(row: HarnessRow, stateFile: string | null): string {
	return keyValues([
		["Harness", row.label],
		["Installed", row.installed ? (row.version ?? "yes") : "no"],
		["Running", row.running ? "yes" : "no"],
		["Registered", row.registered ? (row.scope ? `yes (${row.scope})` : "yes") : "no"],
		[
			"Run",
			row.runSupported
				? `supported — ${row.runHint ?? "yes"}`
				: `unsupported — ${row.runHint ?? "not available"}`,
		],
		["Scope", row.scope ?? "—"],
		["State file", stateFile ?? "—"],
	]);
}

async function registerHarness(host: FeatureHost, row: HarnessRow): Promise<void> {
	const question =
		`Register Machina's MCP server with ${row.label}? ` +
		`This writes ${row.label} config outside this repository.`;
	if (!(await confirm(host, question, "Register"))) return;
	const result = await host.request<InteropActionResult>("interop.enable", { platform: row.key });
	printResult(host, result);
}

async function unregisterHarness(host: FeatureHost, row: HarnessRow): Promise<void> {
	const question =
		`Remove Machina's MCP server registration from ${row.label}? ` +
		`This edits ${row.label} config outside this repository.`;
	if (!(await confirm(host, question, "Remove"))) return;
	const result = await host.request<InteropActionResult>("interop.disable", { platform: row.key });
	printResult(host, result);
}

/** Render the bounded run outcome as scroll-friendly plain text. */
function renderRun(host: FeatureHost, row: HarnessRow, result: HarnessRunResult): void {
	const headline = [`${row.label}: ${result.ok ? "completed" : "failed"}`];
	if (result.exitCode !== null) headline.push(`exit code ${result.exitCode}`);
	if (result.unsupportedReason) headline.push(`unsupported: ${result.unsupportedReason}`);
	host.print(sanitizeDisplay(headline.join(" · ")), result.ok ? "success" : "warning");
	if (result.command) host.print(sanitizeDisplay(`command: ${result.command}`), "plain");
	if (result.unsupportedReason) return;
	if (result.stdout) host.print(sanitizeDisplay(result.stdout), "plain");
	if (result.stderr) host.print(sanitizeDisplay(result.stderr), "warning");
	if (!result.stdout && !result.stderr && result.exitCode === null) {
		host.print(
			"No output; the harness produced no exit code (it may have timed out or been killed).",
			"warning",
		);
	}
}

async function runTask(
	host: FeatureHost,
	row: HarnessRow,
	runTimeoutSeconds: number | undefined,
): Promise<void> {
	if (!row.runSupported) {
		host.print(
			`${row.label}: running a task is not supported${row.runHint ? ` — ${row.runHint}` : ""}.`,
			"warning",
		);
		return;
	}
	if (runTimeoutSeconds === undefined) {
		// H22: without the gateway's deadline the client cannot know how long
		// the harness may run, and its own clock could abandon a live process.
		host.print(
			`${row.label}: this gateway does not report its harness run deadline; ` +
				"refusing to start a task the UI could not wait for. Update Machina, " +
				`or run it from the shell: machina harnesses run ${row.key} "<prompt>".`,
			"warning",
		);
		return;
	}
	const prompt = await host.askText({
		title: `Run a task in ${row.label}`,
		description: `${row.runHint ?? "Delegates one prompt into the external harness."} The harness runs with access to this host.`,
		placeholder: "Describe the task for the harness…",
	});
	if (prompt === undefined) return;
	if (!prompt.trim()) {
		host.print("Empty prompt — nothing was run.", "plain");
		return;
	}
	const warning =
		`Run this prompt inside ${row.label}? It executes in the external harness ` +
		"with host access (files, network, tools).";
	if (!(await confirm(host, warning, "Run task"))) return;
	const result = await host.request<HarnessRunResult>("interop.run", {
		platform: row.key,
		prompt,
		// The gateway applies exactly this deadline; app.ts sizes the client
		// clock past it so a late-but-successful run is never discarded (H22).
		timeout: runTimeoutSeconds,
	});
	renderRun(host, row, result);
}

async function actOnHarness(
	host: FeatureHost,
	row: HarnessRow,
	stateFile: string | null,
	runTimeoutSeconds: number | undefined,
): Promise<void> {
	if (!row.runSupported) {
		host.print(
			`${row.label}: run unsupported — ${row.runHint ?? "no run command for this harness"}.`,
			"warning",
		);
	}
	const actions: Array<{ value: string; label: string; description: string }> = [
		{ value: "status", label: "Status", description: "Version, registration and state file" },
		{ value: "register", label: "Register", description: "Register Machina's MCP server with this harness" },
		{ value: "unregister", label: "Unregister", description: "Remove the registration where the CLI supports it" },
	];
	if (row.runSupported) {
		actions.push({ value: "run", label: "Run a task…", description: row.runHint ?? "Delegate a prompt into this harness" });
	}
	const choice = await host.pick(`${row.label} · actions`, actions);
	if (!choice) return;
	switch (choice.value) {
		case "status":
			host.print(sanitizeDisplay(statusText(row, stateFile)), "plain");
			return;
		case "register":
			await registerHarness(host, row);
			return;
		case "unregister":
			await unregisterHarness(host, row);
			return;
		case "run":
			await runTask(host, row, runTimeoutSeconds);
			return;
		default:
			return;
	}
}

async function openMenu(host: FeatureHost): Promise<void> {
	const body = await host.request<HarnessListResult>("interop.list");
	const rows = body.harnesses ?? [];
	if (!rows.length) {
		host.print("No external harnesses reported.", "warning");
		return;
	}
	const choice = await host.pick(
		"Harnesses",
		rows.map((row) => ({
			value: row.key,
			label: `${row.label} — ${summary(row)}`,
			description: row.runSupported ? `run: ${row.runHint ?? "supported"}` : `run unsupported: ${row.runHint ?? "—"}`,
		})),
	);
	if (!choice) return;
	const row = rows.find((entry) => entry.key === choice.value);
	if (row) await actOnHarness(host, row, body.stateFile, runDeadlineSeconds(body));
}

export const harnessesCommand: Command = {
	name: "harnesses",
	description: "List external agent harnesses; register, unregister or run a task",
	argumentHint: "[<harness>]",
	async run(args, host) {
		const key = args.trim().toLowerCase();
		if (!key) {
			await openMenu(host);
			return;
		}
		const body = await host.request<HarnessListResult>("interop.list");
		const row = (body.harnesses ?? []).find((entry) => entry.key === key);
		if (!row) throw new Error(`Unknown harness "${key}". Run /harnesses to list them.`);
		await actOnHarness(host, row, body.stateFile, runDeadlineSeconds(body));
	},
};
