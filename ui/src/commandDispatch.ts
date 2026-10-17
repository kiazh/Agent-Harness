// Command-dispatch table for submit-time turn guards (split from app.ts).
//
// Dispatch/state divide: this module owns the pure decision of whether a
// parsed input may run while a turn is in flight. All mutable turn state
// (the in-flight set, provisional bindings, timers) stays in App; rendering
// the resulting notices stays with the caller. No behavior change.

import { splitSub } from "./commands.ts";
import type { ParsedCommand } from "./commands.ts";

/**
 * Commands that always mutate in-flight session state or start new work and
 * must wait for the running turn (AH-031).
 *
 * Argument-sensitive mutators live in {@link mutatesInFlightSession} instead:
 * several of these commands also have read-only forms that must stay usable
 * while a turn streams (`/models catalog`, `/provider list`, `/approvals
 * allow|deny`, `/growth show`). In particular, approval decisions must remain
 * available so a paused turn can be unblocked.
 *
 * `fork` is intentionally NOT here: session.fork is a read-copy of the source
 * session, and the fork handler switches to the copy through the same
 * supported session-switch path as `/sessions` (gateway events are routed by
 * sessionId/turnId, so the source turn keeps streaming in the background).
 */
export const CONFLICTING_COMMANDS: ReadonlySet<string> = new Set([
	"compress",
	"delete",
	"rename",
	"goal",
	"delegate",
]);

/** Outcome of the submit-time turn guard. */
export type SubmitGuard = "allow" | "turn-busy" | "stop-first";

/**
 * Whether a parsed command's specific argument form mutates the active
 * session (model/provider/mode/grants/personality) while a turn is running.
 *
 * Read-only forms are deliberately allowed:
 * - `/models catalog|list` prints the catalog; any other form opens the model
 *   picker, which applies `session.setModel`.
 * - `/provider` (bare) and `/provider list` read; `add`, `remove`, and a
 *   provider name change the config and the session pin.
 * - `/mode` (bare) reads; any value calls `mode.set`/`mode.revoke`.
 * - `/approvals` (bare/list) reads and `allow|deny` resolves a pending request
 *   (required to unblock a paused turn); `revoke` drops grants mid-turn.
 * - `/growth show` reads; the bare menu or teach/feedback/forget/rollback
 *   write learned state.
 * - `/settings` and bare `/config` open settings, including the model picker.
 * - `/default-model` (alias `/model`) always picks or sets a model.
 *
 * Aliases are already resolved by `parseCommand`; `splitSub` lowercases the
 * first argument exactly as each handler does.
 */
function mutatesInFlightSession(command: ParsedCommand): boolean {
	switch (command.name) {
		case "models": {
			const [sub] = splitSub(command.args);
			return sub !== "catalog" && sub !== "list";
		}
		case "settings":
			return true;
		case "config":
			return splitSub(command.args)[0] === "";
		case "default-model":
			return true;
		case "provider": {
			const [sub] = splitSub(command.args);
			return sub !== "" && sub !== "list";
		}
		case "mode":
			return splitSub(command.args)[0] !== "";
		case "approvals":
			return splitSub(command.args)[0] === "revoke";
		case "growth": {
			const [sub] = splitSub(command.args);
			return sub === "" || ["teach", "feedback", "forget", "rollback"].includes(sub);
		}
		default:
			return false;
	}
}

/**
 * Decide whether a parsed input may be dispatched while a turn is in
 * flight. Pure: no state reads, no rendering.
 *
 * - not in flight → "allow" (prompts and every command run as usual);
 * - in flight + plain prompt → "turn-busy" (retry once the turn lands);
 * - in flight + a session-mutating command/form → "stop-first";
 * - in flight + any other command (reads, navigation, approvals allow/deny)
 *   → "allow".
 */
export function guardSubmit(
	command: ParsedCommand | undefined,
	inFlight: boolean,
): SubmitGuard {
	if (!inFlight) return "allow";
	if (!command) return "turn-busy";
	if (CONFLICTING_COMMANDS.has(command.name) || mutatesInFlightSession(command)) {
		return "stop-first";
	}
	return "allow";
}
