// Audit finding #2: missing/ambiguous approval identifiers approve the first request.
// Imports the real UI command module and drives it with a fake host.
const { approvalsCommand } = await import("/Users/shadow/machina/ui/src/features/mode.ts");

const pending = [
  { request_id: "aaaaaaaa-1111-4000-a000-000000000001", operation: "terminal", target: "cmd-1", agent_id: "agent" },
  { request_id: "aaabbbbb-2222-4000-a000-000000000002", operation: "file.write", target: "file-2", agent_id: "agent" },
];

function makeHost(resolved) {
  return {
    session: () => ({ id: "sess-1" }),
    print: () => {},
    async request(method, params) {
      if (method === "approvals.list") return { approvals: pending };
      if (method === "approvals.resolve") { resolved.push(params); return {}; }
      throw new Error("unexpected method " + method);
    },
  };
}

async function run(label, args) {
  const resolved = [];
  try {
    await approvalsCommand.run(args, makeHost(resolved));
    console.log(`${label}: dispatched ${JSON.stringify(resolved)}`);
  } catch (err) {
    console.log(`${label}: threw ${err.message} (nothing dispatched=${resolved.length === 0})`);
  }
}

await run("case1 '/approvals allow' (no id)", "allow");
await run("case2 '/approvals allow aa' (ambiguous, both start with aa)", "allow aa");
await run("case3 '/approvals allow aaaaaaaa' (unique)", "allow aaaaaaaa");
