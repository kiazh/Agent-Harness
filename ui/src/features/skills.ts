// Skill commands: list, show, learn, delete, curator.

import { keyValues, resolvePrefix, shortId, table } from "../format.ts";
import type { LearningReviewInfo, SkillInfo } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, requireArgs, requireSession, type Command } from "./types.ts";

export const skillsCommand: Command = {
	name: "skills",
	description: "Skills: list, show, learn, proposals, approve, reject, delete, health report",
	argumentHint: "[list|show|learn|proposals|approve|reject|delete|curator]",
	getArgumentCompletions: options([
		["list", "All installed skills"],
		["show", "Read a skill <name>"],
		["learn", "Create a skill from <file|url|skill>"],
		["proposals", "Review suggested skills"],
		["approve", "Approve a suggested skill <id>"],
		["reject", "Reject a suggested skill <id>"],
		["delete", "Remove a skill <name>"],
		["curator", "Skill health report"],
	]),
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "":
			case "list": {
				const { skills } = await host.request<{ skills: SkillInfo[] }>("skills.list");
				host.print(
					skills.length
						? table(
								["Name", "Uses", "Triggers", "Description"],
								skills.map((s) => [s.name, String(s.usageCount), s.triggers.slice(0, 3).join(", ") || "—", s.description]),
								60,
							)
						: "No skills installed. Add one with /skills learn <file|url>.",
					"plain",
				);
				return;
			}
			case "show": {
				const { skill } = await host.request<{ skill: SkillInfo }>("skills.show", {
					name: requireArgs(rest, "/skills show <name>"),
				});
				const triggers = skill.triggers.length ? `\n\n*Triggers:* ${skill.triggers.join(", ")}` : "";
				host.printMarkdown(`## ${skill.name}\n\n${skill.description}${triggers}\n\n---\n\n${skill.content ?? ""}`);
				return;
			}
			case "learn": {
				const source = requireArgs(rest, "/skills learn <file|url|skill>");
				const { skill } = await host.request<{ skill: SkillInfo }>("skills.learn", { source });
				host.print(`Learned skill "${skill.name}".`, "success");
				return;
			}
			case "proposals": {
				const session = requireSession(host);
				const { reviews } = await host.request<{ reviews: LearningReviewInfo[] }>("learning.list", {
					sessionId: session.id, limit: 50,
				});
				const pending = reviews.filter((review) => review.status === "pending");
				host.print(pending.length
					? table(["ID", "Name", "Description"], pending.map((review) => [shortId(review.id), review.name ?? "—", review.description ?? "—"]), 70)
					: "No learning proposals waiting for review.", "plain");
				return;
			}
			case "approve":
			case "reject": {
				const target = requireArgs(rest, `/skills ${sub} <id>`);
				const session = requireSession(host);
				const { reviews } = await host.request<{ reviews: LearningReviewInfo[] }>("learning.list", {
					sessionId: session.id, limit: 100,
				});
				const id = resolvePrefix(target, reviews.filter((review) => review.status === "pending").map((review) => review.id), "learning proposal");
				const { review } = await host.request<{ review: LearningReviewInfo }>(`learning.${sub}`, { sessionId: session.id, id });
				host.print(`Learning proposal ${shortId(id)} ${review.status}.`, "success");
				return;
			}
			case "delete": {
				const name = requireArgs(rest, "/skills delete <name>");
				if (!(await confirm(host, `Delete skill "${name}"?`, "Delete"))) {
					host.print("Kept the skill.");
					return;
				}
				await host.request("skills.delete", { name });
				host.print(`Deleted skill "${name}".`, "success");
				return;
			}
			case "curator": {
				const report = await host.request<Record<string, unknown>>("skills.curator");
				const unused = (report.unused as string[] | undefined) ?? [];
				host.print(
					keyValues([
						["Skills", report.total_skills],
						["Enabled", report.enabled],
						["Never used", report.never_used],
						["Stale (30d)", report.stale_skills],
						["Total uses", report.total_uses],
						["Unused", unused.join(", ")],
					]),
					"plain",
				);
				return;
			}
			default:
				throw new Error(`Unknown /skills option "${sub}". Try: list, show, learn, proposals, approve, reject, delete, curator.`);
		}
	},
};
