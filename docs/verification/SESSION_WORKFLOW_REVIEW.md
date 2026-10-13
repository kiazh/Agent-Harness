# Session history and workflow review — 2026-10-13

Reviewed the supplied terminal conversation and `session-f63e16e5.md` against current
runtime code and the project architecture, feature ledger, growth/RSI, ECC skills,
permissions, compaction, gateway protocol and release documentation.

## Findings and repairs

The Markdown export read only active model context. Compaction archives originals and
replaces that working set, so an export after resume could begin with a summary and omit
earlier conversation. Gateway resume additionally selected only 200 active chunks. Both
now read a consistent database snapshot of retained active and archived conversation.
Documents and memories stay out of that transcript.

Compaction assigned new IDs even to preserved messages, creating shortened copies with
no shared identity. New compactions retain IDs; the earliest archived original wins when
the same message appears in both stores. Reading history never resurrects archived chunks
into the model prompt. Regression coverage uses real repeated compression and PostgreSQL,
plus fresh context/gateway objects to prove the read does not depend on cached history.

Pre-tool assistant responses were displayed but not saved, streamed text could be lost
when stopped, and tool evidence was stored only as a 500-character preview then exported
as 200 characters. Assistant preambles now save before execution, cancelled preamble
writes retry through bounded cancellation cleanup, stopped provider streams save partial
text, and new tool evidence is stored up to 65,536 characters. Exports label interruption,
storage truncation and historical preview-only evidence. Redaction remains applied.

Skill workflow selection and RSI strategy routing had different conversational checks.
They now share a cheap routing helper, with regressions for greetings, conceptual questions,
follow-on actions and imperative `do`/`have` requests. This is task guidance, not an authority
decision or a semantic classifier. Live-state and repository questions can still use tools.

General-agent guidance now requests a short plan for substantial work, project orientation,
use of relevant automatically selected skills, focused searches for gaps, specialist
selection for substantial independent work, observed verification, and reusable verified
lessons saved through the existing growth tool. A skill lookup or delegation is not required
for every turn. Lesson extraction remains model-guided, not guaranteed on every failure.

## Incorrect claims in the supplied conversation

- Audit is not stderr-only: the existing audit path also submits to a bounded database writer.
  Availability still depends on runtime/database lifecycle; stderr alone proves no durable write.
- Unrated turns can adapt operational retrieval, rankings and approaches from weak execution
  proxies. Three human-rated completed episodes from the exact revision govern persona trial
  evaluation. Neither feature flags nor successful tool calls prove improved task quality.
- Persona lenses have independent configuration; an unset tone description does not disable them.
- No evidence supports a guaranteed five-to-ten-turn improvement threshold.
- Machina delegation and sequential result handoffs go through the parent. Signed memory sharing
  exists; there is no live sibling messaging bus. No peer-chat feature was added in this repair.

The self-improvement plan and harness capability brief now reflect those distinctions.
No separate benchmark harness, live provider benchmark or fabricated human rating was created.

## Evidence and limits

The new regressions first reproduced missing archives, history cutoff, tool truncation,
lost stopped text, duplicated real compactions, imperative misrouting, cancellation during
preamble saves and false secret detection within a long source path. Repairs then passed
focused checks. Independent review found three material issues; each was fixed and rechecked.

The first full backend run had 2,550 passing tests, five skips and one obsolete timeout
fixture failure: its mock provider requested the timed-out tool forever and depended on the
removed iteration cap. The fixture now ends after receiving failure evidence and asserts
two provider calls plus recorded timeout evidence; the focused regression passes.

UI checks pass: typechecking, lint and 325 tests, with six integration tests skipped in the
standalone run. Backend checks use only the dedicated `machina_test` database. Python lint
and format checks cover the changed files.

Final full backend rerun: **2,551 passed, five skipped, zero failures in 122.87s**.
This includes the existing real gateway restart/UI integration wrapper. Changed-file
Ruff check and format verification pass (16 Python files); `git diff --check` passes.

Old text that was never saved cannot be reconstructed. Historical compacted copies already
assigned new IDs lack reliable deduplication provenance and may still appear twice. Archive
retention governs what can be recovered. Full resumed history can be larger than before;
the model's active context budget remains bounded. Real-task replay is still needed to
measure model obedience, delegation quality and learning usefulness.
