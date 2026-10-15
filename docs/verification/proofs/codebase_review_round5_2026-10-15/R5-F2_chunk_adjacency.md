# R5-F2 — chunk adjacency after recursive splitting

**Lane:** R5-F2 (FIX_PLAN_ROUND5_2026-10-15.md, "Additional concerns" #2 /
`HELD-F2`).
**Verdict:** **CONFIRMED and FIXED.** The concern reproduces deterministically;
`_apply_overlap` emitted text that was never contiguous in the source. Fixed by
carrying real source offsets through recursive emissions and refusing overlap
that would bridge a dropped separator.
**Commit:** none (coordinator commits). Working tree only.

## Concern

`_split_recursive` emitted a chunk, then, when the *next* part was too large,
recursed on that part and reset `current_text = ""`. The separator between the
emitted chunk and the recursed part was dropped. `_apply_overlap` later took a
suffix of the previous chunk and prepended it to the next chunk, fabricating a
span that never appeared in the source.

## Reproduction (RED)

Minimal form: a long uninterrupted word (forced through `_force_split`)
followed by a short word separated by a single space, at a small chunk size.

```
source = "W" * 40 + " " + "end"
RecursiveCharacterTextSplitter(chunk_size=4, chunk_overlap=2).split_text(source)
```

Against the pre-fix chunker (`HEAD` f53476e) the final chunk was
`'WWWWWWWWend'` — absent from `'WWWWWWWW…WWWW end'` (the space was replaced by
spliced `W`s). Log: `logs/R5-F2_red.log`.

The new suite also found a second, more general shape:
`'aaaaaaaaaa.a a'` at `chunk_size=2, overlap=1` fabricated `'aa.aa'` by
splicing across a dropped separator. Log: `logs/R5-F2_pytest_red.log`
(5 of 6 new tests fail against HEAD).

Reproduce: `python R5-F2_repro.py <path-to-chunker.py>` (exit 1 = fabricated,
exit 0 = clean).

## Root cause

1. **Dropped separator across recursion.** After `sub_chunks = _split_recursive(part, …)`
   the code set `current_text = ""`; the separator preceding the *following*
   part was never represented, so consecutive emissions were not adjacent in
   the source.
2. **Unconditional overlap splice.** `_apply_overlap` prepended
   `prev_text[-k:]` to the current chunk with no check that the two chunks
   abut, so any non-adjacency became a fabricated span.

A first fix attempt (retain the separator only) still failed fuzzing for custom
separator lists and for recursion that drops a *trailing* separator inside the
recursed part. That drove the stronger offset-based fix below.

## Fix

`machina/rag/chunker.py`:

- `Chunk` gains `start`/`end` half-open source offsets (`None` when unknown).
- `_split_recursive` now builds chunks as **literal slices of the source**
  instead of re-joining a separator-stripped part list:
  - `_separator_spans()` returns exact content spans; for non-empty literal
    separators `sep.join(text.split(sep)) == text` guarantees exactness
    (regex: runs between matches).
  - A too-large part is recursed, then the **last sub-chunk is extended over
    the real source gap** up to the following part's start. The gap is measured
    from the sub-chunk's *actual* `end` (not the outer `part_end`), because the
    recursion may itself drop a trailing separator inside the part.
  - Accumulation uses offsets, so emissions are `text[cur_start:part_start]` —
    real substrings by construction.
- `_force_split` and the hard-bound pass in `split_text` propagate offsets for
  their pieces.
- `_apply_overlap` **only prepends overlap when `prev.end == cur.start`**
  (skipped when offsets are unknown, preserving external callers). This is the
  safety net: it can never fabricate even if an adjacency is still missed.
- `split_markdown`/`split_code` clear offsets on return (their sub-splits are
  per-section, so no document-level offset exists).

## Verification

| Check | Command | Result |
|---|---|---|
| RED repro | `R5-F2_repro.py` vs HEAD chunker | exit 1 — fabricated `'WWWWWWWWend'` (`logs/R5-F2_red.log`) |
| RED suite | `pytest tests/test_r5f2_chunk_adjacency.py` vs HEAD | 5 failed / 1 passed (`logs/R5-F2_pytest_red.log`) |
| GREEN repro | `R5-F2_repro.py` vs working tree | exit 0 — all chunks substrings (`logs/R5-F2_green.log`) |
| GREEN suite | new suite + `test_fm5_chunker` + `test_chunker_overlap` + `test_rag` | 78 passed (`logs/R5-F2_pytest_green.log`) |
| Fuzz | `R5-F2_fuzz.py` — 15000 random trials, literal + regex separators, heavy recursion | 0 failures (`logs/R5-F2_fuzz.log`) |
| Lint | `ruff check` / `ruff format --check` on all 4 files | clean (`logs/R5-F2_ruff.log`) |

New test file `tests/test_r5f2_chunk_adjacency.py` (6 tests): deterministic
long-word+short-word repro, span-faithfulness, double-separator repro,
custom-separator recursion repro, dropped-`.`-space repro, and a Hypothesis
property (no fabricated chunk; every tracked span describes its text;
monotonic progression).

## Behavior / API notes

- **API addition:** `Chunk.start` / `Chunk.end` (default `None`). No existing
  constructor or consumer is broken; offsets are not persisted by the RAG
  pipeline.
- **Behavior change:** emitted chunks now include the true separator run at the
  boundary (previously a single plain separator was appended), and overlap is
  withheld when two chunks are separated by dropped source characters. Chunk
  counts/bounds are unchanged; existing overlap, subsequence, and bound tests
  stay green.
- **Out of scope / pre-existing:** with a caller-chosen separator that drops
  content characters (e.g. `separators=["."]`), the non-whitespace source is
  no longer a subsequence of the output. This predates the lane (verified
  identical on HEAD) and is inherent to treating those characters as
  separators; not changed here.
- `is_separator_regex=True` offset tracking assumes non-capturing separators
  (the option is unused in the tree).
