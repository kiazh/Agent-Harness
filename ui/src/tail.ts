// Hidden-session tail buffer: transcript work that arrived while a session
// was not visible, replayed when the user switches back.
//
// Merge rule (PINNED): the buffered live tail WINS over the freshly fetched
// history. switchTo replays the trimmed history and then the tail, so a
// completed turn whose canonical text already landed in history (or a
// settled tool already persisted as a chunk) renders exactly once: before
// replay, trailing history entries are popped when they are an identical
// completed assistant text, or a settled tool whose name matches and whose
// recorded content equals (or is a prefix of) that reported result. Only the
// newest contiguous overlap is trimmed — the tail is the conversation suffix.

import type { NoticeKind } from "./features/types.ts";
import type { GatewayEvent, HistoryEntry } from "./protocol.ts";

/** One buffered transcript block from a hidden session's live tail. */
export type TailBlock =
	| { kind: "assistant"; turnId: string; text: string; completed: boolean }
	| { kind: "tool"; turnId: string; id: string; name: string; result: string; settled: boolean }
	| { kind: "notice"; turnId: string; text: string; level: NoticeKind };

/** Hidden-session preview: rendered-order blocks plus terminal dedupe. */
export interface SessionTail {
	blocks: TailBlock[];
	/** Bounded terminal turn IDs; late duplicate events are inert. */
	completedTurns: string[];
}

/** Memory bounds for hidden-session tails. */
export const MAX_TAIL_SESSIONS = 20;
export const MAX_TAIL_BLOCKS = 200;
export const MAX_TAIL_COMPLETED_TURNS = 20;

/**
 * Drop trailing fetched-history entries already covered by the buffered
 * tail: identical completed assistant text, or a settled tool whose name
 * matches and whose reported result starts with (or equals) the recorded
 * content.
 */
export function trimSupersededHistory(history: HistoryEntry[], tail: TailBlock[]): HistoryEntry[] {
	const trimmed = [...history];
	for (let index = tail.length - 1; index >= 0; index--) {
		const block = tail[index]!;
		const last = trimmed.at(-1);
		if (!last) break;
		if (block.kind === "assistant" && block.completed) {
			if (last.role === "assistant" && last.content === block.text) trimmed.pop();
		} else if (block.kind === "tool" && block.settled) {
			if (
				last.role === "tool" &&
				last.tool === block.name &&
				(last.content === block.result || block.result.startsWith(last.content))
			) {
				trimmed.pop();
			}
		}
	}
	return trimmed;
}

/** Render buffered tail blocks through the history replay path. */
export function tailEntries(tail: TailBlock[]): HistoryEntry[] {
	const entries: HistoryEntry[] = [];
	for (const block of tail) {
		if (block.kind === "assistant") entries.push({ role: "assistant", content: block.text });
		else if (block.kind === "tool") {
			entries.push(
				block.settled
					? { role: "tool", tool: block.name, content: block.result }
					// A running tool has no reported outcome yet: mark it as
					// activity, never as a recorded result (honesty pin).
					: { role: "system", content: `${block.name} running…` },
			);
		} else entries.push({ role: "system", content: block.text });
	}
	return entries;
}

/**
 * Buffer one hidden-session event into its live tail. Only the newest
 * turn's activity is previewed; completed turns collapse to their
 * canonical final text (switchTo trims the matching history copy), so a
 * re-fetched history never double-renders. Approval events are owned by
 * their own retention (pendingApprovals), never by the tail.
 */
export function bufferHiddenEvent(tail: SessionTail, event: GatewayEvent): void {
	if (tail.completedTurns.includes(event.turnId)) return;
	switch (event.type) {
		case "message.delta": {
			const last = tail.blocks.at(-1);
			if (last?.kind === "assistant" && last.turnId === event.turnId && !last.completed) {
				last.text += event.text;
			} else {
				tail.blocks.push({ kind: "assistant", turnId: event.turnId, text: event.text, completed: false });
			}
			break;
		}
		case "tool.start":
			tail.blocks.push({ kind: "tool", turnId: event.turnId, id: event.id, name: event.name, result: "", settled: false });
			break;
		case "tool.complete": {
			const block = [...tail.blocks].reverse().find(
				(candidate) => candidate.kind === "tool" && candidate.id === event.id && !candidate.settled,
			);
			if (block && block.kind === "tool") {
				block.result = event.result;
				block.settled = true;
			} else {
				tail.blocks.push({ kind: "tool", turnId: event.turnId, id: event.id, name: event.name, result: event.result, settled: true });
			}
			break;
		}
		case "message.complete": {
			tail.completedTurns.push(event.turnId);
			if (tail.completedTurns.length > MAX_TAIL_COMPLETED_TURNS) tail.completedTurns.shift();
			// Completed work is persisted to history at completion: keep only
			// the canonical final text as the tail's fallback and let the
			// fetched history own the tool cards. Turn-failure notices are NOT
			// persisted, so they survive completion: dropping them here would
			// hide the failure from a switch-back (review round 4, R4-094).
			tail.blocks = tail.blocks.filter((block) => block.turnId !== event.turnId || block.kind === "notice");
			if (event.text) {
				tail.blocks.push({ kind: "assistant", turnId: event.turnId, text: event.text, completed: true });
			}
			break;
		}
		case "error":
			tail.blocks.push({ kind: "notice", turnId: event.turnId, text: event.message, level: "error" });
			break;
		case "message.stopping":
			tail.blocks.push({ kind: "notice", turnId: event.turnId, text: "Stopping…", level: "warning" });
			break;
		case "turn.ownership_lost":
			tail.blocks.push({ kind: "notice", turnId: event.turnId, text: "Turn ownership was lost; stopping the running action.", level: "warning" });
			break;
		case "turn.cleanup_pending":
			tail.blocks.push({ kind: "notice", turnId: event.turnId, text: "Stopping: waiting for the running action to finish cleanup.", level: "warning" });
			break;
		default:
			return; // usage/approval/growth frames never render into a tail
	}
	if (tail.blocks.length > MAX_TAIL_BLOCKS) {
		tail.blocks.splice(0, tail.blocks.length - MAX_TAIL_BLOCKS);
	}
}

/** What the tail store must know about App state to bound itself. */
export interface TailRetention {
	/** The visible session id, or undefined before the first switch. */
	visible(): string | undefined;
	/** True while a turn is locally in flight for this session. */
	running(sessionId: string): boolean;
	/** True when the session is current, in flight, or a family member. */
	retained(sessionId: string): boolean;
}

/**
 * Bounded store for per-session tails: entries are created on the first
 * hidden event and consumed by switchTo. Sessions that can no longer need
 * a tail (not current, not in flight, not family) are dropped on prune,
 * and the retained session count is capped.
 */
export class SessionTailStore {
	private readonly tails = new Map<string, SessionTail>();
	private readonly retention: TailRetention;

	constructor(retention: TailRetention) {
		this.retention = retention;
	}

	get(sessionId: string): SessionTail | undefined {
		return this.tails.get(sessionId);
	}

	has(sessionId: string): boolean {
		return this.tails.has(sessionId);
	}

	/** Consume a session's tail; the next hidden event starts a fresh one. */
	take(sessionId: string): SessionTail | undefined {
		const tail = this.tails.get(sessionId);
		if (tail) this.tails.delete(sessionId);
		return tail;
	}

	buffer(event: GatewayEvent): void {
		bufferHiddenEvent(this.ensure(event.sessionId), event);
	}

	private ensure(sessionId: string): SessionTail {
		let tail = this.tails.get(sessionId);
		if (!tail) {
			tail = { blocks: [], completedTurns: [] };
			this.tails.set(sessionId, tail);
			// Cap the session count without dropping the entry just created:
			// family-based pruning runs on refresh, where parentage is known.
			while (this.tails.size > MAX_TAIL_SESSIONS) {
				const candidate = [...this.tails.keys()].find(
					(id) => id !== sessionId && id !== this.retention.visible() && !this.retention.running(id),
				);
				if (candidate === undefined) break;
				this.tails.delete(candidate);
			}
		}
		return tail;
	}

	prune(): void {
		for (const sessionId of [...this.tails.keys()]) {
			if (this.retention.retained(sessionId)) continue;
			this.tails.delete(sessionId);
		}
		while (this.tails.size > MAX_TAIL_SESSIONS) {
			const candidate = [...this.tails.keys()].find(
				(sessionId) => sessionId !== this.retention.visible() && !this.retention.running(sessionId),
			);
			if (candidate === undefined) break;
			this.tails.delete(candidate);
		}
	}
}
