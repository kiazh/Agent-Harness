import assert from "node:assert/strict";
import { test } from "node:test";
import { type Component, Container, stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { helpText, parseCommand } from "../src/commands.ts";
import { Footer, formatArgs, ToolCard } from "../src/components.ts";
import { SLASH_COMMANDS } from "../src/features.ts";
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
	assert.match(out, /✓ read_file path=a\.py/);
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
	assert.match(out, /✗ terminal/);
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

test("every widget respects the render width", () => {
	const card = new ToolCard("search_files", { pattern: "x".repeat(200) });
	card.complete(`${"long line ".repeat(40)}\n${Array.from({ length: 20 }, (_, i) => `row ${i}`).join("\n")}`, false);
	const footer = new Footer();
	Object.assign(footer, { cwd: "C:\\Users\\someone\\a\\very\\long\\path\\to\\a\\project", branch: "main", model: "anthropic/claude-3.5-sonnet", tokens: 123456 });

	for (const width of [20, 40, 80, 120]) {
		for (const line of [...card.render(width), ...footer.render(width)]) {
			assert.ok(visibleWidth(line) <= width, `line wider than ${width}: ${stripTerminalSequences(line)}`);
		}
	}
	assert.match(plain(card, 80), /more lines/, "long results are truncated");
	assert.match(plain(footer, 120), /123,456 tokens/);
});

test("formatArgs summarizes arguments", () => {
	assert.equal(formatArgs({ path: "a.py", limit: 20, flags: ["-n"] }), 'path=a.py, limit=20, flags=["-n"]');
});
