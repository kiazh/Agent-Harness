// Command-dispatch table for submit-time turn guards (split from app.ts).
//
// Dispatch/state divide: this module owns the pure decision of whether a
// parsed input may run while a turn is in flight. All mutable turn state
// (the in-flight set, provisional bindings, timers) stays in App; rendering
// the resulting notices stays with the caller. No behavior change.

import type { ParsedCommand } from "./commands.ts";

/**
 * Commands that mutate the in-flight session or start new work and must wait
 * for the running turn (AH-031). Everything else (/sessions, /new, /exit,
 * /help, /status, …) stays usable during a turn.
 */
export const CONFLICTING_COMMANDS: ReadonlySet<string> = new Set([
	"compress",
	"delete",
	"fork",
	"rename",
	"goal",
	"delegate",
]);

/** Outcome of the submit-time turn guard. */
export type SubmitGuard = "allow" | "turn-busy" | "stop-first";

/**
 * Decide whether a parsed input may be dispatched while a turn is in
 * flight. Pure: no state reads, no rendering.
 *
 * - not in flight → "allow" (prompts and every command run as usual);
 * - in flight + plain prompt → "turn-busy" (retry once the turn lands);
 * - in flight + session-mutating command → "stop-first";
 * - in flight + any other command → "allow" (read-only/navigation stays
 *   usable during a turn).
 */
export function guardSubmit(
	command: ParsedCommand | undefined,
	inFlight: boolean,
): SubmitGuard {
	if (!inFlight) return "allow";
	if (!command) return "turn-busy";
	if (CONFLICTING_COMMANDS.has(command.name)) return "stop-first";
	return "allow";
}
