// Review fixes for the pinned now-playing bar: one toggle per mouse gesture
// (press/release must not each toggle; wheel keeps scrolling the transcript),
// no overlapping 2 s polls while a detection is slow, directional /music
// play/pause that never flips the other way, and an honest disabled-bar
// status line that points at /music on.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { NowPlayingBar } from "../src/components/NowPlayingBar.ts";
import type { NowPlayingState } from "../src/components/NowPlayingBar.ts";
import { NOWPLAYING_POLL_MS, NowPlayingController } from "../src/nowplaying/controller.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";
import { musicCommand } from "../src/features/music.ts";
import type { FeatureHost, MusicHost } from "../src/features/types.ts";

function reading(overrides: Partial<NowPlayingState> = {}): NowPlayingState {
	return {
		available: true,
		state: "playing",
		title: "Night Drive",
		artist: "Aurora",
		album: null,
		source: "Google Chrome",
		elapsed: 12,
		duration: 210,
		error: null,
		fetchedAtMs: Date.now(),
		...overrides,
	};
}

function mouseEvent(type: TuiMouseEvent["type"], button: TuiMouseEvent["button"]): TuiMouseEvent {
	return {
		type,
		button,
		x: 0,
		y: 0,
		screenX: 4,
		screenY: 1,
		width: 80,
		height: 1,
		shift: false,
		alt: false,
		ctrl: false,
	};
}

test("one click gesture toggles once; wheel and other buttons fall through", () => {
	const instance = new NowPlayingBar(new AnimationCoordinator(() => {}));
	instance.state = reading();
	instance.visible = true;
	instance.setBudget(1);
	let toggles = 0;
	instance.onToggle = () => {
		toggles += 1;
	};
	// The shell dispatches press, release, then the synthesized click to the
	// same target: only the completed left click may toggle.
	assert.equal(instance.handleMouse(mouseEvent("press", "left")).handled, false);
	assert.equal(instance.handleMouse(mouseEvent("release", "left")).handled, false);
	assert.equal(toggles, 0, "no toggle before the click is synthesized");
	assert.equal(instance.handleMouse(mouseEvent("click", "left")).handled, true);
	assert.equal(toggles, 1, "exactly one toggle per gesture");
	// Wheel must fall through so the transcript under the bar keeps scrolling.
	assert.equal(instance.handleMouse(mouseEvent("wheel", "none")).handled, false);
	assert.equal(instance.handleMouse(mouseEvent("drag", "left")).handled, false);
	assert.equal(instance.handleMouse(mouseEvent("click", "right")).handled, false);
	assert.equal(toggles, 1);
	// The legacy direct call (no event) still toggles a visible playing bar.
	assert.equal(instance.handleMouse().handled, true);
	assert.equal(toggles, 2);
	// A hidden bar or one with nothing playing stays inert.
	instance.state = reading({ title: null });
	assert.equal(instance.handleMouse(mouseEvent("click", "left")).handled, false);
	instance.visible = false;
	instance.state = reading();
	assert.equal(instance.handleMouse(mouseEvent("click", "left")).handled, false);
	assert.equal(toggles, 2);
});

test("the 2 s poll skips ticks while a slow detection is in flight", async (t) => {
	t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
	const bar = new NowPlayingBar(new AnimationCoordinator(() => {}));
	bar.setBudget(1);
	const pending: Array<(value: unknown) => void> = [];
	const controller = new NowPlayingController({
		bar,
		request: async <T,>() =>
			new Promise((resolve) => {
				pending.push(resolve as (value: unknown) => void);
			}) as Promise<T>,
	});
	const starting = controller.initialize({ nowplaying_enabled: true });
	await Promise.resolve();
	assert.equal(pending.length, 1, "startup read");
	pending[0]!(reading({ elapsed: 10 }));
	await starting;
	assert.equal(bar.state?.elapsed, 10);
	// Real macrotask drain: mock timers leave setImmediate alone, so this
	// flushes every microtask an awaited request resolution spawns.
	const drain = () => new Promise<void>((resolve) => setImmediate(resolve));

	const seconds = (): number => {
		const line = stripTerminalSequences(bar.render(90)[0] ?? "");
		const match = /(\d+):(\d\d)/.exec(line);
		return match ? Number(match[1]) * 60 + Number(match[2]) : Number.NaN;
	};

	// t=2 s: the second read starts and stays slow.
	t.mock.timers.tick(NOWPLAYING_POLL_MS);
	assert.equal(pending.length, 2);
	const before = seconds();
	// t=4 s: its next tick is skipped instead of stacking an overlapping read.
	t.mock.timers.tick(NOWPLAYING_POLL_MS);
	assert.equal(pending.length, 2, "no second read while the slow one is in flight");
	const during = seconds();
	// The slow read settles with the true newer reading.
	pending[1]!(reading({ elapsed: 30 }));
	await drain();
	assert.equal(bar.state?.elapsed, 30, "the slow reading lands when it settles");
	const after = seconds();
	assert.ok(
		after > during && during >= before,
		`the interpolated clock never steps backwards: ${before} -> ${during} -> ${after}`,
	);
	// t=6 s: the guard releases and the tick polls again.
	t.mock.timers.tick(NOWPLAYING_POLL_MS);
	assert.equal(pending.length, 3, "the next tick polls once the slow read settled");
	pending[2]!(reading({ elapsed: 40 }));
	await drain();
	assert.equal(bar.state?.elapsed, 40);
	controller.stop();
});

test("the disabled gate still wins over the poll tick (no fetch)", async (t) => {
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
	assert.equal(fetches, afterDisable, "the disabled gate still wins over the tick");
	controller.stop();
});

class FakeMusic implements MusicHost {
	calls: string[] = [];
	enabled = true;
	current: NowPlayingState | undefined = reading();

	status() {
		return this.current;
	}
	isEnabled() {
		return this.enabled;
	}
	async setEnabled(enabled: boolean) {
		this.calls.push(`setEnabled:${enabled}`);
		this.enabled = enabled;
		return true;
	}
	async refresh() {
		this.calls.push("refresh");
	}
	async toggle() {
		this.calls.push("toggle");
		this.current = reading({ state: this.current?.state === "playing" ? "paused" : "playing" });
	}
	async next() {
		this.calls.push("next");
	}
	async previous() {
		this.calls.push("previous");
	}
}

function fakeHost(music: FakeMusic): { host: FeatureHost; printed: Array<{ text: string; kind: string }> } {
	const printed: Array<{ text: string; kind: string }> = [];
	const host = {
		print: (text: string, kind = "info") => printed.push({ text, kind }),
		pick: async () => undefined,
		music: () => music,
	};
	return { host: host as unknown as FeatureHost, printed };
}

test("/music pause never starts playback; play never pauses it", async () => {
	const music = new FakeMusic();
	const { host, printed } = fakeHost(music);
	music.current = reading({ state: "paused" });
	await musicCommand.run("pause", host);
	assert.deepEqual(music.calls, [], "pause on a paused track must not toggle");
	assert.match(printed.at(-1)!.text, /Already paused/);
	await musicCommand.run("play", host);
	assert.deepEqual(music.calls, ["toggle"], "play resumes while paused");
	await musicCommand.run("play", host);
	assert.deepEqual(music.calls, ["toggle"], "play while playing is a no-op");
	assert.match(printed.at(-1)!.text, /Already playing/);
	await musicCommand.run("pause", host);
	assert.deepEqual(music.calls, ["toggle", "toggle"], "pause toggles while playing");

	// Nothing detected at all: pause reports it instead of toggling blind.
	music.current = reading({ state: null, title: null, artist: null, elapsed: null, duration: null });
	await musicCommand.run("pause", host);
	assert.deepEqual(music.calls, ["toggle", "toggle"]);
	assert.match(printed.at(-1)!.text, /nothing to pause/);
	music.current = reading({ state: "paused" });
	await musicCommand.run("toggle", host);
	assert.deepEqual(music.calls, ["toggle", "toggle", "toggle"], "toggle stays unconditional");
});

test("the disabled bar says so and points at /music on", async () => {
	const music = new FakeMusic();
	music.enabled = false;
	music.current = undefined;
	const { host, printed } = fakeHost(music);
	await musicCommand.run("status", host);
	assert.equal(printed[0]!.text, "Music bar is off (/music on)");
	// Enabled but not yet fetched keeps the neutral message.
	music.enabled = true;
	await musicCommand.run("status", host);
	assert.equal(printed[1]!.text, "Now playing: not checked yet.");
});

test("a denied Automation permission is reported as needs-permission, not Nothing playing", async () => {
	const music = new FakeMusic();
	music.current = reading({
		available: false,
		state: null,
		title: null,
		artist: null,
		source: null,
		elapsed: null,
		duration: null,
		error:
			"macOS blocked automation for Spotify — allow it in System Settings → Privacy & Security → Automation",
	});
	const { host, printed } = fakeHost(music);
	await musicCommand.run("status", host);
	assert.match(printed.at(-1)!.text, /Automation permission needed/);
	assert.doesNotMatch(printed.at(-1)!.text, /Nothing playing/);
});
