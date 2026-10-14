// Quiet activity/learning footer: status line + hints + bottom-right square.
// Timer ownership lives in the AnimationCoordinator; this widget only holds
// the acknowledgment state (last pulse, active window) and selects frames
// at render. No transcript notices, no focus theft.

import { truncateToWidth, visibleWidth, type Component } from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { ACTIVITY_INTERVAL_MS, type AnimationCoordinator, type AnimationSubscription } from "../design/motion.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { theme } from "../theme.ts";

/** Cooldown between learning acknowledgments (ms). */
const GROWTH_COOLDOWN_MS = 30_000;
/** Visible acknowledgment lifetime (ms). */
const GROWTH_DURATION_MS = 800;
/** Growth frame cadence (ms). */
const GROWTH_INTERVAL_MS = ACTIVITY_INTERVAL_MS;

type FooterStatus = "ready" | "working" | "offline" | "awaiting_approval" | "stopping" | "cleanup_pending";

export class Footer implements Component {
	cwd = "";
	branch = "";
	model = "";
	tokens = 0;
	mode = "ask";
	/** Subagents in the visible session's family (0 hides the segment). */
	subagents = 0;
	private currentStatus: FooterStatus = "ready";
	/** Compact single-row mode for short viewports (hints hide first). */
	compact = false;

	private readonly activity: AnimationCoordinator;
	private lastGrowthPulse = -Infinity;
	private growthStart = -Infinity;
	private growthSub: AnimationSubscription | undefined;
	private workingSub: AnimationSubscription | undefined;
	private workingStart = -Infinity;

	constructor(activity: AnimationCoordinator) {
		this.activity = activity;
	}

	get status(): FooterStatus {
		return this.currentStatus;
	}

	set status(status: FooterStatus) {
		this.currentStatus = status;
		if (status === "working") {
			// Text deltas retire the thinking indicator. Keep render demand
			// for the whole visible working phase, without restarting on deltas.
			if (!this.workingSub) {
				this.workingStart = Date.now();
				this.workingSub = this.activity.subscribe("footer:working", ACTIVITY_INTERVAL_MS);
			}
		} else {
			this.workingSub?.stop();
			this.workingSub = undefined;
		}
	}

	get reducedMotion(): boolean {
		return this.activity.isReducedMotion;
	}

	set reducedMotion(enabled: boolean) {
		this.activity.setReducedMotion(enabled);
	}

	/**
	 * A bounded acknowledgment; frequent evidence never restarts or queues it.
	 * Growth from non-visible sessions never reaches here (App filters).
	 */
	pulseGrowth(_requestRender?: () => void): boolean {
		const now = Date.now();
		if (this.status === "offline" || now - this.lastGrowthPulse < GROWTH_COOLDOWN_MS) return false;
		this.lastGrowthPulse = now;
		this.growthStart = now;
		this.growthSub?.stop();
		this.growthSub = this.activity.subscribe("growth", GROWTH_INTERVAL_MS, GROWTH_DURATION_MS);
		return true;
	}

	/** Retire motion when the app exits, disconnects or changes visible session. */
	dispose(): void {
		this.workingSub?.stop();
		this.workingSub = undefined;
		this.growthSub?.stop();
		this.growthSub = undefined;
		this.growthStart = -Infinity;
		this.lastGrowthPulse = -Infinity;
	}

	private get growthActive(): boolean {
		if (!this.growthSub) return false;
		if (Date.now() - this.growthStart >= GROWTH_DURATION_MS) {
			this.growthSub.stop();
			this.growthSub = undefined;
			return false;
		}
		return true;
	}

	private pulsingSquare(startedAt: number): string {
		// Brightness changes, never geometry: one fixed cell, a calm rise/fall.
		// Reduced motion retains a clear lit acknowledgment without ticking.
		if (this.reducedMotion) return theme.accent(ICONS.growthStatic);
		return this.activity.frame([
			theme.dim(ICONS.growthStatic),
			theme.muted(ICONS.growthStatic),
			theme.accent(ICONS.growthStatic),
			theme.muted(ICONS.growthStatic),
		], ACTIVITY_INTERVAL_MS, startedAt);
	}

	render(width: number): string[] {
		if (width <= 0) return [""];
		const square = this.growthActive ? this.pulsingSquare(this.growthStart)
			: this.workingSub ? this.pulsingSquare(this.workingStart) : theme.dim(ICONS.idleSquare);
		const status = this.status === "working"
			? theme.accent(`${this.activity.frame(ICONS.toolActiveFrames, ACTIVITY_INTERVAL_MS)} working`)
			: this.status === "offline" ? theme.error("offline")
			: this.status === "awaiting_approval" ? theme.warning("awaiting approval")
			: this.status === "cleanup_pending" ? theme.warning("cleanup pending")
			: this.status === "stopping" ? theme.warning("stopping") : theme.dim("ready");
		const tokens = this.tokens > 0 ? theme.dim(` · ${this.tokens.toLocaleString("en-US")} tokens`) : "";
		const agents = this.subagents > 0
			? theme.dim(` · ${this.subagents} subagent${this.subagents === 1 ? "" : "s"}`)
			: "";
		const tail = `${status}${agents}${tokens}`;
		const metadata = theme.dim(sanitizeDisplay([this.model, this.branch, this.mode].filter(Boolean).join(" / ")));
		const core = metadata ? `${metadata} / ${tail}` : tail;
		const hint = theme.dim("? shortcuts · / commands");
		const room = Math.max(0, width - 2);
		let left = core;
		if (visibleWidth(left) > room) {
			const budget = Math.max(0, room - visibleWidth(tail) - 3);
			left = budget >= 4 ? `${truncateToWidth(metadata, budget)} / ${tail}` : truncateToWidth(tail, room);
		}
		const right = !this.compact && visibleWidth(left) + visibleWidth(hint) + 5 <= width
			? `${hint}  ${square}` : square;
		const gap = Math.max(0, width - visibleWidth(left) - visibleWidth(right));
		return [truncateToWidth(`${left}${" ".repeat(gap)}${right}`, width)];
	}

	invalidate(): void {}
}
