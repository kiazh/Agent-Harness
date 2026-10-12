# Screenshot-driven TUI correction and MCP inspection

Date: 2026-10-11. Latest base HEAD: `1d80ba0`, branch `main`. The existing OpenCode
working changes were preserved. Changes remain uncommitted.

## What changed

The user’s supplied screenshot supersedes the earlier borderless-tool/two-row
footer specification. The native TUI now has a single header with workspace right,
plain messages, padded rectangular tool frames (104-column cap/4-column indentation),
vertical approval controls above the composer, retained transcript evidence, a thin
composer bar and one footer. Actual model/branch/mode/state and observed tokens are
used. Saved themes remain respected; `/theme codex` selects the reference palette.

After the user’s follow-up, activity is a single cell at 200 ms. The corner square
pulses in brightness while working and for an 800 ms saved-learning acknowledgment
with a 30 s cooldown and an immediate first render while idle. Its geometry and
position stay fixed. The footer owns working demand on the shared timer so text
deltas hiding the thinking indicator cannot strand its animation. Approval,
ready, stopping/cleanup and offline states retire working demand; reduced motion
uses a static lit square with no recurring frame timer. Exit clears motion/watchdogs.

The missing-frame reproduction was visual contrast: native frames were present,
but the old `#232327` edge and `#111113` surface almost merged into the near-black
transcript. Codex now uses `#606069` edges and a distinct `#17171c` fill. Actual
emitted border/fill contrast (including 256-color quantization), rectangular edges,
inset/outside margins and the shared approval palette are regression tested. The
local `machina` launcher was verified to target this checkout; it was not modified.

Approvals preserve Deny by default and exact request/session/turn revalidation.
Queue evidence is attached on activation; overflowed targets/operations remain
wrapped/pageable. Late forms and pickers remain hidden with draft intact while
approval owns focus. Remote resolution advances the queue; unanswered controls
retire on terminal completion without a surprise resolve. Bound fresh continuation
IDs rebind before filtering. This is presentation compatibility, not new HTTP TUI
transport or remote-tool execution.

`/mcp`, `/mcp list`, `/mcp status <id>`, help and completion now read actual shared
worker inventory through authenticated `mcp.list`/`mcp.status`. Inspection never
launches/discovers servers, initializes the SDK/database, or changes permissions.
The runtime owns a lazy service; owned shutdown closes connectors, borrowed HTTP
adapters cannot close another consumer’s service. Real SDK fixtures prove connected,
disconnected and subprocess retirement. Inventory remains in-memory/default-empty:
configuration ingestion, persistence, connection controls, OAuth and agent-loop
remote tool dispatch remain deferred. OpenCode/Codex inventory is outside this worker.

## Transcript gutter and feature awareness follow-up

The latest follow-up is based on clean HEAD `1d80ba0` on `main`. The screenshot
showed frames indented separately from flush-left reply text. Message/notice/
thinking markers now share a two-column outer gutter; reply text and frame edges
align at column four. Widths below 40 omit extra gutters. A shared spacing module
keeps wrap budgets and frame positioning consistent without changing Markdown or
whitespace fidelity. Three margin assertions failed before the change; the focused
native panel/reference run then passed all 14 tests. The revised native PNG/GIF
use this spacing; the independent narrow layout/panel run passed 15 tests.

The model previously received learned notes and tool signatures without an
implementation guide. Its speculation in the screenshot is consistent with that
missing context. The new shared brief explicitly explains persona tone, stored
fact interpretations/emotional retrieval, saved learning and runtime RSI, along
with memory, skills, execution, UI controls and deferred MCP functionality. Empty
notes are not treated as absent features. Allowlisted current settings distinguish
configuration from observed success. Normal and streamed execution, custom prompts,
restricted toolsets and failed growth preparation are covered through actual
provider-call fixtures. The tested brief is at most 650 tokens and needs no extra
database read or model request.

`harness_status` verifies the active session owner, performs read-only learning
lookups with a two-second timeout, and redacts a bounded valid JSON response.
It uses existing `context.read` broker authorization and is excluded from global
read caching. Real DB-backed ordinary/streamed agent loops verify current scoped
notes reach the next model call without an approval request. Initially the normal
1000-character tool-preview cap cut its JSON in half; a failing regression led
to preserving the complete bounded result. Oversized notes/persona/trials retain
counts and explicit omission markers; remaining oversized state is safely refused.

Independent review reproduced an unsafe unrestricted emotion setting in the brief.
Secret-shaped/oversized inputs and an oversized status snapshot failed three
regressions before allowlisting topology names and enforcing the final bound.
The bounded re-review passed five non-DB checks and found no remaining
Critical/Important issue in that fix. Final focused context/boundary/lifecycle/
property checks passed **15 tests**. No live production model prose or measured
quality improvement is claimed; fixtures prove context delivery and tool execution.

The first full backend run returned **2113 passed, 5 skipped, 2 failed**: an older
session test double lacked `execution_mode` (a new compatibility bug, corrected to
an honest unknown label), and Hypothesis exceeded its 200 ms deadline without a
failed budget assertion (317 ms initially, 26 ms replay). Clean HEAD `1d80ba0`
in `/home/user/.codex/worktrees/38df/Machina1` passed the lifecycle check
and reproduced the same property-test timing failure (**1 passed, 1 failed**;
576 ms deadline). Assembler and property-test sources are unchanged. No deadline
was relaxed to suppress the failure. The current focused rerun passed both checks.
Final full verification is recorded below.

## Reproducible visual evidence

[Native renderer preview](../archive/artifacts/tui-reference.png) is generated from the real
`App`/`AppShell`/`TuiAltScreen` components with a deterministic gateway fixture.
The installer guard is explicitly illustrative; it is not a real installer edit.
No browser/CSS mockup, provider, operator terminal, or credentials were used.
The [timed native motion preview](../archive/artifacts/tui-motion.gif) captures working dot
and fixed square brightness changes from that same renderer.

```sh
COLORTERM=truecolor node ui/test/support/capture-reference.ts /tmp/machina-reference-review
```

The generator saves ANSI JSON and stripped text for codex/mono/light at 140×52,
80×24 and 40×9, plus four timed working frames in codex. Visual inspection was bounded to one matrix and one confirmation
round. The PNG is a rasterization of actual native rows; font metrics depend on
the terminal. Automated native tests cover viewport bounds, dock order, short
terminal Allow blocking, resizes without input/events, draft restoration, queue
identity and long evidence paging. This is headless renderer proof, not a manual
real-PTY or production-provider compatibility claim.

## Regression and review evidence

Five reference contract tests failed before the parent implementation; MCP UI
command regressions failed before registration. Coordinator cadence/lifetime
regressions failed before its fix. Read-only gateway tests failed before wiring.

An independent code review reproduced five additional Important issues: queued
request evidence mismatch, unrecoverable target clipping, absent idle growth first
frame, late dialogs stealing approval focus, and discarded fresh continuation IDs.
Every issue was reproduced in a failing regression and fixed. The bounded re-review
confirmed the fixes and found no remaining Important issue in that scope.

The follow-up’s seven failing assertions reproduced the slow dot, absent working
square animation, static saved-learning square and low panel contrast in truecolor
and 256-color modes before fixes. Final review found no Critical/Important defect
in the bounded output/motion correction.

## Verification

- Latest full backend verification after gutter/context/boundary fixes:
  **2119 passed / 5 skipped / 0 failed in 168.11 s**, including the real-gateway
  UI wrapper with all 256 UI tests running and zero UI skips. Command:
  `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test PYTHONPATH=/tmp/machina-ui-reference-runtime:/home/user/Machina1 .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider`.
- Latest touched-file Ruff check/format check passed on seven Python files.
  Bandit `-q machina/core/self_knowledge.py machina/tools/harness.py -ll` emitted no medium+
  findings. Scoped Impeccable layout detector returned `[]`; `git diff --check`
  passed. Current standalone UI results remain the 256-test run below.
- After clarifying that empty notes mean none are *currently* saved (they may
  have been explicitly forgotten), the complete self-knowledge test file passed
  again: **13 passed in 0.86 s**. No runtime behavior changed after the full run.
- `npm --prefix ui run check`: typecheck, **256 tests / 251 passed / 5 environment-
  gated real-gateway skips / 0 failures**, eslint clean.
- Real gateway restart wrapper runs the same UI checks with **zero skipped tests**:
  `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test .venv/bin/python -m pytest -q tests/test_personality_growth.py::test_ui_checks_with_real_gateway_restart --tb=short -p no:cacheprovider`.
- Follow-up isolated real-gateway wrapper: **1 passed in 33.19 s**, exercising
  all 256 UI tests with zero UI skips. Only UI/docs changed in this follow-up;
  the full backend suite was not repeated.
- Prior final sequential backend run after review fixes: **2106 passed / 5 skipped /
  0 failed in 115.43 s**. This includes the real gateway wrapper with all 252
  UI tests running and zero UI skips. Command:
  `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider`.
  Disposable bootstrap described below was provided through PYTHONPATH.
- Ruff check and format check passed on all 10 touched Python files (including
  pre-existing configuration changes). Bandit `-r machina/connectors/ machina/gateway/features/connectors.py -ll`
  found **0 medium / 0 high** issues; its metrics retain 7 low findings.
- Scoped Impeccable detector emitted no findings. `git diff --check` passed.

Python verification runs use a disposable home lookup/bootstrap and an empty env
file, with only the dedicated test DSN enabled. Child tests retain their own
explicit temporary HOME directories. No production `machina` database,
operator configuration, shell startup file, live key or current terminal was used.

The first full run had 2 failures: method inventory lacked the newly added MCP
methods (test contract updated), and the initial isolation bootstrap replaced a
child test’s own temporary HOME (verification setup corrected). Both affected
checks passed on clean HEAD `0ea164e` in managed worktree
`/home/user/.codex/worktrees/27d1/Machina1` (**5 passed**) and in the current
checkout (**5 passed**). This is evidence of the exact causes, not a blanket
pre-existing-failure claim. No unresolved failure is being waived.

## Remaining boundaries

Pending approvals are not reopened automatically on returning to a switched-away
session; the backend remains authoritative and `/approvals` can inspect it.
Server setup/connection controls, OAuth and agent-loop MCP exposure remain deferred.
Menus remain primarily keyboard driven. Real-PTY font/input validation and production
provider acceptance are separate from the automated/native evidence above.

---

## Historical OpenCode overhaul review (superseded visual direction)

# TUI Overhaul Visual Review (T10)

Date: 2026-10-11. Method: batched native-renderer matrix — real
`TuiAltScreen` against mock terminals, safe synthetic fixtures only. No live
provider, no real PTY review (manual terminal verification unclaimed).

## Matrix

- Sizes: 40×9, 80×24, 120×40. Themes: codex, mono, light.
- States per cell: base transcript (user/streaming/tools
  success/error/unknown/interrupted/diff/notice + composer + working
  footer), approval card overlay, filtered picker overlay, persona form,
  masked secret form, output viewer, offline footer.
- 36 stripped captures in `/tmp/tui-matrix/` (not committed).
- Automated invariants per screen: no row exceeds terminal width, no
  OSC/title/clipboard/clear/cursor sequences, masked secrets never appear.

## Findings (one inspection pass, fixed in one batch)

1. `… 1 more lines` grammar in tool previews and diff blocks. Fixed with
   singular/plural counts; regressions in V02 and the diff bound test.
2. Assistant first line rendered `●  The` (double space): the Markdown child
   carried paddingX 1 inside the already-prefixed row. Fixed with paddingX 0;
   regression asserts `● Hello` in V01.
3. Narrow footer (40 cols) mangled the token count and dropped the square:
   tiered truncation cut from the right. Rebuilt as priority tiers —
   status/model/tokens first, cwd/branch yield, model abbreviates before
   tokens or square are touched. Regression in the footer right-edge test.

Re-ran the matrix after the batch: clean (36/36). Full UI check green
afterwards (233 tests / 228 pass / 5 env-skipped / 0 fail, lint clean).

## Honest limits

- Simulated renderer captures, not a real PTY session; no operator
  terminal, credentials or config were touched.
- Overlay ScrollView viewports do not propagate in the installed toolkit
  (verified); OutputViewer and TextForm own their scroll/window state.
- Approval reopen-on-return is not implemented (pending requests stay
  server-side until turn timeout); documented in the progress file.
- Menu mouse support is not implemented (keyboard-first per contract).
