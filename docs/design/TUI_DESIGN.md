# TUI Design Contract

Updated for the user’s screenshot reference (2026-10-11). This describes the implemented
terminal interface: tokens, density, interaction states and the theme
contract. Normative test coverage lives in `ui/test/` (design, motion,
state, approval, output, ui, dialogs, layout, features, app).

## Visual language

- Near-black background, off-white text, gray metadata, restrained blue/indigo
  accent; muted green/amber/red for confirmed success, warning and failure.
- Terminal monospace only. No gradients, shadows, large colored panels,
  emoji controls or decorative ASCII art.
- User and assistant messages stay plain, with a shared two-column outer gutter
  at widths ≥40. Markers occupy two more columns; wrapped reply text and the
  left edge of tool frames align at column four. Notices and thinking activity
  use the same outer gutter. Below 40 columns, extra gutters disappear.
  Tool output and approval evidence use neutral rectangular frames, capped at
  104 columns, with inset padding and space outside. Codex uses a distinct
  `#17171c` panel fill and `#606069` border; emitted border/fill contrast is
  tested in truecolor and 256-color modes. Frames do not turn red/green on outcome changes. The composer
  has a thin accent column; selected rows use a quiet neutral fill in codex.
- Brand and workspace share one header row. The footer has one row: actual
  model / branch / mode / status, observed tokens when nonzero, hints when
  space permits, and a fixed bottom-right activity/learning square. Workspace is
  displayed in the header rather than repeated in the footer.
- Bold is reserved for brand, selections, approval titles and Markdown
  emphasis. Every state keeps a text marker (`✔`/`✖`/`○`/`?`, `✓`/`!`/`✗`)
  so meaning survives monochrome skins.

## Layout and density

- One retained transcript `ScrollView` for the shell's lifetime (`AppShell`);
  composer and footer stay docked. Resizes update density before layout measurement,
  without waiting for input or gateway activity. Viewport classes: full (≥16 rows, gaps),
  reduced (9–15, no gaps, composer window 1–4), compact (6–8, one status row,
  composer 1–2), degraded (<6 rows or <20 columns: resize hint, draft kept).
- Full interactive target is 40×9 and up. Every widget renders inside any
  width 0–120 without throwing; pathological widths are rendering inputs,
  not promises of a complete one-column form.
- Follow-end scrolling freezes while scrolled up and resumes on explicit
  return-to-end. Composer caps multiline input around the visible cursor
  (max 8 rows full density); empty and one-line states share a baseline.

## Music bar

- A pinned now-playing bar sits above the transcript in the chat column
  (chat-only and split layouts alike): it is a sibling of the retained
  transcript `ScrollView`, so it never scrolls away and never rebuilds scroll
  state. It shows **what is playing on this computer** — any app (a browser
  video, Spotify, Music, …) — not a library of its own. Density budgets:
  three rows (title / artist · source / transport + progress) at ≥24 rows,
  two rows at full density, one compact line at reduced, hidden at
  compact/degraded. A click sends play/pause to the detected source when
  that source accepts transport commands.
- Detection is a gateway concern (`nowplaying.get`, `nowplaying.command`):
  layered, local-only, and best-effort — system media APIs (MediaRemote)
  first, then nowplaying-cli/media-control when installed, then Spotify/Music
  via AppleScript, then the front YouTube tab of a running Chromium-family
  browser or Safari. macOS Automation prompts are reported honestly
  ("allow it in System Settings → Privacy & Security → Automation"), a
  browser tab's playback state is `unknown` rather than guessed, and the bar
  never invents a title. Elapsed time interpolates between 2 s polls while
  playing; the transport line and equalizer advance at 250 ms; reduced
  motion keeps the glyph static. `/music` is the keyboard surface
  (play/pause, next/previous, status, on/off) and `nowplaying_enabled=false`
  hides the bar entirely.
- Whatever the OS provides stays on this machine: detection reads local app
  state and never uploads metadata.

## Honest states

- Tools: `running`, `awaiting_approval`, `success`, `error`, `interrupted`,
  `outcome_unknown`, `historical`. Missing completions are never success;
  cancellation preserves completed outcomes; history never infers outcomes.
- Canonical final text reconciles once (withheld tails append; replacements
  replace; duplicates are inert). Cumulative usage adds once per turn,
  including non-visible sessions. The 300 s watchdog warns once and keeps
  the guard; disconnect is offline with local commands intact.
- Approvals resolve `{requestId, sessionId, verdict, grant}` after
  revalidation; Deny is default and Esc denies explicitly, while navigation,
  exit and disconnect invalidate without resolving. Decisions record; only
  later tool events prove execution. Approval evidence is retained in the
  transcript; only vertical decision controls occupy the dock. Queues attach
  evidence on activation, preserving the active request’s evidence in short
  viewports. Long operations/targets remain wrapped and pageable. Late
  management dialogs wait hidden with their drafts intact until the approval
  releases focus. Externally resolved requests advance the queue; terminal
  completion retires unanswered controls without issuing decisions.

## Interaction

- Enter submits, Shift+Enter newlines, Esc stops/denies, double-Esc refills,
  Ctrl+C stops/clears/exits, Ctrl+D exits when idle/empty, Tab completes,
  `?` (empty) and F1 open shortcuts. Safe commands work mid-turn.
- Menus own a real case-insensitive filter (label/value/description) with
  honest empty states and selection-preserving budgets. Forms validate
  inline, keep drafts on failure and save exactly once. Secrets are masked
  everywhere and redacted from failure text.
- Retained tool output (8 newest, 128 KiB each, 1 MiB total) opens in a
  scrollable viewer; evicted output reads unavailable.

## Motion

- One `AnimationCoordinator` per App owns at most one timer. One-cell activity
  frames (`·`, `•`, `●`, `•`) advance every 200 ms and keep labels still. The
  corner square keeps one shape (`▪`) while brightness rises and falls at
  the same cadence. Working footer demand survives the thinking indicator
  hiding on text deltas; ready, approval, stopping and cleanup retire it.
  Reduced motion (`MACHINA_UI_REDUCED_MOTION=1|true`) keeps activity static without
  a recurring frame timer. Saved learning uses one 800 ms pulse, coalesced
  to once per 30 s, with no chat notices; idle acknowledgments request their
  first frame immediately. Growth expiry and disposal retire subscriptions.
  Cleanup/stopping markers freeze without claiming completion; exit clears
  motion and watchdog resources.

## Themes

- 53 skins (`SKINS` in `ui/src/design/theme.ts`); `codex` is the fresh-install
  default, explicit saved themes (including legacy `default`) are respected.
  `/theme` previews live with cancel-restore and persists exactly once; failed
  saves report temporary.
- Adding a skin means one entry in `ui/src/design/theme.ts` (validated
  required IDs in `REQUIRED_THEME_IDS`); styles resolve lazily so switches
  never clear content, drafts, scroll or execution bindings.

## MCP inspection

`/mcp` opens a server picker when inventory exists. `/mcp list` and
`/mcp status <id>` inspect the worker’s actual shared ConnectorService.
Disabled, disconnected, connecting, connected, degraded, auth_required and
error remain distinct; negotiated protocol is displayed rather than guessed.
Inspection is read-only and does not connect or discover tools. Default
inventory is empty and in-memory. Durable setup, connect/disconnect UI, OAuth,
external app inventory and agent tool dispatch remain outside this feature.

## Feature awareness

Both ordinary and streamed agent calls receive the same compact implementation
brief, including agents with custom prompts and turns where growth preparation
fails. It distinguishes requested persona tone, stored fact interpretations,
emotion-weighted recall, saved learning and runtime RSI. Recalled conversations
are not implementation documentation; empty notes are not missing features.
The guide does not claim model-weight training, application self-rewriting,
expanded permissions or completed MCP remote dispatch.

Current flags are allowlisted; emotion values are recognized topology names or
an explicit neutral-fallback label, and unknown modes remain unknown. The
`harness_status` model tool, when offered, uses existing `context.read` authorization,
verifies active session ownership, reads only that agent's learning state and
redacts output. Oversized notes/persona/trials have explicit omission markers;
an oversized remaining snapshot is refused rather than truncated into invalid
JSON. This mutable, agent-specific result is never cached globally.

The screenshot supersedes the earlier borderless-tool/two-footer-row direction.
See [visual verification](../verification/TUI_OVERHAUL_REVIEW.md) and the
[renderer capture](../archive/artifacts/tui-reference.png).
