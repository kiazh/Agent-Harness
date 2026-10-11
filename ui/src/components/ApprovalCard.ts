// Exact approval review: a dedicated bounded bordered widget — never a
// warning dump plus a generic picker. Shows the sanitized argv with argument
// boundaries, session/turn/request binding, cwd, timeout, capabilities,
// network, content identity and bounded evidence. Deny is selected first.
// Allow options are refused while critical fields do not fit.

import { Key, matchesKey, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component, type TUI } from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { selectionBar, theme } from "../theme.ts";
import { panelContentWidth, renderToolPanel } from "./ToolPanel.ts";
import { DiffBlock } from "./DiffBlock.ts";

export interface ApprovalDetails {
	requestId: string;
	sessionId: string;
	originTurnId?: string;
	transportTurnId?: string;
	operation: string;
	tool?: string;
	target: string;
	argv: string[];
	cwd: string;
	backend: string;
	shell?: string;
	timeout?: number;
	capabilities: string[];
	network?: string[];
	contentDigest?: string;
	contentPreview?: string;
	contentLength?: number;
	fileDiff?: { diff?: string; truncated?: boolean };
	durable?: boolean | null;
	status?: string;
}

export interface ApprovalDecision {
	verdict: "approved" | "denied";
	grant: "once" | "session";
}

export interface ApprovalCardOptions {
	tui?: TUI;
	docked?: boolean;
	onDecision?: (decision: ApprovalDecision) => void;
}

const DECISIONS: Array<{ label: string; decision: ApprovalDecision }> = [
	{ label: "Allow once", decision: { verdict: "approved", grant: "once" } },
	{ label: "Allow for this session", decision: { verdict: "approved", grant: "session" } },
	{ label: "Deny", decision: { verdict: "denied", grant: "once" } },
];

export class ApprovalCard implements Component {
	private readonly details: ApprovalDetails;
	private readonly docked: boolean;
	private recorded = "needs approval";
	private readonly tui: TUI | undefined;
	private readonly onDecision?: (decision: ApprovalDecision) => void;
	private selected = 2;
	private evidenceTop = 0;
	private resolving = false;
	private error = "";
	private lastWidth = 80;
	private lastRows = 24;

	constructor(details: ApprovalDetails, options: ApprovalCardOptions = {}) {
		this.details = details;
		this.docked = options.docked ?? false;
		this.tui = options.tui;
		this.onDecision = options.onDecision;
	}

	/** True while critical fields do not fit (Allow refused, Deny/Esc still safe). */
	isAllowBlocked(): boolean {
		return this.allowBlocked(this.tui?.terminal.columns ?? this.lastWidth, this.tui?.terminal.rows ?? this.lastRows);
	}

	setResolving(resolving: boolean): void {
		this.resolving = resolving;
	}

	setError(message: string): void {
		this.error = sanitizeDisplay(message);
		this.resolving = false;
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.escape)) {
			if (!this.resolving) this.onDecision?.({ verdict: "denied", grant: "once" });
			return;
		}
		if (this.resolving) return;
		if (matchesKey(data, Key.enter)) {
			this.confirm();
			return;
		}
		if (matchesKey(data, Key.left) || matchesKey(data, Key.up)) {
			this.selected = (this.selected + DECISIONS.length - 1) % DECISIONS.length;
			return;
		}
		if (matchesKey(data, Key.right) || matchesKey(data, Key.down)) {
			this.selected = (this.selected + 1) % DECISIONS.length;
			return;
		}
		if (matchesKey(data, Key.pageUp)) {
			this.evidenceTop = Math.max(0, this.evidenceTop - 5);
			return;
		}
		if (matchesKey(data, Key.pageDown)) {
			this.evidenceTop += 5;
			return;
		}
		if (matchesKey(data, Key.home)) {
			this.evidenceTop = 0;
			return;
		}
		if (matchesKey(data, Key.end)) {
			this.evidenceTop = Number.MAX_SAFE_INTEGER;
			return;
		}
	}

	private confirm(): void {
		const choice = DECISIONS[this.selected];
		if (!choice) return;
		// Refusing approval selection on an unreviewable clipping is safe;
		// denial stays available and never approves unseen actions.
		if (choice.decision.verdict === "approved" && this.isAllowBlocked()) return;
		this.resolving = true;
		this.onDecision?.(choice.decision);
	}

	private allowBlocked(width: number, rows: number): boolean {
		const chrome = 2 + 8 + 2 + (this.error ? 1 : 0) + 1;
		return rows < chrome || width < 30;
	}

	private evidenceRows(): string[] {
		const d = this.details;
		const clean = (text: string): string => sanitizeDisplay(text);
		const rows: string[] = [];
		rows.push(`Operation: ${d.tool ?? d.operation}`);
		rows.push(`Target: ${d.target}`);
		rows.push(`argv: ${d.argv.map((a) => JSON.stringify(clean(a))).join(" ") || "(none)"}`);
		rows.push(`Session: ${d.sessionId.slice(0, 8)}  turn: ${(d.originTurnId ?? d.transportTurnId ?? "").slice(0, 8)}  request: ${d.requestId.slice(0, 8)}`);
		rows.push(`cwd: ${d.cwd || "(unknown)"}  backend: ${d.backend || "(unknown)"}`);
		rows.push(`Timeout: ${d.timeout === undefined ? "unspecified" : `${d.timeout}s`}`);
		rows.push(`Capabilities: ${d.capabilities.length ? d.capabilities.join(", ") : "(none)"}`);
		if (d.network?.length) rows.push(`Network: ${d.network.join(", ")}`);
		if (d.shell) rows.push(`Shell: ${d.shell}`);
		if (d.contentDigest) {
			rows.push(`Content: ${clean(d.contentDigest)} (${d.contentLength ?? 0} chars)`);
		}
		if (d.contentPreview) {
			rows.push("Preview:");
			for (const line of clean(d.contentPreview).split("\n").slice(0, 6)) rows.push(`  ${line}`);
			if (clean(d.contentPreview).split("\n").length > 6) rows.push("  … preview truncated");
		}
		if (d.durable === false) rows.push("! Local-only approval (database unavailable); cross-worker guarantees are degraded.");
		return rows.map((line) => clean(line));
	}

	render(width: number): string[] {
		if (this.docked) return this.renderDock(width);
		const total = Math.max(0, width);
		const content = Math.max(1, width - 4);
		this.lastWidth = width;
		this.lastRows = this.tui?.terminal.rows ?? 24;
		const blocked = this.allowBlocked(width, this.lastRows);
		const wrap = (line: string): string[] => wrapTextWithAnsi(line, content);
		const title = " Approval required ";
		const top = `┌─${title}${"─".repeat(Math.max(0, content + 1 - visibleWidth(title)))}┐`;
		const bottomHint = this.resolving
			? "Resolving…"
			: `←→/↑↓ choose · enter confirm · esc deny · pgup/pgdn evidence`;
		const bottom = `└─${bottomHint}${"─".repeat(Math.max(0, content + 1 - visibleWidth(bottomHint)))}┘`;
		const out = [truncateToWidth(top, total)];
		const header = this.evidenceRows().flatMap(wrap);
		const diff = this.details.fileDiff?.diff
			? new DiffBlock(sanitizeDisplay(this.details.fileDiff.diff).split("\n"), 6).render(content)
			: [];
		if (this.details.fileDiff?.truncated) diff.push(theme.dim("… diff truncated"));
		const evidence = [...header, ...diff];
		const chromeRows = 1 + 2 + (this.error ? 1 : 0) + 1 + (blocked ? 1 : 0) + 1;
		const avail = Math.max(4, Math.min(Math.floor((this.lastRows * 70) / 100), this.lastRows - 2));
		const budget = Math.max(1, avail - chromeRows);
		const maxTop = Math.max(0, evidence.length - budget);
		this.evidenceTop = Math.max(0, Math.min(this.evidenceTop, maxTop));
		const windowed = evidence.slice(this.evidenceTop, this.evidenceTop + budget);
		const row = (line: string): string => {
			const body = `${truncateToWidth(line, content)}${" ".repeat(Math.max(0, content - visibleWidth(truncateToWidth(line, content))))}`;
			return `│ ${body} │`;
		};
		for (const line of windowed) out.push(row(line));
		if (evidence.length > budget) {
			out.push(row(theme.dim(`… ${evidence.length - budget} more evidence rows (pgup/pgdn)`)));
		}
		if (blocked) {
			out.push(row(theme.warning("! Resize to review — Allow blocked.")));
		}
		if (this.error) out.push(row(theme.error(`✗ ${this.error}`)));
		const options = DECISIONS.map((option, i) => {
			const mark = i === this.selected ? ICONS.selectedMarker : " ";
			const label = i === this.selected ? theme.accentBold(option.label) : option.label;
			return `${mark} ${label}`;
		}).join(theme.dim(" · "));
		out.push(row(this.resolving ? theme.dim("Resolving…") : options));
		out.push(row(theme.dim("Session grants reuse authority for this operation only; changed content asks again.")));
		out.push(truncateToWidth(bottom, total));
		return out.map((line) => truncateToWidth(line, total));
	}

	/** Evidence stays in the retained transcript; only decisions occupy the dock. */
	readonly evidence: Component = {
		render: (width: number): string[] => this.renderEvidence(width),
		invalidate(): void {},
	};

	setRecorded(status: string): void {
		this.recorded = sanitizeDisplay(status);
	}

	private renderEvidence(width: number): string[] {
		const content = panelContentWidth(width);
		const d = this.details;
		const title = `${theme.warning("●")} ${sanitizeDisplay(d.tool ?? d.operation)} ${theme.muted(sanitizeDisplay(d.target))} · ${theme.warning(this.recorded)}`;
		const headerOverflows = visibleWidth(title) > content || /[\r\n]/.test(`${d.tool ?? d.operation}${d.target}`);
		// Overflowed titles must remain fully reviewable, including target suffixes.
		const fields = this.evidenceRows();
		const evidence = (headerOverflows ? fields : fields.slice(2)).flatMap(line => wrapTextWithAnsi(line, content));
		const diff = d.fileDiff?.diff ? new DiffBlock(sanitizeDisplay(d.fileDiff.diff).split("\n"), 8).render(content) : [];
		if (d.fileDiff?.truncated) diff.push(theme.dim("… diff truncated"));
		const all = [...evidence, ...diff];
		// Reserve dock (6), composer (3), footer (1), gaps (3), panel chrome (6).
		const budget = Math.max(2, Math.min(16, (this.tui?.terminal.rows ?? 40) - 19 - (this.error ? 1 : 0)));
		this.evidenceTop = Math.min(this.evidenceTop, Math.max(0, all.length - budget));
		const windowed = all.slice(this.evidenceTop, this.evidenceTop + budget);
		const more = all.length > budget ? [theme.dim(`Evidence ${this.evidenceTop + 1}–${Math.min(all.length, this.evidenceTop + budget)}/${all.length} · PgUp/PgDn review`)] : [];
		return ["", ...renderToolPanel([title, "", ...windowed, ...more], width)];
	}

	private renderDock(width: number): string[] {
		this.lastWidth = width;
		this.lastRows = this.tui?.terminal.rows ?? 24;
		const clean = sanitizeDisplay;
		const title = `Allow ${clean(this.details.tool ?? this.details.operation)} · ${clean(this.details.target)}?`;
		const request = theme.dim(`request: ${clean(this.details.requestId).slice(0, 16)}`);
		const heading = truncateToWidth(title, Math.max(0, width - visibleWidth(request) - 3));
		const first = width >= 80 ? `${heading}${" ".repeat(Math.max(1, width - visibleWidth(heading) - visibleWidth(request)))}${request}` : truncateToWidth(title, width);
		const descriptions = ["Only this exact action", "Where supported by policy", "Do not run this action"];
		const choices = DECISIONS.map((option, i) => {
			const mark = i === this.selected ? "›" : " ";
			const description = width >= 60 ? `  ${theme.dim(descriptions[i]!)}` : "";
			const line = truncateToWidth(` ${mark}  ${option.label}${description}`, width);
			return i === this.selected ? selectionBar(line, width) : line;
		});
		const blocked = this.isAllowBlocked();
		const hints = this.resolving ? "Resolving…" : "↑↓ choose · Enter select · Esc deny";
		const out = [theme.border("─".repeat(Math.max(0, width))), first, ...choices];
		if (this.error) out.push(theme.error(truncateToWidth(this.error, width)));
		else if (blocked) out.push(theme.warning("Resize to review · Allow blocked"));
		out.push(theme.dim(truncateToWidth(hints, width)));
		return out.map(line => truncateToWidth(line, width));
	}

	invalidate(): void {}
}
