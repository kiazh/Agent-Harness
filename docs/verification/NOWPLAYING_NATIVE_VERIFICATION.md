# Now-playing bar — native macOS verification (closes §15 "native playback: UNVERIFIED")

Status: **native live path VERIFIED** (real PTY + real gateway + real detector + real
Spotify source, zero input). No code defect found on the live path; no source change made.

- Date: 2026-10-16
- Verified revision: `HEAD = 95e07478637c688a0d4432bd803f7be254a8acc6`
- Files verified (blob hashes at that revision; all `clean`):
  - `machina/core/nowplaying.py` → `1719701cffc5d6328a97a54ac9457b2740f95625`
  - `machina/gateway/features/nowplaying.py` → `4655acc8e2a241c8c8b0584f41acaa8f90235670`
  - `ui/src/nowplaying/controller.ts` → `aea72518a2ae7f1f7aba47c21c684886c1ad89cb`
  - `ui/src/components/NowPlayingBar.ts` → `6b1dde81cee133c096964c7c6bedc6e5be5efaae`
  - `ui/test/support/pty-driver.py` → `1f72ac974f71a9252af9eaebaf4b34a5092b753c`
- `ui/src/app.ts` was `dirty` from **another concurrent agent** during the run; its diff does
  **not** touch the now-playing path (no `nowplaying`/`PinnedBar`/`music` lines), so it does not
  affect this verification. No other-agent files were touched.

## Verdict

The fix's core claim — **the bar updates without any user input** — is now natively verified:

1. **Equalizer glyph advances at the ~250 ms cadence** — 75 glyph-value changes over 18.92 s
   (mean **256 ms/frame**), cycling all four `SOUND_FRAMES`.
2. **Interpolated elapsed clock changes** — transport elapsed advanced `1:54 → 2:12` = **+18 s
   over 18.92 s wall**.
3. **The 2 s poll updates metadata when the detector's answer changes** — with zero input, a
   natural Spotify track advance flipped the bar from `Wind in her hair` to `Need 2` (title,
   artist, duration, elapsed, progress bar all updated). This is the "state row changes when the
   detector's answer changes" case, demonstrated live.
4. **The app is otherwise idle** — after boot settled, every non-bar row was byte-identical
   across the capture; only bar rows change.
5. `wrote_input_bytes = 0` for every capture — no keys, no turns, no mouse were ever sent.

No defect was observed; nothing was changed in the now-playing path.

---

## §17-style handoff record

**Task ID and status:** §15 native now-playing verification — DONE (native live path verified).

**Baseline commit / dirty paths:** `95e0747`. Concurrent (other-agent) dirty files present
(`machina/core/config.py`, `machina/skills/*`, `ui/src/app.ts`, `ui/src/components.ts`,
`ui/src/components/Menu.ts`, `ui/test/*`, plus an untracked skills enrichment JSON). This task
touched none of them and made **no** repo change other than this document.

**Requirement and invariant:** the pinned music bar must reflect whatever is playing on this
machine and keep updating (glyph animation + progress/clock) while audio plays, driven only by
the 2 s poll + 250 ms animation demand — with no user interaction. Detection must be honest
(`available` ≠ "nothing playing"; permission states named, never fabricated).

**Exact files changed:** only `docs/verification/NOWPLAYING_NATIVE_VERIFICATION.md` (this file).
**No production or test source was modified** — no defect was found in the live path.

**Public interfaces / schema changes:** none.

**Tests added:** none required (no code change). The existing scripted acceptance tests
(`ui/test/nowplaying-app-boot.test.ts`, `ui/test/nowplaying-controller.test.ts`) remain the
UI-level proof; this document is the *native, live* proof they cannot provide.

**Red evidence:** N/A (no fix).

**Green command / runtime / exit code / counts:** see "Reproduce" below; every capture ran to
completion with `wrote_input_bytes = 0`.

**Native or real-server evidence path:**
- Real pseudo-terminal (`pty.fork`, `TIOCSWINSZ` 160×48), real TUI (`node --disable-warning=ExperimentalWarning ui/src/main.ts`), which spawns the **real gateway** (`python -m machina.gateway`) and the **real detector** (`machina.core.nowplaying.detect_now_playing`).
- Live source: real Spotify playback (Automation already granted → no dialog).
- Screens read through `pyte` (actual emulated frames, not ANSI logs).

**False-positive / counterexample checks:**
- *Is the glyph animation faked by a static render?* No — all four distinct frames were observed and the change cadence averaged 256 ms over 18.92 s.
- *Is the "elapsed" actually just the polled integer, not interpolation?* No — it advanced every second (+18 s over 18.92 s) while the poll is only every 2 s, i.e. it moved between polls.
- *Did the track change because I acted?* No — `wrote_input_bytes = 0`; the change (`Wind in her hair` → `Need 2`) was a natural Spotify auto-advance caught by a poll.
- *Could the gateway have returned canned data?* No — the titles/artists/durations changed across runs (`Dark Spin - Live` → `Joint` → `Wind in her hair` → `Need 2`) and matched a direct, independent Spotify AppleScript probe.
- *Could a permission dialog have fired?* See "No-prompt guarantee".

**Unverified assumptions and environment limitations:**
- **Pause/resume and explicit next/previous transport were NOT exercised** — issuing `nowplaying.command` (or pausing) is audio control and is out of scope for this run. The command path therefore remains covered by unit tests only.
- The **honest empty/error rendering** was observed only as the boot frame before the first refresh resolved (`♪ Nothing playing` at t≈0.70 s); forcing the true "nothing is playing" steady state would require stopping the user's audio, which was not done.
- **Layer 1 (MediaRemote) is gated on this machine** (see below), so the live path exercised is layer 3 (Spotify AppleScript). Layers 1/2/4 behavior is unchanged and covered by unit tests.
- Reduced-motion would freeze the glyph at frame 0; it was off here (all four frames cycled), so the reduced-motion branch is not exercised natively.

**Security / privacy implications:** all detection is local. No prompt was raised. The one
environmental adjustment (see below) *reduces* privilege use: a browser app whose Automation
grant is absent was prevented from being queried at all.

**Next task and first action:** optional follow-up — exercise the transport/pause path with the
user present (user pauses/plays or approves `/music` commands), then re-run the same PTY capture
to observe the state row change on a real pause.

---

## STEP 0 — permission-free source probe

### 0a. Layer 1 — MediaRemote (ctypes), no AppleScript, no TCC

`machina.core.nowplaying._media_remote_probe()` called directly (never `detect_now_playing`,
which falls through to AppleScript). Six readings over ~4 s:

```
describe_sources: {"platform": "darwin", "mediaremote": true, "cli": null,
  "apps": ["Spotify","Music"],
  "browsers": ["Google Chrome","Brave Browser","Microsoft Edge","Chromium","Vivaldi","Opera","Safari"]}
[..] playing=False pid=0  info=None        (x6, identical)
```

Result: the framework loads and the callbacks dispatch, but macOS returns an **empty** info
dictionary (`info=None, playing=False, pid=0`) — MediaRemote is gated for third parties on this
build. This is the module's documented "silent-empty" case, not an absent API. Layer 1 yields
nothing usable.

### 0b. Layer 2 — CLI adapters

```
$ command -v nowplaying-cli   # (none)
$ command -v media-control    # (none)
```

Not on PATH → per the probe rules, **no CLI layer**, and the source probe stops here for
non-AppleScript layers (no `osascript` was run for probing).

### 0c. Automation-permission status — WITHOUT prompting

To decide whether the real detector could be run safely, Automation status was queried with
`AEDeterminePermissionToAutomateTarget(target, '****','****', askUserIfNeeded=false)`
(askUserIfNeeded=false → it reports, it never shows a dialog):

| App (bundle id)              | status | meaning |
|---|---|---|
| Spotify (`com.spotify.client`)   | 0 | **authorized** — no prompt |
| Google Chrome (`com.google.Chrome`) | −1744 | `errAEEventWouldRequireUserConsent` → **a dialog WOULD appear** |
| Music / Safari / Brave           | −600 | process not found (not running) |

### 0d. Conclusion — there IS a live source, reached permission-free

Spotify is running, playing, and **already authorized**, so `detect_now_playing()` returns at
**layer 3** before ever reaching the prompt-prone browser layer. An independent, safe probe of
only the Spotify layer (no fall-through):

```
spotify probe ->  {"source":"Spotify","title":"Joint","artist":"Passion Mango",
                   "album":"Clean Slate","duration":148.737,"elapsed":105.063,"state":"playing"}
spotify probe (+2s) -> ... "elapsed":107.296 ...  # advancing = genuinely playing
```

Across the session the title changed on its own: `Dark Spin - Live` (Future Thieves) →
`Joint` (Passion Mango) → `Wind in her hair` (Marcus Amanya) → `Need 2` (Pinegrove).

### No-prompt guarantee (explicit, because Chrome was running)

The detector's **layer 4 iterates every running Chromium/Safari app and runs AppleScript**; with
Google Chrome running and *not* authorized, that query would raise a macOS Automation dialog.
Layer 4 is only reached if Spotify has no track. To make it impossible for a dialog to fire —
including if Spotify stopped mid-capture — the gateway was launched with a tiny `pgrep` shim on
`PATH` that returns "not running" for the browser apps only:

```sh
# /private/tmp/opencode/np-bin/pgrep  (first on PATH)
if [ "$1" = "-x" ]; then
  case "$2" in
    "Google Chrome"|"Brave Browser"|"Microsoft Edge"|Chromium|Vivaldi|Opera|Safari) exit 1;;
  esac
fi
exec /usr/bin/pgrep "$@"
```

This does **not** touch the Spotify path (layer 3 uses `pgrep -x Spotify`, which passes
through to the real `pgrep`), so the live source under test is 100% real. It only guarantees no
TCC prompt. **No dialog appeared at any point during this run.**

---

## STEP 1 — live TUI frames in a real PTY, zero input

### Setup

- Harness: a local driver (`pty.fork` + `pyte`) modeled on the repo's
  `ui/test/support/pty-driver.py` (T11 §24F evidence). It **never writes a byte to the PTY**;
  it records a timestamped screen snapshot at a fixed interval and reports `wrote_input_bytes`.
- Launch replicates `machina/cli/launcher.py::launch_ui` (same `node` argv); the UI spawns the
  real gateway and generates its own `MACHINA_GATEWAY_TOKEN`.
- Deliberate env deviations (documented): `MACHINA_HOME=/private/tmp/opencode/np-home` (scratch,
  so the run cannot mutate the user's config/session store; defaults leave
  `nowplaying_enabled=true`) and the `pgrep` shim above.

### 1a. Boot: honest empty line → live track (state row changes on the detector's answer)

20 s capture, 123 frames (`/private/tmp/opencode/np-out`):

```
frame   4  t= 0.70s  row0: '♪ Nothing playing'          # honest empty, pre-first-refresh
frame   6  t= 1.03s  row0: '▆▂▄ Joint   1:54 / 2:28'     # first reading resolved → live
frame 120  t=19.61s  row0: '▄▆▂ Joint   2:12 / 2:28'
```

### 1b. Glyph cadence and clock (20 s capture)

```
glyph transitions=77   title-clock transitions=19   transport-elapsed transitions=20
glyph cadence: 75 value-changes over 18.92s -> mean 256 ms/frame
distinct glyph frames: ['▂▄▆', '▂▆▄', '▄▆▂', '▆▂▄', '♪']
transport elapsed: 1:54 -> 2:12 = +18s over 18.92s wall
non-bar rows (>2) that differ after boot settles: NONE
wrote_input_bytes: 0
```

- The glyph cycles all four `SOUND_FRAMES` at ~256 ms (code interval 250 ms) — advances without input.
- The clock advances ~1 s/s, i.e. **between** the 2 s polls (interpolation), without input.

### 1c. The 2 s poll on a real detector-answer change (125 s capture, `/private/tmp/opencode/np-long`)

A natural Spotify auto-advance was caught with **no input**:

```
### frame 140  t=75.48s
  row0: '▂▆▄ Wind in her hair                                                2:53 / 2:53'
  row1: 'Marcus Amanya · Spotify'
  row2: '⏸ 2:53 ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬ 2:53'

### frame 141  t=76.02s
  row0: '▄▆▂ Need 2                                                          0:01 / 3:11'
  row1: 'Pinegrove · Spotify'
  row2: '⏸ 0:01 ▬────────────────────────────────────────────────────────────────── 3:11'
```

Title, artist, duration, elapsed and the progress fill all updated in one poll tick. 233 frames,
`wrote_input_bytes: 0`.

---

## Reproduce

```sh
# STEP 0 (safe, no AppleScript, no prompt)
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); from machina.core import nowplaying as np; print(np._media_remote_probe())"

# STEP 1 (real PTY, real gateway, real detector, zero input)
PATH="/private/tmp/opencode/np-bin:$PATH" .venv/bin/python /private/tmp/opencode/np_pty.py \
  --cols 160 --rows 48 --duration 20 --interval 0.15 \
  --cwd /private/tmp/opencode/np-cwd --outdir /private/tmp/opencode/np-out \
  --env MACHINA_PYTHON="$PWD/.venv/bin/python" \
  --env MACHINA_HOME=/private/tmp/opencode/np-home \
  --env PATH="/private/tmp/opencode/np-bin:$PATH" \
  -- node --disable-warning=ExperimentalWarning ui/src/main.ts
```

Scratch artifacts (`/private/tmp/opencode`): `probe_layer1.py`, `probe_spotify.py`,
`aedetermine.py`, `np_pty.py`, `np-bin/pgrep`, `np-out/` (123 frames), `np-long/` (233 frames),
`analyze_final.py`. These are outside the repo and ephemeral.
