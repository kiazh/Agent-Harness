// Pinned now-playing bar for the top of the transcript column.
//
// Shows whatever is playing on this computer (the gateway's nowplaying.get
// detects it: system media APIs, Spotify/Music, or a front YouTube tab). The
// bar is a sibling of the transcript ScrollView in AppShell, so it never
// scrolls away. Rows by viewport budget:
//
//   full (≥24 rows):             2-row (16–23):        1-row (reduced):
//     ▂▄▆ Title         1:24/3:30   ▂▄▆ Title  1:24/3:30   ▂▄▆ Title — Artist · 1:24
//     Artist · source
//     ▶ 1:24 ─████── 3:30
//
// The leading glyph animates while audio is playing (one shared
// AnimationCoordinator, reduced-motion aware). Elapsed time interpolates
// between polls from the last fetch stamp, so progress moves smoothly
// without faster polling. When nothing is playing the bar shows one honest
// line — it is never silently absent while enabled. A click sends
// play/pause to the detected source (when that source accepts commands).

import { type TuiMouseEvent, truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { theme } from "../design/theme.ts";

/** Equalizer frames for the "audio is playing" animation. */
export const SOUND_FRAMES = ["▂▄▆", "▄▆▂", "▆▂▄", "▂▆▄"] as const;
export const PLAYED_GLYPH = "▬";
export const REMAINING_GLYPH = "─";

export interface NowPlayingState {
	available: boolean;
	state: "playing" | "paused" | "unknown" | null;
	title: string | null;
	artist: string | null;
	album: string | null;
	source: string | null;
	/** Seconds. */
	elapsed: number | null;
	/** Seconds. */
	duration: number | null;
	error: string | null;
	/** Local clock stamp (ms) when the gateway answered; drives interpolation. */
	fetchedAtMs?: number;
}

export interface SoundAnimationSource {
	frame(frames: readonly string[], intervalMs?: number, startedAtMs?: number): string;
}

/**
 * Why a reading has no track. macOS Automation denial is a needs-permission
 * state, distinct from "nothing is playing" and from a missing API.
 */
export type NowPlayingProblem = "permission" | "unavailable";

export function nowPlayingProblem(state: NowPlayingState | undefined): NowPlayingProblem | undefined {
	if (!state || state.title) return undefined;
	const error = (state.error ?? "").trim().toLowerCase();
	if (!error) return undefined;
	if (error.includes("automation") || error.includes("permission") || error.includes("not authorized")) {
		return "permission";
	}
	return "unavailable";
}

export interface PinnedBarBudget {
	setBudget(rows: number): void;
}

export function formatClock(seconds: number): string {
	if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
	const total = Math.floor(seconds);
	const secs = total % 60;
	const mins = Math.floor(total / 60) % 60;
	const hours = Math.floor(total / 3600);
	if (hours > 0) {
		return `${hours}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
	}
	return `${mins}:${String(secs).padStart(2, "0")}`;
}

export class NowPlayingBar implements PinnedBarBudget {
	state: NowPlayingState | undefined;
	/** Hidden only when disabled via config. */
	visible = false;
	onToggle?: () => void;
	private rows = 1;
	private readonly animation: SoundAnimationSource;

	constructor(animation: SoundAnimationSource) {
		this.animation = animation;
	}

	setBudget(rows: number): void {
		this.rows = Math.max(0, Math.min(3, Math.floor(rows)));
	}

	get budgetRows(): number {
		return this.rows;
	}

	/** Elapsed seconds now: interpolated while playing between polls. */
	private elapsedNow(): number {
		const state = this.state;
		if (!state || state.elapsed === null) return 0;
		if (state.state !== "playing" || state.fetchedAtMs === undefined) return state.elapsed;
		const drift = Math.max(0, (Date.now() - state.fetchedAtMs) / 1000);
		const value = state.elapsed + drift;
		return state.duration !== null ? Math.min(value, state.duration) : value;
	}

	private glyph(): string {
		const state = this.state;
		if (state?.state === "playing") return theme.accent(this.animation.frame(SOUND_FRAMES, 250));
		if (state?.state === "paused") return theme.secondary("⏸");
		return theme.muted("♪");
	}

	private clockSuffix(): string {
		const state = this.state;
		if (!state || state.duration === null) return "";
		return `${formatClock(this.elapsedNow())} / ${formatClock(state.duration)}`;
	}

	private identity(): string {
		const state = this.state;
		if (!state?.title) {
			// A denied Automation permission is actionable and must never read
			// as the same empty state as "nothing is playing".
			return nowPlayingProblem(state) === "permission" ? "Automation permission needed" : "Nothing playing";
		}
		return state.title;
	}

	private titleLine(width: number): string {
		const glyph = this.glyph();
		const title = theme.bold(
			truncateToWidth(this.identity(), Math.max(0, width - 10), "…"),
		);
		const left = `${glyph} ${title}`;
		const right = this.clockSuffix();
		if (!right) return truncateToWidth(left, width);
		const rightText = theme.dim(right);
		const gap = Math.max(1, width - visibleWidth(left) - visibleWidth(rightText));
		return truncateToWidth(`${left}${" ".repeat(gap)}${rightText}`, width);
	}

	private metaLine(width: number): string {
		const state = this.state;
		if (!state) return truncateToWidth(theme.dim("♪ Nothing playing"), width);
		if (state.error) return truncateToWidth(theme.warning(state.error), width);
		if (!state.title) return truncateToWidth(theme.dim("♪ Nothing playing"), width);
		const parts = [state.artist, state.source].filter(
			(part): part is string => Boolean(part && part.trim()),
		);
		return truncateToWidth(theme.dim(parts.join(" · ")), width);
	}

	private progressCells(width: number, ratio: number | undefined): string {
		if (width <= 0) return "";
		if (ratio === undefined) return theme.dim(REMAINING_GLYPH.repeat(width));
		const clamped = Math.max(0, Math.min(1, ratio));
		const filled = Math.round(clamped * width);
		return `${theme.accent(PLAYED_GLYPH.repeat(filled))}${theme.dim(
			REMAINING_GLYPH.repeat(Math.max(0, width - filled)),
		)}`;
	}

	private transportLine(width: number): string {
		const state = this.state;
		const playing = state?.state === "playing";
		const glyph = playing
			? theme.accent("⏸")
			: state?.state === "paused"
				? theme.secondary("▶")
				: theme.muted("▶");
		if (!state?.title || state.duration === null) {
			return truncateToWidth(glyph, width);
		}
		const elapsed = formatClock(this.elapsedNow());
		const total = formatClock(state.duration);
		const prefix = `${glyph} ${theme.dim(elapsed)} `;
		const suffix = ` ${theme.dim(total)}`;
		const barWidth = Math.max(0, width - visibleWidth(prefix) - visibleWidth(suffix));
		const ratio = state.duration > 0 ? this.elapsedNow() / state.duration : undefined;
		return truncateToWidth(`${prefix}${this.progressCells(barWidth, ratio)}${suffix}`, width);
	}

	private compactLine(width: number): string {
		const state = this.state;
		if (!state?.title) {
			const message =
				nowPlayingProblem(state) === "permission"
					? theme.warning(`♪ Automation permission needed — ${state?.error ?? "check System Settings"}`)
					: state?.error
						? theme.warning(state.error)
						: theme.dim("♪ Nothing playing");
			return truncateToWidth(message, width);
		}
		const artist = state.artist ? ` — ${state.artist}` : "";
		const clock = state.duration !== null ? ` · ${formatClock(this.elapsedNow())}` : "";
		return truncateToWidth(
			`${this.glyph()} ${theme.bold(`${state.title}${artist}`)}${theme.dim(clock)}`,
			width,
		);
	}

	render(width: number): string[] {
		if (!this.visible || width <= 0 || this.rows <= 0) return [];
		const state = this.state;
		if (!state?.title) {
			// No track: one honest line. A denied Automation permission keeps
			// its needs-permission wording (with the actionable error), never
			// the "Nothing playing" empty line.
			return [this.compactLine(width)];
		}
		if (this.rows >= 3) {
			return [this.titleLine(width), this.metaLine(width), this.transportLine(width)];
		}
		if (this.rows === 2) {
			return [this.titleLine(width), this.transportLine(width)];
		}
		return [this.compactLine(width)];
	}

	/**
	 * A completed left click toggles; press, release, wheel, drag and other
	 * buttons fall through to the renderer. A press/release pair must not
	 * toggle on its own — the shell synthesizes one click per gesture — and
	 * letting wheel through keeps the transcript scrolling under the bar
	 * (WorkspaceShell grip pattern). Called without an event (legacy/direct),
	 * a visible bar with a track still toggles.
	 */
	handleMouse(event?: TuiMouseEvent): { handled: boolean; render: boolean } {
		if (event && (event.type !== "click" || event.button !== "left")) {
			return { handled: false, render: false };
		}
		if (!this.visible || !this.state?.title) return { handled: false, render: false };
		this.onToggle?.();
		return { handled: true, render: true };
	}

	invalidate(): void {
		// Styles resolve lazily at render; nothing is cached.
	}
}
