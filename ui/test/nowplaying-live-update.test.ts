// Live-update contract for the pinned bar (diagnose regression).
//
// The user-facing bug: the bar never refreshed — the poll only requested
// re-renders of a stale snapshot, so state and the elapsed clock froze until
// a command was issued. These tests advance fake timers and assert a real
// re-fetch happens on schedule, that changed readings reach the bar, and
// that the rendered clock moves while playing (interpolation between polls).

import assert from "node:assert/strict";
import { test } from "node:test";
import { NowPlayingBar } from "../src/components/NowPlayingBar.ts";
import { NowPlayingController, NOWPLAYING_POLL_MS } from "../src/nowplaying/controller.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";
import { stripTerminalSequences } from "@earendil-works/pi-tui";

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

test("the controller re-fetches on a real interval and publishes new readings", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	let fetches = 0;
	let payload = reading();
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() => {
			fetches += 1;
			return payload as T;
		},
	});
	try {
		await controller.initialize({ nowplaying_enabled: true });
		assert.equal(fetches, 1, "one fetch at startup");

		t.mock.timers.tick(NOWPLAYING_POLL_MS);
		await Promise.resolve();
		t.mock.timers.tick(NOWPLAYING_POLL_MS);
		await Promise.resolve();
		assert.ok(fetches >= 3, `scheduled re-fetches, saw ${fetches}`);

		// A changed reading reaches the bar without any user action.
		payload = reading({ title: "Different Song", state: "paused" });
		t.mock.timers.tick(NOWPLAYING_POLL_MS);
		await Promise.resolve();
		assert.equal(bar.state?.title, "Different Song");
		assert.equal(bar.state?.state, "paused");
	} finally {
		controller.stop();
	}
});

test("stop() and disabling halt the polling", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	let fetches = 0;
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() => {
			fetches += 1;
			return reading() as T;
		},
	});
	await controller.initialize({ nowplaying_enabled: true });
	await controller.setEnabled(false);
	const afterDisable = fetches;
	t.mock.timers.tick(NOWPLAYING_POLL_MS * 3);
	await Promise.resolve();
	assert.equal(fetches, afterDisable, "no fetches while disabled");
	controller.stop();
	const afterStop = fetches;
	t.mock.timers.tick(NOWPLAYING_POLL_MS * 3);
	await Promise.resolve();
	assert.equal(fetches, afterStop, "no fetches after stop");
});

test("a scheduled poll notifies the constructor callback without input", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const coordinator = new AnimationCoordinator(() => {});
	const bar = new NowPlayingBar(coordinator);
	let payload = reading();
	let notifications = 0;
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() => payload as T,
		onChange: () => {
			notifications += 1;
		},
	});
	// Real macrotask drain: mock timers leave setImmediate alone, so awaited
	// request resolutions settle deterministically without real sleeps.
	const drain = () => new Promise<void>((resolve) => setImmediate(resolve));
	try {
		await controller.initialize({ nowplaying_enabled: true });
		assert.equal(notifications, 1, "the startup read notified once");

		// The gateway changes its answer; no click, no command, no agent turn.
		payload = reading({ title: "Different Song" });
		t.mock.timers.tick(NOWPLAYING_POLL_MS);
		await drain();
		t.mock.timers.tick(NOWPLAYING_POLL_MS);
		await drain();
		assert.equal(bar.state?.title, "Different Song", "the scheduled read reached the bar");
		assert.ok(notifications >= 2, `the scheduled polls notified App (saw ${notifications})`);
	} finally {
		controller.stop();
		coordinator.dispose();
	}
});

test("the rendered clock advances between polls while playing (interpolation)", (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	bar.visible = true;
	bar.setBudget(1);
	bar.state = {
		available: true,
		state: "playing",
		title: "Night Drive",
		artist: "Aurora",
		album: null,
		source: "Spotify",
		elapsed: 60,
		duration: 210,
		error: null,
		fetchedAtMs: Date.now(),
	};
	const first = stripTerminalSequences(bar.render(90)[0]!);
	t.mock.timers.tick(5000);
	const second = stripTerminalSequences(bar.render(90)[0]!);
	assert.match(first, /1:00/, `first frame shows the fetched elapsed: ${first}`);
	assert.match(second, /1:05/, `clock moved with wall time: ${second}`);
});
