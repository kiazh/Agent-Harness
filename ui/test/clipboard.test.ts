import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

async function writeClipboardFixture(directory: string, output: string): Promise<void> {
	// A shell fixture boots in milliseconds — a Node fixture can outlive the
	// copy timeout (CLIPBOARD_COPY_TIMEOUT_MS) under parallel test load — while
	// still proving the value arrives on stdin and is never shell-evaluated:
	// cat reads stdin, argv is ignored.
	const script = `#!/bin/sh\nexec /bin/cat > ${JSON.stringify(output)}\n`;
	for (const command of ["pbcopy", "wl-copy", "xclip"]) {
		await writeFile(join(directory, command), script, { mode: 0o700 });
	}
}

test("clipboard transfer sends the complete value on stdin without shell evaluation", async (t) => {
	if (process.platform === "win32") { t.skip("POSIX executable fixture"); return; }
	const { copyText } = await import("../src/clipboard.ts");
	const directory = await mkdtemp(join(tmpdir(), "machina-copy-"));
	const output = join(directory, "copied.txt");
	const originalPath = process.env.PATH;
	try {
		await writeClipboardFixture(directory, output);
		process.env.PATH = directory;
		const value = `https://auth.example.com/authorize?state=${"a".repeat(500)}&literal=$(touch should-not-exist)\nsecond line`;
		assert.equal(await copyText(value), true);
		assert.equal(await readFile(output, "utf8"), value);
	} finally {
		process.env.PATH = originalPath;
		await rm(directory, { recursive: true, force: true });
	}
});

test("the copy timeout leaves headroom for a loaded-machine clipboard", async () => {
	// The product bound is the only thing between a slow-but-working pbcopy and
	// a false "Copy failed". A deterministic slow-copy fixture is not feasible
	// with the PATH shim: the child would have to block in real wall-clock time
	// past the old bound, which reintroduces exactly the load-sensitive waiting
	// this bound exists to absorb. Assert the bound itself instead of a literal.
	const { CLIPBOARD_COPY_TIMEOUT_MS } = await import("../src/clipboard.ts");
	assert.ok(Number.isFinite(CLIPBOARD_COPY_TIMEOUT_MS) && CLIPBOARD_COPY_TIMEOUT_MS > 0);
	assert.ok(CLIPBOARD_COPY_TIMEOUT_MS >= 6000, "at least 2x the old 3 s cap that was breached at 3.0769 s under load");
	assert.ok(CLIPBOARD_COPY_TIMEOUT_MS <= 30_000, "still fails fast for a stuck backend");
});

test("unavailable clipboard commands return a recoverable failure", async (t) => {
	if (process.platform === "win32") { t.skip("POSIX executable fixture"); return; }
	const { copyText } = await import("../src/clipboard.ts");
	const directory = await mkdtemp(join(tmpdir(), "machina-copy-"));
	const originalPath = process.env.PATH;
	try {
		process.env.PATH = directory;
		assert.equal(await copyText("private transient link"), false);
	} finally {
		process.env.PATH = originalPath;
		await rm(directory, { recursive: true, force: true });
	}
});

test("the fullscreen TUI copies selections through the real clipboard, not OSC 52", async (t) => {
	if (process.platform === "win32") { t.skip("POSIX executable fixture"); return; }
	const { tuiClipboardOptions } = await import("../src/clipboard.ts");
	const { TuiAltScreen } = await import("@earendil-works/pi-tui");
	const directory = await mkdtemp(join(tmpdir(), "machina-copy-"));
	const output = join(directory, "copied.txt");
	const originalPath = process.env.PATH;
	const writes: string[] = [];
	const term = {
		columns: 80, rows: 24, kittyProtocolActive: false,
		start() {}, stop() {}, async drainInput() {},
		write(data: string) { writes.push(data); },
		moveBy() {}, hideCursor() {}, showCursor() {}, clearLine() {},
		clearFromCursor() {}, clearScreen() {}, setTitle() {}, setProgress() {},
	};
	try {
		await writeClipboardFixture(directory, output);
		process.env.PATH = directory;
		const tui = new TuiAltScreen(term as never, undefined, undefined, tuiClipboardOptions());
		(tui as unknown as { getActiveSelectionText: () => string }).getActiveSelectionText = () => "selected highlight";
		assert.equal(await (tui as unknown as { copySelectionToClipboard(): Promise<boolean> }).copySelectionToClipboard(), true);
		assert.equal(await readFile(output, "utf8"), "selected highlight");
		assert.ok(!writes.some((chunk) => chunk.includes("\x1b]52")), "no OSC 52 fallback write");
	} finally {
		process.env.PATH = originalPath;
		await rm(directory, { recursive: true, force: true });
	}
});
