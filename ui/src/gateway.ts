// JSON-RPC client for the Python gateway (`python -m ah.gateway`).
//
// JsonRpcChannel is transport-agnostic (feed it lines, give it a writer) so it
// can be unit tested; GatewayClient wires it to a child process over stdio.

import { type ChildProcess, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { type GatewayEvent, RpcError } from "./protocol.ts";

type Pending = {
	resolve: (value: unknown) => void;
	reject: (reason: Error) => void;
	timer: NodeJS.Timeout;
};

export class JsonRpcChannel {
	onEvent?: (event: GatewayEvent) => void;
	private readonly send: (line: string) => void;
	private readonly pending = new Map<number, Pending>();
	private nextId = 1;
	private closedError: Error | undefined;

	constructor(send: (line: string) => void) {
		this.send = send;
	}

	request<T>(method: string, params: Record<string, unknown> = {}, timeoutMs = 30_000): Promise<T> {
		if (this.closedError) return Promise.reject(this.closedError);
		const id = this.nextId++;
		return new Promise<T>((resolve, reject) => {
			const timer = setTimeout(() => {
				this.pending.delete(id);
				reject(new Error(`gateway did not answer ${method} within ${timeoutMs} ms`));
			}, timeoutMs);
			this.pending.set(id, { resolve: (v) => resolve(v as T), reject, timer });
			try {
				this.send(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
			} catch (error) {
				clearTimeout(timer);
				this.pending.delete(id);
				reject(error instanceof Error ? error : new Error(String(error)));
			}
		});
	}

	/** Handle one line received from the gateway. Unparseable lines are ignored. */
	handleLine(line: string): void {
		let frame: unknown;
		try {
			frame = JSON.parse(line);
		} catch {
			return;
		}
		if (typeof frame !== "object" || frame === null) return;
		const message = frame as {
			id?: unknown;
			method?: unknown;
			params?: unknown;
			result?: unknown;
			error?: { code?: unknown; message?: unknown };
		};

		if (message.method === "event") {
			if (message.params && typeof message.params === "object") {
				this.onEvent?.(message.params as GatewayEvent);
			}
			return;
		}
		if (typeof message.id !== "number") return;
		const pending = this.pending.get(message.id);
		if (!pending) return;
		this.pending.delete(message.id);
		clearTimeout(pending.timer);
		if (message.error) {
			const code = typeof message.error.code === "number" ? message.error.code : -32603;
			pending.reject(new RpcError(code, String(message.error.message ?? "unknown gateway error")));
		} else {
			pending.resolve(message.result);
		}
	}

	/** Reject every in-flight and future request with *error*. */
	close(error: Error): void {
		this.closedError = error;
		for (const { reject, timer } of this.pending.values()) {
			clearTimeout(timer);
			reject(error);
		}
		this.pending.clear();
	}
}

export interface GatewayClientOptions {
	/** Python interpreter with AgentHarness installed. */
	python: string;
	cwd?: string;
	env?: NodeJS.ProcessEnv;
}

const STDERR_TAIL_LINES = 40;

export class GatewayClient {
	onEvent?: (event: GatewayEvent) => void;
	/** Called once if the gateway process exits on its own. */
	onExit?: (code: number | null, stderrTail: string[]) => void;

	private readonly options: GatewayClientOptions;
	private proc: ChildProcess | undefined;
	private channel: JsonRpcChannel | undefined;
	private readonly stderrTail: string[] = [];
	private stopping = false;

	constructor(options: GatewayClientOptions) {
		this.options = options;
	}

	start(): void {
		const proc = spawn(this.options.python, ["-m", "ah.gateway"], {
			cwd: this.options.cwd ?? process.cwd(),
			env: { ...process.env, ...this.options.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
			stdio: ["pipe", "pipe", "pipe"],
			windowsHide: true,
		});
		this.proc = proc;

		const channel = new JsonRpcChannel((line) => {
			if (!proc.stdin || proc.stdin.destroyed) throw new Error("gateway is not running");
			proc.stdin.write(line);
		});
		channel.onEvent = (event) => this.onEvent?.(event);
		this.channel = channel;

		createInterface({ input: proc.stdout! }).on("line", (line) => channel.handleLine(line));
		createInterface({ input: proc.stderr! }).on("line", (line) => {
			this.stderrTail.push(line);
			if (this.stderrTail.length > STDERR_TAIL_LINES) this.stderrTail.shift();
		});
		proc.stdin?.on("error", () => {}); // EPIPE when the gateway dies; surfaced via 'exit'

		proc.on("error", (error) => {
			channel.close(new Error(`could not start the gateway (${this.options.python}): ${error.message}`));
			if (!this.stopping) this.onExit?.(null, this.stderr());
		});
		proc.on("exit", (code) => {
			channel.close(new Error(`gateway exited (code ${code})`));
			if (!this.stopping) this.onExit?.(code, this.stderr());
		});
	}

	request<T>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
		if (!this.channel) return Promise.reject(new Error("gateway not started"));
		return this.channel.request<T>(method, params, timeoutMs);
	}

	stderr(): string[] {
		return [...this.stderrTail];
	}

	/** Ask the gateway to shut down, then make sure the process is gone. */
	async stop(): Promise<void> {
		const proc = this.proc;
		if (!proc || proc.exitCode !== null || this.stopping) return;
		this.stopping = true;
		const exited = new Promise<void>((resolve) => proc.once("exit", () => resolve()));
		try {
			await this.request("shutdown", {}, 2_000);
		} catch {
			// already gone or unresponsive; fall through to kill
		}
		proc.stdin?.end();
		const timeout = new Promise<"timeout">((resolve) => setTimeout(() => resolve("timeout"), 3_000).unref());
		if ((await Promise.race([exited, timeout])) === "timeout") proc.kill();
	}
}
