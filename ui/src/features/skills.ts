// Skill commands: list, show, learn, delete, curator.

import { keyValues, table } from "../format.ts";
import type { SkillInfo } from "../protocol.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, requireArgs, type Command } from "./types.ts";

export const skillsCommand: Command = {
	name: "skills",
	description: "Skills: list, show, learn, delete, health report",
	argumentHint: "[list|show|learn|delete|curator]",
	getArgumentCompletions: options([
		["list", "All installed skills"],
		["show", "Read a skill <name>"],
		["learn", "Create a skill from <file|url|skill>"],
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
				throw new Error(`Unknown /skills option "${sub}". Try: list, show, learn, delete, curator.`);
		}
	},
};
