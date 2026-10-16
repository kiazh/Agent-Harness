// Integrated-workspace slash commands (plan §8/§12/§23): thin wrappers over
// the app-provided WorkspaceHost surface. Every layout mutation goes through
// the host so the shell geometry and focus router stay in sync; reads use the
// controller's public API. When the feature is disabled — the
// MACHINA_WORKSPACE flag is off, the backend does not serve workspace.*, or
// the host does not implement the surface — every command no-ops with one
// notice and never issues an RPC.

import { splitSub } from "../commands.ts";
import type {
	WorkspaceController,
	WorkspaceLspStatusSnapshot,
} from "../workspace/controller.ts";
import { options, type Command, type FeatureHost, type WorkspaceHost } from "./types.ts";

const UNAVAILABLE = "Workspace is not available.";

/** Resolve the workspace surface or report unavailability once. */
function requireWorkspace(host: FeatureHost): WorkspaceHost | undefined {
	const surface = host.workspace?.();
	if (!surface || !surface.controllerAvailable()) {
		host.print(UNAVAILABLE, "warning");
		return undefined;
	}
	return surface;
}

/**
 * One-line-per-server status text for `/lsp status` and the editor status
 * row. Honest states only: the values are the protocol's server states, and
 * an absent capability summary is reported as unknown, never invented.
 */
export function formatLspStatusLine(snapshot: WorkspaceLspStatusSnapshot): string | undefined {
	const parts: string[] = [];
	const seen = new Set<string>();
	for (const server of snapshot.servers) {
		const name = server.languageId || server.profile;
		if (!name || seen.has(name)) continue;
		seen.add(name);
		parts.push(`${name} ${server.state}`);
	}
	if (parts.length === 0) {
		for (const [profile, entry] of Object.entries(snapshot.profiles)) {
			parts.push(`${profile} ${entry.state}`);
		}
	}
	if (parts.length === 0) return undefined;
	const shown = parts.slice(0, 3);
	if (parts.length > shown.length) shown.push(`+${parts.length - shown.length}`);
	return shown.join(" · ");
}

function capabilitiesSummary(capabilities: Record<string, unknown> | undefined): string {
	if (!capabilities) return "capabilities unknown";
	const names = Object.entries(capabilities)
		.filter(([, value]) => value === true)
		.map(([name]) => name);
	if (names.length === 0) return "capabilities unknown";
	const shown = names.slice(0, 6);
	if (names.length > shown.length) shown.push(`+${names.length - shown.length}`);
	return `capabilities: ${shown.join(", ")}`;
}

function printLspRows(host: FeatureHost, snapshot: WorkspaceLspStatusSnapshot): void {
	if (snapshot.servers.length === 0) {
		const profiles = Object.entries(snapshot.profiles);
		if (profiles.length === 0) {
			host.print("lsp: no language servers reported", "plain");
			return;
		}
		host.print(
			[
				`lsp: ${profiles.length} profile(s)`,
				...profiles.map(([profile, entry]) => `  ${profile} — ${entry.state} (generation ${entry.generation})`),
			].join("\n"),
			"plain",
		);
		return;
	}
	host.print(
		[
			`lsp: ${snapshot.servers.length} server(s)`,
			...snapshot.servers.map(
				(server) =>
					`  ${server.languageId || server.profile} — ${server.state} (generation ${server.generation}) · ${capabilitiesSummary(server.capabilities)}`,
			),
		].join("\n"),
		"plain",
	);
}

async function printWorkspaceStatus(host: FeatureHost, controller: WorkspaceController): Promise<void> {
	if (controller.phase === "closed" && !controller.identity) {
		host.print("workspace: closed — run /workspace or reopen a session to reconnect", "plain");
		return;
	}
	const identity = controller.identity;
	if (!identity) {
		host.print(`workspace: ${controller.phase}…`, "plain");
		return;
	}
	host.print(
		[
			`workspace: ${identity.rootLabel} (${controller.rootKey ?? identity.resolvedRoot})`,
			`  epoch ${identity.epoch} · phase ${controller.phase} · mode ${controller.mode}`,
			`  ${identity.trusted ? "trusted" : "untrusted (intelligence disabled)"} · ${identity.capabilities.length > 0 ? identity.capabilities.join(", ") : "no capabilities reported"}`,
		].join("\n"),
		"plain",
	);
	// Poll once so the printed rows reflect the backend now, not a stale cache.
	await controller.lspStatus();
	printLspRows(host, controller.lspStatusSnapshot());
}

async function closeWorkspace(host: FeatureHost, surface: WorkspaceHost): Promise<void> {
	const dirty = surface.dirtyBuffers();
	if (dirty.count > 0) {
		const listed = dirty.names.join(", ");
		const more = dirty.count > dirty.names.length ? `, +${dirty.count - dirty.names.length} more` : "";
		const choice = await host.pick(`Close the workspace with ${dirty.count} unsaved file(s) (${listed}${more})?`, [
			{ value: "cancel", label: "Cancel" },
			{ value: "close", label: "Close workspace", description: "Local buffers stay open; unsaved edits are kept" },
		]);
		if (choice?.value !== "close") return;
	}
	await surface.controller.closeWorkspace();
	surface.layoutChanged();
	host.print("workspace closed; local buffers stay open", "info");
}

const workspaceCommand: Command = {
	name: "workspace",
	description: "Show or change the integrated workspace",
	argumentHint: "[split|chat|editor|status|close]",
	getArgumentCompletions: options([
		["split", "Show chat and editor side by side"],
		["chat", "Maximize the chat pane"],
		["editor", "Maximize the editor pane"],
		["status", "Print root, epoch and language servers"],
		["close", "Close the workspace (local buffers stay)"],
	]),
	async run(args, host) {
		const surface = requireWorkspace(host);
		if (!surface) return;
		const [sub] = splitSub(args);
		switch (sub) {
			case "":
			case "status":
				await printWorkspaceStatus(host, surface.controller);
				return;
			case "split":
			case "chat":
			case "editor":
				surface.setMode(sub);
				host.print(`workspace mode: ${sub}`, "info");
				return;
			case "close":
				await closeWorkspace(host, surface);
				return;
			default:
				host.print("Usage: /workspace [split|chat|editor|status|close]", "warning");
		}
	},
};

const editorCommand: Command = {
	name: "editor",
	description: "Maximize or restore the editor pane",
	argumentHint: "[maximize|restore]",
	getArgumentCompletions: options([
		["maximize", "Show the editor pane alone"],
		["restore", "Return to the chat/editor split"],
	]),
	async run(args, host) {
		const surface = requireWorkspace(host);
		if (!surface) return;
		const [sub] = splitSub(args);
		if (sub === "maximize") {
			surface.toggleEditorMaximize();
			host.print("editor maximized", "info");
			return;
		}
		if (sub === "restore" || sub === "") {
			surface.restoreSplit();
			host.print("split restored", "info");
			return;
		}
		host.print("Usage: /editor [maximize|restore]", "warning");
	},
};

const explorerCommand: Command = {
	name: "explorer",
	description: "Toggle the workspace file explorer",
	argumentHint: "[toggle|open|close]",
	getArgumentCompletions: options([
		["toggle", "Show or hide the explorer column"],
		["open", "Show the explorer column"],
		["close", "Hide the explorer column"],
	]),
	async run(args, host) {
		const surface = requireWorkspace(host);
		if (!surface) return;
		const [sub] = splitSub(args);
		const requested = surface.controller.getLayoutInput().explorerRequested;
		if (sub === "" || sub === "toggle") {
			surface.setExplorer(!requested);
			host.print(requested ? "explorer hidden" : "explorer shown", "info");
			return;
		}
		if (sub === "open") {
			surface.setExplorer(true);
			host.print("explorer shown", "info");
			return;
		}
		if (sub === "close") {
			surface.setExplorer(false);
			host.print("explorer hidden", "info");
			return;
		}
		host.print("Usage: /explorer [toggle|open|close]", "warning");
	},
};

const lspCommand: Command = {
	name: "lsp",
	description: "Language-server status and control",
	argumentHint: "[status|start|stop|restart] [profile]",
	getArgumentCompletions: options([
		["status", "Show per-profile state and capabilities"],
		["start", "Start a language server"],
		["stop", "Stop a language server"],
		["restart", "Restart a language server"],
	]),
	async run(args, host) {
		const surface = requireWorkspace(host);
		if (!surface) return;
		const controller = surface.controller;
		if (controller.phase !== "ready") {
			host.print("Open a workspace first (/workspace).", "warning");
			return;
		}
		const [sub, profile] = splitSub(args);
		if (sub === "" || sub === "status") {
			await controller.lspStatus();
			printLspRows(host, controller.lspStatusSnapshot());
			return;
		}
		if (sub !== "start" && sub !== "stop" && sub !== "restart") {
			host.print("Usage: /lsp [status|start|stop|restart] [profile]", "warning");
			return;
		}
		if (!profile) {
			host.print(`Usage: /lsp ${sub} <profile> — see /lsp status for profiles`, "warning");
			return;
		}
		const outcome = await controller.lspControl(profile, sub);
		if (outcome.status === "ok") {
			const detail = outcome.detail ? ` — ${outcome.detail}` : "";
			host.print(`lsp ${sub}: ${outcome.profile} ${outcome.state}${detail}`, "info");
		}
		// refused/failed outcomes are already notified by the controller.
	},
};

const problemsCommand: Command = {
	name: "problems",
	description: "Open the Problems pane (diagnostics)",
	async run(_args, host) {
		const surface = requireWorkspace(host);
		if (!surface) return;
		surface.openProblems();
	},
};

const quickOpenCommand: Command = {
	name: "quickopen",
	description: "Open Quick Open (also Ctrl+P)",
	async run(_args, host) {
		const surface = requireWorkspace(host);
		if (!surface) return;
		surface.openQuickOpen();
	},
};

export const workspaceCommands: Command[] = [
	workspaceCommand,
	editorCommand,
	explorerCommand,
	lspCommand,
	problemsCommand,
	quickOpenCommand,
];
