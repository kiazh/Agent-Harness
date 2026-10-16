// Real-App idle animation proof for the now-playing bar fix.
//
// Boots the actual App with the real NowPlayingController, NowPlayingBar and
// shared AnimationCoordinator over a scripted gateway, a real TuiAltScreen and
// a fake terminal that records writes. The agent stays idle — no turns, no
// clicks, no keys. Proves the production path: accepted reading -> App
// onChange -> one `nowplaying.anim` subscription -> 250 ms renders -> visible
// sound frame / elapsed clock changes.
//
// Evidence scope: fixture/native-renderer (real renderer output over a fake
// terminal, scripted gateway readings) — NOT a real macOS playback session.

import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { stripTerminalSequences, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import type { GatewayClient } from "../src/gateway.ts";
import { SOUND_FRAMES } from "../src/components/NowPlayingBar.ts";
import { NOWPLAYING_POLL_MS } from "../src/nowplaying/controller.ts";

function reading(overrides: Record<string, unknown> = {}) {
	return {
		available: true,
		state: "playing",
		title: "Night Drive",
		artist: "Aurora",
		album: null,
		source: "Spotify",
		elapsed: 12,
		duration: 210,
		error: null,
		...overrides,
	};
}

/** Fake terminal that records writes so render demand is observable. */
function countingTerminal(columns: number, rows: number): Terminal & { writes: string[] } {
	const writes: string[] = [];
	return {
		writes,
		columns,
		rows,
		kittyProtocolActive: false,
		start() {},
		stop() {},
		async drainInput() {},
		write(data: string) {
			writes.push(data);
		},
		moveBy() {},
		hideCursor() {},
		showCursor() {},
		clearLine() {},
		clearFromCursor() {},
		clearScreen() {},
		setTitle() {},
		setProgress() {},
	} as unknown as Terminal & { writes: string[] };
}

function scriptedClient(options: {
	config: Record<string, unknown>;
	current: () => Record<string, unknown>;
	requests: Array<{ method: string; at: number }>;
}): GatewayClient & { stopped: boolean } {
	const session = {
		id: "nowplaying-anim",
		title: "new",
		model: "m",
		provider: "p",
		status: "active",
		lastActivity: null,
	};
	const client = {
		stopped: false,
		start() {},
		async stop() {
			client.stopped = true;
		},
		stderr() {
			return [];
		},
		onEvent: undefined,
		onExit: undefined,
		async request(method: string) {
			options.requests.push({ method, at: Date.now() });
			switch (method) {
				case "initialize":
					return { version: "test", model: "m", provider: "p", cwd: "/tmp", branch: "main" };
				case "config.get":
					return { config: options.config, secrets: [] };
				case "mode.get":
					return { mode: "ask" };
				case "profile.get":
					return {};
				case "session.create":
					return { session };
				case "nowplaying.get":
					return options.current();
				default:
					return {};
			}
		},
	};
	return client as unknown as GatewayClient & { stopped: boolean };
}

const drain = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

/**
 * Advance fake time and settle the deferred render path end to end:
 * poll/coordinator tick -> App.requestRender (process.nextTick) ->
 * throttled render setTimeout (<= 16 ms) -> TuiAltScreen.doRender.
 * No input is involved anywhere in this chain.
 */
async function settle(t: TestContext, ms: number): Promise<void> {
	if (ms > 0) t.mock.timers.tick(ms);
	await drain(); // flush process.nextTick(scheduleRender)
	t.mock.timers.tick(20); // fire the throttled render timer (16 ms max)
	await drain(); // let the render and its follow-up chains complete
}

function screen(tui: TuiAltScreen): string[] {
	return tui.getScreenLines().map((line) => stripTerminalSequences(line));
}

function frameIndex(line: string | undefined): number {
	if (line === undefined) return -1;
	return SOUND_FRAMES.findIndex((frame) => line.includes(frame));
}

/**
 * Read-only peek at App's private music subscription handle. App exposes no
 * accessor for it and this fix must not touch app.ts, yet section 10.1 asks
 * for proof of "exactly one music subscription" and its retirement. This
 * never mutates the field.
 */
function musicAnimHandle(app: App): { stop(): void } | undefined {
	return (app as unknown as { nowPlayingAnim?: { stop(): void } }).nowPlayingAnim;
}

test("real App: idle frame, clock, track change, pause/resume and exit need no input", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const savedReduced = process.env.MACHINA_UI_REDUCED_MOTION;
	// Normal-motion case; restored in finally.
	delete process.env.MACHINA_UI_REDUCED_MOTION;

	let current = reading();
	let fail = false;
	const requests: Array<{ method: string; at: number }> = [];
	const client = scriptedClient({
		config: { theme: "codex", nowplaying_enabled: true },
		current: () => {
			if (fail) throw new Error("source denied");
			return current;
		},
		requests,
	});
	const terminal = countingTerminal(100, 30);
	const tui = new TuiAltScreen(terminal);
	const app = new App(tui, client);
	try {
		const pollCount = (): number =>
			requests.filter((request) => request.method === "nowplaying.get").length;
		const lastPollAt = (): number =>
			requests.filter((request) => request.method === "nowplaying.get").at(-1)?.at ?? 0;

		await app.start();
		await settle(t, 0); // first frame through the production render path

		// Initial playing header renders with a sound frame (fixture evidence).
		const initial = screen(tui);
		assert.match(initial[0] ?? "", /Night Drive/);
		assert.equal(frameIndex(initial[0]), 0, `initial playing frame: ${initial[0]}`);

		// +250 ms while idle: the coordinator drives a new frame into the
		// rendered output with no input.
		await settle(t, 250);
		const moved = screen(tui);
		assert.equal(frameIndex(moved[0]), 1, `one 250 ms step changes the sound frame: ${moved[0]}`);

		// Another full second: the interpolated clock advances (no input).
		await settle(t, 1000);
		const later = screen(tui);
		assert.match(later[0] ?? "", /0:13 \/ 3:30/, `elapsed advanced without input: ${later[0]}`);
		assert.notEqual(later[0], initial[0], "the music header changed");

		// Track change on the provider: the next scheduled poll publishes it
		// without a click or a /music command.
		current = reading({ title: "Second Track", elapsed: 5 });
		const pollsBefore = pollCount();
		await settle(t, NOWPLAYING_POLL_MS);
		const switched = screen(tui);
		assert.ok(pollCount() > pollsBefore, "the scheduled poll re-read the gateway");
		assert.match(switched[0] ?? "", /Second Track/, `the new title rendered: ${switched[0]}`);
		assert.match(switched[0] ?? "", /0:05 \/ 3:30/);
		assert.doesNotMatch(switched[0] ?? "", /Night Drive/);

		// Metadata polls never replace the live music subscription.
		const handleWhilePlaying = musicAnimHandle(app);
		assert.ok(handleWhilePlaying, "playing owns one music subscription");
		await settle(t, NOWPLAYING_POLL_MS);
		assert.equal(musicAnimHandle(app), handleWhilePlaying, "a later poll kept the same subscription");

		// Pause on the provider: paused state renders and the music subscription
		// retires, so no periodic render demand remains.
		current = reading({ title: "Second Track", state: "paused", elapsed: 65 });
		await settle(t, NOWPLAYING_POLL_MS);
		const paused = screen(tui);
		assert.match(paused[0] ?? "", /⏸ Second Track/, `paused state rendered: ${paused[0]}`);
		assert.equal(frameIndex(paused[0]), -1, "no playing sound frame while paused");
		assert.equal(musicAnimHandle(app), undefined, "pause retired the music subscription");

		// The pause poll just landed; a 250 ms window cannot contain another
		// poll (2 s cadence), so a new write here would be a stray music frame.
		assert.ok(
			Date.now() - lastPollAt() < NOWPLAYING_POLL_MS - 250,
			"the retirement window is poll-free",
		);
		await settle(t, 0); // drain any trailing scheduled frame
		const writesPaused = terminal.writes.length;
		await settle(t, 250);
		assert.equal(terminal.writes.length, writesPaused, "paused: no periodic music renders");

		// Paused -> playing resumes exactly one subscription (no duplicate).
		current = reading({ title: "Second Track", elapsed: 70 });
		await settle(t, NOWPLAYING_POLL_MS);
		const resumed = screen(tui);
		assert.ok(frameIndex(resumed[0]) >= 0, `motion resumed: ${resumed[0]}`);
		const resumedHandle = musicAnimHandle(app);
		assert.ok(resumedHandle, "resume subscribes the music animation again");
		await settle(t, NOWPLAYING_POLL_MS);
		assert.equal(musicAnimHandle(app), resumedHandle, "no duplicated handle across polls");

		// Detection failure: the normalized unavailable state notifies, the
		// stale playing animation retires, and the honest error renders.
		fail = true;
		await settle(t, NOWPLAYING_POLL_MS);
		const failed = screen(tui);
		assert.equal(musicAnimHandle(app), undefined, "a failed read retired the stale music animation");
		assert.equal(frameIndex(failed[0]), -1, "no stale sound frame after failure");
		assert.match(failed[0] ?? "", /source denied/, `honest failure rendered: ${failed[0]}`);

		// Exit: polling and render demand stop; resources are cleaned up.
		await app.exit();
		assert.equal(musicAnimHandle(app), undefined, "exit retired the music subscription");
		const writesAtExit = terminal.writes.length;
		const pollsAtExit = pollCount();
		await settle(t, 5000);
		assert.equal(pollCount(), pollsAtExit, "no polling after exit");
		assert.equal(terminal.writes.length, writesAtExit, "no renders after exit");
		assert.equal(client.stopped, true, "the gateway client was stopped");
	} finally {
		if (savedReduced === undefined) delete process.env.MACHINA_UI_REDUCED_MOTION;
		else process.env.MACHINA_UI_REDUCED_MOTION = savedReduced;
		await app.exit();
	}
});

test("real App: disable stops polling and retires the music handle without disposing the coordinator", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const savedReduced = process.env.MACHINA_UI_REDUCED_MOTION;
	delete process.env.MACHINA_UI_REDUCED_MOTION;

	const requests: Array<{ method: string; at: number }> = [];
	const client = scriptedClient({
		config: { theme: "codex", nowplaying_enabled: true },
		current: () => reading(),
		requests,
	});
	const terminal = countingTerminal(100, 30);
	const tui = new TuiAltScreen(terminal);
	const app = new App(tui, client);
	try {
		const pollCount = (): number =>
			requests.filter((request) => request.method === "nowplaying.get").length;

		await app.start();
		await settle(t, 0);
		assert.match(screen(tui)[0] ?? "", /Night Drive/);
		assert.ok(musicAnimHandle(app), "playing after boot");

		// Disable through the supported MusicHost surface.
		const controller = app.music();
		assert.ok(controller, "the music host exists after boot");
		await controller.setEnabled(false);
		await settle(t, 250);

		assert.equal(controller.isEnabled(), false);
		assert.doesNotMatch(
			screen(tui)[0] ?? "",
			/Night Drive|Nothing playing/,
			"the bar rows are gone while disabled",
		);
		assert.equal(musicAnimHandle(app), undefined, "disable retired the music subscription");
		const pollsDisabled = pollCount();
		await settle(t, NOWPLAYING_POLL_MS * 2);
		assert.equal(pollCount(), pollsDisabled, "disable stopped polling");

		// The shared coordinator survives: re-enabling resumes exactly one
		// live subscription (a disposed coordinator would hand back no-op
		// subscriptions and no frames would render again).
		const saved = await controller.setEnabled(true);
		await settle(t, 250);
		assert.equal(saved, true);
		assert.equal(controller.isEnabled(), true);
		assert.ok(musicAnimHandle(app), "re-enable resubscribed one music animation");
		assert.match(screen(tui)[0] ?? "", /Night Drive/);
		assert.ok(frameIndex(screen(tui)[0]) >= 0, "motion is live again after re-enable");

		await app.exit();
	} finally {
		if (savedReduced === undefined) delete process.env.MACHINA_UI_REDUCED_MOTION;
		else process.env.MACHINA_UI_REDUCED_MOTION = savedReduced;
		await app.exit();
	}
});

test("real App: reduced motion suppresses decorative frames but metadata still renders", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const savedReduced = process.env.MACHINA_UI_REDUCED_MOTION;
	// This is the reduced-motion case; the App reads the flag at construction.
	process.env.MACHINA_UI_REDUCED_MOTION = "1";

	let current = reading();
	const requests: Array<{ method: string; at: number }> = [];
	const client = scriptedClient({
		config: { theme: "codex", nowplaying_enabled: true },
		current: () => current,
		requests,
	});
	const terminal = countingTerminal(100, 30);
	const tui = new TuiAltScreen(terminal);
	const app = new App(tui, client);
	try {
		await app.start();
		await settle(t, 0);
		assert.match(screen(tui)[0] ?? "", /Night Drive/);
		assert.equal(frameIndex(screen(tui)[0]), 0, "reduced motion pins the first sound frame");

		// No decorative recurring frames: a 250 ms step alone changes nothing.
		await settle(t, 250);
		assert.equal(frameIndex(screen(tui)[0]), 0, "the decorative frame did not advance");

		// Section 12: the 2 s metadata poll must still request a render and
		// reach the screen even when decorative animation is suppressed.
		// Per-second clock interpolation under reduced motion is a separate
		// scheduler-policy follow-up and is deliberately not promised here.
		current = reading({ title: "Reduced Track" });
		await settle(t, NOWPLAYING_POLL_MS);
		assert.match(screen(tui)[0] ?? "", /Reduced Track/, "poll-driven metadata still renders");

		await app.exit();
	} finally {
		if (savedReduced === undefined) delete process.env.MACHINA_UI_REDUCED_MOTION;
		else process.env.MACHINA_UI_REDUCED_MOTION = savedReduced;
		await app.exit();
	}
});
