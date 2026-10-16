// /music command surface: fast paths, menu flow, honest failure modes over a
// scripted MusicHost double. No gateway, no detection.

import assert from "node:assert/strict";
import { test } from "node:test";
import { musicCommand } from "../src/features/music.ts";
import type { FeatureHost, MusicHost } from "../src/features/types.ts";
import type { NowPlayingState } from "../src/components/NowPlayingBar.ts";

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

class FakeMusic implements MusicHost {
	calls: string[] = [];
	enabled = true;
	saved = true;
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
		return this.saved;
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

function fakeHost(music: FakeMusic | undefined) {
	const printed: Array<{ text: string; kind: string }> = [];
	const picks: string[] = [];
	const host = {
		print: (text: string, kind = "info") => printed.push({ text, kind }),
		pick: async () => {
			const next = picks.shift();
			return next === undefined ? undefined : { value: next, label: next };
		},
		music: () => music,
	};
	return {
		host: host as unknown as FeatureHost,
		printed,
		picks: (items: string[]) => picks.push(...items),
	};
}

test("without a music host the command fails with one honest error", async () => {
	await assert.rejects(() => musicCommand.run("status", fakeHost(undefined).host), /Music is unavailable/);
});

test("fast paths drive the host and report what is playing", async () => {
	const music = new FakeMusic();
	const { host, printed } = fakeHost(music);
	await musicCommand.run("toggle", host);
	await musicCommand.run("next", host);
	await musicCommand.run("prev", host);
	await musicCommand.run("status", host);
	assert.deepEqual(music.calls, ["toggle", "next", "previous"]);
	assert.ok(printed.some((entry) => entry.text.includes("Aurora — Night Drive")));
	assert.ok(printed.some((entry) => entry.text.includes("[Google Chrome]")));
});

test("nothing playing and unavailable are distinct honest states", async () => {
	const idle = new FakeMusic();
	idle.current = reading({ state: null, title: null, artist: null, elapsed: null, duration: null });
	const idleHost = fakeHost(idle);
	await musicCommand.run("status", idleHost.host);
	assert.equal(idleHost.printed[0]!.text, "Nothing playing right now.");

	const broken = new FakeMusic();
	broken.current = reading({ available: false, title: null, error: "no now-playing source" });
	const brokenHost = fakeHost(broken);
	await musicCommand.run("status", brokenHost.host);
	assert.match(brokenHost.printed[0]!.text, /Now playing unavailable: no now-playing source/);
});

test("on/off persist and surface unsaved sessions honestly", async () => {
	const music = new FakeMusic();
	const { host, printed } = fakeHost(music);
	await musicCommand.run("off", host);
	assert.deepEqual(music.calls, ["setEnabled:false"]);
	music.saved = false;
	await musicCommand.run("on", host);
	assert.deepEqual(music.calls, ["setEnabled:false", "setEnabled:true"]);
	assert.ok(printed.some((entry) => entry.kind === "warning" && /could not save/.test(entry.text)));
});

test("unknown actions fail with the available list", async () => {
	await assert.rejects(
		() => musicCommand.run("waltz", fakeHost(new FakeMusic()).host),
		/Unknown music action "waltz"/,
	);
});

test("bare /music opens the menu; toggle flows through", async () => {
	const music = new FakeMusic();
	const { host, picks } = fakeHost(music);
	picks(["toggle"]);
	await musicCommand.run("", host);
	assert.deepEqual(music.calls, ["toggle"]);
});

test("the command is registered for autocomplete", async () => {
	const { SLASH_COMMANDS } = await import("../src/features/index.ts");
	assert.ok(SLASH_COMMANDS.some((command) => command.name === "music"));
});
