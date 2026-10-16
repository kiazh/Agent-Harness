// Footer workspace hints (discoverability pass): the hint segment shows the
// explorer/panes keys only while the workspace is available, and drops first
// when the row runs out of room.
import { test } from "node:test";
import assert from "node:assert/strict";
import { stripTerminalSequences } from "@earendil-works/pi-tui";
import { Footer } from "../src/components/Footer.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";

function plain(footer: Footer, width: number): string {
	return stripTerminalSequences(footer.render(width)[0] ?? "");
}

test("workspace hints render before the shortcuts hint when they fit", () => {
	const footer = new Footer(new AnimationCoordinator(() => {}));
	footer.model = "m";
	footer.workspaceHints = "Ctrl+T explorer · Ctrl+←/→ panes";
	const row = plain(footer, 160);
	assert.ok(row.includes("Ctrl+T explorer"), row);
	assert.ok(row.includes("? shortcuts · / commands"), row);
	assert.ok(row.indexOf("Ctrl+T explorer") < row.indexOf("? shortcuts"), "workspace hints come first");
});

test("workspace hints drop before the shortcuts hint on narrow widths", () => {
	const footer = new Footer(new AnimationCoordinator(() => {}));
	footer.model = "m";
	footer.workspaceHints = "Ctrl+T explorer · Ctrl+←/→ panes";
	const narrow = plain(footer, 60);
	assert.ok(!narrow.includes("Ctrl+T explorer"), "hint dropped when it cannot fit");
	assert.ok(narrow.includes("shortcuts"), "shortcuts hint kept");
});

test("compact mode hides workspace hints entirely", () => {
	const footer = new Footer(new AnimationCoordinator(() => {}));
	footer.model = "m";
	footer.workspaceHints = "Ctrl+T explorer";
	footer.compact = true;
	const row = plain(footer, 160);
	assert.ok(!row.includes("Ctrl+T explorer"));
});

test("no workspace hints leaves the footer exactly as before", () => {
	const footer = new Footer(new AnimationCoordinator(() => {}));
	footer.model = "m";
	const row = plain(footer, 160);
	assert.ok(row.includes("? shortcuts · / commands"));
	assert.ok(!row.includes("explorer"));
});
