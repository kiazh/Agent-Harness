// Mode + approvals commands: /mode, /approvals.

import { shortId, table } from "../format.ts";
import { splitSub } from "../commands.ts";
import { options, requireSession, type Command, type FeatureHost } from "./types.ts";
import type { ApprovalsListResult, ModeResult } from "../protocol.ts";

const MODE_HELP = [
	"ask — routine project tools proceed; risky actions ask (default)",
	"workspace — routine project tools proceed; crossing boundaries asks",
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
		["ask", "Ask for risky actions (default)"],
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
			throw new Error(`Unknown mode "${sub}". Use ask|workspace|sandbox|full.`);
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
				host.print("Full mode not activated.", "plain");
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
	getArgumentCompletions: options([
		["list", "Pending approvals"],
		["allow", "Approve a pending request <id>"],
		["deny", "Deny a pending request <id>"],
		["revoke", "Revoke session grants"],
	]),
	async run(args, host) {
		const session = requireSession(host);
		const [sub, rest] = splitSub(args);
		if (sub === "allow" || sub === "deny") {
			if (!rest) throw new Error(`Usage: /approvals ${sub} <id>. Run /approvals to list pending ids.`);
			const { approvals } = await host.request<ApprovalsListResult>("approvals.list", {
				sessionId: session.id,
			});
			const matches = approvals.filter((a) => a.request_id.startsWith(rest));
			if (!matches.length) throw new Error(`No pending approval starts with "${rest}".`);
			if (matches.length > 1) {
				const candidates = matches
					.map((a) => `${shortId(a.request_id)} (${a.operation} ${a.target})`)
					.join("\n");
				throw new Error(
					`"${rest}" matches ${matches.length} pending approvals; use a longer id:\n${candidates}`,
				);
			}
			const match = matches[0]!;
			await host.request("approvals.resolve", {
				requestId: match.request_id,
				verdict: sub === "allow" ? "approved" : "denied",
				sessionId: session.id,
			});
			host.print(`Approval ${sub}ed: ${match.operation} ${match.target}`, "success");
			return;
		}
		if (sub === "revoke") {
			await host.request("mode.revoke", { sessionId: session.id });
			host.print("Grants revoked; back to ask mode.", "success");
			return;
		}
		if (sub && sub !== "list") throw new Error(`Unknown /approvals option "${sub}". Try: list, allow, deny, revoke.`);
		const { approvals } = await host.request<ApprovalsListResult>("approvals.list", {
			sessionId: session.id,
		});
		if (!approvals.length) {
			host.print("No pending approvals.", "plain");
			return;
		}
		host.print(
			`${table(
				["ID", "Operation", "Target", "Agent"],
				approvals.map((a) => [shortId(a.request_id), a.operation, a.target, a.agent_id]),
				40,
			)}\n\nUse /approvals allow <id> or /approvals deny <id>.`,
			"plain",
		);
	},
};
