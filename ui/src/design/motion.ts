// One animation coordinator per App: at most one live timer for activity
// and the learning-square acknowledgment. Widgets select the current frame
// at render; subscriptions control render demand only.
//
// Key uniqueness + subscription ownership: starting one key twice replaces
// the earlier subscription, and a stale stop() cannot delete the newer one.

export const ACTIVITY_INTERVAL_MS = 200;

/**
 * F-23: one shared reader for the reduced-motion environment. The primary
 * name is `MACHINA_UI_REDUCED_MOTION`; `AH_UI_REDUCED_MOTION` is the legacy
 * name and applies only when the primary variable is unset or empty, so an
 * explicit primary value (even "0") always wins over the fallback.
 */
export function reducedMotionFromEnv(env: NodeJS.ProcessEnv): boolean {
	const primary = (env.MACHINA_UI_REDUCED_MOTION ?? "").trim();
	if (primary.length > 0) return /^(1|true)$/i.test(primary);
	const legacy = (env.AH_UI_REDUCED_MOTION ?? "").trim();
	return /^(1|true)$/i.test(legacy);
}

/**
 * Focus-caret blink cadence. The block caret is painted only while the
 * coordinator's `cursor.blink` frame is on, so the two-frame phase flips once
 * per interval (500 ms on, 500 ms off). Reduced motion pins the caret static
 * (frame 0) and the app retires the subscription entirely, so there is no
 * timer at all.
 */
export const CURSOR_BLINK_INTERVAL_MS = 500;

export interface AnimationSubscription {
	stop(): void;
}

export interface ActivitySource {
	frame(frames: readonly string[], intervalMs?: number, startedAtMs?: number): string;
}

interface Entry {
	generation: number;
	intervalMs: number;
	startedAt: number;
	expiresAt: number | undefined;
}

const NOOP_SUBSCRIPTION: AnimationSubscription = {
	stop(): void {},
};

export class AnimationCoordinator implements ActivitySource {
	private readonly requestRenderFn: () => void;
	private reduced: boolean;
	private readonly entries = new Map<string, Entry>();
	private generationCounter = 0;
	private timer: NodeJS.Timeout | undefined;
	private readonly epoch: number;
	private disposed = false;

	constructor(requestRender: () => void, reducedMotion = false) {
		this.requestRenderFn = requestRender;
		this.reduced = reducedMotion;
		this.epoch = Date.now();
	}

	/** Active subscription count (demand). */
	get size(): number {
		return this.entries.size;
	}

	/** Live timer count: 0 or 1. */
	get timerCount(): number {
		return this.timer ? 1 : 0;
	}

	get isReducedMotion(): boolean {
		return this.reduced;
	}

	get isDisposed(): boolean {
		return this.disposed;
	}

	subscribe(key: string, intervalMs = ACTIVITY_INTERVAL_MS, durationMs?: number): AnimationSubscription {
		if (this.disposed) return NOOP_SUBSCRIPTION;
		this.generationCounter++;
		const generation = this.generationCounter;
		const now = Date.now();
		this.entries.set(key, {
			generation,
			intervalMs: Math.max(20, intervalMs),
			startedAt: now,
			expiresAt: durationMs === undefined ? undefined : now + Math.max(0, durationMs),
		});
		this.reschedule();
		let stopped = false;
		return {
			stop: (): void => {
				if (stopped || this.disposed) return;
				stopped = true;
				const current = this.entries.get(key);
				if (current && current.generation === generation) {
					this.entries.delete(key);
					this.reschedule();
				}
			},
		};
	}

	frame(frames: readonly string[], intervalMs = ACTIVITY_INTERVAL_MS, startedAtMs?: number): string {
		if (frames.length === 0) return "";
		if (frames.length === 1) return frames[0]!;
		if (this.reduced) return frames[0]!;
		const step = Math.max(20, intervalMs);
		const start = startedAtMs ?? this.epoch;
		const elapsed = Math.max(0, Date.now() - start);
		return frames[Math.floor(elapsed / step) % frames.length]!;
	}

	setReducedMotion(enabled: boolean): void {
		if (this.disposed || this.reduced === enabled) return;
		this.reduced = enabled;
		this.reschedule();
		this.requestRender();
	}

	dispose(): void {
		if (this.disposed) return;
		this.disposed = true;
		if (this.timer) {
			clearInterval(this.timer);
			clearTimeout(this.timer);
			this.timer = undefined;
		}
		this.entries.clear();
	}

	private requestRender(): void {
		if (this.disposed) return;
		this.requestRenderFn();
	}

	private reschedule(): void {
		if (this.timer) {
			clearInterval(this.timer);
			clearTimeout(this.timer);
			this.timer = undefined;
		}
		if (this.disposed || this.entries.size === 0) return;
		if (this.reduced) {
			let earliest: number | undefined;
			for (const entry of this.entries.values()) {
				if (entry.expiresAt !== undefined && (earliest === undefined || entry.expiresAt < earliest)) {
					earliest = entry.expiresAt;
				}
			}
			if (earliest === undefined) return;
			const delay = Math.max(0, earliest - Date.now());
			this.timer = setTimeout(() => this.onTick(), delay);
			this.timer.unref?.();
			return;
		}
		let interval = Infinity;
		for (const entry of this.entries.values()) {
			if (entry.intervalMs < interval) interval = entry.intervalMs;
		}
		if (interval === Infinity) return;
		const tickMs = Math.min(1000, Math.max(20, interval));
		this.timer = setInterval(() => this.onTick(), tickMs);
		this.timer.unref?.();
	}

	private onTick(): void {
		if (this.disposed) return;
		const now = Date.now();
		let expired = false;
		for (const [key, entry] of [...this.entries]) {
			if (entry.expiresAt !== undefined && now >= entry.expiresAt) {
				this.entries.delete(key);
				expired = true;
			}
		}
		this.requestRender();
		if (expired) this.reschedule();
	}
}
