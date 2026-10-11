import assert from "node:assert/strict";
import { test } from "node:test";
import { type Component, Container, stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { helpText, parseCommand } from "../src/commands.ts";
import { Footer, formatArgs, Notice, ToolCard, UserMessage } from "../src/components.ts";
import { SLASH_COMMANDS } from "../src/features/index.ts";
import type { GatewayEvent } from "../src/protocol.ts";
import { Transcript } from "../src/transcript.ts";

class FakeSpinner implements Component {
	running = false;
	start(): void {
		this.running = true;
	}
	stop(): void {
		this.running = false;
	}
	render(): string[] {
		return ["(spinner)"];
	}
	invalidate(): void {}
}

const ev = (e: Record<string, unknown>) => ({ sessionId: "s", turnId: "t", ...e }) as GatewayEvent;

function plain(component: Component, width = 60): string {
	return component
		.render(width)
		.map((line) => stripTerminalSequences(line))
		.join("\n");
}

test("learning square stays at the right edge, pulses briefly and coalesces frequent updates", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const footer = new Footer();
	footer.reducedMotion = false;
	let renders = 0;
	const render = () => renders++;
	t.after(() => footer.dispose());
	const idle = plain(footer, 80);
	assert.match(idle, /□$/);
	assert.equal(footer.pulseGrowth(render), true);
	const initial = plain(footer, 80);
	assert.notEqual(initial, idle);
	t.mock.timers.tick(200);
	const advanced = plain(footer, 80);
	assert.notEqual(advanced, initial);
	assert.equal(footer.pulseGrowth(render), false);
	assert.equal(plain(footer, 80), advanced, "a burst never restarts the animation");
	for (const width of [1, 2, 20, 40, 80, 120]) {
		const lines = footer.render(width);
		assert.equal(lines.length, 2);
		assert.equal(visibleWidth(lines[1]!), width);
		assert.match(stripTerminalSequences(lines[1]!), /[□▪■▣]$/);
		assert.ok(lines.every(line => visibleWidth(line) <= width));
	}
	t.mock.timers.tick(1000);
	assert.equal(plain(footer, 80), idle);
	const settled = renders;
	t.mock.timers.tick(20000);
	assert.equal(renders, settled, "no background rendering while idle");
	assert.equal(footer.pulseGrowth(render), false);
	t.mock.timers.tick(10000);
	assert.equal(footer.pulseGrowth(render), true);
	footer.dispose();
	const stopped = renders;
	t.mock.timers.tick(2000);
	assert.equal(renders, stopped);
});

test("reduced motion keeps learning feedback static and clears it without animation", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const footer = new Footer();
	footer.reducedMotion = true;
	t.after(() => footer.dispose());
	footer.pulseGrowth(() => {});
	const initial = plain(footer, 40);
	t.mock.timers.tick(400);
	assert.equal(plain(footer, 40), initial);
	t.mock.timers.tick(1000);
	assert.match(plain(footer, 40), /□$/);
});

test("parseCommand recognizes commands, aliases and plain prompts", () => {
	assert.deepEqual(parseCommand("/new  My title "), { name: "new", args: "My title" });
	assert.deepEqual(parseCommand("/quit"), { name: "exit", args: "" });
	assert.deepEqual(parseCommand("/MODEL openai/gpt-4o"), { name: "default-model", args: "openai/gpt-4o" });
	assert.equal(parseCommand("explain /etc/hosts"), undefined);
	assert.equal(parseCommand("//not a command"), undefined);
	assert.equal(parseCommand("/"), undefined);
});

test("help lists every command", () => {
	const help = helpText(SLASH_COMMANDS);
	for (const command of SLASH_COMMANDS) assert.match(help, new RegExp(`/${command.name}\\b`));
});

test("a streamed turn renders text, a tool card and a final summary", () => {
	const view = new Container();
	const spinner = new FakeSpinner();
	const transcript = new Transcript(view, spinner);

	transcript.addUser("read a.py");
	transcript.apply(ev({ type: "message.start" }));
	assert.ok(spinner.running, "spinner shows while waiting");

	transcript.apply(ev({ type: "message.delta", text: "Let me " }));
	transcript.apply(ev({ type: "message.delta", text: "look." }));
	assert.ok(!spinner.running, "spinner hides once text arrives");

	transcript.apply(ev({ type: "tool.start", id: "1", name: "read_file", args: { path: "a.py" } }));
	transcript.apply(ev({ type: "tool.complete", id: "1", name: "read_file", result: "print('hi')", isError: false }));
	transcript.apply(ev({ type: "message.delta", text: "It prints hi." }));
	const summary = transcript.apply(
		ev({ type: "message.complete", text: "It prints hi.", tokens: 120, iterations: 2, toolCalls: 1, cancelled: false }),
	);

	assert.deepEqual(summary, { tokens: 120, cancelled: false });
	const out = plain(view);
	assert.match(out, /read a\.py/);
	assert.match(out, /Let me look\./);
	assert.match(out, /✓ Ran read_file path=a\.py/);
	assert.match(out, /print\('hi'\)/);
	assert.match(out, /It prints hi\./);
	assert.equal(out.match(/It prints hi\./g)?.length, 1, "final text is not duplicated");
	assert.doesNotMatch(out, /\(spinner\)/);
});

test("a withheld redaction tail reconciles exactly once (AH-AUDIT-027)", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(ev({ type: "message.start" }));
	transcript.apply(ev({ type: "message.delta", text: "Hello " }));
	// The canonical final extends the displayed deltas by the flushed tail.
	const summary = transcript.apply(
		ev({ type: "message.complete", text: "Hello world", tokens: 5, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.deepEqual(summary, { tokens: 5, cancelled: false });
	const out = plain(view);
	assert.match(out, /Hello world/);
	assert.equal(out.match(/Hello /g)?.length, 1, "tail appended exactly once, never duplicated");
});

test("message.stopping is non-terminal (AH-AUDIT-028)", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(ev({ type: "message.start" }));
	const summary = transcript.apply(ev({ type: "message.stopping", reason: "cancelled" }));
	assert.equal(summary, undefined, "stopping emits no terminal summary");
	assert.match(plain(view), /Stopping/);
});

test("a reply with no streamed text still shows the final answer", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(ev({ type: "message.start" }));
	transcript.apply(
		ev({ type: "message.complete", text: "LLM provider error: 401", tokens: 0, iterations: 1, toolCalls: 0, cancelled: false }),
	);
	assert.match(plain(view), /LLM provider error: 401/);
});

test("cancelling marks unfinished tools and says so", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(ev({ type: "message.start" }));
	transcript.apply(ev({ type: "tool.start", id: "1", name: "terminal", args: { command: "pytest" } }));
	const summary = transcript.apply(
		ev({ type: "message.complete", text: "", tokens: 0, iterations: 0, toolCalls: 0, cancelled: true }),
	);
	assert.equal(summary?.cancelled, true);
	const out = plain(view);
	assert.match(out, /✗ Failed terminal/);
	assert.match(out, /Stopped\./);
});

test("a normal completion does not mark unfinished tools as interrupted", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.apply(ev({ type: "message.start" }));
	transcript.apply(ev({ type: "tool.start", id: "1", name: "terminal", args: { command: "pytest" } }));
	const summary = transcript.apply(
		ev({ type: "message.complete", text: "", tokens: 10, iterations: 1, toolCalls: 1, cancelled: false }),
	);
	assert.equal(summary?.cancelled, false);
	const out = plain(view);
	assert.doesNotMatch(out, /\(interrupted\)/, "unfinished tool is not marked interrupted on normal completion");
	assert.doesNotMatch(out, /Stopped\./, "no cancellation notice on normal completion");
});

test("replay renders stored history", () => {
	const view = new Container();
	const transcript = new Transcript(view, new FakeSpinner());
	transcript.replay([
		{ role: "user", content: "hello" },
		{ role: "tool", tool: "web_search", content: "3 results" },
		{ role: "assistant", content: "**Hi** there" },
	]);
	const out = plain(view);
	assert.match(out, /hello/);
	assert.match(out, /web_search/);
	assert.match(out, /Hi there/);
});

test("every widget respects the render width", async () => {
	const { Picker, header } = await import("../src/components.ts");
	const card = new ToolCard("search_files", { pattern: "x".repeat(200) });
	card.complete(`${"long line ".repeat(40)}\n${Array.from({ length: 20 }, (_, i) => `row ${i}`).join("\n")}`, false);
	const footer = new Footer();
	Object.assign(footer, { cwd: "C:\\Users\\someone\\a\\very\\long\\path\\to\\a\\project", branch: "main", model: "anthropic/claude-3.5-sonnet", tokens: 123456 });
	const picker = new Picker("Sessions", [{ value: "a", label: "session-a" }, { value: "b", label: "session-b" }], () => {}, () => {});

	for (const width of [20, 40, 80, 120]) {
		for (const line of [...card.render(width), ...footer.render(width), ...picker.render(width), ...header("0.1.0").render(width)]) {
			assert.ok(visibleWidth(line) <= width, `line wider than ${width}: ${stripTerminalSequences(line)}`);
		}
	}
	assert.match(plain(card, 80), /more lines/, "long results are truncated");
	assert.match(plain(footer, 120), /123,456 tokens/);
	assert.match(plain(picker, 80), /Sessions/);
	assert.doesNotMatch(
		picker.render(80).map((line) => stripTerminalSequences(line)).join("\n"),
		/[╭╮╰╯│]/,
		"menu is borderless",
	);
});

test("formatArgs summarizes arguments", () => {
	assert.equal(formatArgs({ path: "a.py", limit: 20, flags: ["-n"] }), 'path=a.py, limit=20, flags=["-n"]');
});

test("assistant output renders boxed and respects width", async () => {
	const { AssistantMessage, header } = await import("../src/components.ts");
	const message = new AssistantMessage("hello **world**");
	message.append(" more");
	for (const width of [20, 40, 80, 120]) {
		for (const line of message.render(width)) {
			assert.ok(visibleWidth(line) <= width, `line wider than ${width}`);
		}
	}
	assert.match(plain(message, 80), /hello/);
	const banner = plain(header("0.1.0"), 80);
	assert.match(banner, />_ AgentHarness \(v0\.1\.0\)/);
	assert.doesNotMatch(banner, /─/);
});

test("transcript blocks leave a margin row so box corners never touch", async () => {
	const { AssistantMessage, Notice, ToolCard, UserMessage } = await import("../src/components.ts");
	const blocks = [
		new UserMessage("hello"),
		new AssistantMessage("hi **there**"),
		new Notice("a note", "plain"),
		new ToolCard("read_file", { path: "a.py" }),
	];
	for (const block of blocks) {
		const lines = block.render(80).map((line) => stripTerminalSequences(line));
		assert.equal(lines[0], "", `${block.constructor.name} opens with a blank margin row`);
		assert.ok(lines.length > 1, `${block.constructor.name} still renders content after the margin`);
	}
	const card = new ToolCard("search_files", { pattern: "x" });
	card.complete("ok", false);
	for (const line of card.render(40)) assert.ok(visibleWidth(line) <= 40, "margin row respects width");
});

test("codex-style diff coloring marks additions and deletions", async () => {
	const { diffStyle } = await import("../src/components.ts");
	assert.match(diffStyle("+ added"), /added/);
	assert.match(diffStyle("- removed"), /removed/);
	assert.match(diffStyle("@@ -1 +1 @@"), /@@/);
	assert.equal(stripTerminalSequences(diffStyle("+ added")), "+ added");
	assert.notEqual(diffStyle("+ added"), "+ added", "additions carry color");
	assert.notEqual(diffStyle("- removed"), "- removed", "deletions carry color");
	assert.equal(stripTerminalSequences(diffStyle("  context")), "  context");
	assert.equal(diffStyle("  context").length > 0, true);
});

test("key hints join bold labels with dot separators", async () => {
	const { hintLine } = await import("../src/format.ts");
	const out = hintLine([
		["Enter", "send"],
		["Esc", "stop"],
	]);
	assert.match(out, /Enter/);
	assert.match(out, /send/);
	assert.match(out, /·/);
	assert.equal(stripTerminalSequences(out), "Enter send · Esc stop");
});

test("fill repair keeps background after nested resets", async () => {
	const { repairFill } = await import("../src/theme.ts");
	// Simulate a Box-filled line containing a nested pill (bg open … reset).
	const nested = `ab\x1b[48;2;250;178;131mCD\x1b[49m  `;
	const filled = `\x1b[48;2;10;10;10m${nested}\x1b[49m`;
	const out = repairFill("userBg", filled);
	assert.equal(stripTerminalSequences(out), stripTerminalSequences(filled));
	const opens = out.split("\x1b[48;2;").length - 1;
	assert.ok(opens >= 2, "fill background is re-asserted after the nested reset");
	assert.ok(out.endsWith("\x1b[49m"));
});

test("selected rows paint an accent background pill", async () => {
	const { selectListTheme } = await import("../src/theme.ts");
	const row = selectListTheme.selectedText("model");
	assert.match(stripTerminalSequences(row), /model/);
	assert.ok(row.includes("\x1b[48;2;") || row.includes("\x1b[48;5;"), "selection carries a background fill in the terminal's color mode");
});

test("selection bars recognize and replace truecolor and indexed backgrounds", async () => {
	const { isSelectionRow, selectionBar } = await import("../src/theme.ts");
	for (const fill of ["\x1b[48;2;1;2;3m", "\x1b[48;5;123m"]) {
		const row = `${fill}\x1b[38;5;7malpha\x1b[49m`;
		assert.equal(isSelectionRow(row), true);
		const bar = selectionBar(row, 40);
		assert.equal(visibleWidth(bar), 40);
		assert.equal(stripTerminalSequences(bar), `alpha${" ".repeat(35)}`);
		assert.ok(!bar.includes(fill), "the old partial background is removed before repainting");
		assert.ok(bar.includes("\x1b[38;5;7m"), "foreground styling survives");
	}
	assert.equal(isSelectionRow("\x1b[38;5;123malpha"), false);
});

test("picker menu is a docked codex-style list with a full-width bar", async () => {
	const { Picker } = await import("../src/components.ts");
	const picker = new Picker("Models", [{ value: "a", label: "alpha" }, { value: "b", label: "beta-longer" }], () => {}, () => {});
	const lines = picker.render(40).map((line) => stripTerminalSequences(line));
	assert.match(lines[0] ?? "", /Models/);
	assert.match(lines.at(-1) ?? "", /enter select · esc back/);
	const bar = lines.find((line) => line.includes("alpha")) ?? "";
	assert.equal(visibleWidth(bar), 40, "selected row spans the full width");
	assert.match(lines.join("\n"), /beta-longer/);
});

test("slash menu opens above the input", async () => {
	const { ComposerBox, popAbove } = await import("../src/components.ts");
	assert.deepEqual(popAbove(["t", "b", "p1", "p2"], 2), ["p1", "p2", "t", "b"]);
	assert.deepEqual(popAbove(["t", "b"], 0), ["t", "b"]);
	assert.deepEqual(popAbove(["t"], 5), ["t"]);

	const { editorTheme } = await import("../src/theme.ts");
	const tui = { terminal: { rows: 40 }, requestRender() {}, invalidate() {} } as never;
	const box = new ComposerBox(tui, editorTheme, { paddingX: 1 });
	box.editor.setAutocompleteProvider({
		triggerCharacters: ["/"],
		getSuggestions: async () => ({
			items: [
				{ value: "help", label: "help", description: "Show help" },
				{ value: "new", label: "new", description: "New session" },
			],
			prefix: "/",
		}),
		applyCompletion: (lines: string[]) => ({ lines, cursorLine: 0, cursorCol: 1 }),
	} as never);
	box.editor.handleInput("/");
	let popupRow = -1;
	let inputRow = -1;
	for (let i = 0; i < 30; i++) {
		await new Promise((r) => setTimeout(r, 100));
		const rows = box.editor.render(60).map((line) => stripTerminalSequences(line));
		popupRow = rows.findIndex((line) => line.includes("Show help"));
		inputRow = rows.findIndex((line) => line.startsWith(" /"));
		if (popupRow !== -1 && inputRow !== -1) break;
	}
	assert.ok(popupRow !== -1, "menu opened");
	assert.ok(inputRow !== -1, "input rendered");
	assert.ok(popupRow < inputRow, "menu renders above the input");
	const press = {
		type: "press", button: "left", x: 5, y: popupRow,
		screenX: 5, screenY: popupRow, width: 60, height: 10,
		shift: false, alt: false, ctrl: false,
	} as never;
	const res = box.handleMouse(press);
	assert.ok(res && res.handled, "menu click is handled");
});

test("codex skin ships with the clones", async () => {
	const { SKIN_NAMES, skinDescription } = await import("../src/theme.ts");
	assert.ok(SKIN_NAMES.includes("codex"));
	assert.match(skinDescription("codex"), /codex/i);
});

test("composer is a filled codex-style block with no rules", async () => {
	const { ComposerBox } = await import("../src/components.ts");
	const { editorTheme } = await import("../src/theme.ts");
	const composer = new ComposerBox({ terminal: { rows: 40 } } as never, editorTheme);
	assert.equal(stripTerminalSequences(composer.editor.renderTopBorder(40, 0)), "");
	assert.equal(stripTerminalSequences(composer.editor.renderBottomBorder(40, 0)), "");
	assert.match(stripTerminalSequences(composer.editor.renderTopBorder(40, 3)), /↑ 3 more/);
	for (const width of [20, 40, 80]) {
		for (const line of composer.render(width)) {
			assert.ok(visibleWidth(line) <= width, `line wider than ${width}`);
		}
		assert.doesNotMatch(
			composer.render(width).map((line) => stripTerminalSequences(line)).join("\n"),
			/─/,
			"no border rules in the filled composer",
		);
	}
	assert.match(stripTerminalSequences(composer.render(80)[1] ?? ""), /Ask AgentHarness to do anything/);
	for (const line of composer.render(80)) {
		assert.match(stripTerminalSequences(line), /^┃ /);
	}
	assert.equal(composer.handleMouse({ x: 0, y: 0 } as never), undefined);
	composer.editor.setText("hello");
	assert.doesNotMatch(
		composer.render(80).map((line) => stripTerminalSequences(line)).join("\n"),
		/Ask AgentHarness/,
		"placeholder hides once typing starts",
	);
});

test("session header is a minimal codex-style brand line", async () => {
	const { header } = await import("../src/components.ts");
	const out = plain(header("0.1.0", { model: "gpt-x", provider: "acme", cwd: "/repo", branch: "main" }), 80);
	assert.match(out, />_ AgentHarness \(v0\.1\.0\)/);
	assert.match(out, /\/repo \(main\)/);
	assert.doesNotMatch(out, /─/);
});

test("tool cards use codex running/ran/failed titles", async () => {
	const { ToolCard } = await import("../src/components.ts");
	const running = new ToolCard("terminal", { command: "pytest" });
	assert.match(plain(running, 60), /Running terminal/);
	running.complete("ok", false);
	assert.match(plain(running, 60), /Ran terminal/);
	const failed = new ToolCard("terminal", { command: "pytest" });
	failed.complete("boom", true);
	assert.match(plain(failed, 60), /Failed terminal/);
});

test("skins switch palette and art, unknown names throw", async () => {
	const { SKIN_NAMES, getSkin, setSkin, skinArt, skinDescription } = await import("../src/theme.ts");
	assert.ok(SKIN_NAMES.includes("default") && SKIN_NAMES.length >= 5);
	for (const clone of ["tokyonight", "catppuccin", "dracula", "gruvbox", "rosepine", "nord", "everforest", "matrix", "onedark", "monokai", "github", "solarized", "kanagawa", "palenight", "catppuccin-frappe", "catppuccin-macchiato", "cobalt2", "ayu", "nightowl", "one-dark", "flexoki"]) {
		assert.ok(SKIN_NAMES.includes(clone), `missing theme: ${clone}`);
		assert.ok(skinDescription(clone).length > 0, `${clone} has a description`);
		assert.doesNotMatch(skinDescription(clone), /cloned exactly/);
	}
	const before = getSkin();
	try {
		for (const name of SKIN_NAMES) {
			setSkin(name);
			assert.equal(getSkin(), name);
			assert.ok(skinArt(name).length > 0);
		}
		assert.throws(() => setSkin("nope"), /Unknown skin/);
	} finally {
		setSkin(before);
	}
});

test("transcript blocks re-skin live without losing content", async () => {
	const { getSkin, setSkin } = await import("../src/theme.ts");
	const before = getSkin();
	try {
		const note = new Notice("all systems go", "success");
		const user = new UserMessage("hello");
		const firstPaint = [...note.render(80), ...user.render(80)];
		setSkin("tokyonight");
		note.refreshSkin();
		user.refreshSkin();
		const secondPaint = [...note.render(80), ...user.render(80)];
		assert.deepEqual(
			secondPaint.map((line) => stripTerminalSequences(line)),
			firstPaint.map((line) => stripTerminalSequences(line)),
			"text survives the skin switch",
		);
		const before = note.render(80).map((line) => stripTerminalSequences(line));
		note.refreshSkin();
		assert.deepEqual(
			note.render(80).map((line) => stripTerminalSequences(line)),
			before,
			"re-styling is idempotent (no doubled markers)",
		);
	} finally {
		setSkin(before);
	}
});

test("transcript restyle keeps every block", async () => {
	const container = new Container();
	const transcript = new Transcript(container, new FakeSpinner());
	transcript.addUser("hello");
	transcript.addNotice("working", "plain");
	const count = container.children.length;
	transcript.restyle();
	assert.equal(container.children.length, count, "restyle drops nothing");
});

test("every skin keeps its accent roles distinct", async () => {
	const { SKIN_NAMES, skinBase } = await import("../src/theme.ts");
	for (const name of SKIN_NAMES) {
		const base = skinBase(name);
		assert.ok(base, `${name} has a palette`);
		const roles = [base.accent, base.secondary, base.success, base.warning, base.error, base.info].map((h) =>
			h.toLowerCase(),
		);
		assert.equal(new Set(roles).size, roles.length, `${name} reuses a color across roles`);
	}
});
