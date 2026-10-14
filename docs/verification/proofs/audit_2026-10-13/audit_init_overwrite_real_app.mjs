// Stronger /init repro: uses App.prototype.writeFile (ui/src/app.ts:566-571)
// itself, not a copy.
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const { App } = await import("/Users/shadow/machina/ui/src/app.ts");
const { codexCommands } = await import("/Users/shadow/machina/ui/src/features/codex.ts");

const work = await mkdtemp(join(tmpdir(), "audit-init-app-"));
await writeFile(join(work, "AGENTS.md"), "# AGENTS.md\n\nIMPORTANT EXISTING INSTRUCTIONS\n", "utf8");

const init = codexCommands.find((c) => c.name === "init");
const previousCwd = process.cwd();
process.chdir(work);
let printed = "";
const host = {
  print: (text) => { printed = text; },
  writeFile: (path, text) => App.prototype.writeFile.call(null, path, text),
};
try {
  await init.run("", host);
} finally {
  process.chdir(previousCwd);
}
const after = await readFile(join(work, "AGENTS.md"), "utf8");
console.log("printed:", printed);
console.log("old content survived:", after.includes("IMPORTANT EXISTING INSTRUCTIONS"));
const confirmed = !after.includes("IMPORTANT EXISTING INSTRUCTIONS");
console.log(confirmed ? "CLAIM CONFIRMED" : "CLAIM NOT REPRODUCED");
process.exit(confirmed ? 0 : 1);
