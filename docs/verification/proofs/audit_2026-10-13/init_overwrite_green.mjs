// GREEN check for audit_init_overwrite_real_app: drives the real /init command
// through the real runCommand error path and App.prototype.writeFile, then
// asserts the existing AGENTS.md survived and the refusal was reported.
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const { App } = await import("/Users/shadow/machina/ui/src/app.ts");
const { runCommand } = await import("/Users/shadow/machina/ui/src/features/index.ts");
const { parseCommand } = await import("/Users/shadow/machina/ui/src/commands.ts");

const work = await mkdtemp(join(tmpdir(), "green-init-app-"));
await writeFile(join(work, "AGENTS.md"), "# AGENTS.md\n\nIMPORTANT EXISTING INSTRUCTIONS\n", "utf8");

const previousCwd = process.cwd();
process.chdir(work);
const printed = [];
const host = {
  request: async () => { throw new Error("no RPC expected for /init"); },
  print: (text) => { printed.push(text); },
  writeFile: (path, text) => App.prototype.writeFile.call(null, path, text),
};
try {
  await runCommand(parseCommand("/init"), host);
} finally {
  process.chdir(previousCwd);
}
const after = await readFile(join(work, "AGENTS.md"), "utf8");
const refusalReported = printed.some((t) => t.includes("Refusing to overwrite existing file"));
const survived = after.includes("IMPORTANT EXISTING INSTRUCTIONS");
const notReplaced = !after.includes("Instructions for AI agents");
console.log("printed:", JSON.stringify(printed));
console.log("refusal reported:", refusalReported);
console.log("old content survived:", survived);
console.log("new content present:", !notReplaced);
const green = refusalReported && survived && notReplaced;
console.log(green ? "CLAIM NOT REPRODUCED (existing AGENTS.md preserved; refusal reported)" : "STILL BROKEN");
process.exit(green ? 0 : 1);
