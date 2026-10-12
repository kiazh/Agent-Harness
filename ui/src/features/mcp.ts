// Human-operated MCP management. Inspection never connects or discovers tools.
import type { MCPConnectorConfig, MCPOAuthSettings, MCPOAuthView, MCPServerView, MCPToolView } from "../protocol.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { confirm, options, requireSession, type Command, type FeatureHost } from "./types.ts";
import { authorizeConnector, forgetConnectorAccess, formatOAuthStatus } from "./mcp-oauth.ts";
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
function stringList(value: unknown, field: string): string[] {
 if (!Array.isArray(value) || value.some(item => typeof item !== "string")) throw new Error(`${field} must be a JSON array of strings. Open /mcp to try again.`);
 return value;
}
async function addServer(host: FeatureHost): Promise<void> {
 const transport = await host.pick("Add MCP server · transport", [
  { value: "stdio", label: "Local command", description: "Start a local MCP process when you connect" },
  { value: "streamable_http", label: "HTTP endpoint", description: "Connect to a remote MCP server" },
 ]);
 if (!transport) return;
 const id = await host.askText({ title: "Server ID", description: "A unique name for this connection", example: "docs" });
 if (id === undefined) return;
 if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id.trim())) throw new Error("Server ID must start with a lowercase letter or number and use lowercase letters, numbers, underscores or dashes (maximum 64 characters). Open /mcp to try again.");
 const name = await host.askText({ title: "Display name", initial: id.trim() });
 if (name === undefined) return;
 const config: MCPConnectorConfig = { id: id.trim(), display_name: name.trim() || id.trim(), transport: transport.value as MCPConnectorConfig["transport"], trust: "untrusted" };
 if (config.transport === "stdio") {
  const command = await host.askText({ title: "Executable", description: "Executable path or command name; put arguments in the next field", example: "node" });
  if (command === undefined) return;
  if (!command.trim()) throw new Error("An executable is required. Open /mcp to try again.");
  config.command = command.trim();
  const argv = await host.askText({ title: "Arguments (JSON array)", initial: "[]", example: '["/path/to/server.js"]' });
  if (argv === undefined) return;
  config.argv = stringList(JSON.parse(argv || "[]"), "Arguments");
  const cwd = await host.askText({ title: "Working directory (optional)", description: "Leave empty to use the harness configured workspace directory" });
  if (cwd === undefined) return;
  if (cwd.trim()) config.cwd = cwd.trim();
 } else {
  const endpoint = await host.askText({ title: "MCP endpoint", example: "https://example.com/mcp", description: "Do not include passwords, tokens or credential query parameters" });
  if (endpoint === undefined) return;
  const url = new URL(endpoint.trim());
  if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("Use an HTTP(S) endpoint without credentials, query parameters or fragments. Open /mcp to try again.");
  config.endpoint = endpoint.trim();
  const authentication = await host.pick("HTTP authentication", [
   { value: "none", label: "None", description: "Connect without credentials" },
   { value: "oauth", label: "OAuth", description: "Authorize in a browser with PKCE" },
  ]);
  if (!authentication) return;
  if (authentication.value === "oauth") {
   const oauth = await askOAuthSettings(host);
   if (!oauth) return;
   config.oauth = oauth;
  }
 }
 const advanced = await host.askText({ title: "Advanced options (JSON, optional)", initial: "{}", description: "Allowed: local_access, secret_refs (names only), tool_allowlist, trust (untrusted or trusted_read). Never enter secret values.", example: '{"secret_refs":["DOCS_TOKEN"],"tool_allowlist":["search"]}' });
 if (advanced === undefined) return;
 const extra: unknown = JSON.parse(advanced || "{}");
 if (!extra || typeof extra !== "object" || Array.isArray(extra)) throw new Error("Advanced options must be a JSON object. Open /mcp to try again.");
 for (const [key, value] of Object.entries(extra)) {
  if (key === "secret_refs" || key === "tool_allowlist") config[key] = stringList(value, key);
  else if (key === "local_access" && typeof value === "boolean") config.local_access = value;
  else if (key === "trust" && (value === "untrusted" || value === "trusted_read")) config.trust = value;
  else throw new Error(`Unsupported advanced option: ${sanitizeDisplay(key)}. Use secret_refs for secret names; never enter secret values.`);
 }
 const { server } = await host.request<{ server: MCPServerView }>("mcp.configure", { config });
 host.print(`${nameOf(server)} saved. Open /mcp and choose Connect when ready.`, "success");
}
async function askOAuthSettings(host: FeatureHost): Promise<MCPOAuthSettings | undefined> {
 const mode = await host.pick("OAuth client registration", [
  { value: "auto", label: "Automatic", description: "Use dynamic registration when the server supports it" },
  { value: "preregistered", label: "Pre-registered", description: "Use an existing client ID" },
  { value: "metadata_document", label: "Client metadata document", description: "Reference a published client metadata URL" },
 ]);
 if (!mode) return undefined;
 const settings: MCPOAuthSettings = {
  client_mode: mode.value, client_id: "", client_metadata_url: "", client_secret_ref: "",
  token_endpoint_auth_method: "none", issuer: "", scope_allowlist: [], callback_port: 0,
 };
 if (mode.value === "preregistered") {
  const clientId = await host.askText({ title: "Client ID", description: "Public client ID from the provider" });
  if (clientId === undefined) return undefined;
  settings.client_id = clientId.trim();
 }
 if (mode.value === "metadata_document") {
  const documentUrl = await host.askText({ title: "Client metadata URL", example: "https://client.example.com/metadata" });
  if (documentUrl === undefined) return undefined;
  settings.client_metadata_url = documentUrl.trim();
  if (settings.client_id === "") settings.client_id = settings.client_metadata_url;
 }
 const issuer = await host.askText({ title: "Issuer (optional)", description: "Leave empty to use the server-advertised issuer" });
 if (issuer === undefined) return undefined;
 if (issuer.trim()) settings.issuer = issuer.trim();
 const scopes = await host.askText({ title: "Allowed scopes (JSON array, optional)", initial: "[]", description: "Empty means every discovered scope needs explicit review" });
 if (scopes === undefined) return undefined;
 const parsed: unknown = JSON.parse(scopes || "[]");
 if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== "string")) throw new Error("Allowed scopes must be a JSON array of strings. Open /mcp to try again.");
 settings.scope_allowlist = parsed;
 return settings;
}
async function manageServer(host: FeatureHost, id: string): Promise<void> {
 const { server } = await host.request<{ server: MCPServerView & { oauth?: MCPOAuthSettings | null } }>("mcp.status", { id });
 host.print(describe(server), "plain");
 const isHttp = server.transport === "streamable_http";
 const hasOAuth = isHttp && server.oauth !== undefined && server.oauth !== null;
 if (isHttp) {
  try {
   const session = host.session();
   if (session) {
    const { auth } = await host.request<{ auth: MCPOAuthView }>("mcp.oauth.status", { id, sessionId: session.id });
    host.print(formatOAuthStatus(auth), "plain");
   }
  } catch {
   // Auth status is advisory; management stays usable without it.
  }
 }
 const action = await host.pick(`${nameOf(server)} · actions`, [
  server.state === "connected" ? { value: "disconnect", label: "Disconnect", description: "Close this server connection" } : { value: "connect", label: "Connect", description: "Request session approval and discover tools" },
  ...(isHttp ? [
   { value: "authorize", label: hasOAuth ? "Authorize / Reauthorize" : "Authorize", description: "Authorize in an external browser" },
   { value: "forget", label: "Forget access", description: "Remove saved OAuth credentials" },
  ] : []),
  ...(isHttp && !hasOAuth ? [{ value: "configure-oauth", label: "Configure OAuth", description: "Add OAuth settings to this server" }] : []),
  { value: "tools", label: "View tools", description: "Inspect cached tools without connecting" },
  { value: "remove", label: "Remove server", description: "Delete its configuration and close its connection" },
 ]);
 if (!action) return;
 if (action.value === "authorize") {
  await authorizeConnector(host, id);
 } else if (action.value === "forget") {
  await forgetConnectorAccess(host, id);
 } else if (action.value === "configure-oauth") {
  const oauth = await askOAuthSettings(host);
  if (!oauth) return;
  const { server: saved } = await host.request<{ server: MCPServerView }>("mcp.configure", { config: { id, oauth } });
  host.print(`${nameOf(saved)} OAuth settings saved. Choose Authorize when ready.`, "success");
 } else if (action.value === "connect") {
  const session = requireSession(host);
  const preview = await host.request<{ digest: string; proposal: { argv?: string[]; cwd?: string | null; network?: string[]; backend?: string; endpoint?: string; contentPreview?: string }; description?: string }>("mcp.preview", { id, sessionId: session.id });
  const review = [`Connect ${nameOf(server)}?`, preview.proposal.argv?.length ? `Command argv: ${JSON.stringify(preview.proposal.argv)}` : "", preview.proposal.cwd ? `Working directory: ${preview.proposal.cwd}` : "", preview.proposal.endpoint ? `Endpoint: ${preview.proposal.endpoint}` : "", preview.proposal.network?.length ? `Network: ${JSON.stringify(preview.proposal.network)}` : "", preview.proposal.backend ? `Backend: ${preview.proposal.backend}` : "", preview.proposal.contentPreview ? `Configuration: ${preview.proposal.contentPreview}` : "", preview.description || ""].filter(Boolean).map(line => sanitizeDisplay(line)).join("\n");
  if (!await confirm(host, review, "Connect")) return;
  const result = await host.request<{ server: MCPServerView }>("mcp.connect", { id, sessionId: session.id, digest: preview.digest });
  host.print(describe(result.server), result.server.state === "connected" ? "success" : "warning");
 } else if (action.value === "disconnect") {
  const result = await host.request<{ server: MCPServerView }>("mcp.disconnect", { id });
  host.print(describe(result.server), "plain");
 } else if (action.value === "remove") {
  if (!await confirm(host, `Remove ${nameOf(server)} and disconnect it?`, "Remove server")) return;
  const { removed } = await host.request<{ removed: boolean }>("mcp.remove", { id });
  host.print(removed ? `${nameOf(server)} removed.` : "Server was already removed.", removed ? "success" : "warning");
 } else if (action.value === "tools") {
  const { tools, truncated } = await host.request<{ tools: MCPToolView[]; truncated: boolean }>("mcp.tools", { id });
  const lines = tools.map(tool => `${sanitizeDisplay(tool.name)}${tool.verdict ? ` · ${sanitizeDisplay(tool.verdict)}` : ""}${tool.description ? ` · ${sanitizeDisplay(tool.description)}` : ""}`);
  if (!lines.length) lines.push("No cached tools. Connect the server to discover its tools.");
  if (truncated) lines.push("Tool list truncated.");
  host.print(lines.join("\n"), "plain");
 }
}
export const mcpCommand: Command = {
 name: "mcp",
 description: "Add, connect, inspect and remove MCP servers",
 argumentHint: "[list | status <id>]",
 getArgumentCompletions: options([["list", "List configured harness servers"], ["status", "Inspect a server by ID"]]),
 async run(args, host) {
  const words = args.trim().split(/\s+/).filter(Boolean);
  if (words[0] === "status" && words.length === 2) {
   const { server } = await host.request<{ server: MCPServerView }>("mcp.status", { id: words[1] });
   host.print(describe(server), "plain");
   return;
  }
  if (words.length && !(words[0] === "list" && words.length === 1)) {
   host.print("Usage: /mcp (manage servers) · /mcp list · /mcp status <id>", "warning");
   return;
  }
  const { servers } = await host.request<{ servers: MCPServerView[] }>("mcp.list");
  if (!servers.length) host.print("No MCP servers registered in this harness. Choose Add server to configure a connection.", "plain");
  if (words[0] === "list") {
   if (servers.length) host.print(servers.map(server => `${nameOf(server)} · ${stateOf(server)} · ${sanitizeDisplay(server.id)}`).join("\n"), "plain");
   return;
  }
  const selected = await host.pick("MCP servers", [
   ...servers.map(server => ({ value: server.id, label: nameOf(server), description: `${stateOf(server)} · ${sanitizeDisplay(server.transport)} · ${sanitizeDisplay(server.id)}` })),
   { value: "__add__", label: "Add server", description: "Configure a local command or HTTP endpoint" },
  ]);
  if (!selected) return;
  if (selected.value === "__add__") await addServer(host);
  else await manageServer(host, selected.value);
 },
};
