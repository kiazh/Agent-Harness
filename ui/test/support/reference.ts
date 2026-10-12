// Deterministic native renderer fixture. No model, subprocess, or database.
import { TuiAltScreen, stripTerminalSequences, type Terminal } from "@earendil-works/pi-tui";
import { App } from "../../src/app.ts";
import type { GatewayClient } from "../../src/gateway.ts";
import type { GatewayEvent } from "../../src/protocol.ts";
export async function referenceFixture(columns = 140, rows = 52) {
 let input: (data: string) => void = () => {};
 const term: Terminal & {columns:number; rows:number} = {
  columns, rows, kittyProtocolActive:false, start(onInput) {input=onInput;}, stop(){}, async drainInput(){},
  write(){}, moveBy(){}, hideCursor(){}, showCursor(){}, clearLine(){}, clearFromCursor(){}, clearScreen(){}, setTitle(){}, setProgress(){},
 };
 const requests: Array<{method:string; params:Record<string, unknown>}> = [];
 const client = {
  onEvent:undefined as ((e:GatewayEvent) => void) | undefined,
  start(){}, async stop(){}, stderr:() => [],
  async request(method:string, params:Record<string, unknown> = {}) {
   requests.push({method,params});
   if (method === "initialize") return {version:"fixture", model:"preview", cwd:"~/machina", branch:"main", provider:"fixture"};
   if (method === "config.get") return {config:{theme:"codex"}};
   if (method === "mode.get") return {mode:"ask"};
   if (method === "session.create") return {session:{id:"reference-session", title:"Preview", model:"preview", provider:"fixture", status:"active", lastActivity:null}};
   if (method === "prompt.submit") return {turnId:"reference-turn"};
   return {};
  },
 };
 const tui = new TuiAltScreen(term);
 const app = new App(tui, client as unknown as GatewayClient);
 await app.start();
 const emit = (event:Record<string, unknown>) => client.onEvent?.({sessionId:"reference-session", turnId:"reference-turn", ...event} as GatewayEvent);
 await app.submitTurn("Fix the installer so it preserves my custom database URL.");
 emit({type:"message.start"});
 emit({type:"message.delta", text:"I’ll inspect the fallback and prepare a targeted change."});
 emit({type:"tool.start", id:"read-1", name:"read_file", args:{path:"install.sh"}});
 emit({type:"tool.complete", id:"read-1", name:"read_file", result:"Example inspection · Homebrew fallback configuration", isError:false});
 emit({type:"message.delta", text:"The proposed change preserves the existing URL. Review the file-write evidence below; approval options are docked above the composer."});
 const approval = (requestId:string) => ({type:"needs_approval", requestId, operation:"write_file", tool:"write_file", target:"install.sh", argv:[], cwd:"~/machina", backend:"local", timeout:30, capabilities:["file.write"], fileDiff:{diff:"@@ proposed installer guard · illustrative preview @@\n- write_generated_database_url()\n+ if custom_database_url_is_set; then\n+   log \"Preserving custom DATABASE_URL\"\n+ else\n+   write_generated_database_url()\n+ fi"}});
 emit(approval("demo-7f2a"));
 tui.renderNow(true);
 return {app, tui, term, emit, approval, requests, type:(data:string) => input(data), plain:() => tui.getScreenLines().map(stripTerminalSequences)};
}
