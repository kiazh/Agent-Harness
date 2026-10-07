// Mode + approvals commands: /mode, /approvals.

import { splitSub } from "../commands.ts";
import { options, type Command, type FeatureHost } from "./types.ts";
import type { ApprovalsListResult, ModeResult } from "../protocol.ts";

const MODE_HELP = [
	"ask — host tools; writes/exec/out-of-scope ask first (default)",
	"workspace — project reads/writes proceed; crossing boundaries asks",
	"sandbox — isolated Docker backend with scoped mounts",
	"full — explicit FULL HOST grant (session-scoped by default)",
].join("\n");

async function refreshMode(host: FeatureHost): Promise<string> {
	const { mode } = await host.request<ModeResult>("mode.get", {});
	return mode;
}

export const modeCommand: Command = {
	name: "mode",
	description: "Show or set execution mode (ask|workspace|sandbox|full)",
	argumentHint: "[ask|workspace|sandbox|full]",
	getArgumentCompletions: options([
		["ask", "Approval-based access (default)"],
		["workspace", "Project operation"],
		["sandbox", "Isolated container"],
		["full", "Explicit full-host access"],
	]),
	async run(args, host) {
		const [sub] = splitSub(args);
		if (!sub) {
			const mode = await refreshMode(host);
			host.print(`Mode: ${mode}\n${MODE_HELP}`, "plain");
			return;
		}
		if (!["ask", "workspace", "sandbox", "full"].includes(sub)) {
			host.print(`Unknown mode /mode ${sub}. Use ask|workspace|sandbox|full.`, "warning");
			return;
		}
		const params: Record<string, unknown> = { mode: sub };
		const session = host.session();
		if (session) params.sessionId = session.id;
		if (sub === "full") {
			const choice = await host.pick("FULL HOST access — grant to this session?", [
				{ label: "Grant this session", value: "session" },
				{ label: "Cancel", value: "cancel" },
			]);
			if (!choice || choice.value === "cancel") {
				host.print("Full mode not activated.", "warning");
				return;
			}
			params.scope = "session";
		}
		const result = await host.request<ModeResult>("mode.set", params);
		host.print(
			sub === "full"
				? `FULL HOST active for this session. Sensitive actions (credentials, elevation, destructive system ops) still ask. /mode revoke to leave.`
				: `Mode: ${result.mode} (backend: ${result.backend})`,
			sub === "full" ? "warning" : "success",
		);
	},
};

export const approvalsCommand: Command = {
	name: "approvals",
	description: "List pending approvals, or allow/deny one",
	argumentHint: "[list|allow <id>|deny <id>|revoke]",
	async run(args, host) {
		const session = host.session();
		if (!session) {
			host.print("No session.", "warning");
			return;
		}
		const [sub, rest] = splitSub(args);
		if (sub === "allow" || sub === "deny") {
			const { approvals } = await host.request<ApprovalsListResult>("approvals.list", {
				sessionId: session.id,
			});
			const match = approvals.find((a) => a.request_id.startsWith(rest));
			if (!match) {
				host.print(`No pending approval ${rest}.`, "warning");
				return;
			}
			await host.request("approvals.resolve", {
				requestId: match.request_id,
				verdict: sub === "allow" ? "approved" : "denied",
			});
			host.print(`Approval ${sub}ed: ${match.operation} ${match.target}`, "success");
			return;
		}
		if (sub === "revoke") {
			await host.request("mode.revoke", { sessionId: session.id });
			host.print("Grants revoked; back to ask mode.", "success");
			return;
		}
		const { approvals } = await host.request<ApprovalsListResult>("approvals.list", {
			sessionId: session.id,
		});
		if (!approvals.length) {
			host.print("No pending approvals.", "plain");
			return;
		}
		for (const a of approvals) {
			host.print(`${a.request_id.slice(0, 8)} ${a.operation} ${a.target} (agent: ${a.agent_id})`, "plain");
		}
		host.print("Use /approvals allow <id> or /approvals deny <id>.", "plain");
	},
};
