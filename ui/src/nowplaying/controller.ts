// Now-playing controller: polls the gateway's system now-playing detection
// (whatever app is playing — browser, Spotify, Music, …) and keeps the pinned
// bar in sync. Transport commands go to the detected source; the UI never
// guesses the resulting state — it re-reads it.

import type { NowPlayingBar, NowPlayingState } from "../components/NowPlayingBar.ts";

/** Data-poll cadence: metadata changes are seconds-scale; frames interpolate. */
export const NOWPLAYING_POLL_MS = 2000;

export interface NowPlayingControllerOptions {
	bar: NowPlayingBar;
	request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>;
	onChange?: () => void;
}

function normalize(payload: unknown): NowPlayingState {
	const raw = (payload ?? {}) as Partial<NowPlayingState>;
	const state: NowPlayingState["state"] =
		raw.state === "playing" || raw.state === "paused" ? raw.state : raw.state ? "unknown" : null;
	return {
		available: raw.available === true,
		state,
		title: typeof raw.title === "string" && raw.title.trim() ? raw.title : null,
		artist: typeof raw.artist === "string" && raw.artist.trim() ? raw.artist : null,
		album: typeof raw.album === "string" && raw.album.trim() ? raw.album : null,
		source: typeof raw.source === "string" && raw.source.trim() ? raw.source : null,
		elapsed: typeof raw.elapsed === "number" && raw.elapsed >= 0 ? raw.elapsed : null,
		duration: typeof raw.duration === "number" && raw.duration > 0 ? raw.duration : null,
		error: typeof raw.error === "string" && raw.error.trim() ? raw.error : null,
		fetchedAtMs: Date.now(),
	};
}

export class NowPlayingController {
	private readonly bar: NowPlayingBar;
	private readonly request: NowPlayingControllerOptions["request"];
	private enabled = true;
	private pollTimer: NodeJS.Timeout | undefined;
	/** Invalidates in-flight refreshes on disable/stop and orders overlaps. */
	private seq = 0;
	/** Orders enable/disable transitions: only the newest mutates visible state. */
	private transition = 0;
	/** Reads awaiting a gateway answer; the poll tick skips while this is > 0. */
	private inFlight = 0;
	onChange?: () => void;

	constructor(options: NowPlayingControllerOptions) {
		this.bar = options.bar;
		this.request = options.request;
		this.onChange = options.onChange;
		this.bar.onToggle = () => void this.toggle();
	}

	/** Load the config gate, take the first reading, and start polling. */
	async initialize(config: Record<string, unknown>): Promise<void> {
		const transition = ++this.transition;
		this.enabled = config.nowplaying_enabled !== false;
		this.bar.visible = this.enabled;
		if (this.enabled) {
			await this.refresh();
			if (transition !== this.transition) return;
			this.start();
		} else {
			this.sync();
		}
	}

	/** Begin the periodic re-read (idempotent; no-op while disabled). */
	start(): void {
		if (!this.enabled || this.pollTimer) return;
		this.pollTimer = setInterval(() => {
			// One poll at a time: a slow detection that overlaps its next tick
			// only doubles gateway load and delays the newest reading. Explicit
			// refreshes (commands, enable) are still ordered by seq.
			if (this.inFlight > 0) return;
			void this.refresh();
		}, NOWPLAYING_POLL_MS);
		// Never keep the process alive for a music bar.
		(this.pollTimer as { unref?: () => void }).unref?.();
	}

	/** Stop polling (exit, or while the bar is disabled). */
	stop(): void {
		this.seq += 1;
		// Initialize captures the transition before its first await; bumping
		// it makes a refresh that lands after stop() unable to start polling
		// (combined #36 / original #52).
		this.transition += 1;
		if (!this.pollTimer) return;
		clearInterval(this.pollTimer);
		this.pollTimer = undefined;
	}

	isEnabled(): boolean {
		return this.enabled;
	}

	status(): NowPlayingState | undefined {
		return this.bar.state;
	}

	/** One poll; failures degrade to an honest "unavailable" reading. */
	async refresh(): Promise<void> {
		if (!this.enabled) return;
		const id = ++this.seq;
		this.inFlight += 1;
		let next: NowPlayingState;
		try {
			const payload = await this.request<unknown>("nowplaying.get", {});
			next = normalize(payload);
		} catch (error) {
			next = {
				available: false,
				state: null,
				title: null,
				artist: null,
				album: null,
				source: null,
				elapsed: null,
				duration: null,
				error: error instanceof Error ? error.message : "now playing unavailable",
				fetchedAtMs: Date.now(),
			};
		} finally {
			this.inFlight -= 1;
		}
		// A slower older poll must never overwrite a newer reading, and a
		// refresh that lands after disabling must not resurface state.
		if (!this.enabled || id !== this.seq) return;
		this.bar.state = next;
		this.sync();
	}

	/** Re-read after a command; the state is never guessed. */
	private async command(action: "toggle" | "next" | "previous"): Promise<void> {
		if (!this.enabled) return;
		try {
			await this.request("nowplaying.command", { action });
		} catch {
			// The follow-up refresh reports the real state either way.
		}
		await this.refresh();
	}

	async toggle(): Promise<void> {
		await this.command("toggle");
	}

	async next(): Promise<void> {
		await this.command("next");
	}

	async previous(): Promise<void> {
		await this.command("previous");
	}

	/** Persist the bar on/off; hiding also drops the stale reading. */
	async setEnabled(enabled: boolean): Promise<boolean> {
		// The newest intent wins immediately: an older in-flight transition can
		// no longer mutate timers, visibility or the cleared reading, and its
		// (possibly later) write is compensated to the live value below.
		const transition = ++this.transition;
		this.enabled = enabled;
		let saved = true;
		try {
			await this.request("config.set", {
				key: "nowplaying_enabled",
				value: enabled,
				persist: true,
			});
		} catch {
			saved = false;
		}
		if (transition !== this.transition) {
			// A newer transition owns the state. If this stale write landed
			// last it may have clobbered the persisted gate; re-assert the
			// newest value (same value as the newest write, so ordering safe).
			if (saved) void this.persistLiveEnabled();
			return saved;
		}
		if (!enabled) {
			this.stop();
			this.seq += 1;
			this.bar.state = undefined;
			this.bar.visible = false;
			this.sync();
			return saved;
		}
		this.bar.visible = true;
		await this.refresh();
		if (transition !== this.transition) {
			if (saved) void this.persistLiveEnabled();
			return saved;
		}
		this.start();
		return saved;
	}

	/** Best-effort re-write of the live enabled value after a stale write. */
	private async persistLiveEnabled(): Promise<void> {
		try {
			await this.request("config.set", {
				key: "nowplaying_enabled",
				value: this.enabled,
				persist: true,
			});
		} catch {
			// The UI state is already authoritative; persistence stays best-effort.
		}
	}

	private sync(): void {
		this.bar.visible = this.enabled;
		this.onChange?.();
	}
}
