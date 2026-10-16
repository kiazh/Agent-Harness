// Editor tab model (plan §9): preview/pinned/dirty rules, close-neighbor
// selection and overflow eviction.
// Pure data model: no store, App, gateway or theme imports. Dirtiness is
// injected through `isDirty` so the tab model never owns buffer state.
//
// Rules:
// - One preview tab at a time. Opening another preview replaces the previous
//   preview only when it is clean; a dirty preview is promoted to persistent
//   instead of being discarded. Editing or pinning a preview makes it
//   persistent.
// - Dirty tabs are never evicted by overflow. Overflow keeps the active and
//   pinned tabs and evicts the oldest clean non-active tabs first.
// - Closing the active tab selects the next sibling, else the previous one.
//   Closing a dirty tab returns a needs-decision result and changes nothing
//   (Cancel is the absence of any further call).

export interface EditorTab {
	documentId: string;
	pinned: boolean;
	preview: boolean;
}

export interface TabModelOptions {
	/** Dirty predicate supplied by the workspace controller (EditorStore). */
	isDirty: (documentId: string) => boolean;
}

export interface TabError {
	code: "not-open";
	message: string;
}

export interface OpenTabOptions {
	/** Simple explorer selection: may take over the single preview slot. */
	preview?: boolean;
}

export type OpenTabResult =
	| {
		ok: true;
		created: boolean;
		tab: EditorTab;
		activeDocumentId: string;
		/** Set when a clean preview tab was replaced in place. */
		replacedDocumentId?: string;
		/** Set when a dirty preview was promoted to persistent. */
		convertedDocumentId?: string;
	}
	| { ok: false; error: TabError };

export type PinTabResult = { ok: true; tab: EditorTab } | { ok: false; error: TabError };

export interface NoteEditedResult {
	ok: true;
	converted: boolean;
}

export type SetActiveResult = { ok: true; activeDocumentId: string } | { ok: false; error: TabError };

export type CloseTabResult =
	| { ok: true; closed: string; activeDocumentId: string | null }
	| { ok: false; needsDecision: true; documentId: string; options: readonly ["save", "discard", "cancel"] }
	| { ok: false; needsDecision: false; error: TabError };

export interface OverflowResult {
	ok: true;
	/** Documents whose tabs were removed, oldest first. */
	evicted: string[];
	/** True when the tab count is within the requested limit afterwards. */
	satisfied: boolean;
}

const CLOSE_DECISIONS: readonly ["save", "discard", "cancel"] = ["save", "discard", "cancel"];

export class TabModel {
	private readonly isDirty: (documentId: string) => boolean;
	private readonly tabs: EditorTab[] = [];
	private activeId: string | null = null;

	constructor(options: TabModelOptions) {
		this.isDirty = options.isDirty;
	}

	/**
	 * Open (or focus) a tab. Reopening an existing document focuses its tab and
	 * never creates a competing entry; it also never silently promotes or
	 * demotes an existing preview.
	 */
	open(documentId: string, options: OpenTabOptions = {}): OpenTabResult {
		const existing = this.tabs.find((tab) => tab.documentId === documentId);
		if (existing) {
			this.activeId = documentId;
			return { ok: true, created: false, tab: { ...existing }, activeDocumentId: documentId };
		}
		const preview = options.preview ?? false;
		let replacedDocumentId: string | undefined;
		let convertedDocumentId: string | undefined;
		let insertAt = this.tabs.length;
		if (preview) {
			const previousPreview = this.tabs.find((tab) => tab.preview);
			if (previousPreview) {
				if (this.isDirty(previousPreview.documentId)) {
					// The dirty preview becomes persistent; the new preview is appended.
					previousPreview.preview = false;
					convertedDocumentId = previousPreview.documentId;
				} else {
					const index = this.tabs.indexOf(previousPreview);
					this.tabs.splice(index, 1);
					insertAt = index;
					replacedDocumentId = previousPreview.documentId;
				}
			}
		}
		const tab: EditorTab = { documentId, pinned: false, preview };
		this.tabs.splice(insertAt, 0, tab);
		this.activeId = documentId;
		const result: OpenTabResult = {
			ok: true,
			created: true,
			tab: { ...tab },
			activeDocumentId: documentId,
		};
		if (replacedDocumentId !== undefined) result.replacedDocumentId = replacedDocumentId;
		if (convertedDocumentId !== undefined) result.convertedDocumentId = convertedDocumentId;
		return result;
	}

	/** Pin a tab: always persistent, never a preview replacement candidate. */
	pin(documentId: string): PinTabResult {
		const tab = this.tabs.find((candidate) => candidate.documentId === documentId);
		if (!tab) return { ok: false, error: notOpen(documentId) };
		tab.pinned = true;
		tab.preview = false;
		return { ok: true, tab: { ...tab } };
	}

	/** Note that a document was edited: a preview becomes persistent. */
	noteEdited(documentId: string): NoteEditedResult {
		const tab = this.tabs.find((candidate) => candidate.documentId === documentId);
		if (!tab || !tab.preview) return { ok: true, converted: false };
		tab.preview = false;
		return { ok: true, converted: true };
	}

	setActive(documentId: string): SetActiveResult {
		const tab = this.tabs.find((candidate) => candidate.documentId === documentId);
		if (!tab) return { ok: false, error: notOpen(documentId) };
		this.activeId = documentId;
		return { ok: true, activeDocumentId: documentId };
	}

	/**
	 * Close a tab. A dirty tab returns `needsDecision` without mutating
	 * anything (Save is handled by the caller, Discard passes `force`, Cancel
	 * simply stops). Closing the active tab selects the next sibling, else the
	 * previous one.
	 */
	close(documentId: string, options: { force?: boolean } = {}): CloseTabResult {
		const index = this.tabs.findIndex((tab) => tab.documentId === documentId);
		if (index < 0) return { ok: false, needsDecision: false, error: notOpen(documentId) };
		const tab = this.tabs[index];
		if (tab && this.isDirty(documentId) && options.force !== true) {
			return { ok: false, needsDecision: true, documentId, options: CLOSE_DECISIONS };
		}
		this.tabs.splice(index, 1);
		if (this.activeId === documentId) {
			const next = this.tabs[index] ?? this.tabs[index - 1] ?? null;
			this.activeId = next ? next.documentId : null;
		}
		return { ok: true, closed: documentId, activeDocumentId: this.activeId };
	}

	/**
	 * Evict tabs until at most `maxTabs` remain. Keeps the active and pinned
	 * tabs; evicts the oldest clean non-active tabs first; never evicts dirty
	 * tabs. `satisfied` reports whether the limit was actually reached.
	 */
	enforceOverflow(maxTabs: number): OverflowResult {
		const limit = Math.max(0, Math.floor(Number.isFinite(maxTabs) ? maxTabs : 0));
		const evicted: string[] = [];
		while (this.tabs.length > limit) {
			const index = this.tabs.findIndex(
				(tab) => tab.documentId !== this.activeId && !tab.pinned && !this.isDirty(tab.documentId),
			);
			if (index < 0) break;
			const removed = this.tabs.splice(index, 1)[0];
			if (removed) evicted.push(removed.documentId);
		}
		return { ok: true, evicted, satisfied: this.tabs.length <= limit };
	}

	/** Ordered copies; callers cannot corrupt the model. */
	list(): EditorTab[] {
		return this.tabs.map((tab) => ({ ...tab }));
	}

	activeDocumentId(): string | null {
		return this.activeId;
	}

	has(documentId: string): boolean {
		return this.tabs.some((tab) => tab.documentId === documentId);
	}

	count(): number {
		return this.tabs.length;
	}
}

function notOpen(documentId: string): TabError {
	return { code: "not-open", message: `tab is not open: ${documentId}` };
}
