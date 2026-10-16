// Bounded back/forward jump history (plan §17): definition/reference jumps
// keep their origin URI and position so returning restores the previous
// cursor/scroll state.
//
// Classic browser semantics over plain data entries: one array plus a cursor.
// - Every push truncates the forward tail, including a push whose target is
//   the current entry: a navigation attempt supersedes the forward timeline.
// - A push identical to the current entry (all four fields) appends no entry;
//   consecutive duplicate jumps therefore never stack up.
// - The stack is bounded at JUMP_HISTORY_LIMIT entries; overflow evicts the
//   oldest entries first.
// Entries are detached copies of the caller's plain data: no document
// records, no store references, no shared mutation.

export interface JumpEntry {
	uri: string;
	offset: number;
	documentId?: string;
	line?: number;
}

export const JUMP_HISTORY_LIMIT = 50;

function copyEntry(entry: JumpEntry): JumpEntry {
	const copy: JumpEntry = { uri: entry.uri, offset: entry.offset };
	if (entry.documentId !== undefined) copy.documentId = entry.documentId;
	if (entry.line !== undefined) copy.line = entry.line;
	return copy;
}

function sameEntry(a: JumpEntry, b: JumpEntry): boolean {
	return a.uri === b.uri && a.offset === b.offset && a.documentId === b.documentId && a.line === b.line;
}

export class JumpHistory {
	private readonly entries: JumpEntry[] = [];
	/** Cursor into `entries`; -1 when empty. */
	private index = -1;

	/** Number of retained entries (back and forward both count). */
	get size(): number {
		return this.entries.length;
	}

	/**
	 * Record a jump target. Any push truncates the forward tail (a navigation
	 * attempt supersedes the forward timeline); a target identical to the
	 * current entry appends no duplicate entry.
	 */
	push(entry: JumpEntry): void {
		const next = copyEntry(entry);
		const current = this.index >= 0 ? this.entries[this.index] : undefined;
		this.entries.splice(this.index + 1);
		if (current && sameEntry(current, next)) return;
		this.entries.push(next);
		if (this.entries.length > JUMP_HISTORY_LIMIT) {
			this.entries.splice(0, this.entries.length - JUMP_HISTORY_LIMIT);
		}
		this.index = this.entries.length - 1;
	}

	/** Move one entry back; returns it, or undefined at the oldest entry. */
	back(): JumpEntry | undefined {
		if (this.index <= 0) return undefined;
		this.index -= 1;
		return copyEntry(this.entries[this.index]!);
	}

	/** Move one entry forward; returns it, or undefined at the newest entry. */
	forward(): JumpEntry | undefined {
		if (this.index < 0 || this.index >= this.entries.length - 1) return undefined;
		this.index += 1;
		return copyEntry(this.entries[this.index]!);
	}

	/** The entry the cursor is on; undefined for an empty history. */
	current(): JumpEntry | undefined {
		if (this.index < 0) return undefined;
		return copyEntry(this.entries[this.index]!);
	}
}
