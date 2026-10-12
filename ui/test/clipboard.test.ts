import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("clipboard transfer sends the complete value on stdin without shell evaluation", async (t) => {
	if (process.platform === "win32") { t.skip("POSIX executable fixture"); return; }
	const { copyText } = await import("../src/clipboard.ts");
	const directory = await mkdtemp(join(tmpdir(), "ah-copy-"));
	const output = join(directory, "copied.txt");
	const originalPath = process.env.PATH;
	try {
		for (const command of ["pbcopy", "wl-copy", "xclip"]) {
			await writeFile(join(directory, command), `#!${process.execPath}\nconst fs = require("node:fs"); fs.writeFileSync(${JSON.stringify(output)}, fs.readFileSync(0));`, { mode: 0o700 });
		}
		process.env.PATH = directory;
		const value = `https://auth.example.com/authorize?state=${"a".repeat(500)}&literal=$(touch should-not-exist)\nsecond line`;
		assert.equal(await copyText(value), true);
		assert.equal(await readFile(output, "utf8"), value);
	} finally {
		process.env.PATH = originalPath;
		await rm(directory, { recursive: true, force: true });
	}
});

test("unavailable clipboard commands return a recoverable failure", async (t) => {
	if (process.platform === "win32") { t.skip("POSIX executable fixture"); return; }
	const { copyText } = await import("../src/clipboard.ts");
	const directory = await mkdtemp(join(tmpdir(), "ah-copy-"));
	const originalPath = process.env.PATH;
	try {
		process.env.PATH = directory;
		assert.equal(await copyText("private transient link"), false);
	} finally {
		process.env.PATH = originalPath;
		await rm(directory, { recursive: true, force: true });
	}
});
