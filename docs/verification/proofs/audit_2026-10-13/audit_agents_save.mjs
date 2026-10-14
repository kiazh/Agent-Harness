// Audit finding: `/agents save helper My helper description` saves the name
// as "helper my helper description".
const { agentsCommand } = await import("/Users/shadow/machina/ui/src/features/agents.ts");

const captured = [];
const host = {
  session: () => ({ id: "sess-1" }),
  print: () => {},
  async request(method, params) {
    if (method === "agents.list") return { agents: [] };
    if (method === "agents.save") { captured.push(params); return { agent: { name: params.name } }; }
    throw new Error("unexpected method " + method);
  },
};

await agentsCommand.run("save helper My helper description", host);
const params = captured[0];
console.log("agents.save params:", JSON.stringify(params));
const confirmed = params && params.name === "helper my helper description";
console.log(confirmed
  ? "CLAIM CONFIRMED (description became part of the name)"
  : "CLAIM NOT REPRODUCED");
process.exit(confirmed ? 0 : 1);
