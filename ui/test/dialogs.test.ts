import assert from "node:assert/strict";
import { test } from "node:test";
import { CURSOR_MARKER, stripTerminalSequences, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { TextForm } from "../src/components.ts";
import { Menu } from "../src/components/Menu.ts";
import { SecretForm } from "../src/components/Form.ts";
import { parseCommand } from "../src/commands.ts";
import { runCommand } from "../src/features/index.ts";
import type { GatewayClient } from "../src/gateway.ts";
import { editorTheme } from "../src/theme.ts";

function terminal(columns = 80, rows = 24) {
	let input: (data: string) => void = () => {};
	const term: Terminal & { columns: number; rows: number } = {
		columns, rows, kittyProtocolActive: false,
		start(onInput) { input = onInput; }, stop() {}, async drainInput() {},
		write() {}, moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	return { term, type: (data: string) => input(data) };
}

async function boot(columns = 80, rows = 24) {
	const { term, type } = terminal(columns, rows);
	const tui = new TuiAltScreen(term);
	const requests: string[] = [];
	const client = {
		start() {}, async stop() {}, stderr: () => [],
		async request(method: string) {
			requests.push(method);
			if (method === "initialize") return { version: "test", model: "m", cwd: "/tmp" };
			if (method === "config.get") return { config: {} };
			if (method === "session.create") return { session: { id: "s", title: "test", model: "m", provider: "p", status: "active", lastActivity: null } };
			return {};
		},
	};
	const app = new App(tui, client as unknown as GatewayClient);
	await app.start();
	return { app, tui, type, requests, client, term };
}

test("? opens shortcuts on an empty composer and Esc restores input", async (t) => {
	const { app, tui, type, requests } = await boot();
	t.after(() => app.exit());
	type("?");
	tui.renderNow(true);
	const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.match(screen, /Keyboard shortcuts/);
	assert.match(screen, /Shift\+Enter/);
	assert.doesNotMatch(screen, /Ask anything, or type \/ for commands\.\?/);
	type("\x1b");
	tui.renderNow(true);
	assert.doesNotMatch(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Keyboard shortcuts/);
	type("Is this working?");
	type("\r");
	await new Promise(resolve => setImmediate(resolve));
	assert.equal(requests.filter(method => method === "prompt.submit").length, 1);
});

test("F1 opens shortcuts without erasing a draft and ? stays text inside forms", async (t) => {
	const { app, tui, type } = await boot();
	t.after(() => app.exit());
	type("A question?");
	type("\x1bOP");
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Keyboard shortcuts/);
	type("\x1b");
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /A question\?/);
	const form = app.askText({ title: "Persona", initial: "Warm" });
	type("?");
	type("\r");
	assert.equal(await form, "Warm?");
});

for (const key of ["\x1b[63u", "\x1b[63;2u", "\x1b[47;2u"]) {
	test(`question key ${JSON.stringify(key)} opens shortcuts with enhanced keyboard protocols`, async (t) => {
		const { app, tui, type } = await boot();
		t.after(() => app.exit());
		type(key);
		tui.renderNow(true);
		assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Keyboard shortcuts/);
	});
}

test("shortcuts keep their close controls visible in a small terminal", async (t) => {
	const { app, tui, type } = await boot(40, 9);
	t.after(() => app.exit());
	type("?");
	tui.renderNow(true);
	const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.match(screen, /Keyboard shortcuts/);
	assert.match(screen, /enter\/esc close/);
	type("\x1b");
});

test("closing an interrupting picker restores the text dialog and saves without submitting a prompt", async (t) => {
	const { app, tui, type, requests } = await boot();
	t.after(() => app.exit());
	const form = app.askText({ title: "Persona", initial: "draft" });
	const selection = app.pick("Approval", [{ value: "yes", label: "Allow once" }]);
	type("\r");
	assert.equal((await selection)?.value, "yes");
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Persona/);
	type(" updated");
	type("\r");
	await Promise.resolve();
	assert.equal(requests.filter((method) => method === "prompt.submit").length, 0);
	assert.equal(await form, "draft updated");
});

test("Ctrl+C after an interrupting picker cancels the restored form instead of exiting", async (t) => {
	const { app, type } = await boot();
	t.after(() => app.exit());
	const form = app.askText({ title: "Persona" });
	const choice = app.pick("Approval", [{ value: "yes", label: "Allow once" }]);
	type("\r");
	await choice;
	let exited = false;
	void app.exited.then(() => { exited = true; });
	type("\x03");
	await Promise.resolve();
	await Promise.resolve();
	assert.equal(exited, false, "the restored dialog owns cancellation keys");
	assert.equal(await form, undefined);
});

test("cancelling nested text dialogs restores the underlying dialog", async (t) => {
	const { app, type } = await boot();
	t.after(() => app.exit());
	const first = app.askText({ title: "First", initial: "retained" });
	const second = app.askText({ title: "Second" });
	type("\x1b");
	assert.equal(await second, undefined);
	type("\x13");
	assert.equal(await first, "retained");
});

test("exit settles outstanding dialogs and prevents new overlays", async () => {
	const { app, tui } = await boot();
	const form = app.askText({ title: "Draft" });
	const picker = app.pick("Choice", [{ value: "a", label: "A" }]);
	let pending = 2;
	void form.then(() => pending--);
	void picker.then(() => pending--);
	await app.exit();
	assert.equal(pending, 0, "dialog promises settle on exit");
	assert.equal(tui.hasOverlay(), false);
	assert.equal(await app.askText({ title: "After exit" }), undefined);
	assert.equal(await app.pick("After exit", []), undefined);
});

test("text forms save and count the expanded contents of a large paste with either shortcut", () => {
	const { term } = terminal();
	const tui = new TuiAltScreen(term);
	const text = "😀 persona detail\n".repeat(12).trimEnd();
	try {
		for (const key of ["\x13", "\r"]) {
			const form = new TextForm(tui, editorTheme, { title: "Persona" });
			let saved = "";
			form.onSubmit = (value) => { saved = value; };
			form.handleInput(`\x1b[200~${text}\x1b[201~`);
			assert.match(form.editor.getText(), /\[paste #/);
			assert.equal(form.getValue(), text);
			assert.match(form.render(80).map(stripTerminalSequences).join("\n"), new RegExp(`${Array.from(text).length} characters`));
			form.handleInput(key);
			assert.equal(saved, text);
		}
	} finally {
		tui.stop();
	}
});

for (const rows of [9, 10, 12]) {
	test(`a text form keeps the cursor, count, and save/cancel controls visible in a 40x${rows} terminal`, async (t) => {
		const { app, tui, type } = await boot(40, rows);
		t.after(() => app.exit());
		const form = app.askText({
			title: "Persona", description: "Edit the instructions used for all future conversations. Keep specific preferences here.",
			example: "Respond briefly, and include examples when requested.",
			initial: Array.from({ length: 20 }, (_, i) => `instruction ${i}`).join("\n"),
		});
		tui.renderNow(true);
		const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
		assert.match(screen, /instruction 19/);
		assert.match(screen, /characters/);
		assert.match(screen, /enter save/);
		assert.match(screen, /esc cancel/);
		type("\x13");
		assert.match((await form)!, /instruction 19$/);
	});
}

test("a nine-row text form reserves space for validation errors and controls", () => {
	const { term } = terminal(40, 9);
	const tui = new TuiAltScreen(term);
	try {
		const form = new TextForm(tui, editorTheme, { title: "Persona", initial: "draft" });
		form.setError("Shorten your instructions");
		tui.start();
		tui.showOverlay(form, { width: "100%", maxHeight: "70%", anchor: "bottom-center", margin: { bottom: 5 } });
		tui.renderNow(true);
		const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
		assert.match(screen, /draft/);
		assert.match(screen, /5 characters/);
		assert.match(screen, /Shorten your instructions/);
		assert.match(screen, /enter save/);
		assert.match(screen, /esc cancel/);
		assert.equal(form.render(40).length, 4);
		assert.ok(form.render(40).some((line) => line.includes(CURSOR_MARKER)));
	} finally {
		tui.stop();
	}
});

test("the form's bounded editor keeps its cursor visible at the start and end of long text", () => {
	const { term } = terminal(40, 12);
	const tui = new TuiAltScreen(term);
	try {
		const form = new TextForm(tui, editorTheme, { title: "Persona", initial: "line\n".repeat(20) });
		form.focused = true;
		for (const key of ["\x1b[1;5H", "\x1b[1;5F"]) {
			form.handleInput(key);
			const rows = form.render(40);
			assert.ok(rows.length <= 7, "form fits the available sheet height above the composer");
			assert.ok(rows.some((line) => line.includes(CURSOR_MARKER)), "cursor survives height budgeting");
		}
	} finally {
		tui.stop();
	}
});

test("output viewer opens over a draft and Esc returns to the composer", async (t) => {
	const { app, tui, type, client } = await boot();
	t.after(() => app.exit());
	await app.submitTurn("do work");
	const emit = (event: Record<string, unknown>) =>
		(client as unknown as { onEvent?: (e: never) => void }).onEvent?.({
			sessionId: "s",
			turnId: "t",
			...event,
		} as never);
	emit({ type: "message.start" });
	emit({ type: "tool.start", id: "abcd1234", name: "terminal", args: { command: "ls" } });
	emit({ type: "tool.complete", id: "abcd1234", name: "terminal", result: "a\nb\nc", isError: false });
	emit({ type: "message.complete", text: "done", tokens: 1, iterations: 1, toolCalls: 1, cancelled: false });
	type("my draft");
	const viewing = app.inspectOutput("abcd1234");
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Output.*terminal/);
	type("\x1b");
	await viewing;
	tui.renderNow(true);
	const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.doesNotMatch(screen, /Output.*terminal/, "closing returns to the composer");
	assert.match(screen, /my draft/, "the chat draft survives inspection");
});

test("output viewer reports unknown ids without opening", async (t) => {
	const { app, tui } = await boot();
	t.after(() => app.exit());
	await app.inspectOutput("nope");
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /No retained output/);
});

test("bare output with nothing retained stays local and quiet", async (t) => {
	const { app, tui, requests } = await boot();
	t.after(() => app.exit());
	const before = requests.length;
	await app.inspectOutput();
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /No retained tool output/);
	assert.equal(requests.length, before, "inspection never hits the gateway");
});

test("I05 menus filter across label, value and description with honest empty states", () => {
	const items = [
		{ value: "alpha", label: "Alpha session", description: "first" },
		{ value: "beta", label: "Beta session", description: "second" },
		{ value: "gamma", label: "Gamma", description: "third" },
	];
	const selected: string[] = [];
	let cancelled = 0;
	const menu = new Menu("Sessions", items, {});
	menu.onSelect = (item) => void selected.push(item.value);
	menu.onCancel = () => void cancelled++;
	assert.deepEqual(menu.filtered().map((i) => i.value), ["alpha", "beta", "gamma"], "empty filter shows all");
	menu.handleInput("BETA");
	assert.deepEqual(menu.filtered().map((i) => i.value), ["beta"], "case-insensitive label match");
	for (let i = 0; i < "BETA".length; i++) menu.handleInput("\x7f");
	assert.deepEqual(menu.filtered().length, 3, "backspace edits the filter");
	menu.handleInput("second");
	assert.deepEqual(menu.filtered().map((i) => i.value), ["beta"], "description match");
	menu.handleInput("\r");
	assert.deepEqual(selected, ["beta"], "Enter selects the value, not the filter text");
	const rows = menu.render(80).map((line) => stripTerminalSequences(line));
	assert.ok(rows.some((line) => line.includes("❯") && line.includes("Beta session")), "selected row carries a non-color marker");
	const empty = new Menu("Sessions", items, {});
	empty.onCancel = () => void cancelled++;
	empty.handleInput("zzz");
	assert.equal(empty.filtered().length, 0);
	assert.match(empty.render(80).map(stripTerminalSequences).join("\n"), /no matches/);
	empty.handleInput("\r");
	assert.equal(selected.length, 1, "Enter on no match selects nothing");
	empty.handleInput("\x1b");
	assert.equal(cancelled, 1, "Esc still cancels from an empty result");
});

test("I05 menu selection survives budget changes and arrows move it", () => {
	const items = Array.from({ length: 12 }, (_, i) => ({ value: `v${i}`, label: `Item ${i}` }));
	const menu = new Menu("Pick", items, { visibleRows: 10 });
	menu.handleInput("Item 1");
	assert.ok(menu.filtered().length >= 3, "prefix narrows across labels");
	menu.handleInput("\x1b[B");
	menu.handleInput("\x1b[B");
	const before = menu.getSelectedItem()?.value;
	menu.setVisibleRows(3);
	assert.equal(menu.getSelectedItem()?.value, before, "budget change preserves the selected value");
	const narrow = menu.render(40).map((line) => stripTerminalSequences(line));
	assert.ok(narrow.some((line) => line.includes(`Item ${before?.slice(1)}`)), "primary labels carry actions at 40 columns");
});

test("I06 secret values stay masked through edit, save, error and cancel", () => {
	const { term } = terminal(80, 24);
	const tui = new TuiAltScreen(term);
	const sentinel = "SENTINEL-hunter2-value";
	try {
		const saved: string[] = [];
		let cancelled = 0;
		const form = new SecretForm(tui, { title: "Set K" });
		form.onSubmit = (value) => void saved.push(value);
		form.onCancel = () => void cancelled++;
		for (const ch of sentinel) form.handleInput(ch);
		const frames = form.render(80).map((line) => stripTerminalSequences(line)).join("\n");
		assert.ok(!frames.includes(sentinel), "raw secret never rendered");
		assert.ok(!frames.includes(sentinel.slice(0, 8)), "no secret prefix leaks either");
		assert.match(frames, /•+/, "masked dots stand in for the value");
		form.handleInput("\x7f");
		assert.equal(form.getValue(), sentinel.slice(0, -1), "backspace edits the real value");
		form.handleInput("\r");
		form.handleInput("\r");
		assert.deepEqual(saved, [sentinel.slice(0, -1)], "double Enter saves exactly once");
		assert.equal(form.getValue(), "", "references cleared after submit");
		const blank = new SecretForm(tui, { title: "Set K" });
		let blankSaved = 0;
		blank.onSubmit = () => void blankSaved++;
		blank.handleInput("\r");
		assert.equal(blankSaved, 0, "blank rejects");
		assert.match(blank.render(80).map(stripTerminalSequences).join("\n"), /Value is required/);
		const cancelForm = new SecretForm(tui, { title: "Set K" });
		cancelForm.onCancel = () => void cancelled++;
		cancelForm.handleInput("x");
		cancelForm.handleInput("\x1b");
		assert.equal(cancelled, 1);
		assert.equal(cancelForm.getValue(), "", "references cleared after cancel");
	} finally {
		tui.stop();
	}
});

test("I07 text forms submit once while pending and unlock on error", () => {
	const { term } = terminal(80, 24);
	const tui = new TuiAltScreen(term);
	try {
		const form = new TextForm(tui, editorTheme, { title: "Persona" });
		const saved: string[] = [];
		form.onSubmit = (value) => void saved.push(value);
		form.editor.setText("hello");
		form.handleInput("\x13");
		form.handleInput("\r");
		form.handleInput("\x13");
		assert.deepEqual(saved, ["hello"], "repeated save keys settle once");
		form.setError("Backend rejected it");
		form.handleInput("\x13");
		assert.deepEqual(saved, ["hello", "hello"], "error unlocks the form for retry with the draft intact");
		assert.equal(form.getValue(), "hello", "draft preserved on failure");
	} finally {
		tui.stop();
	}
});

test("V07 pickers and forms survive a live resize with state intact", async (t) => {
	const { app, tui, type, term } = await boot(80, 24);
	t.after(() => app.exit());
	const choice = app.pick("Sessions", [
		{ value: "a", label: "Alpha" },
		{ value: "b", label: "Beta" },
	]);
	term.rows = 9;
	term.columns = 40;
	tui.renderNow(true);
	let screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.match(screen, /Sessions/);
	assert.match(screen, /Alpha/, "selected window keeps state after resize");
	assert.match(screen, /esc back/, "cancel hint survives short windows");
	type("Bet");
	tui.renderNow(true);
	screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.match(screen, /Beta/);
	assert.doesNotMatch(screen, /Alpha/, "filter narrows after resize");
	type("\r");
	assert.equal((await choice)?.value, "b", "selection works in the resized menu");
	const form = app.askText({ title: "Persona", initial: "keep me" });
	term.rows = 9;
	tui.renderNow(true);
	screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.match(screen, /keep me/);
	assert.match(screen, /characters/);
	assert.match(screen, /esc cancel/);
	type("\x13");
	assert.equal(await form, "keep me", "save works after resize");
});

test("I03 approval over a form denies, closes, and restores the draft", async (t) => {
	const { app, tui, type, requests, client } = await boot();
	t.after(() => app.exit());
	await app.submitTurn("do work");
	const emit = (event: Record<string, unknown>) =>
		(client as unknown as { onEvent?: (e: never) => void }).onEvent?.({
			sessionId: "s",
			turnId: "t",
			...event,
		} as never);
	emit({ type: "message.start" });
	const form = app.askText({ title: "Persona", initial: "draft" });
	emit({
		type: "needs_approval",
		requestId: "req-9",
		operation: "terminal",
		target: "host",
		argv: ["rm", "-rf", "/tmp/x"],
		cwd: "/tmp",
		backend: "host",
		capabilities: ["exec"],
	});
	tui.renderNow(true);
	assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Allow terminal · host\?/);
	type("\x1b");
	for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
	tui.renderNow(true);
	const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
	assert.doesNotMatch(screen, /Allow terminal · host\?/, "denial settles only its own dialog");
	assert.match(screen, /draft/, "underlying form draft restored");
	const resolves = requests.filter((method) => method === "approvals.resolve");
	assert.equal(resolves.length, 1, "exactly one deliberate decision");
	type(" updated");
	type("\x13");
	assert.equal(await form, "draft updated", "form saves after the interruption");
	assert.equal(requests.filter((method) => method === "prompt.submit").length, 1, "no accidental prompt submission");
});

test("H02 theme preview commits on Enter and restores on Esc", async (t) => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const { app, tui, type } = await boot();
	t.after(() => app.exit());
	const before = getSkin();
	setSkin("mono");
	try {
		const picking = app.pickTheme();
		tui.renderNow(true);
		assert.match(tui.getScreenLines().map(stripTerminalSequences).join("\n"), /Skins/);
		type("\x1b[B");
		tui.renderNow(true);
		assert.notEqual(getSkin(), "mono", "moving the selection previews immediately");
		type("\x1b");
		assert.equal(await picking, undefined);
		assert.equal(getSkin(), "mono", "cancel restores the original theme");
		const confirm = app.pickTheme();
		tui.renderNow(true);
		type("\r");
		assert.equal(await confirm, "mono", "Enter commits the selected ID");
		assert.equal(getSkin(), "mono");
	} finally {
		setSkin(before);
	}
});

test("H03 theme change mid-stream preserves content, draft, scroll and subs", async (t) => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const { app, tui, type, client } = await boot();
	t.after(() => app.exit());
	const before = getSkin();
	setSkin("codex");
	try {
		await app.submitTurn("mid-stream work");
		(client as unknown as { onEvent?: (e: never) => void }).onEvent?.({
			sessionId: "s",
			turnId: "t",
			type: "message.start",
		} as never);
		type("draft here");
		const timers = (app as unknown as { coordinator: { timerCount: number } }).coordinator.timerCount;
		assert.equal(timers, 1, "thinking subscribes while streaming starts");
		await runCommand(parseCommand("/theme dracula")!, app);
		tui.renderNow(true);
		const screen = tui.getScreenLines().map(stripTerminalSequences).join("\n");
		assert.match(screen, /Thinking/, "active turn presentation survives the switch");
		assert.match(screen, /draft here/, "composer draft survives the switch");
		assert.match(screen, /Skin: dracula/, "persistence confirmed");
		assert.equal(
			(app as unknown as { coordinator: { timerCount: number } }).coordinator.timerCount,
			1,
			"no duplicated subscriptions after switching",
		);
	} finally {
		setSkin(before);
	}
});
