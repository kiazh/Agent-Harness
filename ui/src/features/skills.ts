// Skill commands: browse, list, search, show, learn, delete, curator.
//
// `/skills`, `/skills list` and `/skills search` open the interactive
// directory when the host provides one (spec §12.3 — the menu is the product
// surface; a typed `list` must not dump the banner table on an interactive
// terminal). Hosts without that surface keep the legacy printed table / the
// bounded deterministic `skills.search` table (U07 fallback). `/skills show`
// stays byte-compatible for existing callers.

import { keyValues, resolvePrefix, shortId, table } from "../format.ts";
import type { LearningReviewInfo, SkillInfo } from "../protocol.ts";
import { classificationLabel, type SkillsSearchResult } from "../components/SkillsMenu.ts";
import { splitSub } from "../commands.ts";
import { confirm, options, requireArgs, requireSession, type Command, type FeatureHost } from "./types.ts";
import { isFullUuid, scopedMiss } from "./memory.ts";

async function listSkills(host: FeatureHost): Promise<void> {
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
}

/** Bounded printed search for hosts without the interactive overlay. */
async function printSkillSearch(host: FeatureHost, query: string): Promise<void> {
	const result = await host.request<SkillsSearchResult>("skills.search", {
		query,
		filters: { include_excluded: true },
		limit: 20,
	});
	if (result.candidates.length === 0) {
		const label = classificationLabel(result.diagnostics?.classification ?? "", query);
		host.print(query ? `No skills match "${query}" (${label || "no matches"}).` : "No skills match (no query given).", "plain");
		return;
	}
	host.print(
		table(
			["Name", "State", "Source", "Why"],
			result.candidates.map((candidate) => [
				candidate.name,
				candidate.applicability,
				`${candidate.trust} (${candidate.sourceType})`,
				candidate.reasons[0] ?? candidate.matchedPhrases[0] ?? candidate.explanation,
			]),
			70,
		),
		"plain",
	);
}

export const skillsCommand: Command = {
	name: "skills",
	description: "Skills: browse, list, search, show, learn, proposals, approve, reject, revert, delete, curator",
	argumentHint: "[list|search|show|learn|proposals|approve|reject|revert|delete|curator]",
	getArgumentCompletions: options([
		["list", "All installed skills (interactive directory)"],
		["search", "Search skills (interactive picker)"],
		["show", "Read a skill <name>"],
		["learn", "Create a skill from <file|url|skill>"],
		["proposals", "Review suggested and auto-applied skills"],
		["approve", "Approve a suggested skill <id>"],
		["reject", "Reject a suggested skill <id>"],
		["revert", "Revert an approved or auto-applied skill <id>"],
		["delete", "Remove a skill <name>"],
		["curator", "Skill health report"],
	]),
	async run(args, host) {
		const [sub, rest] = splitSub(args);
		switch (sub) {
			case "": {
				// Interactive directory when available; legacy table otherwise.
				const surface = host.skillsDirectory?.();
				if (surface) {
					surface.openSkillsPicker();
					return;
				}
				await listSkills(host);
				return;
			}
			case "list": {
				// Same interactive path as bare `/skills` on overlay hosts:
				// typing the documented `list` verb must not regress to the
				// printed wall of text on an interactive terminal.
				const surface = host.skillsDirectory?.();
				if (surface) {
					surface.openSkillsPicker();
					return;
				}
				await listSkills(host);
				return;
			}
			case "search": {
				const query = rest.trim();
				const surface = host.skillsDirectory?.();
				if (surface) {
					surface.openSkillsPicker(query || undefined);
					return;
				}
				await printSkillSearch(host, query);
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
				// Pending proposals await a decision; approved auto-applied rows
				// stay visible (marked) so a post-hoc revert is possible.
				const shown = reviews.filter(
					(review) => review.status === "pending" || (review.status === "approved" && review.autoApplied),
				);
				host.print(shown.length
					? table(
							["ID", "Status", "Name", "Description"],
							shown.map((review) => [
								shortId(review.id),
								review.autoApplied && review.status === "approved" ? "auto-applied" : review.status,
								review.name ?? "—",
								review.description ?? "—",
							]),
							70,
						)
					: "No learning proposals waiting for review.", "plain");
				return;
			}
			case "approve":
			case "reject":
			case "revert": {
				const target = requireArgs(rest, `/skills ${sub} <id>`);
				const session = requireSession(host);
				// Complete IDs bypass the latest-100 window: learning.approve/
				// reject/revert ownership-check against the session's agent and
				// report NOT_FOUND for foreign or missing IDs.
				let id: string;
				if (isFullUuid(target)) {
					id = target.trim();
				} else {
					const { reviews } = await host.request<{ reviews: LearningReviewInfo[] }>("learning.list", {
						sessionId: session.id, limit: 100,
					});
					const candidates = sub === "revert"
						? reviews.filter((review) => review.status === "approved")
						: reviews.filter((review) => review.status === "pending");
					try {
						id = resolvePrefix(target, candidates.map((review) => review.id), "learning proposal");
					} catch (error) {
						throw scopedMiss(error, "latest 100 learning proposals");
					}
				}
				const { review } = await host.request<{ review: LearningReviewInfo }>(`learning.${sub}`, { sessionId: session.id, id });
				host.print(`Learning proposal ${shortId(id)} ${review.status}.`, "success");
				return;
			}
			case "delete": {
				const name = requireArgs(rest, "/skills delete <name>");
				if (!(await confirm(host, `Delete skill "${name}"?`, "Delete"))) {
					host.print("Kept the skill.", "plain");
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
				throw new Error(`Unknown /skills option "${sub}". Try: list, search, show, learn, proposals, approve, reject, revert, delete, curator.`);
		}
	},
};
