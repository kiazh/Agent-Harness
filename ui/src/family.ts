// Session family (subagents): the root rule, the one family predicate, the
// bounded family listings, and the switch actions App delegates to.
//
// Family rule (documented, single level): the root is
// `current.parentSessionId ?? current.id`; members are the root plus every
// session whose `parentSessionId` is that root. If the visible session is
// itself a child, its parent's family shows with the child highlighted.
// Deeper descendants stay out of the strip by design.
//
// Delegation is non-streaming (`agent.run`): a delegated child never emits
// frames of its own. The child session becomes visible to this TUI when the
// parent's `delegate` tool.complete lands (the child exists in the catalog
// by then), or when a turn-start frame arrives from an unfamiliar session.

import type { FamilySession } from "./features/types.ts";
import { listAllSessions, type SessionListHost } from "./features/sessions.ts";
import type { HistoryEntry, ResumeResult, SessionInfo } from "./protocol.ts";

/** Upper bound on cursor pages scanned for a family (features/sessions.ts owns the protocol). */
export const MAX_FAMILY_PAGES = 10;

/** Unknown-session refreshes are throttled; explicit triggers bypass. */
export const FAMILY_REFRESH_THROTTLE_MS = 1500;

/** Bound on the discovery throttle and negative-cache maps. */
const MAX_TRACKED_UNKNOWN = 64;

/** Root rule: a delegated session belongs to its parent's family; otherwise its own. */
export function familyRootOf(session: SessionInfo): string {
	return session.parentSessionId ?? session.id;
}

/** The one family predicate: the root itself or one of its direct children. */
export function isFamilySession(session: SessionInfo, rootId: string): boolean {
	return session.id === rootId || session.parentSessionId === rootId;
}

/**
 * Bounded family listing for pickers and menus: the family root first, then
 * its children, with the visible session always winning over its listed copy.
 */
export async function listFamilySessions(host: SessionListHost, current: SessionInfo): Promise<SessionInfo[]> {
	const rootId = familyRootOf(current);
	const members = new Map<string, SessionInfo>();
	await listAllSessions(host, {
		maxPages: MAX_FAMILY_PAGES,
		onPage: (page) => {
			for (const session of page) {
				if (isFamilySession(session, rootId)) members.set(session.id, session);
			}
		},
	});
	members.set(current.id, current); // the visible session always wins
	const root = members.get(rootId);
	const children = [...members.values()].filter((session) => session.id !== rootId);
	// A bounded listing may omit the root; never relabel/duplicate a child.
	return root ? [root, ...children] : children;
}

/** What the family store needs from App (state plus navigation side effects). */
export interface FamilyHost {
	/** The visible session, or undefined before the first switch. */
	current(): SessionInfo | undefined;
	/** True while a turn is locally in flight for this session. */
	isRunning(sessionId: string): boolean;
	request<T>(method: string, params?: Record<string, unknown>): Promise<T>;
	/** Make a resumed session current (App.switchTo). */
	switchTo(session: SessionInfo, history: HistoryEntry[]): void;
	/** Members settled: prune tails and re-render the chrome. */
	changed(): void;
	/** A subagent switch failed; report it in the visible transcript. */
	report(error: unknown): void;
}

/**
 * Family store: members, root rule, bounded refresh, and the subagent switch
 * actions. App keeps rendering and key dispatch and delegates here.
 */
export class FamilyStore {
	/** Current family members: main (root) first, delegated children after. */
	private members: SessionInfo[] = [];
	/** Root of the current family (current.parentSessionId ?? current.id). */
	private rootId: string | undefined;
	/** Unknown-session ids already refreshed (throttled) — see noteUnknown. */
	private readonly unknown = new Map<string, number>();
	/** Sessions already scanned and found outside this family — never rescanned. */
	private readonly foreign = new Map<string, number>();
	private pending = false;
	private queued = false;
	/** One subagent switch at a time (rapid key repeats must not interleave). */
	private opening = false;
	private readonly host: FamilyHost;

	constructor(host: FamilyHost) {
		this.host = host;
	}

	/**
	 * Re-seed synchronously so the strip renders immediately on switch; the
	 * async refresh replaces the members with the authoritative list.
	 */
	reseed(): void {
		const session = this.host.current();
		if (!session) return;
		const rootId = familyRootOf(session);
		if (rootId !== this.rootId) {
			this.rootId = rootId;
			this.members = [];
			// Discovery verdicts are per family: a new root re-opens them.
			this.unknown.clear();
			this.foreign.clear();
		}
		this.members = [session, ...this.members.filter((member) => member.id !== session.id)];
		// Current is a child and its root was not listed yet: keep a stub so
		// the main marker and the family rule stay explainable.
		if (session.parentSessionId && !this.members.some((member) => member.id === rootId)) {
			this.members.push({
				id: rootId,
				title: "",
				model: session.model,
				provider: session.provider,
				status: session.status,
				lastActivity: null,
				parentSessionId: null,
			});
		}
	}

	/** Family rows, main first, with run/current state for chrome and menus. */
	rows(): FamilySession[] {
		const current = this.host.current();
		if (!current) return [];
		const rootId = this.rootId ?? familyRootOf(current);
		const members = this.members.filter((member) => isFamilySession(member, rootId));
		if (!members.some((member) => member.id === current.id)) members.unshift(current);
		const main = members.find((member) => member.id === rootId);
		const ordered = main ? [main, ...members.filter((member) => member.id !== rootId)] : [...members];
		return ordered.map((session) => ({
			session,
			main: session.id === rootId,
			running: this.host.isRunning(session.id),
		}));
	}

	isMember(sessionId: string): boolean {
		return this.rootId === sessionId || this.members.some((member) => member.id === sessionId);
	}

	/**
	 * Bounded, best-effort family listing (features/sessions.ts owns the
	 * cursor protocol). Failures stay quiet: the strip is chrome and commands
	 * surface their own errors.
	 */
	async refresh(): Promise<void> {
		const current = this.host.current();
		if (!current) return;
		const rootId = familyRootOf(current);
		if (this.pending) {
			this.queued = true;
			return;
		}
		this.pending = true;
		try {
			const found: SessionInfo[] = [];
			await listAllSessions(this.host, {
				maxPages: MAX_FAMILY_PAGES,
				onPage: (page) => {
					found.push(...page);
				},
			});
			// The family root may have changed while listing; never install
			// rows for a family the user already left.
			const still = this.host.current();
			if (!still || familyRootOf(still) !== rootId) return;
			const merged = new Map<string, SessionInfo>();
			for (const session of found) {
				if (isFamilySession(session, rootId)) merged.set(session.id, session);
			}
			merged.set(still.id, still); // the visible session always wins
			this.members = [...merged.values()];
			this.reseed();
			this.settleUnknown();
			this.host.changed();
		} catch {
			// Best-effort listing — the command path reports real failures.
		} finally {
			this.pending = false;
			if (this.queued) {
				this.queued = false;
				void this.refresh();
			}
		}
	}

	/**
	 * Refresh kick for turn-start frames from ids that are not yet known
	 * family subagents. Scans are throttled, and an id already scanned and found
	 * outside the family is negatively cached: later frames never rescan it.
	 */
	noteUnknown(sessionId: string): void {
		if (this.isMember(sessionId)) return;
		if (this.foreign.has(sessionId)) return;
		const now = Date.now();
		const last = this.unknown.get(sessionId) ?? 0;
		if (now - last < FAMILY_REFRESH_THROTTLE_MS) return;
		this.unknown.set(sessionId, now);
		void this.refresh();
	}

	/** Switch to a family subagent through session.resume; one switch at a time. */
	async open(sessionId: string): Promise<void> {
		if (this.opening || sessionId === this.host.current()?.id) return;
		this.opening = true;
		try {
			const { session, history } = await this.host.request<ResumeResult>("session.resume", { sessionId });
			this.host.switchTo(session, history);
		} catch (error) {
			this.host.report(error);
		} finally {
			this.opening = false;
		}
	}

	/** Ctrl+Shift+←/→: cycle the family; wraps around. */
	async cycle(direction: 1 | -1): Promise<void> {
		const rows = this.rows();
		if (rows.length < 2) return;
		const index = rows.findIndex((row) => row.session.id === this.host.current()?.id);
		const next = rows[(((index < 0 ? 0 : index) + direction) + rows.length) % rows.length]!;
		if (next.session.id === this.host.current()?.id) return;
		await this.open(next.session.id);
	}

	/** Move scanned ids to the negative cache once a listing has settled. */
	private settleUnknown(): void {
		for (const [sessionId] of [...this.unknown]) {
			if (this.isMember(sessionId)) {
				this.unknown.delete(sessionId);
				this.foreign.delete(sessionId);
			} else {
				this.foreign.set(sessionId, Date.now());
			}
		}
		for (const [sessionId] of [...this.foreign]) {
			if (this.isMember(sessionId)) this.foreign.delete(sessionId);
		}
		this.trim(this.unknown);
		this.trim(this.foreign);
	}

	private trim(bounded: Map<string, number>): void {
		while (bounded.size > MAX_TRACKED_UNKNOWN) {
			const oldest = [...bounded.entries()].sort((a, b) => a[1] - b[1])[0]!;
			bounded.delete(oldest[0]);
		}
	}
}
