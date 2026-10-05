// Plain-text formatting helpers for command output.

import { theme } from "./theme.ts";

/**
 * Codex-style keyboard hints: bold key labels with dim descriptions,
 * joined by ` · ` — e.g. `Enter send · Esc stop`.
 */
export function hintLine(pairs: Array<[label: string, action: string]>): string {
	const sep = theme.dim(" · ");
	return pairs.map(([label, action]) => `${theme.bold(label)} ${theme.dim(action)}`).join(sep);
}

/** First 8 characters of an id, as shown everywhere in the UI. */
export function shortId(id: string): string {
	return id.slice(0, 8);
}

/** Local `YYYY-MM-DD HH:MM`, or an em dash when missing. */
export function when(iso: string | null | undefined): string {
	if (!iso) return "—";
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return "—";
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function clip(text: string, max: number): string {
	const flat = text.replace(/\s+/g, " ").trim();
	return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** Fixed-width table: one header row, a rule, then rows. Cells are clipped to *maxCell*. */
export function table(headers: string[], rows: string[][], maxCell = 48): string {
	const cells = rows.map((row) => headers.map((_, i) => clip(row[i] ?? "", maxCell)));
	const widths = headers.map((h, i) => Math.max(h.length, ...cells.map((row) => row[i]!.length)));
	const line = (row: string[]) =>
		row
			.map((cell, i) => (i === row.length - 1 ? cell : cell.padEnd(widths[i]!)))
			.join("  ")
			.trimEnd();
	return [line(headers), widths.map((w) => "─".repeat(w)).join("  "), ...cells.map(line)].join("\n");
}

/** `key: value` lines with aligned values. */
export function keyValues(pairs: Array<[string, unknown]>): string {
	const width = Math.max(...pairs.map(([k]) => k.length));
	return pairs.map(([k, v]) => `${k.padEnd(width)}  ${v === undefined || v === null || v === "" ? "—" : String(v)}`).join("\n");
}

/** Resolve an id or unique id prefix among *ids*. Throws a readable error otherwise. */
export function resolvePrefix(prefix: string, ids: string[], what: string): string {
	const wanted = prefix.trim().toLowerCase();
	if (!wanted) throw new Error(`Give a ${what} id.`);
	const exact = ids.find((id) => id === wanted);
	if (exact) return exact;
	const matches = ids.filter((id) => id.startsWith(wanted));
	if (matches.length === 1) return matches[0]!;
	if (matches.length === 0) throw new Error(`No ${what} starts with "${prefix}".`);
	throw new Error(`"${prefix}" matches ${matches.length} ${what}s; use more characters.`);
}
