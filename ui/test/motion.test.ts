import assert from "node:assert/strict";
import { test } from "node:test";
import { stripTerminalSequences, visibleWidth } from "@earendil-works/pi-tui";
import { ICONS } from "../src/design/icons.ts";
import { AnimationCoordinator } from "../src/design/motion.ts";
import { Footer } from "../src/components/Footer.ts";

function coordinatorWithCounter(reducedMotion = false): { coordinator: AnimationCoordinator; renders: () => number } {
	let renders = 0;
	const coordinator = new AnimationCoordinator(() => renders++, reducedMotion);
	return { coordinator, renders: () => renders };
}

test("working footer pulses without relying on thinking or tool subscriptions", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator, renders } = coordinatorWithCounter();
	const footer = new Footer(coordinator);
	t.after(() => { footer.dispose(); coordinator.dispose(); });
	footer.status = "working";
	const first = footer.render(80)[0]!;
	assert.match(stripTerminalSequences(first), /▪$/);
	assert.equal(coordinator.timerCount, 1);
	t.mock.timers.tick(200);
	const next = footer.render(80)[0]!;
	assert.equal(renders(), 1, "streaming text must not strand the footer's animation");
	assert.notEqual(next.slice(next.lastIndexOf("\x1b[", next.indexOf("▪"))), first.slice(first.lastIndexOf("\x1b[", first.indexOf("▪"))), "the corner itself changes brightness");
	assert.match(stripTerminalSequences(next), /▪$/);
	for (const status of ["awaiting_approval", "stopping", "cleanup_pending", "offline", "ready"] as const) {
		footer.status = status;
		assert.equal(coordinator.timerCount, 0, `${status} retires working motion`);
		assert.match(stripTerminalSequences(footer.render(80)[0]!), /□$/);
		footer.status = "working";
	}
	footer.dispose();
	assert.equal(coordinator.timerCount, 0);
});

test("working and learning share one timer, with static reduced motion", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator, renders } = coordinatorWithCounter();
	const footer = new Footer(coordinator);
	t.after(() => { footer.dispose(); coordinator.dispose(); });
	footer.status = "working";
	footer.pulseGrowth();
	assert.equal(coordinator.timerCount, 1);
	t.mock.timers.tick(800);
	footer.render(80);
	assert.equal(coordinator.size, 1, "learning expires while working continues");
	footer.reducedMotion = true;
	assert.equal(coordinator.timerCount, 0);
	const first = footer.render(80)[0]!;
	const count = renders();
	t.mock.timers.tick(1000);
	assert.equal(footer.render(80)[0], first);
	assert.equal(renders(), count);
	footer.status = "ready";
	assert.equal(coordinator.size, 0);
});

test("default_activity_stays_one_cell_and_advances_every_200ms", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator, renders } = coordinatorWithCounter();
	t.after(() => coordinator.dispose());
	const activity = coordinator.subscribe("thinking");
	for (const frame of ICONS.toolActiveFrames) assert.equal(visibleWidth(frame), 1);
	assert.equal(coordinator.frame(ICONS.toolActiveFrames), "·");
	t.mock.timers.tick(199);
	assert.equal(renders(), 0, "activity must not request an early frame");
	assert.equal(coordinator.frame(ICONS.toolActiveFrames), "·");
	t.mock.timers.tick(1);
	assert.equal(renders(), 1);
	assert.equal(coordinator.frame(ICONS.toolActiveFrames), "•");
	for (const expected of ["●", "•", "·"]) {
		t.mock.timers.tick(200);
		assert.equal(coordinator.frame(ICONS.toolActiveFrames), expected);
	}
	activity.stop();
	assert.equal(coordinator.timerCount, 0);
	const settled = renders();
	t.mock.timers.tick(6000);
	assert.equal(renders(), settled, "idle activity must stop rendering");
});

test("unchanged_activity_demand_preserves_tick_cadence_until_disposal", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator, renders } = coordinatorWithCounter();
	coordinator.subscribe("thinking", 100);
	t.mock.timers.tick(300);
	assert.equal(renders(), 3, "each elapsed interval must render while demand is stable");
	assert.equal(coordinator.timerCount, 1);
	coordinator.dispose();
	t.mock.timers.tick(300);
	assert.equal(renders(), 3, "disposal cancels the continuing ticker");
	assert.equal(coordinator.timerCount, 0);
});

test("many_activity_sources_use_one_timer", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator } = coordinatorWithCounter();
	t.after(() => coordinator.dispose());
	coordinator.subscribe("thinking", 100);
	coordinator.subscribe("tool:fixture-turn-a:1", 100);
	coordinator.subscribe("tool:fixture-turn-a:2", 100);
	coordinator.subscribe("approval:fixture-session-a:req-1", 220);
	coordinator.subscribe("growth", 200, 800);
	assert.equal(coordinator.size, 5);
	assert.equal(coordinator.timerCount, 1);
	// Frames are fixed-width so labels never shift.
	for (const frame of ICONS.toolActiveFrames) assert.equal(visibleWidth(frame), 1);
	const a = coordinator.frame(ICONS.toolActiveFrames, 100, 1000);
	t.mock.timers.tick(100);
	const b = coordinator.frame(ICONS.toolActiveFrames, 100, 1000);
	assert.notEqual(a, b);
	// A second source started later begins at its own start on the same timer.
	t.mock.timers.tick(50);
	assert.equal(coordinator.frame(ICONS.toolActiveFrames, 100, 1150), ICONS.toolActiveFrames[0]);
});

test("reused_key_old_handle_cannot_stop_new_subscription", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator } = coordinatorWithCounter();
	t.after(() => coordinator.dispose());
	const first = coordinator.subscribe("tool:fixture-turn-a:1", 100);
	const second = coordinator.subscribe("tool:fixture-turn-a:1", 100);
	assert.equal(coordinator.size, 1);
	first.stop();
	assert.equal(coordinator.size, 1, "stale handle must not kill the newer subscription");
	second.stop();
	assert.equal(coordinator.size, 0);
	assert.equal(coordinator.timerCount, 0);
	first.stop();
	assert.equal(coordinator.size, 0, "stop is idempotent");
});

test("dispose_prevents_future_renders", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator, renders } = coordinatorWithCounter();
	coordinator.subscribe("thinking", 100);
	assert.equal(coordinator.timerCount, 1);
	coordinator.dispose();
	assert.equal(coordinator.timerCount, 0);
	assert.equal(coordinator.size, 0);
	const settled = renders();
	t.mock.timers.tick(5000);
	assert.equal(renders(), settled, "no callbacks after dispose");
	const noop = coordinator.subscribe("thinking", 100);
	assert.equal(coordinator.size, 0);
	assert.equal(coordinator.timerCount, 0);
	noop.stop();
});

test("reduced_motion_has_no_frame_timer", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator, renders } = coordinatorWithCounter(true);
	t.after(() => coordinator.dispose());
	coordinator.subscribe("thinking", 100);
	coordinator.subscribe("tool:fixture-turn-a:1", 100);
	assert.equal(coordinator.timerCount, 0, "recurring activity never ticks in reduced motion");
	const first = coordinator.frame(ICONS.toolActiveFrames, 100, 1000);
	t.mock.timers.tick(1000);
	assert.equal(coordinator.frame(ICONS.toolActiveFrames, 100, 1000), first, "static in reduced motion");
	// A finite acknowledgment may use the single one-shot deadline timer.
	const before = renders();
	coordinator.subscribe("growth", 200, 800);
	assert.equal(coordinator.timerCount, 1);
	t.mock.timers.tick(800);
	assert.equal(coordinator.timerCount, 0);
	assert.ok(renders() > before, "final idle render after expiry");
});

test("growth_pulse_expires_and_cooldown_coalesces", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	let renders = 0;
	const coordinator = new AnimationCoordinator(() => renders++, false);
	const footer = new Footer(coordinator);
	t.after(() => {
		footer.dispose();
		coordinator.dispose();
	});
	const idle = footer.render(80).map((line) => stripTerminalSequences(line)).join("\n");
	assert.match(idle, /□$/);
	assert.equal(footer.pulseGrowth(), true);
	const lit = footer.render(80)[0]!;
	assert.notEqual(stripTerminalSequences(lit), idle);
	t.mock.timers.tick(200);
	const advanced = footer.render(80)[0]!;
	assert.notEqual(advanced, lit, "learning square brightness must pulse");
	assert.equal(stripTerminalSequences(advanced), stripTerminalSequences(lit), "square shape and footer positions stay fixed");
	// A burst of 20 updates never restarts or queues the acknowledgment.
	for (let i = 0; i < 20; i++) assert.equal(footer.pulseGrowth(), false);
	assert.equal(footer.render(80)[0], advanced);
	t.mock.timers.tick(600);
	assert.equal(footer.render(80).map((line) => stripTerminalSequences(line)).join("\n"), idle);
	const settled = renders;
	t.mock.timers.tick(20000);
	assert.equal(renders, settled, "no background rendering while idle");
	assert.equal(footer.pulseGrowth(), false, "cooldown refuses before 30s");
	t.mock.timers.tick(10000);
	assert.equal(footer.pulseGrowth(), true, "cooldown accepts at the 30s boundary");
});

test("growth_dispose_during_pulse_stops_renders", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	let renders = 0;
	const coordinator = new AnimationCoordinator(() => renders++, false);
	const footer = new Footer(coordinator);
	assert.equal(footer.pulseGrowth(), true);
	footer.dispose();
	coordinator.dispose();
	const settled = renders;
	t.mock.timers.tick(5000);
	assert.equal(renders, settled);
});

test("growth_pulse_reduced_motion_is_static_then_idle", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	let renders = 0;
	const coordinator = new AnimationCoordinator(() => renders++, true);
	const footer = new Footer(coordinator);
	t.after(() => {
		footer.dispose();
		coordinator.dispose();
	});
	footer.pulseGrowth();
	const initial = footer.render(40).map((line) => stripTerminalSequences(line)).join("\n");
	t.mock.timers.tick(400);
	assert.equal(footer.render(40).map((line) => stripTerminalSequences(line)).join("\n"), initial);
	t.mock.timers.tick(1000);
	assert.match(footer.render(40).map((line) => stripTerminalSequences(line)).join("\n"), /□$/);
});

test("footer_keeps_right_edge_on_one_row", (t) => {
	t.mock.timers.enable({ apis: ["Date", "setInterval", "setTimeout"], now: 1000 });
	const { coordinator } = coordinatorWithCounter();
	const footer = new Footer(coordinator);
	t.after(() => {
		footer.dispose();
		coordinator.dispose();
	});
	for (const width of [20, 40, 80, 120]) {
		const lines = footer.render(width);
		assert.equal(lines.length, 1);
		assert.ok(lines.every((line) => visibleWidth(line) <= width));
		assert.match(stripTerminalSequences(lines[0]!), /[□▪■▣]$/);
	}
	footer.model = "codex-model";
	footer.cwd = "/Users/dev/Agent-Harness1";
	footer.branch = "main";
	footer.tokens = 12400;
	footer.compact = true;
	const narrow = stripTerminalSequences(footer.render(40)[0]!);
	assert.match(narrow, /12,400 tokens/, "token count survives narrow widths");
	assert.match(narrow, /[□▪■▣]$/, "square stays docked at 40 columns");
});
