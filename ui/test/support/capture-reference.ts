// Capture actual native renderer frames, not browser/CSS mockups.
// Usage: node ui/test/support/capture-reference.ts /tmp/ah-reference-review
import { mkdir, writeFile } from "node:fs/promises";
import { resolve, join } from "node:path";
import { referenceFixture } from "./reference.ts";
import { setSkin } from "../../src/theme.ts";
const output = resolve(process.argv[2] ?? "/tmp/ah-reference-review");
await mkdir(output, {recursive:true});
for (const skin of ["codex", "mono", "light"]) {
 for (const [columns, rows] of [[140,52], [80,24], [40,9]]) {
  const fixture = await referenceFixture(columns!, rows!);
  try {
   setSkin(skin);
   fixture.tui.renderNow(true);
   const name = `${skin}-${columns}x${rows}`;
   await writeFile(join(output, `${name}.json`), JSON.stringify({skin, columns, rows, lines:fixture.tui.getScreenLines()}, null, 2));
   await writeFile(join(output, `${name}.txt`), fixture.plain().join("\n") + "\n");
  } finally {await fixture.app.exit();}
 }
}
console.log(`Native renderer captures: ${output}`);

// Capture timed frames after streaming text hides the thinking indicator.
// The native renderer's timer, rather than this script's render calls, drives
// the working dot and corner pulse between captures.
const working = await referenceFixture();
try {
 setSkin("codex");
 working.emit({type:"approval.resolved", requestId:"demo-7f2a", status:"approved"});
 working.emit({type:"message.delta", text:"The turn is continuing."});
 working.emit({type:"tool.start", id:"motion-preview", name:"terminal", args:{command:"npm run check"}});
 working.tui.renderNow(true);
 for (let frame = 0; frame < 4; frame++) {
  if (frame > 0) await new Promise(resolve => setTimeout(resolve, 210));
  await writeFile(join(output, `motion-codex-${frame}.json`), JSON.stringify({skin:"codex", columns:140, rows:52, durationMs:200, lines:working.tui.getScreenLines()}, null, 2));
 }
} finally {await working.app.exit();}
