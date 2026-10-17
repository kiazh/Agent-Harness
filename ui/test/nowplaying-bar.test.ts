// Pinned now-playing bar rendering: rows by budget, width safety, clock
// formatting, animation frames (reduced-motion aware), elapsed interpolation,
// and click-to-toggle.

import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { NowPlayingBar, SOUND_FRAMES, formatClock } from "../src/components/NowPlayingBar.ts";
import type { NowPlayingState } from "../src/components/NowPlayingBar.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";

function state(overrides: Partial<NowPlayingState> = {}): NowPlayingState {
	return {
		available: true,
		state: "playing",
		title: "Example track title",
		artist: "Example artist",
		album: null,
		source: "Google Chrome",
		elapsed: 84,
		duration: 210,
		error: null,
		fetchedAtMs: Date.now(),
		...overrides,
	};
}

function bar(overrides: Partial<NowPlayingState> = {}, rows = 3, reduced = false): NowPlayingBar {
	const coordinator = new AnimationCoordinator(() => {}, reduced);
	const instance = new NowPlayingBar(coordinator);
	instance.state = state(overrides);
	instance.visible = true;
	instance.setBudget(rows);
	return instance;
}

function plainLines(instance: NowPlayingBar, width: number): string[] {
	return instance.render(width).map((line) => stripTerminalSequences(line));
}

test("formatClock formats minutes, hours, and clamps garbage", () => {
	assert.equal(formatClock(0), "0:00");
	assert.equal(formatClock(83), "1:23");
	assert.equal(formatClock(3600), "1:00:00");
	assert.equal(formatClock(-5), "0:00");
	assert.equal(formatClock(Number.NaN), "0:00");
});

test("disabled renders nothing; nothing playing shows one honest line", () => {
	const hidden = bar();
	hidden.visible = false;
	assert.deepEqual(hidden.render(90), []);
	const idle = bar({ state: null, title: null, artist: null, source: null, elapsed: null, duration: null });
	const rows = plainLines(idle, 90);
	assert.equal(rows.length, 1);
	assert.match(rows[0]!, /Nothing playing/);
	assert.deepEqual(bar({}, 0).render(90), []);
	assert.deepEqual(bar({}, 3).render(0), []);
});

test("three-row layout: title / artist · source / transport with progress", () => {
	const lines = plainLines(bar(), 90);
	assert.equal(lines.length, 3);
	assert.match(lines[0]!, /Example track title/);
	assert.match(lines[0]!, /1:24 \/ 3:30/);
	assert.match(lines[1]!, /Example artist · Google Chrome/);
	assert.match(lines[2]!, /1:24/);
	assert.match(lines[2]!, /3:30/);
	assert.match(lines[2]!, /▬+─*/);
});

test("two-row and one-row budgets stay informative", () => {
	const two = plainLines(bar({}, 2), 90);
	assert.equal(two.length, 2);
	assert.match(two[0]!, /Example track title/);
	const one = plainLines(bar({}, 1), 90);
	assert.equal(one.length, 1);
	assert.match(one[0]!, /Example track title — Example artist/);
});

test("a browser source without state or duration is still shown honestly", () => {
	const lines = plainLines(
		bar({ state: "unknown", duration: null, elapsed: null, source: "Google Chrome (front tab)" }, 3),
		90,
	);
	assert.match(lines[0]!, /Example track title/);
	assert.doesNotMatch(lines[0]!, /\d:\d\d \/ /, "no invented total");
	assert.match(lines[1]!, /Google Chrome \(front tab\)/);
});

test("an error line replaces the meta row while a title survives", () => {
	const lines = plainLines(bar({ error: "macOS blocked automation for Spotify" }, 3), 90);
	assert.match(lines[0]!, /Example track title/);
	assert.match(lines[1]!, /blocked automation/);
});

test("a denied Automation permission reads as needs-permission, never as Nothing playing", () => {
	const denied = {
		available: false,
		state: null,
		title: null,
		artist: null,
		album: null,
		source: null,
		elapsed: null,
		duration: null,
		error:
			"macOS blocked automation for Spotify — allow it in System Settings → Privacy & Security → Automation",
	};
	const full = plainLines(bar(denied, 3), 90);
	assert.equal(full.length, 1, "the denied state stays one honest line");
	assert.match(full[0]!, /Automation permission needed/);
	assert.match(full[0]!, /blocked automation for Spotify/);
	assert.doesNotMatch(full.join("\n"), /Nothing playing/);
	for (const rows of [1, 2]) {
		const lines = plainLines(bar(denied, rows), 90);
		assert.doesNotMatch(lines.join("\n"), /Nothing playing/);
		assert.match(lines.join("\n"), /automation/i);
	}
});

test("paused shows the paused glyph; the animation only runs while playing", () => {
	const paused = plainLines(bar({ state: "paused" }, 3), 90);
	assert.match(paused[0]!, /⏸/);
	const playing = bar();
	assert.ok(
		SOUND_FRAMES.some((frame) => plainLines(playing, 90)[0]!.includes(frame)),
		"animated frame while playing",
	);
	const reduced = bar({}, 3, true);
	assert.match(plainLines(reduced, 90)[0]!, new RegExp(SOUND_FRAMES[0]!));
});

test("elapsed interpolates from the fetch stamp while playing", () => {
	const instance = bar({ elapsed: 10, duration: 100, fetchedAtMs: Date.now() - 5000 });
	const lines = plainLines(instance, 90);
	assert.match(lines[0]!, /0:15 \/ 1:40/, "5s of playback added since the fetch");
	const pausedInstance = bar({
		state: "paused",
		elapsed: 10,
		duration: 100,
		fetchedAtMs: Date.now() - 5000,
	});
	assert.match(plainLines(pausedInstance, 90)[0]!, /0:10 \/ 1:40/, "paused time does not advance");
});

test("every line stays within any width 0–120", () => {
	for (const rows of [1, 2, 3]) {
		const instance = bar({}, rows);
		const long = bar({ title: "T".repeat(300), artist: "A".repeat(200), source: "S".repeat(200) }, rows);
		for (let width = 1; width <= 120; width++) {
			for (const line of [...instance.render(width), ...long.render(width)]) {
				assert.ok(visibleWidth(line) <= width, `rows=${rows} width=${width}`);
			}
		}
	}
});

test("a click sends toggle only when something is playing", () => {
	const instance = bar();
	let toggles = 0;
	instance.onToggle = () => {
		toggles += 1;
	};
	assert.equal(instance.handleMouse().handled, true);
	assert.equal(toggles, 1);
	instance.state = state({ title: null });
	assert.equal(instance.handleMouse().handled, false, "nothing to toggle");
	instance.visible = false;
	assert.equal(instance.handleMouse().handled, false);
	assert.equal(toggles, 1);
});
