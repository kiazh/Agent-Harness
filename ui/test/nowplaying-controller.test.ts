// Now-playing controller: gateway polling, honest failure normalization,
// command→refresh (never guesses state), config-gated visibility.

import assert from "node:assert/strict";
import { test } from "node:test";
import { NowPlayingBar } from "../src/components/NowPlayingBar.ts";
import { NOWPLAYING_POLL_MS, NowPlayingController } from "../src/nowplaying/controller.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";

function makeController(payloads: Record<string, unknown | (() => unknown)> = {}) {
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
	const controller = new NowPlayingController({
		bar,
		request: async <T,>(method: string, params: Record<string, unknown> = {}) => {
			calls.push({ method, params });
			const payload = payloads[method];
			if (payload === undefined) throw new Error(`unexpected request ${method}`);
			return (typeof payload === "function" ? (payload as () => unknown)() : payload) as T;
		},
	});
	return { controller, bar, calls };
}

const PLAYING = {
	available: true,
	state: "playing",
	title: "Night Drive",
	artist: "Aurora",
	album: null,
	source: "Google Chrome",
	elapsed: 12,
	duration: 210,
	error: null,
};

test("initialize reads config, fetches once, and shows the bar", async () => {
	const { controller, bar, calls } = makeController({ "nowplaying.get": PLAYING });
	await controller.initialize({ nowplaying_enabled: true });
	assert.equal(bar.visible, true);
	assert.equal(bar.state?.title, "Night Drive");
	assert.equal(bar.state?.state, "playing");
	assert.deepEqual(calls.map((call) => call.method), ["nowplaying.get"]);
});

test("disabled config skips the fetch and hides the bar", async () => {
	const { controller, bar, calls } = makeController({});
	await controller.initialize({ nowplaying_enabled: false });
	assert.equal(bar.visible, false);
	assert.deepEqual(calls, []);
	assert.equal(controller.isEnabled(), false);
});

test("refresh failures degrade to an honest unavailable reading", async () => {
	const { controller, bar } = makeController({
		"nowplaying.get": () => {
			throw new Error("gateway offline");
		},
	});
	await controller.initialize({ nowplaying_enabled: true });
	assert.equal(bar.state?.available, false);
	assert.match(bar.state?.error ?? "", /gateway offline/);
});

test("commands are followed by a re-read, never a guessed state", async () => {
	let reading: Record<string, unknown> = { ...PLAYING, state: "playing" };
	const { controller, calls } = makeController({
		"nowplaying.get": () => reading,
		"nowplaying.command": () => {
			reading = { ...PLAYING, state: "paused" };
			return { ok: true };
		},
	});
	await controller.initialize({ nowplaying_enabled: true });
	await controller.toggle();
	assert.deepEqual(
		calls.map((call) => call.method),
		["nowplaying.get", "nowplaying.command", "nowplaying.get"],
	);
	assert.equal(controller.status()?.state, "paused", "state comes from the re-read");
});

test("nothing playing is a valid reading (no error, no title)", async () => {
	const { controller, bar } = makeController({
		"nowplaying.get": { available: true, state: null, title: null, error: null },
	});
	await controller.initialize({ nowplaying_enabled: true });
	assert.equal(bar.visible, true, "the bar stays visible with an honest empty line");
	assert.equal(bar.state?.title, null);
	assert.equal(bar.state?.error, null);
});

test("setEnabled(false) hides and drops state; setEnabled(true) polls again", async () => {
	const { controller, bar, calls } = makeController({
		"nowplaying.get": PLAYING,
		"config.set": { ok: true },
	});
	await controller.initialize({ nowplaying_enabled: true });
	const saved = await controller.setEnabled(false);
	assert.equal(saved, true);
	assert.equal(bar.visible, false);
	const dropped = bar.state;
	assert.ok(dropped === undefined, "state dropped while hidden");
	const enabled = await controller.setEnabled(true);
	assert.equal(enabled, true);
	assert.equal(bar.visible, true);
	assert.equal(bar.state?.title, "Night Drive");
	const configSet = calls.find((call) => call.method === "config.set");
	assert.deepEqual(configSet?.params, { key: "nowplaying_enabled", value: false, persist: true });
});

test("a failed config save still applies the session behavior", async () => {
	const { controller, bar } = makeController({
		"nowplaying.get": PLAYING,
		"config.set": () => {
			throw new Error("save failed");
		},
	});
	await controller.initialize({ nowplaying_enabled: true });
	const saved = await controller.setEnabled(false);
	assert.equal(saved, false);
	assert.equal(bar.visible, false);
});

test("a refresh that lands after disabling is dropped (stale state cannot return)", async () => {
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	let resolvePending: ((value: unknown) => void) | undefined;
	const controller = new NowPlayingController({
		bar,
		request: async <T,>(method: string) => {
			if (method === "config.set") return { ok: true } as T;
			return new Promise((resolve) => {
				resolvePending = resolve as (value: unknown) => void;
			}) as Promise<T>;
		},
	});
	const inFlight = controller.refresh();
	await controller.setEnabled(false);
	resolvePending?.({
		available: true,
		state: "playing",
		title: "Zombie Song",
		error: null,
	});
	await inFlight;
	assert.ok(bar.state === undefined, "the late reading must not resurface after disabling");
	assert.equal(bar.visible, false);
});

test("constructor onChange is called after accepted readings", async () => {
	const coordinator = new AnimationCoordinator(() => {});
	const bar = new NowPlayingBar(coordinator);
	let payload: Record<string, unknown> = { ...PLAYING };
	let notifications = 0;
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() => payload as T,
		onChange: () => {
			notifications += 1;
		},
	});
	try {
		// App supplies onChange through the constructor (startNowPlaying);
		// post-assignment (controller.onChange = …) would bypass the defect.
		await controller.initialize({ nowplaying_enabled: true });
		assert.equal(notifications, 1, "initial accepted reading notifies once");

		payload = { ...PLAYING, title: "Second Track" };
		await controller.refresh();
		assert.equal(notifications, 2, "the later accepted reading notifies again");
		assert.equal(bar.state?.title, "Second Track");
	} finally {
		controller.stop();
		coordinator.dispose();
	}
});

test("omitting onChange stays supported and causes no exception", async () => {
	const coordinator = new AnimationCoordinator(() => {});
	const bar = new NowPlayingBar(coordinator);
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() => PLAYING as T,
	});
	try {
		await controller.initialize({ nowplaying_enabled: true });
		await controller.refresh();
		assert.equal(bar.state?.title, "Night Drive");
	} finally {
		controller.stop();
		coordinator.dispose();
	}
});

test("a refresh landing after stop() neither updates state nor notifies", async () => {
	const coordinator = new AnimationCoordinator(() => {});
	const bar = new NowPlayingBar(coordinator);
	let resolvePending: ((value: unknown) => void) | undefined;
	let notifications = 0;
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() =>
			new Promise((resolve) => {
				resolvePending = resolve as (value: unknown) => void;
			}) as Promise<T>,
		onChange: () => {
			notifications += 1;
		},
	});
	try {
		const inFlight = controller.refresh();
		controller.stop();
		resolvePending?.({ ...PLAYING, title: "Late Track" });
		await inFlight;
		assert.ok(bar.state === undefined, "the late reading is discarded");
		assert.equal(notifications, 0, "and it never notifies after exit");
	} finally {
		controller.stop();
		coordinator.dispose();
	}
});

test("a failed read notifies with the normalized unavailable state", async () => {
	const coordinator = new AnimationCoordinator(() => {});
	const bar = new NowPlayingBar(coordinator);
	let fail = false;
	let notifications = 0;
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() => {
			if (fail) throw new Error("detector crashed");
			return PLAYING as T;
		},
		onChange: () => {
			notifications += 1;
		},
	});
	try {
		await controller.initialize({ nowplaying_enabled: true });
		assert.equal(notifications, 1, "the playing reading notified");
		fail = true;
		await controller.refresh();
		assert.equal(notifications, 2, "a degraded read still notifies App");
		assert.equal(bar.state?.available, false);
		assert.match(bar.state?.error ?? "", /detector crashed/);
	} finally {
		controller.stop();
		coordinator.dispose();
	}
});

test("an older overlapping poll never overwrites a newer reading", async () => {
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	const pending: Array<(value: unknown) => void> = [];
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() =>
			new Promise((resolve) => {
				pending.push(resolve as (value: unknown) => void);
			}) as Promise<T>,
	});
	// initialize awaits its first poll; resolve it so the controller starts.
	const starting = controller.initialize({ nowplaying_enabled: true });
	await Promise.resolve();
	assert.equal(pending.length, 1);
	pending[0]!({ available: true, state: "playing", title: "Initial", error: null });
	await starting;
	// Two more polls overlap on top of the settled first one.
	const second = controller.refresh();
	const third = controller.refresh();
	assert.equal(pending.length, 3);
	pending[2]!({
		available: true,
		state: "playing",
		title: "Newest",
		error: null,
	});
	await third;
	assert.equal(bar.state?.title, "Newest");
	pending[1]!({
		available: true,
		state: "playing",
		title: "Older",
		error: null,
	});
	await second;
	assert.equal(bar.state?.title, "Newest", "older response dropped");
	pending[0]?.({
		available: true,
		state: "playing",
		title: "Oldest",
		error: null,
	});
	controller.stop();
});

/** Resolve mock-timer microtasks without advancing the mocked clock. */
async function drainMicrotasks(): Promise<void> {
	for (let index = 0; index < 8; index++) await Promise.resolve();
}

test("MCH-006 a stale off resolving after a newer on cannot stop or clear the enabled bar", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	bar.setBudget(1);
	let fetches = 0;
	const writes: Array<(value: unknown) => void> = [];
	const controller = new NowPlayingController({
		bar,
		request: async <T,>(method: string) => {
			if (method === "nowplaying.get") {
				fetches += 1;
				return PLAYING as T;
			}
			if (method === "config.set") {
				return new Promise((resolve) => {
					writes.push(resolve as (value: unknown) => void);
				}) as Promise<T>;
			}
			throw new Error(`unexpected request ${method}`);
		},
	});
	await controller.initialize({ nowplaying_enabled: true });
	const before = fetches;

	const off = controller.setEnabled(false);
	const on = controller.setEnabled(true);
	assert.equal(writes.length, 2, "both transitions issued their persistence write");
	// The newer transition (on) settles first; the stale off settles last.
	writes[1]!({ ok: true });
	await on;
	writes[0]!({ ok: true });
	await off;
	await drainMicrotasks();

	assert.equal(controller.isEnabled(), true, "the newest intent is enabled");
	assert.equal(bar.visible, true);
	assert.ok(bar.state, "the stale off did not clear the reading");
	// Polling is still live for the newest transition.
	t.mock.timers.tick(NOWPLAYING_POLL_MS);
	await drainMicrotasks();
	assert.ok(fetches > before + 1, "the poll timer survived the stale off");
	// The stale write was compensated so the persisted gate agrees with the UI.
	assert.ok(writes.length >= 3, "compensation issued for the clobbered write");
	writes.at(-1)!({ ok: true });
	await drainMicrotasks();
	controller.stop();
});

test("MCH-006 a stale on resolving after a newer off cannot resurrect visibility or polling", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	bar.setBudget(1);
	let fetches = 0;
	const writes: Array<(value: unknown) => void> = [];
	const controller = new NowPlayingController({
		bar,
		request: async <T,>(method: string) => {
			if (method === "nowplaying.get") {
				fetches += 1;
				return PLAYING as T;
			}
			if (method === "config.set") {
				return new Promise((resolve) => {
					writes.push(resolve as (value: unknown) => void);
				}) as Promise<T>;
			}
			throw new Error(`unexpected request ${method}`);
		},
	});
	await controller.initialize({ nowplaying_enabled: true });

	const on = controller.setEnabled(true);
	const off = controller.setEnabled(false);
	assert.equal(writes.length, 2);
	// The newer transition (off) settles first; the stale on settles last.
	writes[1]!({ ok: true });
	await off;
	writes[0]!({ ok: true });
	await on;
	await drainMicrotasks();

	assert.equal(controller.isEnabled(), false, "the newest intent is disabled");
	assert.equal(bar.visible, false, "the stale on never re-showed the bar");
	assert.equal(bar.state, undefined, "the stale on never republished a reading");
	const before = fetches;
	t.mock.timers.tick(NOWPLAYING_POLL_MS * 3);
	await drainMicrotasks();
	assert.equal(fetches, before, "polling stayed stopped");
	assert.ok(writes.length >= 3, "the stale write was compensated with the newest value");
	writes.at(-1)!({ ok: true });
	await drainMicrotasks();
});
