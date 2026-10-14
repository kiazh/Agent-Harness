// Compact single-row tab strip for the current session's agent family: the
// main session (family root) plus the delegated child sessions that point at
// it (parentSessionId === root). Rendered as chrome between the transcript and
// the composer. Renders zero rows when the family has a single member, so
// single-agent use is visually unchanged.
//
// Keys: Ctrl+Shift+←/→ cycle the family (handled by App). The hint is shown
// inline while it still fits.

import { truncateToWidth, visibleWidth, type Component } from "@earendil-works/pi-tui";
import { ICONS } from "../design/icons.ts";
import { sanitizeDisplay } from "../design/text.ts";
import { theme } from "../theme.ts";

export interface SessionTab {
	id: string;
	/** Session title when meaningful, else a short id. */
	label: string;
	/** Family root — the main session the family is anchored to. */
	main: boolean;
	/** The visibly rendered session. */
	current: boolean;
	/** A turn is locally in flight for this session. */
	running: boolean;
}

/** Per-tab label budget so one chatty title cannot crowd out the strip. */
const MAX_LABEL = 24;
const TAB_HINT = "ctrl+shift+←/→";

export class SessionTabs implements Component {
	private tabs: SessionTab[] = [];

	setTabs(tabs: SessionTab[]): void {
		this.tabs = tabs;
	}

	/** The family row, or no rows at all for single-session use. */
	render(width: number): string[] {
		if (width <= 0 || this.tabs.length <= 1) return [];
		const segments = this.tabs.map((tab) => this.segment(tab));
		const sep = theme.dim(" · ");
		const line = segments.join(sep);
		const hint = theme.dim(`  ${TAB_HINT}`);
		if (visibleWidth(line) + visibleWidth(hint) <= width) {
			return [truncateToWidth(line + hint, width)];
		}
		if (visibleWidth(line) <= width) return [truncateToWidth(line, width)];
		return [truncateToWidth(this.windowed(segments, width), width)];
	}

	invalidate(): void {}

	/** One tab: current marker, run state, label, and the main marker. */
	private segment(tab: SessionTab): string {
		const marker = tab.current ? theme.accentBold(ICONS.selectedMarker) : " ";
		const state = tab.running ? theme.accent(ICONS.tabRunning) : theme.dim(ICONS.tabIdle);
		const label = truncateToWidth(sanitizeDisplay(tab.label), MAX_LABEL, "…");
		const main = tab.main ? theme.dim(" (main)") : "";
		const text = tab.current ? theme.accentBold(label) : tab.main ? theme.text(label) : theme.muted(label);
		return `${marker} ${state} ${text}${main}`;
	}

	/**
	 * Narrow terminals keep the current tab and grow outward while room
	 * remains; clipped sides carry ‹ › markers plus the position.
	 */
	private windowed(segments: string[], width: number): string {
		const index = Math.max(0, this.tabs.findIndex((tab) => tab.current));
		const total = segments.length;
		const parts = [segments[index]!];
		const position = theme.dim(` (${index + 1}/${total})`);
		let left = index;
		let right = index;
		let used = visibleWidth(parts[0]!) + visibleWidth(position) + 2;
		for (;;) {
			let grew = false;
			if (left - 1 >= 0 && used + visibleWidth(segments[left - 1]!) + 3 <= width) {
				left -= 1;
				parts.unshift(segments[left]!);
				used += visibleWidth(segments[left]!) + 3;
				grew = true;
			}
			if (right + 1 < total && used + visibleWidth(segments[right + 1]!) + 3 <= width) {
				right += 1;
				parts.push(segments[right]!);
				used += visibleWidth(segments[right]!) + 3;
				grew = true;
			}
			if (!grew) break;
		}
		const lead = left > 0 ? theme.dim("‹ ") : "";
		const trail = right < total - 1 ? theme.dim(" ›") : "";
		return `${lead}${parts.join(theme.dim(" · "))}${trail}${position}`;
	}
}
