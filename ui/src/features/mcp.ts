// Read-only MCP inspection. Inventory belongs to the harness runtime, not
// the UI or another coding agent. Listing never connects or discovers tools.
import type { MCPServerView } from "../protocol.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { options, type Command } from "./types.ts";
const stateOf = (server: MCPServerView): string => server.enabled ? server.state : "disabled";
const nameOf = (server: MCPServerView): string => sanitizeDisplay(server.display_name || server.id);
function describe(server: MCPServerView): string {
 const lines = [`${nameOf(server)} · ${stateOf(server)}`, `ID: ${sanitizeDisplay(server.id)} · Transport: ${sanitizeDisplay(server.transport)}`];
 if (server.state === "connected") {
  if (server.server_name) lines.push(`Server: ${sanitizeDisplay(server.server_name)}${server.server_version ? ` ${sanitizeDisplay(server.server_version)}` : ""}`);
  if (server.protocol_version) lines.push(`Protocol: ${sanitizeDisplay(server.protocol_version)}`);
 }
 if (server.error) lines.push(`Error: ${sanitizeDisplay(server.error)}`);
 return lines.join("\n");
}
export const mcpCommand: Command = {
 name: "mcp",
 description: "Inspect MCP servers and their connection status",
 argumentHint: "[list | status <id>]",
 getArgumentCompletions: options([["list", "List configured harness servers"], ["status", "Inspect a server by ID"]]),
 async run(args, host) {
  const words = args.trim().split(/\s+/).filter(Boolean);
  if (words[0] === "status" && words.length === 2) {
   const {server} = await host.request<{server:MCPServerView}>("mcp.status", {id:words[1]});
   host.print(describe(server), "plain");
   return;
  }
  if (words.length && !(words[0] === "list" && words.length === 1)) {
   host.print("Usage: /mcp · /mcp list · /mcp status <id>", "warning");
   return;
  }
  const {servers} = await host.request<{servers:MCPServerView[]}>("mcp.list");
  if (!servers.length) {
   host.print("No MCP servers registered in this harness. Server configuration and connection management are not yet available in the TUI.", "plain");
   return;
  }
  if (words[0] === "list") {
   host.print(servers.map(server => `${nameOf(server)} · ${stateOf(server)} · ${sanitizeDisplay(server.id)}`).join("\n"), "plain");
   return;
  }
  const selected = await host.pick("MCP servers", servers.map(server => ({value:server.id, label:nameOf(server), description:`${stateOf(server)} · ${sanitizeDisplay(server.transport)} · ${sanitizeDisplay(server.id)}`})));
  if (!selected) return;
  const {server} = await host.request<{server:MCPServerView}>("mcp.status", {id:selected.value});
  host.print(describe(server), "plain");
 },
};
