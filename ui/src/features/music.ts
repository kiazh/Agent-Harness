// /music — show and control what's playing on this computer.
//
// The bar itself is pinned above the transcript and always visible while
// enabled; this command is the keyboard surface: play/pause, next/previous,
// status, and hiding the bar. Detection and transport go through the
// gateway (nowplaying.get / nowplaying.command); the UI never guesses state.

import type { Command, FeatureHost, MusicHost } from "./types.ts";
import { formatClock } from "../components/NowPlayingBar.ts";
import { options } from "./types.ts";

function requireMusic(host: FeatureHost): MusicHost {
	const music = host.music?.();
	if (!music) throw new Error("Music is unavailable in this host.");
	return music;
}

function statusLine(music: MusicHost): string {
	const status = music.status();
	if (!status) {
		return music.isEnabled() ? "Now playing: not checked yet." : "Music bar is off (/music on)";
	}
	if (status.error && !status.title) return `Now playing unavailable: ${status.error}`;
	if (!status.title) return "Nothing playing right now.";
	const bits = [
		status.state === "paused" ? "paused" : status.state === "playing" ? "playing" : "playing?",
		status.artist ? `${status.artist} — ${status.title}` : status.title,
	];
	if (status.duration !== null) {
		bits.push(`${formatClock(status.elapsed ?? 0)} / ${formatClock(status.duration)}`);
	}
	if (status.source) bits.push(`[${status.source}]`);
	return bits.join(" · ");
}

const MUSIC_ACTIONS: Array<[string, string]> = [
	["toggle", "Play or pause the current source"],
	["play", "Resume playback (toggle)"],
	["pause", "Pause playback (toggle)"],
	["next", "Skip to the next track"],
	["prev", "Previous track"],
	["status", "Show what is playing right now"],
	["on", "Show the music bar (persists)"],
	["off", "Hide the music bar (persists)"],
];

export const musicCommand: Command = {
	name: "music",
	description: "Show and control what's playing on this computer",
	argumentHint: "[toggle|next|prev|status|on|off]",
	getArgumentCompletions: options(MUSIC_ACTIONS),
	async run(args, host) {
		const music = requireMusic(host);
		const action = args.trim().split(/\s+/).filter(Boolean)[0]?.toLowerCase() ?? "";
		switch (action) {
			case "toggle":
				await music.toggle();
				host.print(statusLine(music), "plain");
				return;
			case "play":
			case "pause": {
				// play/pause are directional: never send the opposite command.
				const status = music.status();
				const playing = status?.state === "playing";
				if (action === "pause" && !playing) {
					host.print(
						status?.state === "paused"
							? "Already paused — use /music play to resume."
							: "Not playing — nothing to pause.",
						"plain",
					);
					return;
				}
				if (action === "play" && playing) {
					host.print("Already playing — use /music toggle to pause.", "plain");
					return;
				}
				await music.toggle();
				host.print(statusLine(music), "plain");
				return;
			}
			case "next":
				await music.next();
				host.print(statusLine(music), "plain");
				return;
			case "prev":
			case "previous":
				await music.previous();
				host.print(statusLine(music), "plain");
				return;
			case "status":
				host.print(statusLine(music), "plain");
				return;
			case "on": {
				const saved = await music.setEnabled(true);
				host.print(
					saved ? "Music bar enabled." : "Music bar enabled for this session (could not save).",
					saved ? "success" : "warning",
				);
				return;
			}
			case "off": {
				const saved = await music.setEnabled(false);
				host.print(
					saved ? "Music bar hidden." : "Music bar hidden for this session (could not save).",
					saved ? "success" : "warning",
				);
				return;
			}
			case "":
				break;
			default:
				throw new Error(
					`Unknown music action "${action}". Try: ${MUSIC_ACTIONS.map(([name]) => name).join(", ")}.`,
				);
		}

		const status = music.status();
		const choice = await host.pick("Music — what's playing", [
			{
				value: "toggle",
				label: status?.state === "paused" ? "▶ Play" : "⏸ Pause / Play",
				description: statusLine(music),
			},
			{ value: "next", label: "Next track" },
			{ value: "previous", label: "Previous track" },
			{ value: "refresh", label: "Refresh", description: "re-read what's playing now" },
			{
				value: "toggle-enabled",
				label: music.isEnabled() ? "Hide music bar" : "Show music bar",
				description: "persists via config",
			},
		]);
		if (!choice) return;
		switch (choice.value) {
			case "toggle":
				await music.toggle();
				host.print(statusLine(music), "plain");
				return;
			case "next":
				await music.next();
				host.print(statusLine(music), "plain");
				return;
			case "previous":
				await music.previous();
				host.print(statusLine(music), "plain");
				return;
			case "refresh":
				await music.refresh();
				host.print(statusLine(music), "plain");
				return;
			case "toggle-enabled": {
				const saved = await music.setEnabled(!music.isEnabled());
				host.print(
					saved ? "Music setting saved." : "Music setting applied for this session (could not save).",
					saved ? "success" : "warning",
				);
				return;
			}
		}
	},
};
