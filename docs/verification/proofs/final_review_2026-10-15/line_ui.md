# Final review — line pass, UI slice

- Scope: `ui/src/**/*.ts` (~56 files) + skim of `ui/test/*.test.ts`.
- Mode: READ-ONLY, no runtime execution; findings verified by reading call
  sites, producers/handlers, and the gateway twins.
- Tree: `main` @ `1e1edbf`. **Post-fix state**: committed recent fixes
  (R4-022/023, R4-024, R4-065, R4-094, R5-D, R5-F, R5-QR) plus the
  uncommitted final-review UI fix lane (working tree, mtime 00:41–00:45):
  `components.ts`, `components/OAuthProgress.ts`, `components/OutputViewer.ts`,
  `components/ToolTurn.ts`, `design/theme.ts`, `features/agents.ts`,
  `test/design.test.ts`, and the new `test/final-review-ui-fix.test.ts`.
- Skills applied: `code-reviewer`, `senior-frontend`.
- Provenance: this file merges the baseline UI pass (first written 00:36) with
  an independent post-fix reconciliation. Items marked **[FIXED]** were open in
  the baseline and are landed in the current worktree; items marked **RESIDUAL**
  survive the fix lane.

## Verdict

No P1 or P2 defects confirmed in the slice. One **RESIDUAL P3** (the `dim`
contrast fix covers `default`/`mono` but not the runtime-default `codex` skin),
one **P3** (a write-only private field), and several Q (dead registry surfaces,
white-box tests, minor hygiene). Protocol/events parity, timeout/budget
consistency, escaping, and every targeted recent fix verified clean.

---

## Confirmed findings

### P3-1 [FIXED] — Retained-output viewer rendered the tool name/ID unsanitized
Baseline: `ui/src/components/OutputViewer.ts:164` interpolated `this.record.name`
and `this.record.toolId.slice(0,8)` raw into the header row.
- Producer: `app.ts:1338` `outputStore.put(sessionId, event.id, event.name, …)`;
  `OutputStore.put` stores `name` verbatim; the viewer body was sanitized
  (`:129`, `:149`) but the header was not — the lone raw untrusted-text path,
  breaking the `design/text.ts:3-4` contract ("untrusted tool/approval/user
  content must pass through `sanitizeDisplay` first"). Every other tool-name
  surface sanitizes: `ToolTurn.ts:64`, `Menu.ts:168/174`.
- Falsification: traced the name origin through `agent.py` →
  `turn_engine.py:347/358` (`"name": event.tool_name`) →
  `gateway/server.py:1002/1010`. Backend MCP names are `[A-Za-z0-9_]` and
  built-ins are fixed, so exploitation needs a misbehaving provider; still a
  defensive inconsistency → P3.
- Fix (worktree): `OutputViewer.ts:165-166` now
  `sanitizeDisplay(this.record.name).replace(/\n/g," ")` and the same for the
  8-char id (newlines flattened because the header is one physical row).
  Covered by `ui/test/final-review-ui-fix.test.ts:14-38`.

### P3-2 [PARTIALLY FIXED — RESIDUAL] — `dim` contrast below WCAG AA; the fix
misses the runtime-default skin
Baseline: `design/theme.ts:75` `default.dim="#5f5f5f"` on `#0a0a0a` = **3.10:1**;
`:347` `mono.dim="#6e6e6e"` on `#101010` = **3.73:1** (AA normal text 4.5:1).
- Fix (worktree): `default.dim → "#7c7c7c"` = **4.74:1** (`theme.ts:75`) and
  `mono.dim → "#808080"` = **4.82:1** (`theme.ts:347`). New tests
  `ui/test/design.test.ts:81-102` and `ui/test/final-review-ui-fix.test.ts:40-58`
  pin `["default","mono"]`.
- **Residual:** the app's actual startup skin is **`codex`** —
  `design/theme.ts:626` `let currentSkin = "codex"`, and `App.start()` only
  overrides it from `config.theme` (`app.ts:283`). `codex.base.dim =
  CODEX_SEMANTIC_START.faint = "#5d5d66"` (`theme.ts:219`, `tokens.ts:117`) on
  `CODEX_SEMANTIC_START.background = "#0b0b0c"` (`tokens.ts:111`) = **3.02:1**,
  still below AA. The new tests assert only `default`/`mono`, so the gap is
  unguarded. `opencode` shares the same failing pair (`theme.ts` `opencode`
  block / `tokens.ts`); `tokyonight.dim` is 2.76:1.
- Consumers of `theme.dim` (non-decorative; the test's own rationale calls it
  "essential"): footer status/hints/metadata/tokens (`Footer.ts:126,127,129,
  132,134`), menu title/filter/no-match/hint/section rows (`Menu.ts:298,304,
  307,308,311,319,331`), tool status lines (`ToolTurn.ts:90,100,102,110,116,
  120,141,143`), header cwd/branch (`components.ts:109`).
- Falsification: `selectionInkColor` (`theme.ts:662-671`) guarantees ≥4.5:1
  **only** for the selected-row fill, and the file header scopes the guarantee
  to selection rows — so `dim` is a deliberately unconstrained token, not a
  regression. Meaning is redundantly carried by text markers (`Notice`,
  `ToolTurn`), so impact is lowered → P3, not P2.
- Fix direction (one line): apply the same ≥4.5:1 derivation to every skin's
  `dim` (e.g. lift `codex`/`opencode`/`tokyonight` `faint`), and extend the
  test to iterate `SKIN_NAMES` (or at least the startup skin).

### P3-3 — `App.inFlightSessionId` is write-only dead state
`ui/src/app.ts:118` (`private inFlightSessionId`), set at `:1121`, reset at
`:1229-1230`, and never read for any decision.
- Falsification: `grep -rn inFlightSessionId ui/src ui/test` returns only those
  four sites — assign, self-compare in `clearInFlight`, clear. No consumer reads
  it; the "most recent in-flight turn for cancel" comment is stale because
  `cancel()` (`:1234-1247`) iterates the whole `inFlight` set instead.
- Impact: dead state only (the fix wave removed sibling dead code —
  `windowAroundCursor`, `ToolTurn.setOutcome/getState`); this one was missed.
  → P3 (dead code), fix: delete the field and its three sites.

---

## Q notes (quality; not defects)

- **Q-1 dead exported/registered surfaces (grep: definitions only).**
  - `ui/src/design/text.ts:102` `displayWidth` — zero callers.
  - `ui/src/components/Menu.ts:339` `isMenuSelection` — zero callers.
  - `ui/src/design/icons.ts:35,56` `ASCII_FALLBACKS` + `iconFor` — zero callers;
    14 `ICONS` entries are unreferenced (`userPrefix`, `assistantPrefix`,
    `assistantIdle`, `toolRunningDouble`, `noticeSuccess`, `noticeWarning`,
    `noticeError`, `successAlt`, `errorAlt`, `selectedMarkerAscii`,
    `separatorDot`, `ellipsisMore`, `diffHunk`, `growthFrames`).
  - `ui/src/design/theme.ts:578` `LEGACY_SLOT_TO_SEMANTIC` (+ re-export
    `theme.ts:7`) — zero callers.
  - `ui/src/design/tokens.ts` registered-but-unused: `VIEWPORT`,
    `toolPreviewLinesLegacy`, `pickerVisibleRowsDefault`, `composerMinRows`,
    `maxCellWidth`, `formMaxLengthPersona`.
  - Disposition: the fix wave already removed `windowAroundCursor`
    (`components.ts`), `ToolTurn.setOutcome`/`getState`; the above are the
    remaining residue. `displayWidth`/`isMenuSelection`/`LEGACY_SLOT_TO_SEMANTIC`
    are clear deletions; `ICONS`/`VIEWPORT` are arguable "reserved" registries —
    keep only if a documented catalog is intended, otherwise prune.
- **Q-2 tests are white-box, not behavior-first.** Most suites drive `App`
  through `as unknown as` casts and reach private state — e.g.
  `ui/test/state.test.ts:311` (`approvalDialogs`, `footer.status`),
  `app.test.ts:185,190` (`submitTurn`/`cancel`). This pins internal field
  names/shape rather than observable behavior. `r4-approval-expiry.test.ts`
  uses a `FakeTUI` whose `showOverlay` ignores its options, so overlay geometry
  is not exercised there.
- **Q-3 [FIXED] OAuthProgress `"c"` branch fell through.** Baseline
  `components/OAuthProgress.ts` set `cancelled` + `onCancel()` without
  `return`; worktree adds `return` (`:81`). Behaviorally harmless (no arrow key
  equals `"c"`), now explicit. Covered by `final-review-ui-fix.test.ts:60-70`.
- **Q-4 [FIXED] `agents.ts` shadowed parameter + indent drift.** Baseline
  `case "save"` used a block-local `const args` shadowing the `run(args, host)`
  parameter; worktree renames it `saveArgs` (`features/agents.ts:98`) and
  re-indents cases 98-141. Legal before, clearer now.
- **Q-6 `/provider add` inline key leaks into editor history and is ignored.**
  `app.ts:1075` only exempts `/keys set` from `editor.addToHistory`; every other
  command's text is recorded. `settings.ts:129-131` reads only `parts[0]`/`parts[1]`
  of `/provider add`, so a third token (`/provider add name url KEY`) is
  silently discarded while `KEY` is stored in the session's in-memory input
  history (Up-arrow). Undocumented form, in-memory only → Q (secret hygiene);
  either consume-and-redact or reject a third token. (The R5-QR lane touched
  this flow, so it is worth a one-line guard.)
- **Q-7 `format.ts table` pads by UTF-16 length, not display width.** `clip`
  and `padEnd` (`format.ts:28-42`) count code units, so CJK/emoji cells
  misalign columns in `/sessions`, `/keys`, `/status`, etc. Cosmetic (rows are
  later sanitized/wrapped); pre-existing.
- **Q-8 `transcript.ts:224-257` mixed indentation** in the `apply` switch
  (cosmetic, no behavior change).

---

## Falsified candidates (checked and cleared)

- **Protocol drift — none.** `ui/src/events.ts` is consistent with
  `machina/protocol/events.json` (all canonical v2 types + the two legacy
  `permission.*` aliases), enforced by `ui/test/protocol-parity.test.ts`.
  `needs_approval` wire fields match `machina/protocol/__init__.py:95`
  `approval_fields` exactly (requestId/operation/tool/target/argv/shell/cwd/
  backend/timeout/network/capabilities/contentDigest/contentPreview/
  contentLength/fileDiff/durable/status/originTurnId). `tool.complete`
  (`turn_engine.py:356-365` → `server.py:1007-1015`), `message.complete`
  (`engine.build_terminal`), `growth.status`, `auth.chatgpt.*`,
  `approval.resolved` (`server.py:898-902`) all match the TS readers.
  `session.setModel` returns `{sessionId,model,provider}`
  (`features/sessions.py:87-91`) matching `adoptSessionRoute` (`app.ts:358-371`);
  `mode.get`/`mode.set`/`mode.revoke` all return `.mode`
  (`features/mode.py:24-29,128,151,155,157`). Every method string in
  `ui/src` has a gateway handler.
- **`approval.resumed` dormancy — not a defect.** The stdio gateway never
  yields it (`server.py:989-1038` does not handle `approval_resumed`, and the
  stdio path never sets `TurnSpec.resume`); it is emitted by the SSE transport
  (`api/routes/streaming.py:234-243`). The TUI handler (`app.ts:1386-1405`) is
  transport-parity code, dormant but correct; `events.ts` still requires the
  member (SSE emits it), so it is not a dead type.
- **Tool-outcome honesty.** `toolOutcomeState` (`transcript.ts:27-40`) maps the
  backend taxonomy exactly (`paused`→awaiting, `denied`→denied,
  `outcome_unknown`, `failed`→error, else legacy `isError`). No outcome falls
  through to a false success. R4-022/023 holds.
- **Menu hit-testing (R5-F).** `itemRows` (`Menu.ts:164-176`) is the single
  physical-row map shared by `render` (`:315-329`), `handleMouse`
  (`:214-228`, walks from `y - titleLines().length - 1`) and `visibleWindow`
  (`:178-206`). Item rows genuinely start at `titleLines().length + 1`
  (title rows + one filter row), so the offset is exact; section/multi-line
  entries map consistently and the footer.hit row math is span-based.
- **R4-024 pin refresh.** `/provider remove` re-pin compares the live pin
  (`settings.ts:229`) after `adoptSessionRoute` (`app.ts:358-371`) updates
  `this.current` on every `session.setModel`.
- **R4-065 expiry.** A pause frame carrying terminal `status` retires evidence
  + card and never issues a decision RPC (`app.ts:1340-1358`); mirrors the
  `approval.resolved` path (`:1372-1385`).
- **R5-QR keyless path.** `SecretForm.attempt` blank + `allowEmpty` submits `""`
  while Escape still resolves `undefined` (`Form.ts:230-247`), consumed at
  `settings.ts:152-161`; full-UUID bypass (`memory.ts:18-23,108-160`;
  `skills.ts:80-107`) matches the gateway `_uuid` braced/URN forms.
- **`footer.mode` freshness race.** Candidate: `request` mirrors any `mode.*`
  result into `footer.mode` with no session guard (`app.ts:341-345`) while
  `refreshSessionMode` is fire-and-forget (`:459-461`). Falsified: the gateway
  processes requests serially (`gateway/__main__.py:81-85` one loop,
  `await gateway.handle_line`), so `mode.get` responses resolve in issue order
  and the latest switch wins. Not reproducible → no finding.
- **Rendering/injection elsewhere.** Every other untrusted-text path sanitizes:
  `Notice` (`Notice.ts:27`), `AssistantTurn` (`:26,37,43`), `UserTurn` (`:26`),
  `ToolTurn` (`:64-67` + `shapePreview`), `DiffBlock` (via `shapePreview`),
  `ApprovalCard` (evidence rows `clean()`-mapped; dock title/target `clean()`),
  `Menu` (title/label/description), `OAuthProgress` (`sanitizeStatus`),
  `header`/`Footer` (`components.ts:109`, `Footer.ts:132`), `OutputViewer` body
  (`:149`) and now header (P3-1). No remaining raw path.
- **tsc looseness — none.** `ui/tsconfig.json` is `strict` +
  `noUnusedLocals`/`noUnusedParameters`/`noFallthroughCasesInSwitch`. No `any`,
  `@ts-ignore`, `@ts-expect-error`, or `@ts-nocheck` in `ui/src`; the only casts
  are deliberate private-field reads (`components.ts:253,260,359` popup height;
  `AppShell.ts:44` layout node; `Form.ts:180` masked value).
- **Timeouts/budgets consistent.** `IN_FLIGHT_TIMEOUT_MS=300_000` matches
  gateway `turn_timeout` default 300 (`server.py:1144`);
  `DELEGATION_TIMEOUT_MS=150_000` outlasts the orchestrator's 120s; auth settle
  200s > gateway `LOGIN_TIMEOUT_SECONDS=180`; OAuth poll 500ms / 300s overall;
  clipboard/image 3s. `prompt.submit`/`session.resume` at the channel-default
  30s is safe (submit returns early; the turn runs async).
- **Theme/state determinism.** `ThemeBackground.paint` (enter/leave bracketing)
  and the `repairFill`/`selectionBar` background handling are internally
  consistent; `selectionInk`/`selectionFill` are per-skin precomputed.
  `AnimationCoordinator` timer ownership is sound (single timer, generation-
  guarded stops, reduced-motion path).
