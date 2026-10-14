// Interop self-setup: detect external harnesses and register Machina's MCP
// server with them. The gateway delegates to machina/interop.py in-process.
//
// enable/disable edit platform config outside this repository, so both always
// go through an explicit confirmation first; a dry run states plainly that
// nothing is written. Outcomes are printed exactly as reported — ok=false is
// a warning, never a fake success.

import { table } from "../format.ts";
import type { InteropActionResult, InteropPlatformRow, InteropStatusResult } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, type Command, type FeatureHost } from "./types.ts";

const SUBCOMMANDS: Array<[string, string]> = [
	["status", "Show detected harnesses and registration state"],
	["enable", "Register Machina: enable <platform> [--scope global|project] [--dry-run]"],
	["disable", "Remove the registration: disable <platform>"],
];

const PLATFORMS: Array<[string, string]> = [
	["opencode", "OpenCode — project or global registration"],
	["codex", "Codex — user-level registration"],
	["hermes", "Hermes — user-level registration"],
];

const SCOPES: Array<[string, string]> = [
	["project", "This checkout only (opencode.json)"],
	["global", "User-level platform config"],
];

const ENABLE_FLAGS: Array<[string, string]> = [
	["--scope", "Registration scope (default project)"],
	["--dry-run", "Plan the command without running it"],
];

const PLATFORM_LABELS: Record<string, string> = {
	opencode: "OpenCode",
	codex: "Codex",
	hermes: "Hermes",
};

function beginMatches(items: Array<[string, string]>, current: string) {
	const matches = items
		.filter(([value]) => value.startsWith(current))
		.map(([value, description]) => ({ value, label: value, description }));
	return matches.length ? matches : null;
}

function argCompletions(prefix: string) {
	const words = prefix.trimStart().split(/\s+/).filter(Boolean);
	const trailing = !prefix.trimStart() || /\s$/.test(prefix);
	const current = trailing ? "" : (words.pop() ?? "").toLowerCase();
	if (!words.length) return beginMatches(SUBCOMMANDS, current);
	const [sub] = words;
	if (sub !== "enable" && sub !== "disable") return null;
	if (words.length === 1) return beginMatches(PLATFORMS, current);
	if (sub === "disable") return null;
	if (words.length === 2) {
		return current === "" || current.startsWith("-") ? beginMatches(ENABLE_FLAGS, current) : null;
	}
	if (words.length === 3 && words[2] === "--scope") return beginMatches(SCOPES, current);
	return null;
}

function parseEnableArgs(rest: string): { platform: string; scope?: string; dryRun: boolean } {
	const usage = "Usage: /interop enable <platform> [--scope global|project] [--dry-run]";
	const words = rest.trim().split(/\s+/).filter(Boolean);
	const platform = words.shift();
	if (!platform) throw new Error(usage);
	let scope: string | undefined;
	let dryRun = false;
	for (let i = 0; i < words.length; i++) {
		const word = words[i]!;
		if (word === "--dry-run") {
			dryRun = true;
		} else if (word === "--scope") {
			scope = words[++i];
			if (!scope) throw new Error("--scope needs global or project.");
		} else if (word.startsWith("--scope=")) {
			scope = word.slice("--scope=".length);
		} else {
			throw new Error(`Unknown option "${word}". ${usage}`);
		}
	}
	if (scope && scope !== "global" && scope !== "project") {
		throw new Error("--scope must be global or project.");
	}
	return { platform, scope, dryRun };
}

function labelOf(row: InteropPlatformRow): string {
	return row.label || row.platform;
}

function renderStatus(body: InteropStatusResult): string {
	const rows = body.platforms ?? [];
	if (!rows.length) return "No harness platforms reported.";
	const rendered = table(
		["Platform", "Installed", "Version", "Running", "Registered", "Enabled", "Scope"],
		rows.map((row) => [
			labelOf(row),
			row.installed ? "yes" : "no",
			row.version ?? "—",
			row.running ? "yes" : "no",
			row.registered ? "yes" : "no",
			row.enabled === null ? (row.registered ? "manual" : "no") : row.enabled ? "yes" : "no",
			row.scope ?? "—",
		]),
	);
	const notes = rows.flatMap((row) => (row.notes ?? []).map((note) => `· ${labelOf(row)}: ${note}`));
	return [
		rendered,
		"",
		...notes,
		"Use /interop enable <platform> [--scope global|project] [--dry-run] to register; /interop disable <platform> to remove.",
	].join("\n");
}

function printResult(host: FeatureHost, result: InteropActionResult): void {
	host.print(result.message, result.ok ? "success" : "warning");
	const lines = [...(result.steps ?? [])];
	const detail = result.verify?.detail;
	if (detail) lines.push(`${result.verify?.ok ? "verified" : "not verified"}: ${detail}`);
	if (lines.length) host.print(lines.map((line) => `  ${line}`).join("\n"), "plain");
}

export const interopCommand: Command = {
	name: "interop",
	description: "Register Machina's MCP server with OpenCode, Codex or Hermes",
	argumentHint: "[status | enable <platform> [--scope global|project] [--dry-run] | disable <platform>]",
	getArgumentCompletions: argCompletions,
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "":
			case "status": {
				const body = await host.request<InteropStatusResult>("interop.status");
				host.print(renderStatus(body), "plain");
				return;
			}
			case "enable": {
				const { platform, scope, dryRun } = parseEnableArgs(rest);
				const label = PLATFORM_LABELS[platform.toLowerCase()] ?? platform;
				const question = dryRun
					? `Preview the ${label} registration command? Nothing is written.`
					: `Register Machina's MCP server with ${label}? This writes ${label} config outside this repository (scope: ${scope ?? "project"}).`;
				if (!(await confirm(host, question, dryRun ? "Preview" : "Register"))) return;
				const result = await host.request<InteropActionResult>("interop.enable", {
					platform,
					...(scope ? { scope } : {}),
					dry_run: dryRun,
				});
				printResult(host, result);
				return;
			}
			case "disable": {
				const [platform, ...extra] = rest.trim().split(/\s+/).filter(Boolean);
				if (!platform || extra.length) throw new Error("Usage: /interop disable <platform>");
				const label = PLATFORM_LABELS[platform.toLowerCase()] ?? platform;
				const question = `Remove Machina's MCP server registration from ${label}? This edits ${label} config outside this repository.`;
				if (!(await confirm(host, question, "Remove"))) return;
				const result = await host.request<InteropActionResult>("interop.disable", { platform });
				printResult(host, result);
				return;
			}
			default:
				throw new Error(`Unknown /interop option "${sub}". Try: status, enable, disable.`);
		}
	},
};
