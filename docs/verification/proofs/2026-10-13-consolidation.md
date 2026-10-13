# Proof — memory consolidation roundtrip (first durable memories)

- **Date observed:** 2026-10-13 (post-program second pass, live DeepSeek run)
- **Status:** PASS (observed on the live dev checkout)
- **Provenance:** `docs/verification/COMPLETION_BUNDLE.md`, section
  "10. Post-program fixes (second pass, 2026-10-13)", bullet
  "Memory consolidation never worked — two stacked bugs (`8c0e013`)".
- **Fixes covered:** bounded shutdown drain (`core/background_tasks.py`, called
  by CLI chat before provider+pool close, gateway close and runtime shutdown)
  and `agent_factory` now passing the session provider to `MemoryConsolidator`.

## Command / action

Live CLI chat on the dev database (DeepSeek provider), followed by the
production consolidation path on session shutdown. Exact command was not
transcribed in the bundle; the bundle records the observed end state below.

## Observed end state (as recorded in the bundle)

- First memories ever stored in this checkout's dev DB:
  - `User's favorite color is teal.` — category `preference`, importance 0.5
  - `User adopted a beagle named Pixel.` — category `fact`, importance 0.6
- Consolidation cursor advanced.
- Zero stderr errors (the prior `InterfaceError` on every CLI run is gone).

## Re-verification in the dev database (read-only, 2026-10-13)

- Session owning these rows: `4c2fb3e8-1a67-4798-9a34-8c503dab1944`
  (title begins "I just adopted a beagle named Pixel. My favorite c").
- `memories` rows, created `2026-10-13 22:19:11 UTC`:
  - `preference` 0.5 — `User's favorite color is teal.`
  - `fact` 0.6 — `User adopted a beagle named Pixel.`
- Session cursor (`sessions.state.mem_consolidated_up_to`):
  `{"id": "e96b4b44-44d1-4d05-8d4c-9749d2999daf", "at": "2026-10-13T22:19:12.254113+00:00"}`.

## Where this is enforced today

`tools/e2e_gate.py --journeys j1` reproduces the roundtrip shape on the
current tree (real DeepSeek call, `memories` row assertion, cursor assertion,
grant revoke). Because consolidation deduplicates content, the gate uses a
per-run unique fixture fact.
