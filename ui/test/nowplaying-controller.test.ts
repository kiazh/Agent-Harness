// Now-playing controller: gateway polling, honest failure normalization,
// command→refresh (never guesses state), config-gated visibility.

import assert from "node:assert/strict";
import { test } from "node:test";
import { NowPlayingBar } from "../src/components/NowPlayingBar.ts";
import { NowPlayingController } from "../src/nowplaying/controller.ts";
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
