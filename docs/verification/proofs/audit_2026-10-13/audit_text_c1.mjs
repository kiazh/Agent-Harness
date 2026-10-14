// Audit finding: sanitizeDisplay leaves C1 OSC/ST controls intact and
// hasNoTerminalControls accepts them.
const { sanitizeDisplay, hasNoTerminalControls } = await import(
  "/Users/shadow/machina/ui/src/design/text.ts"
);

const c1OscWithSt = "A\u009d0;attack\u009cB";   // C1 OSC ... C1 ST
const c1StAlone = "A\u009cB";
const c1Dcs = "A\u0090payload\u009cB";

for (const input of [c1OscWithSt, c1StAlone, c1Dcs]) {
  const out = sanitizeDisplay(input);
  const clean = hasNoTerminalControls(input);
  console.log(JSON.stringify(input), "-> sanitized:", JSON.stringify(out),
              "| validator says clean:", clean,
              "| C1 survived:", /[\u0080-\u009f]/.test(out));
}

const survived = /[\u0080-\u009f]/.test(sanitizeDisplay(c1OscWithSt));
const accepted = hasNoTerminalControls(c1OscWithSt);
const confirmed = survived && accepted;
console.log(confirmed
  ? "CLAIM CONFIRMED (C1 controls survive and are accepted)"
  : "CLAIM NOT REPRODUCED");
process.exit(confirmed ? 0 : 1);
