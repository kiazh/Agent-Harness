// Audit finding: `/init` writes AGENTS.md with default truncating flags,
// silently replacing existing instructions.
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const work = await mkdtemp(join(tmpdir(), "audit-init-"));
const existing = "# AGENTS.md\n\nIMPORTANT EXISTING INSTRUCTIONS\n";
await writeFile(join(work, "AGENTS.md"), existing, "utf8");

// Exercise the real /init command with host.writeFile implemented exactly as
// ui/src/app.ts:566-571 (three lines, no flags -> truncate).
const { codexCommands } = await import("/Users/shadow/machina/ui/src/features/codex.ts");
const init = codexCommands.find((c) => c.name === "init");
const previousCwd = process.cwd();
process.chdir(work);
let printed = "";
const host = {
  print: (text) => { printed = text; },
  writeFile: async (path, text) => {
    const { resolve, dirname } = await import("node:path");
    const full = resolve(process.cwd(), path);
    const { mkdir } = await import("node:fs/promises");
    await mkdir(dirname(full), { recursive: true });
    await writeFile(full, text, "utf8");
    return full;
  },
};
try {
  await init.run("", host);
} finally {
  process.chdir(previousCwd);
}
const after = await readFile(join(work, "AGENTS.md"), "utf8");
console.log("printed:", printed);
console.log("old content survived:", after.includes("IMPORTANT EXISTING INSTRUCTIONS"));
console.log("new content present:", after.includes("Instructions for AI agents"));
const confirmed = !after.includes("IMPORTANT EXISTING INSTRUCTIONS")
  && after.includes("Instructions for AI agents");
console.log(confirmed
  ? "CLAIM CONFIRMED (existing AGENTS.md silently replaced)"
  : "CLAIM NOT REPRODUCED");
process.exit(confirmed ? 0 : 1);
