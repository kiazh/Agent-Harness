import assert from "node:assert/strict";
import { test } from "node:test";
import { CURSOR_MARKER, stripTerminalSequences, TuiAltScreen, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../src/app.ts";
import { TextForm } from "../src/components.ts";
import type { GatewayClient } from "../src/gateway.ts";
import { editorTheme } from "../src/theme.ts";

function terminal(columns = 80, rows = 24) {
	let input: (data: string) => void = () => {};
	const term: Terminal = {
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
	return { app, tui, type, requests };
}

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
