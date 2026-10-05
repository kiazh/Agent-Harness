import assert from "node:assert/strict";
import { test } from "node:test";
import { type Component, Container, stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { helpText, parseCommand } from "../src/commands.ts";
import { Footer, formatArgs, ToolCard } from "../src/components.ts";
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

test("parseCommand recognizes commands, aliases and plain prompts", () => {
	assert.deepEqual(parseCommand("/new  My title "), { name: "new", args: "My title" });
	assert.deepEqual(parseCommand("/quit"), { name: "exit", args: "" });
	assert.deepEqual(parseCommand("/MODEL openai/gpt-4o"), { name: "model", args: "openai/gpt-4o" });
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

test("selected rows paint an accent background pill", async () => {
	const { selectListTheme } = await import("../src/theme.ts");
	const row = selectListTheme.selectedText("model");
	assert.match(stripTerminalSequences(row), /model/);
	assert.ok(row.includes("\x1b[48;2"), "selection carries a background fill");
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
	assert.match(stripTerminalSequences(composer.render(80)[0] ?? ""), /Ask AgentHarness to do anything/);
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
	const { header, logo } = await import("../src/components.ts");
	const out = plain(header("0.1.0", { model: "gpt-x", provider: "acme", cwd: "/repo", branch: "main" }), 80);
	assert.match(out, />_ AgentHarness \(v0\.1\.0\)/);
	assert.match(out, /\/repo \(main\)/);
	assert.doesNotMatch(out, /─/);
	const art = plain(logo(), 80);
	assert.match(art, /◆/);
	assert.doesNotMatch(art, /Ask AgentHarness/, "caption lives in the composer placeholder");
	assert.equal(logo().render(80)[0], "", "blank gap pushes the mark down");
	for (const width of [20, 40, 80, 120]) {
		for (const line of logo().render(width)) {
			assert.ok(visibleWidth(line) <= width, `line wider than ${width}`);
		}
	}
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
	for (const clone of ["tokyonight", "catppuccin", "dracula", "gruvbox", "rosepine", "nord", "everforest", "matrix"]) {
		assert.ok(SKIN_NAMES.includes(clone), `missing theme: ${clone}`);
		assert.match(skinDescription(clone), /\(dark\)/);
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
